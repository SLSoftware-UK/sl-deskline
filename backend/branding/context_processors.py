from .utils import get_site_settings


def site_settings(request):
    """Makes the singleton SiteSettings row available as `site_settings`
    in every template — kb/templates/kb/base.html (which every page in
    this service extends, tickets included) reads it for the page
    title, og:site_name, header brand, accent colour and footer.

    `get_site_settings`, not `SiteSettings.load()` directly: several
    views (kb/views.py, accounts/views.py, tickets/views.py) already
    load the same row themselves to build their own meta_title before
    the template even renders, so this must reuse that same
    request-scoped cache rather than issuing a second, identical query
    on every request — see branding/utils.py.

    `render_to_string()` for an email has no request, so this processor
    never runs there — tickets/notifications.py passes `SiteSettings.load()`
    into the email context explicitly instead. See its module docstring.
    """
    return {'site_settings': get_site_settings(request)}
