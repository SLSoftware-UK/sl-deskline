"""
Markdown -> sanitised HTML for Article.body.

One function, used two places, deliberately kept in sync:
  - MARKDOWNX_MARKDOWNIFY_FUNCTION (settings.py) — powers the live
    preview pane in the Django Admin editor.
  - Article.body_html (kb/models.py) — powers the actual public
    article page.

Using the same function for both means the admin preview is a true
preview: whatever an author sees while writing is exactly what a reader gets,
not an unsanitised approximation.

Sanitisation matters even though only trusted superuser accounts can
publish today: the roadmap's multi-tenant add-on (docs/help-center-
roadmap.md) means other companies' staff will eventually author their
own private KB content through this same field, so raw, unsanitised
HTML passthrough (which python-markdown allows by default — there's no
`safe_mode` any more) isn't a risk worth carrying even at low volume.
bleach strips anything outside the allowlist rather than escaping it,
so a pasted <script> tag just disappears rather than rendering as
visible text.

`style` on `img` is a deliberate, narrow exception to "no inline
styles", there so authors can size images by percentage — the plain
`width` HTML attribute only reliably means pixels; `width: 50%` needs to
be CSS. Sanitised at the *property* level via bleach's
CSSSanitizer (requires the `tinycss2`-backed `bleach[css]` extra — see
requirements.txt), not just the attribute-name level: only `width`/
`height`/`max-width`/`max-height` survive, so `style="width: 50%;
background: url(javascript:alert(1)); position: fixed"` comes out the
other side as just `style="width: 50%;"` — verified directly against
bleach before wiring this up, not assumed. help.css's `max-width: 100%
!important` on `.body img` is the other half of this: bleach doesn't
validate *values* (an author could still write `width: 5000%` and it
would survive sanitisation), so that's the actual safety net stopping
an inline style from ever breaking the page layout, regardless of what
number someone writes.
"""
import re

import bleach
import markdown
from bleach.css_sanitizer import CSSSanitizer

from django.conf import settings
from django.core.files.storage import default_storage

# bleach.clean(strip=True) removes disallowed *tags* but keeps their
# inner text, so a stray <script>alert(1)</script> in pasted HTML would
# render as harmless-but-ugly visible text ("alert(1)") rather than
# vanishing outright. Strip these two element bodies outright before
# bleach runs — cosmetic, not the actual security boundary (that's
# bleach's tag/attribute allowlist below), but tidier.
_STRIP_CONTENT_TAGS = re.compile(
    r'<(script|style)\b[^>]*>.*?</\1>', re.IGNORECASE | re.DOTALL,
)

# kb/views.py::markdown_image_upload stores the bare storage path (e.g.
# "markdownx/photo.jpg") in Article.body rather than a resolved URL, so
# every render below gets a fresh default_storage.url() — on the S3
# backend that's a presigned URL good for AWS_QUERYSTRING_EXPIRE
# seconds, which would otherwise go dead an hour after upload if baked
# into the stored Markdown once and reused forever. External/absolute
# image URLs (http(s):// or data:) are left untouched.
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=")([^"]+)(")')


def _resolve_relative_image_srcs(html):
    def _resolve(match):
        prefix, src, suffix = match.groups()
        if src.startswith(('http://', 'https://', 'data:', '//')):
            return match.group(0)
        return f'{prefix}{default_storage.url(src)}{suffix}'
    return _IMG_SRC_RE.sub(_resolve, html)

ALLOWED_TAGS = [
    'p', 'br', 'hr',
    'strong', 'b', 'em', 'i', 'u', 's', 'sup', 'sub', 'code', 'pre',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li',
    'blockquote',
    'a', 'img',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'dl', 'dt', 'dd',
    'div', 'span',  # 'extra' extension's footnotes/abbr wrap output in these
]

ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'rel', 'id'],
    # 'style' — see module docstring for why this is safe: CSSSanitizer
    # below restricts it to four sizing properties, not "anything goes".
    'img': ['src', 'alt', 'title', 'width', 'height', 'style'],
    'th': ['align'],
    'td': ['align', 'colspan', 'rowspan'],
    'code': ['class'],
    'pre': ['class'],
    'div': ['class', 'id'],
    'span': ['class', 'id'],
    'li': ['id'],
    'sup': ['id'],
}

ALLOWED_PROTOCOLS = ['http', 'https', 'mailto']

# Only sizing properties — no colours, positioning, backgrounds, etc.
# `img` is the only allowlisted tag with `style` at all (see above), so
# this only ever runs against attacker/author-controlled width/height
# hints, never a general "here's a style attribute, sanitise it" surface.
CSS_SANITIZER = CSSSanitizer(
    allowed_css_properties=['width', 'height', 'max-width', 'max-height'],
)


def render_markdown(content):
    """
    Markdown source -> sanitised HTML string.

    :param content: Raw Markdown text (Article.body).
    :type content: str
    :return: Sanitised HTML.
    :rtype: str
    """
    html = markdown.markdown(
        content or '',
        extensions=settings.MARKDOWNX_MARKDOWN_EXTENSIONS,
    )
    html = _STRIP_CONTENT_TAGS.sub('', html)
    html = _resolve_relative_image_srcs(html)
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        css_sanitizer=CSS_SANITIZER,
        strip=True,
    )
