"""
Adds Content-Security-Policy and Permissions-Policy to every response.
Django's SecurityMiddleware covers HSTS/nosniff/XSS-filter but has no
built-in support for either of these two.

CSP built from what this app actually loads (checked directly against
the templates):
  - htmx is loaded from cdnjs.cloudflare.com (see base template) rather
    than bundled -- script-src allows that host specifically.
  - Google Fonts stylesheet + font files -- style-src/font-src allow
    fonts.googleapis.com / fonts.gstatic.com.
  - A handful of templates use inline <script>/onclick/style="", so
    'unsafe-inline' is needed for script-src/style-src until those are
    refactored to external files/nonces -- still blocks the main things
    CSP guards against here (loading a script from an attacker-
    controlled remote host, framing this site elsewhere).
  - img-src is 'self' plus data: and nothing more, because uploaded
    images are served by this app itself off MEDIA_ROOT (see
    support_core/urls.py). If a remote image origin is ever reintroduced
    -- an object store, a CDN -- it MUST be added here or every inline
    image in every article is silently blocked: 'self' only ever covers
    this app's own origin. That failure is easy to miss: images simply
    stop rendering, with the reason visible only in the browser console.

Article supporting videos (kb/youtube.py) need exactly two more hosts,
and nothing wider: frame-src allows only YouTube's privacy-enhanced
embed domain (www.youtube-nocookie.com -- the only host the templates
ever frame), and img-src adds i.ytimg.com for "thumbnail" display mode.
The player's own scripts/requests run inside the youtube-nocookie
frame's origin, governed by YouTube's policy, not this one, so
script-src/connect-src don't need to change.

frame-ancestors 'none' is the CSP-level equivalent of X-Frame-Options
and takes precedence in modern browsers.
"""


class SecurityHeadersMiddleware:
    PERMISSIONS_POLICY = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "interest-cohort=()"
    )

    def __init__(self, get_response):
        self.get_response = get_response

        self.CSP = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://i.ytimg.com; "
            "connect-src 'self'; "
            "frame-src https://www.youtube-nocookie.com; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.CSP)
        response.setdefault("Permissions-Policy", self.PERMISSIONS_POLICY)
        return response
