"""
sitemap.xml — per docs/help-center-roadmap.md ('sitemap.xml, clean
canonical URLs per article'). Wired at the domain root in
support_core/urls.py, not under /kb/.
"""
from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from .models import Article, Category


class ArticleSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.8

    def items(self):
        # The site's own public KB only (org_id=None) — a private org KB
        # would get its own sitemap, scoped to that org, once the
        # multi-tenant add-on has domain routing (see roadmap).
        #
        # The visibility exclusion is NOT redundant with the detail
        # view's gate (kb/views.py::_published_articles), which already
        # 404s a members-only article for anonymous readers. The sitemap
        # is fetched anonymously by definition, and without this it
        # listed every members-only article's slug and lastmod date to
        # crawlers: no body text leaks, but the titles are in the slugs
        # and the edit dates are in the file, on a document whose whole
        # purpose is to be crawled and archived.
        return Article.objects.filter(
            status=Article.STATUS_PUBLISHED, org_id=None,
        ).exclude(category__visibility=Category.VISIBILITY_MEMBERS)

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return reverse('kb:article-detail', args=[obj.slug])


class StaticViewSitemap(Sitemap):
    changefreq = 'daily'
    priority = 1.0

    def items(self):
        return ['kb:article-list']

    def location(self, item):
        return reverse(item)
