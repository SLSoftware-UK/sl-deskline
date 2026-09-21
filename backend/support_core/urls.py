"""
Root URLconf.

The full URL map:

    /                        kb article list (KB home)
    /robots.txt              kb
    /sitemap.xml             django.contrib.sitemaps
    /articles/...            kb (authoring + detail + rate)
    /categories/  /tags/  /taxonomy/   kb authoring
    /tickets/...             tickets (login required)
    /login/  /sso/callback/  /logout/  accounts
    /staff/login/  /staff/logout/      accounts (support agents)
    /markdownx/upload/  /markdownx/markdownify/   superuser-gated
    /media/<path>            uploaded images, served from MEDIA_ROOT

Ordering matters: `kb.urls` is mounted at the root and owns
`articles/<slug>/`, so every more specific prefix (admin, tickets,
accounts, markdownx, sitemap.xml) has to be registered above it or the
root include would shadow it.

`/media/<path>` is registered in every environment.
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.sitemaps.views import sitemap
from django.http import JsonResponse
from django.urls import include, path, re_path
from django.views.decorators.http import require_safe
from django.views.static import serve as serve_static
from markdownx.views import MarkdownifyView

from kb.sitemaps import ArticleSitemap, StaticViewSitemap
from kb.views import markdown_image_upload

sitemaps = {
    'articles': ArticleSitemap,
    'static': StaticViewSitemap,
}

# markdownx's own views ship with no auth check of their own — wiring
# them up via `include('markdownx.urls')` (the package's documented
# usage) left /markdownx/upload/ and /markdownx/markdownify/ reachable
# by anyone who knew the URL, previously "protected" only by the fact
# that they were merely linked from within Django Admin (which is
# itself login-gated) rather than by any check on the endpoints
# themselves. Now that the same editor widget is also used outside
# Admin (kb/views.py's article_create/article_edit — see kb/forms.py),
# gate both views behind the same superuser check as those views (see
# kb/decorators.py) instead of leaving them open. URL names
# (`markdownx_upload`, `markdownx_markdownify`) are kept identical to
# the ones the widget's JS looks up by name, so nothing else changes.
_superuser_required = user_passes_test(lambda u: u.is_superuser)


def health(request):
    """Liveness — a plain Django view; DRF is not installed here at all
    (nothing in the service needs it)."""
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', health, name='api-health'),

    # Markdown editor live-preview + drag-and-drop image upload
    # endpoints — used by both Django Admin and the in-app article
    # editor (kb/views.py). markdown_image_upload is our own view
    # (kb/views.py — see its module docstring for why it replaces
    # markdownx's stock ImageUploadView entirely, not just wraps it),
    # already superuser-gated via @superuser_required; markdownify
    # (the live-preview endpoint, unaffected by any of that) is still
    # the stock view, just wrapped the same way as before.
    path('markdownx/upload/', markdown_image_upload, name='markdownx_upload'),
    path(
        'markdownx/markdownify/',
        login_required(_superuser_required(MarkdownifyView.as_view())),
        name='markdownx_markdownify',
    ),

    # SEO plumbing — must be reachable at the domain root, not under /kb/
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),

    # Identity, mounted at the root with an empty prefix rather than
    # under /accounts/: /login/, /logout/, /sso/callback/ and the two
    # /staff/ routes (see accounts/urls.py). Registered above kb.urls,
    # which owns the root and would otherwise shadow them.
    path('', include('accounts.urls')),

    # Support desk. Login-required throughout, and not public: kb's
    # robots.txt disallows /tickets/ and tickets/base.html marks every
    # page noindex. Registered above kb.urls for the same reason the
    # accounts routes are. Landing this include is also what makes
    # `reverse('tickets:list')` resolve for
    # support_core/host_middleware.py (the bare ticket-host root) and
    # accounts/views.py's STAFF_LOGIN_REDIRECT.
    path('tickets/', include('tickets.urls')),

    # Public KB, mounted at root. Stays last: it matches `articles/...`,
    # `categories/...`, `tags/...` and the bare root, and /tickets/ plus
    # the accounts routes get registered above it by their own tasks.
    path('', include('kb.urls')),
]


@require_safe
def serve_media(request, path):
    """Serve an uploaded file from MEDIA_ROOT (the mounted volume in production)."""
    response = serve_static(request, path, document_root=settings.MEDIA_ROOT)
    response['Cache-Control'] = 'public, max-age=86400'
    return response


# Uploads are served from MEDIA_ROOT — the mounted volume in production, the
# repo's own media/ locally. serve_static refuses paths that escape
# MEDIA_ROOT and 404s on directories. Uploads never overwrite an existing
# name (Django appends a suffix), so a day's browser caching cannot serve a
# stale image under a name that has been reused.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve_media, name='media'),
]
