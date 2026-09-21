"""
Optional single sign-on: redeeming an identity provider's one-time code.

Only used when `settings.SSO_BACKEND == 'code_exchange'`. With the default
`'local'` backend nothing in this module runs -- every account signs in
with a username/email and password at /login/ -- and a deployment that
never sets `SSO_BACKEND` never talks to any external service.

The handshake is push-only
--------------------------
This service never *starts* a sign-in at the provider. There is no
authorize redirect, no `prompt=none` round trip and no middleware that
bounces anonymous visitors anywhere. Instead:

  1. The user is already signed in to your identity provider (usually
     the product this desk supports). When they click "Help" or
     "Support" there, the provider mints a short-lived, single-use
     `code` for them.
  2. The provider sends the browser to this service's
     `/sso/callback/?code=<code>&next=<local path>`.
  3. This service POSTs that code, server-to-server, to the provider's
     exchange endpoint and reads who the user is out of the response.

That shape is deliberately the least a provider has to implement: one
"mint a code" action inside its own authenticated app and one public
"redeem a code" endpoint. It is also why `/login/` in this mode is an
informational page that links out to the provider rather than a redirect
into it -- this service has nothing to redirect *to* that would come back
recognised on its own.

Why nothing client-editable decides identity
--------------------------------------------
The browser only ever carries the opaque code. The code is not a signed
token and is never decoded or verified here -- there is deliberately no
shared secret in this service to verify one with. Whoever the provider's
exchange response says the user is, is who they are; a customer who
edits the callback URL can at most present a different code, which the
provider will refuse. The exchange response body is therefore the only
source for the local "shadow" User and its organisation memberships.

Exchange contract
-----------------
Request::

    POST {SSO_EXCHANGE_URL}
    Content-Type: application/json

    {"code": "<the code from the callback URL>"}

Success is HTTP 200 with a JSON body::

    {
      "user": {
        "email": "dana@example.com",          # required
        "first_name": "Dana",                 # optional
        "last_name": "Owner",                 # optional
        "organisations": [                    # optional; [] or absent = none
          {"id": 42, "name": "Northside Dental",
           "slug": "northside-dental", "role": "owner"}
        ]
      }
    }

  - Any other status code is treated as "invalid or expired code".
  - Any other top-level keys (a `token`, say) are ignored: this service
    keeps its own Django session and never stores provider tokens.
  - `email` becomes the shadow user's username. It is required; without
    it the sign-in is refused.
  - `organisations[].id` is the provider's own id for the organisation.
    It is stringified and stored as `Organisation.external_id` (at most
    64 characters), which is how later sign-ins find the same row -- the
    local primary key is never exposed to or trusted from the provider.
    `0` is a perfectly good id; only a missing/null/empty one is refused.
  - `role` is one of `owner`, `admin`, `member`; anything else (or
    nothing) is stored as `member`, the least privileged.
  - A malformed body -- `user` or `organisations` of the wrong type, an
    organisation that is not an object, an id that is missing or too
    long -- refuses the whole sign-in rather than half-syncing it.

Memberships: a full replace, but only of provider-managed ones
--------------------------------------------------------------
On every sign-in the user's memberships are re-synced from the response,
as a full replace rather than a merge: an organisation the provider no
longer reports for this user loses its membership immediately, so it
stops exposing that organisation's tickets and members-only articles.

That replace is scoped to **provider-managed organisations** -- those
with an `external_id`. An operator can also create organisations by hand
in Django Admin (external_id left empty) and put SSO users in them, for
example to give one customer access to a partner's tickets. The provider
knows nothing about those, so its silence about them is not a statement
that the user left; memberships in hand-made organisations are never
touched by this module, and survive every sign-in until an operator
removes them.

Organisation rows themselves are only ever created or updated here,
never deleted: an organisation this user has left may still have other
members and certainly has tickets.

Support agents are never signed in this way
-------------------------------------------
See the `is_staff`/`is_superuser` guard in `resolve_code`.
"""
import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.text import slugify

from .models import Membership, Organisation

logger = logging.getLogger(__name__)

User = get_user_model()

# Organisation.external_id's max_length. Checked here, before the write,
# because an over-long value is a DataError on Postgres (a 500 mid-login)
# and silently accepted on SQLite -- neither is a useful answer.
EXTERNAL_ID_MAX_LENGTH = Organisation._meta.get_field('external_id').max_length

