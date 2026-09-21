"""Per-request memoisation for SiteSettings.load().

Nearly every page needs the singleton at least twice: once for the
context processor (branding/context_processors.py — every template
extends kb/templates/kb/base.html, which reads `site_settings`) and
again wherever a view builds its own meta_title/canonical context by
hand before rendering (kb/views.py, accounts/views.py, tickets/views.py
all do this). Loading it separately at each call site would mean two
identical `SELECT ... WHERE id = 1` queries per request for no reason —
`get_site_settings` caches the row on the request object instead, so
however many call sites ask for it during one request/response cycle,
only the first one actually queries the database.

Deliberately not `functools.lru_cache` or a module-level global: either
would cache across requests (and across threads/workers), so a change
saved in Admin could keep showing the old value to other requests still
served from the cache, or a value from one self-hoster's settings could
leak into concurrent requests' output under specific caching setups. A
plain attribute on `request` lives exactly as long as the request does.
"""
from .models import SiteSettings


def get_site_settings(request):
    """SiteSettings.load(), memoised on `request` for the lifetime of
    one request/response cycle."""
    cached = getattr(request, '_cached_site_settings', None)
    if cached is None:
        cached = SiteSettings.load()
        request._cached_site_settings = cached
    return cached
