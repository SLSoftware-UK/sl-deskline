"""
The shared identity layer for both halves of this service (KB + tickets).

Two kinds of person sign in here, told apart by Django's `is_staff` flag
and nothing else:

  - **Customers** -- this desk's end users. Their `accounts.Membership`
    rows (see models.py) are what scope their tickets and what open
    members-only KB categories.
  - **Support agents** -- `is_staff` accounts, typically made with
    `createsuperuser`. They work every organisation's tickets, author KB
    articles and use Django Admin, and need no memberships.

How they sign in depends on `settings.SSO_BACKEND`
--------------------------------------------------
`'local'` (the default) is plain Django auth. `/login/` is a username-
or-email + password form for everyone, customers and agents alike.
Accounts are created by an operator in Django Admin (or with
`createsuperuser`); there is no self-signup. `/sso/callback/` does not
exist in this mode (404), and `/staff/login/` is a temporary (302)
redirect to `/login/` so old bookmarks keep working. This is what makes a fresh
clone usable with no external service: `migrate`, `createsuperuser`,
sign in.

`'code_exchange'` hands customer identity to an external identity
provider. Customers never have a password here: they arrive at
`/sso/callback/?code=...` carrying a one-time code the provider minted,
which accounts/sso.py redeems server-to-server (its module docstring
documents the handshake and the exchange contract). `/login/` becomes an
informational page that links out to the provider -- this service cannot
*start* a handshake, only receive one -- and support agents, who still
have local passwords, use the password form at `/staff/login/`, which in
this mode admits `is_staff` accounts only.

Views read `settings.SSO_BACKEND` at request time, never at import, so
`override_settings` works in tests and nothing is cached across a
settings change. The setting itself is validated when settings.py loads.

Why the session cookie can be parent-domain scoped
--------------------------------------------------
The same service can be served on two hostnames -- a help-centre host
and a ticket-desk host (settings.TICKET_HOSTS). With SSO, recognition
happens exactly once, on whichever host the provider sent the user to,
so the session cookie can be scoped to the parent domain
(SESSION_COOKIE_DOMAIN) for both hosts to share it; see settings.py's
session block, including why the cookie name must not be plain
`sessionid`.

Django Admin is left completely alone
-------------------------------------
Admin is the support agents' tool and those accounts have real
passwords in either mode, so Django's own admin login form is the correct
one and must keep working. See accounts/apps.py.
"""
import hashlib
import math
import time
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import (
    authenticate, get_user_model, login as django_login, logout as django_logout,
)
from django.core.cache import cache
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from branding.utils import get_site_settings
from support_core.host_middleware import is_ticket_host

from . import sso

User = get_user_model()

REDIRECT_FIELD_NAME = 'next'

# Where a support agent lands after signing in, when no `next` says
# otherwise. reverse_lazy, not reverse: this is evaluated at import
# time, before the URLconf is loaded.
STAFF_LOGIN_REDIRECT = reverse_lazy('tickets:list')


def _sso_enabled():
    return settings.SSO_BACKEND == 'code_exchange'