_BAD_RESPONSE = 'The sign-in service returned an unexpected response. Please try again.'


class SSOResolutionError(Exception):
    """Raised by `resolve_code` on any failure. `public_message` is safe
    to show a customer -- it never leaks which half of the exchange
    failed beyond what is useful to them."""

    def __init__(self, public_message):
        self.public_message = public_message
        super().__init__(public_message)


def _unique_org_slug(wanted, external_id):
    """A slug for a synced Organisation that no *other* row already holds.

    Organisation.slug is unique, but the provider's slugs and the ones an
    operator types into Django Admin share one namespace here, so a
    collision is possible and must not turn a sign-in into an
    IntegrityError. The row that already owns the slug keeps it; the
    synced one gets `-2`, `-3`, ... Excluding its own external_id means a
    re-sync of an unchanged organisation keeps the slug it already has.
    """
    base = slugify(wanted)[:240] or f'org-{external_id}'
    slug, n = base, 1
    while Organisation.objects.filter(slug=slug).exclude(external_id=external_id).exists():
        n += 1
        slug = f'{base}-{n}'
    return slug


def _clean_organisations(organisations):
    """Validates the `organisations` claim and returns a list of
    `(external_id, name, slug, role)` tuples, or raises
    SSOResolutionError.

    All-or-nothing on purpose: validating every entry before any row is
    written means a malformed payload refuses the sign-in cleanly
    instead of leaving the user with half their memberships synced, and
    turns what would otherwise be a TypeError/DataError 500 into a
    message the customer can act on."""
    if organisations is None:
        return []
    if not isinstance(organisations, list):
        logger.error('SSO exchange: "organisations" is %s, not a list', type(organisations).__name__)
        raise SSOResolutionError(_BAD_RESPONSE)

    cleaned = []
    for org in organisations:
        if not isinstance(org, dict):
            logger.error('SSO exchange: organisation entry is %s, not an object', type(org).__name__)
            raise SSOResolutionError(_BAD_RESPONSE)

        # `0` is a valid id, so no `or ''` shortcut here -- only a
        # missing/null id or one that is empty once stringified is
        # refused. Anything that is not a plain string or integer (a
        # nested object, a bool) would stringify into nonsense that
        # could never match on the next sign-in, so it is refused too.
        raw_id = org.get('id')
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            logger.error('SSO exchange: organisation id %r is not a string or integer', raw_id)
            raise SSOResolutionError(_BAD_RESPONSE)
        external_id = str(raw_id).strip()
        if not external_id:
            logger.error('SSO exchange: organisation with an empty id')
            raise SSOResolutionError(_BAD_RESPONSE)
        if len(external_id) > EXTERNAL_ID_MAX_LENGTH:
            logger.error(
                'SSO exchange: organisation id is %d characters (max %d)',
                len(external_id), EXTERNAL_ID_MAX_LENGTH,
            )
            raise SSOResolutionError(_BAD_RESPONSE)

        name = org.get('name') or ''
        slug = org.get('slug') or ''
        if not isinstance(name, str) or not isinstance(slug, str):
            logger.error('SSO exchange: organisation %s has a non-string name or slug', external_id)
            raise SSOResolutionError(_BAD_RESPONSE)
        # Truncated rather than refused: a long display name is a
        # cosmetic problem, not a reason to lock someone out.
        name = name[:Organisation._meta.get_field('name').max_length]

        role = org.get('role')
        if role not in Membership.Role.values:
            role = Membership.Role.MEMBER

        cleaned.append((external_id, name, slug, role))
    return cleaned


