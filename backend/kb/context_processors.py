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