def _safe_next_path(request):
    r"""Only ever a local path, never an absolute URL.

    `next` comes straight off a query string (and, with SSO, through a
    redirect chain that passes through the identity provider), so it is
    attacker-influenced input, not something to hand to `redirect()`
    verbatim -- an open redirect off a help desk is a ready-made phishing
    hop. Anything that is not a plain local path is discarded rather
    than rejected loudly: the visitor did nothing wrong and the homepage
    is a fine place to land.

    Django's `url_has_allowed_host_and_scheme` does the test, rather than
    the obvious hand-rolled `startswith('/') and not startswith('//')`.
    That pair catches `//evil.com` and `https://evil.com` but misses the
    variants the helper exists for: `/\evil.com`, a path with an
    embedded tab or CR/LF (`/<TAB>/evil.com`), and `/%09//evil.com`.
    Chrome and Firefox normalise a backslash to `/` and strip tab/CR/LF
    before they parse the authority, so all three read as off-site to a
    browser while passing a naive prefix check.

    None of those would be a live open redirect through `redirect()`
    alone, because `HttpResponseRedirect` runs the value through
    `iri_to_uri`, which percent-encodes `\` to `%5C` and tab to `%09`.
    This is hardening: the docstring promises "only ever a local path",
    and the first caller that bypasses `redirect()` (a bare
    `HttpResponse`, an htmx `HX-Redirect` header, a JS hop, a link
    rendered into a template) would otherwise inherit the real thing.
    """
    next_path = request.GET.get(REDIRECT_FIELD_NAME) or ''
    if not url_has_allowed_host_and_scheme(
        next_path,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return '/'
    return next_path


def _post_login_path(request, user):
    """`?next=` if one was supplied and is a safe local path; otherwise
    the ticket list for a support agent and the KB home for a customer.

    An agent who signs in with no destination in mind wants the queue,
    not the help centre. `_safe_next_path` collapses an unsafe value to
    '/', which is the KB home -- not where an agent meant to go -- so a
    '/' result falls through to the per-kind default rather than being
    honoured."""
    if request.GET.get(REDIRECT_FIELD_NAME):
        next_path = _safe_next_path(request)
        if next_path != '/':
            return next_path
    if user.is_staff:
        return STAFF_LOGIN_REDIRECT
    return reverse('kb:article-list')


# ---------------------------------------------------------------------------
# Password sign-in brute-force throttle
# ---------------------------------------------------------------------------
# The password form is a plain HTML POST at a guessable URL, and in
# either mode it is the door to support-agent accounts: every
# organisation's tickets, Django Admin, and KB authoring. Without this
# there is nothing between an attacker and an unlimited password-guessing
# loop.
#
# Counter lives in the cache (see CACHES in settings.py), keyed per
# (client IP, submitted identifier) so one person fat-fingering their
# password cannot lock out a colleague, and a spray across many usernames
# from one IP does not hide behind a per-account counter.
#
# /admin/login/ is NOT covered by this. It is Django's own view and left
# untouched deliberately (see this module's docstring) -- it remains
# unthrottled and is worth revisiting.

_THROTTLE_KEY_PREFIX = 'login-throttle'


def _client_ip(request):
    """Best available client IP.

    Behind a reverse proxy or load balancer every request arrives from
    the proxy, so REMOTE_ADDR would put every user in the world into one
    shared counter -- the first few failures anywhere would lock out
    everyone. The left-most X-Forwarded-For entry is the real client.

    That entry is client-supplied and therefore spoofable, so an attacker
    who rotates it evades the IP half of the key. That is accepted here:
    the alternative (counting per identifier alone) hands the same
    attacker a way to lock a named account out on demand, which is a
    worse trade. The throttle is a brake on casual/automated guessing,
    not a claim to stop a determined attacker -- for that the answer is a
    strong password policy and, eventually, 2FA on agent accounts.
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or 'unknown'


def _throttle_key(request, identifier):
    # Hashed, not interpolated raw: the identifier is unvalidated user
    # input, and cache keys have character and length rules (memcached's
    # in particular, which Django warns about). Hashing also keeps the
    # submitted email out of the cache in plain text.
    digest = hashlib.sha256(identifier.strip().lower().encode('utf-8')).hexdigest()[:32]
    return f'{_THROTTLE_KEY_PREFIX}:{_client_ip(request)}:{digest}'


def _lockout_seconds_remaining(key):
    """Seconds left on the lockout for this key, or 0 if not locked."""
    state = cache.get(key)
    if not state or state.get('count', 0) < settings.LOGIN_MAX_ATTEMPTS:
        return 0
    return max(0, int(state.get('until', 0) - time.time()))


def _record_failed_attempt(key):
    """Count one failure and (re)start the window. Sliding, not fixed:
    each failure pushes the unlock time out again, so a slow drip of
    guesses can't wait out the window while keeping its count."""
    state = cache.get(key) or {'count': 0, 'until': 0}
    state['count'] = state.get('count', 0) + 1
    state['until'] = time.time() + settings.LOGIN_LOCKOUT_SECONDS
    cache.set(key, state, timeout=settings.LOGIN_LOCKOUT_SECONDS)


def _clear_failed_attempts(key):
    cache.delete(key)


def _lockout_message(seconds_remaining):
    """Says what actually happened, rather than reusing the generic
    "credentials were not recognised". The usual argument for a vague
    message -- don't confirm an account exists -- does not apply: the
    lockout is keyed on whatever was typed, existing account or not, so
    it leaks nothing. Being vague here would instead have someone who
    mistyped twice retrying a correct password forever and concluding
    the login is broken."""
    minutes = max(1, math.ceil(seconds_remaining / 60))
    unit = 'minute' if minutes == 1 else 'minutes'
    return (
        f'Too many failed sign-in attempts. Please try again in '
        f'{minutes} {unit}.'
    )


class LoginForm(forms.Form):
    """Username-or-email plus password. See `_password_login` for the
    resolution order."""
    username = forms.CharField(label='Email or username', max_length=254)
    password = forms.CharField(label='Password', widget=forms.PasswordInput, strip=False)


def _password_login(request, *, agents_only):
    """The one password form, shared by local-mode `/login/` (every
    account) and SSO-mode `/staff/login/` (`is_staff` accounts only).

    Credential resolution: `username` is not guaranteed to equal `email`
    (a createsuperuser account rarely does), so the input is first
    resolved as an email address to that account's username, then falls
    back to being treated as the username itself. Both outcomes go
    through the same `authenticate()` call and fail with the same single
    message, so the form never says whether an account exists -- and
    Django's ModelBackend runs the password hasher even for an unknown
    username, so the timing does not say so either.

    With `agents_only`, the email lookup is filtered to `is_staff=True`
    too. In SSO mode customer accounts are shadow users with unusable
    passwords, and without that filter typing a customer's email would at
    least exercise a real account; the explicit `not user.is_staff`
    re-check after `authenticate()` covers the fallback branch, where the
    input was taken as a username and never went through the filter.

    Returns an HttpResponse (a redirect on success, otherwise the form).
    """
    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        identifier = form.cleaned_data['username'].strip()
        password = form.cleaned_data['password']

        # Throttle check happens before authenticate(), so a locked-out
        # caller does not even get a password comparison done for them --
        # no timing signal, no work for us.
        throttle_key = _throttle_key(request, identifier)
        remaining = _lockout_seconds_remaining(throttle_key)
        if remaining:
            form.add_error(None, _lockout_message(remaining))
            return _render_password_form(request, form, agents_only)

        by_email = User.objects.filter(email__iexact=identifier)
        if agents_only:
            by_email = by_email.filter(is_staff=True)
        existing = by_email.order_by('pk').first()
        username = existing.username if existing else identifier
        user = authenticate(request, username=username, password=password)

        if user is None or (agents_only and not user.is_staff):
            _record_failed_attempt(throttle_key)
            remaining = _lockout_seconds_remaining(throttle_key)
            if remaining:
                # This failure is the one that tripped the lockout; say
                # so now rather than letting them try again to find out.
                form.add_error(None, _lockout_message(remaining))
            else:
                # One message for every failure on purpose -- saying
                # which part was wrong would tell an attacker whether an
                # account exists.
                form.add_error(None, 'Those credentials were not recognised.')
        else:
            # A correct password clears the slate, so a few fumbled
            # attempts leave nothing behind to bite later.
            _clear_failed_attempts(throttle_key)
            django_login(request, user)
            return redirect(_post_login_path(request, user))

    return _render_password_form(request, form, agents_only)


def _render_password_form(request, form, agents_only):
    """`canonical_path`/`meta_title`/`meta_description` are what stop
    kb/base.html emitting a canonical tag pointing at the KB home (which
    would tell a crawler this page IS the home page) and empty
    og:title/og:description. robots.txt disallows /staff/ as well, but
    the tags should be right regardless of whether a given crawler
    honours it."""
    # get_site_settings(request), not SiteSettings.load(): branding's
    # context processor loads the same row again for this request once
    # the template renders -- caching it on request keeps that to one
    # query total (see branding/utils.py).
    site_name = get_site_settings(request).site_name
    if agents_only:
        canonical = reverse('accounts:staff-login')
        title = f'Support agent sign in — {site_name}'
        description = 'Sign-in page for support agents.'
    else:
        canonical = reverse('accounts:login')
        title = f'Sign in — {site_name}'
        description = f'Sign in to {site_name}.'
    return render(request, 'accounts/password_login.html', {
        'form': form,
        'agents_only': agents_only,
        'next_path': request.GET.get(REDIRECT_FIELD_NAME) or '',
        'canonical_path': canonical,
        'meta_title': title,
        'meta_description': description,
    })


def _provider_login_url(next_path):
    """SSO_PROVIDER_LOGIN_URL with `next` appended -- `&` if the
    configured URL already carries a query string, `?` otherwise.
    SSO_PROVIDER_LOGIN_URL is trusted configuration and `next_path` has
    already been through `_safe_next_path`."""
    base = settings.SSO_PROVIDER_LOGIN_URL
    if not next_path or next_path == '/':
        return base
    separator = '&' if '?' in base else '?'
    return f'{base}{separator}{urlencode({REDIRECT_FIELD_NAME: next_path})}'


def login_view(request):
    """
    GET/POST /login/?next=<path>

    `settings.LOGIN_URL` points here, so this is where `@login_required`
    sends an anonymous visitor.

    Local mode: the password form, for every account. A signed-in visitor
    is shown the form rather than bounced onward, deliberately -- a view
    gated on something stricter than "signed in" can send an
    authenticated user here, and redirecting them straight back to
    `next` would loop.

    SSO mode: an *informational* page, not a redirect into an authorize
    endpoint, because this service cannot initiate SSO (see
    accounts/sso.py). It explains the handoff and links to the
    provider's sign-in, carrying `next`, plus the support-agent form at
    /staff/login/ as the other door.
    """
    if not _sso_enabled():
        return _password_login(request, agents_only=False)

    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])

    next_path = _safe_next_path(request)
    site_name = get_site_settings(request).site_name
    return render(request, 'accounts/login.html', {
        'next_path': next_path,
        'provider_login_url': _provider_login_url(next_path),
        # kb/base.html builds <link rel="canonical">, og:url, og:title
        # and og:description from these three. Without canonical_path it
        # emits a canonical pointing at the KB home, telling a crawler
        # this page IS the home page, and og:title/og:description come
        # out empty.
        'canonical_path': reverse('accounts:login'),
        'meta_title': f'Sign in — {site_name}',
        'meta_description': f'Sign in to {site_name} with your account.',
    })


