from django.conf import settings

from support_core.host_middleware import is_ticket_host


def site_url(request):
    """Makes SITE_URL available in every template — used to build
    absolute canonical/OG URLs (base.html) without hand-building them
    per view."""
    return {'SITE_URL': settings.SITE_URL}


def tickets_url(request):
    """The header's "Tickets" link, resolved per host.

    One service, optionally two hostnames: e.g. help.example.com and
    support.example.com serve the identical URL space (see
    support_core/host_middleware.py). A reader signed in on the help
    host still needs the Tickets link to *land* on the ticket host —
    that is the hostname customers are given for the desk, and it is
    the origin the outbound ticket emails use — so on a help host the
    link has to be absolute, built on settings.TICKET_SITE_URL (which
    defaults to SITE_URL, making it same-origin on a single-host
    deployment). On a ticket host we are already there and a relative
    path is correct (and keeps working on a preview/staging domain,
    where TICKET_SITE_URL may still name production).

    Deliberately NOT `{% url 'tickets:list' %}`, even now that the
    tickets app exists and that name resolves: `reverse()` only ever
    yields a path, never another origin, so it cannot express the
    cross-host half of this at all.

    support_core/urls.py mounts the tickets app at /tickets/, so the
    literal here cannot drift unnoticed; kb/tests.py asserts both
    halves.
    """
    if is_ticket_host(request):
        return {'TICKETS_URL': '/tickets/'}
    return {'TICKETS_URL': settings.TICKET_SITE_URL.rstrip('/') + '/tickets/'}


def help_url(request):
    """The way back from tickets to the help centre — tickets_url's twin.

    The brand link, the search form and the header/sidebar "Help" links
    all used to be `{% url 'kb:article-list' %}`, i.e. a bare `/`. On a
    ticket host that path is exactly the one TicketHostRootRedirect-
    Middleware bounces straight back to /tickets/, so a reader on a
    separate ticket host had no route to the KB at all (and a search
    typed there silently landed on the ticket list, query dropped). On
    a ticket host the link therefore has to be absolute, built on
    settings.SITE_URL — the help host's canonical origin. Otherwise
    (including every single-host deployment) a relative `/` is correct.
    """
    if is_ticket_host(request):
        return {'HELP_URL': settings.SITE_URL.rstrip('/') + '/'}
    return {'HELP_URL': '/'}


def kb_settings(request):
    """KB_SETTINGS for every template (article cards, the rating widget).
    Lazy, so pages that never look at it — ticket pages, admin — don't
    pay for the query."""
    from django.utils.functional import SimpleLazyObject

    from .models import KBSettings
    return {'KB_SETTINGS': SimpleLazyObject(KBSettings.load)}