def sync_memberships(user, organisations):
    """Re-syncs `user`'s memberships in provider-managed organisations
    from a cleaned `organisations` list (see `_clean_organisations`).

    Full replace within the provider-managed set, and only that set: see
    the module docstring for why memberships in hand-made organisations
    (external_id NULL) are left alone. Organisations are matched on
    `external_id`, never on the local pk, and are only created or updated
    here -- never deleted."""
    seen_org_ids = set()
    for external_id, name, wanted_slug, role in organisations:
        organisation = Organisation.objects.filter(external_id=external_id).first()
        slug = _unique_org_slug(wanted_slug or name, external_id)
        if organisation is None:
            organisation = Organisation.objects.create(external_id=external_id, name=name, slug=slug)
        elif (organisation.name, organisation.slug) != (name, slug):
            organisation.name, organisation.slug = name, slug
            organisation.save(update_fields=['name', 'slug'])
        seen_org_ids.add(organisation.pk)

        Membership.objects.update_or_create(
            user=user, organisation=organisation, defaults={'role': role},
        )

    Membership.objects.filter(
        user=user, organisation__external_id__isnull=False,
    ).exclude(organisation_id__in=seen_org_ids).delete()


def _exchange(code):
    """The server-to-server POST. Returns the `user` claims dict, or
    raises SSOResolutionError."""
    try:
        response = requests.post(settings.SSO_EXCHANGE_URL, json={'code': code}, timeout=10)
    except requests.RequestException:
        logger.exception('SSO exchange request failed')
        raise SSOResolutionError(
            'Could not reach the sign-in service to confirm your login. Please try again.'
        )

    if response.status_code != 200:
        raise SSOResolutionError(
            'That sign-in link is invalid or has expired. Please sign in again.'
        )

    try:
        payload = response.json()
    except ValueError:
        logger.error('SSO exchange returned a non-JSON body (status %s)', response.status_code)
        raise SSOResolutionError(_BAD_RESPONSE)

    claims = payload.get('user') if isinstance(payload, dict) else None
    if not isinstance(claims, dict):
        logger.error('SSO exchange response has no "user" object')
        raise SSOResolutionError(_BAD_RESPONSE)
    return claims


def resolve_code(code):
    """Redeems a one-time code against `settings.SSO_EXCHANGE_URL`, then
    get-or-creates and re-syncs the local shadow User and its
    memberships from the response.

    Returns the User (with `.backend` set, ready for `login()`); raises
    SSOResolutionError otherwise. Nothing is written unless the whole
    response validates.
    """
    if not code:
        raise SSOResolutionError('That sign-in link is incomplete. Please sign in again.')

    claims = _exchange(code)

    # `email` is load-bearing and not recoverable: it is the shadow
    # user's username. An empty `organisations` list, by contrast, is a
    # perfectly good answer -- a customer who belongs to no organisation
    # can still raise (organisation-less) tickets -- so it is synced as
    # "no provider-managed memberships" rather than refused.
    email = claims.get('email') or ''
    if not isinstance(email, str) or not email.strip():
        raise SSOResolutionError(
            'The sign-in service did not return an email address for your account.'
        )
    email = email.strip()
    if len(email) > User._meta.get_field('username').max_length:
        logger.error('SSO exchange: email is too long to be a username (%d chars)', len(email))
        raise SSOResolutionError(_BAD_RESPONSE)

    organisations = _clean_organisations(claims.get('organisations'))

    def _name(key):
        value = claims.get(key) or ''
        return value[:150] if isinstance(value, str) else ''

    with transaction.atomic():
        user, _ = User.objects.get_or_create(username=email, defaults={'email': email})

        # An existing local support-agent account (createsuperuser /
        # Django Admin) could have a username equal to some customer's
        # email -- an operator's own address is the obvious case. Without
        # this guard an SSO login would authenticate AS that agent
        # account (every organisation's tickets, KB authoring, Django
        # Admin), and the unconditional set_unusable_password()/save()
        # below would permanently wipe the agent's password on the way
        # through. Checked BEFORE any field sync or save touches the user,
        # so an existing staff account is never mutated by this code path
        # -- and the surrounding atomic() rolls back the get_or_create, so
        # a probe with an unknown address leaves no stray row behind.
        if user.is_staff or user.is_superuser:
            raise SSOResolutionError('This account cannot be signed in this way.')

        # Re-synced on every login, not just at creation, so a name
        # change at the provider lands here within one sign-in.
        user.email = email
        user.first_name = _name('first_name')
        user.last_name = _name('last_name')
        user.set_unusable_password()
        user.save()

        sync_memberships(user, organisations)

    # login() needs `user.backend` set when the user was not produced by
    # authenticate() -- there is no password to authenticate with here
    # by design; the exchange itself *is* the authentication.
    user.backend = 'django.contrib.auth.backends.ModelBackend'
    return user