@require_GET
def sso_callback(request):
    """
    GET /sso/callback/?code=...&next=<path>

    SSO mode only; a 404 in local mode, so a deployment that does not use
    SSO exposes no endpoint that would try to reach one.

    The single landing point for the push handshake. `sso_code` is
    accepted as an alias for `code` because providers spell it both ways
    and accepting both costs one `or`. `next` is optional.

    On failure this redirects to the KB home with a message rather than
    returning a 400: a customer who has just clicked a link inside
    another product has no idea what an SSO code is, and a message on a
    working page is kinder than a dead end.
    """
    if not _sso_enabled():
        raise Http404

    code = request.GET.get('code') or request.GET.get('sso_code')
    try:
        user = sso.resolve_code(code)
    except sso.SSOResolutionError as exc:
        messages.error(request, exc.public_message)
        return redirect('kb:article-list')

    django_login(request, user)

    # `next=/` means the knowledge-base home, on either hostname. This is
    # the one place in the service where a bare `/` is disambiguated, and
    # it takes both halves of the story to see why:
    #
    #   1. support_core/host_middleware.py sends a bare `/` on a ticket
    #      host to /tickets/. That is right for its own case: a callback
    #      with no `next` at all falls back to `/`, and on the ticket
    #      host the support queue is the sensible place to land.
    #   2. A provider may build every callback on the ticket host (one
    #      configured origin for "the desk") and send `next=/` to mean
    #      the help centre. Without this, asking for Help would land on
    #      the ticket queue: callback on the ticket host -> log in -> 302
    #      to `/` -> middleware -> /tickets/.
    #
    # Both decisions are individually correct; they just collide at `/`
    # on the ticket host. An explicit `next=/` is a request for the help
    # centre, so it wins -- and it has to be an *absolute* redirect to the
    # help origin, because a local `/` is precisely what the middleware
    # would rewrite. The cross-host hop keeps the user signed in when the
    # session cookie is scoped to the parent domain (SESSION_COOKIE_DOMAIN
    # in settings.py).
    #
    # Tested against the *supplied* value, not the resolved one, so a
    # callback carrying no `next` at all keeps landing on the queue via
    # the middleware.
    #
    # Every other `next` stays a local redirect. SITE_URL is trusted
    # configuration, never request input, so this cannot be turned into
    # an open redirect.
    if request.GET.get(REDIRECT_FIELD_NAME) == '/' and is_ticket_host(request):
        return redirect(settings.SITE_URL)
    return redirect(_safe_next_path(request))


def staff_login_view(request):
    """
    GET/POST /staff/login/

    Local mode: a redirect to /login/, which already takes every
    account, keeping `?next=` so a bookmarked or linked agent URL still
    ends up where it meant to. Deliberately a temporary 302, not a 301:
    browsers cache a 301 indefinitely, so a deployment that starts on
    'local' and later switches to 'code_exchange' would leave every
    browser that ever visited /staff/login/ bouncing to /login/ -- whose
    agent link points back here -- and agents could never reach the
    password form again.

    SSO mode: the password form for local support-agent (`is_staff`)
    accounts only. Customers never come through here -- they arrive at
    /sso/callback/ instead. An agent who is already signed in is sent
    straight on.
    """
    if not _sso_enabled():
        target = reverse('accounts:login')
        next_value = request.GET.get(REDIRECT_FIELD_NAME)
        if next_value:
            target = f'{target}?{urlencode({REDIRECT_FIELD_NAME: next_value})}'
        return redirect(target)

    if request.user.is_authenticated and request.user.is_staff:
        return redirect(_post_login_path(request, request.user))
    return _password_login(request, agents_only=True)


@require_POST
def logout_view(request):
    """
    POST /logout/  (and /staff/logout/, the same view)

    POST-only, because a GET logout is CSRF-able and pre-fetchable, and
    it flushes the session server-side rather than just clearing it
    client-side -- a "log out" that leaves a valid session cookie behind
    is worth nothing on a shared workstation. (`django_logout` is what
    does the flush.)

    In SSO mode this ends the session on this service only; it does not
    sign the user out of the identity provider, which exposes no logout
    for this service to call. Following a Help/Support link from the
    provider again will re-recognise them, which is the honest behaviour
    for a push-only handshake.
    """
    django_logout(request)
    messages.success(request, 'You have been signed out.')
    return redirect('kb:article-list')
