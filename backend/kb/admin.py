"""
Superuser publish/moderate tooling.

Per docs/help-center-roadmap.md: "Could genuinely start as Django Admin
customisations rather than bespoke screens, since this is internal-only
and low-volume at first" — approve/reject/unpublish via admin actions
rather than a bespoke moderation UI. (For org-scoped private KBs, the
equivalent org-admin screens are future work — see the roadmap doc's
"Multi-tenant add-on".)
"""
from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html
from markdownx.admin import MarkdownxModelAdmin

from .models import (
    Article, ArticleStep, ArticlePhoto, Tag, Category,
    Rating, FeaturedArticle, ProductShowcase,
)


class ArticleStepInline(admin.StackedInline):
    model = ArticleStep
    extra = 1
    ordering = ['order']


class ArticlePhotoInline(admin.TabularInline):
    model = ArticlePhoto
    extra = 1
    fields = ['image', 'preview', 'step', 'alt_text', 'order']
    readonly_fields = ['preview']

    def preview(self, obj):
        if obj.pk and obj.image:
            return format_html('<img src="{}" style="max-height:80px;" />', obj.image.url)
        return '—'
    preview.short_description = 'Preview'


@admin.register(Article)
class ArticleAdmin(MarkdownxModelAdmin):
    # MarkdownxModelAdmin swaps the plain textarea for the live-preview
    # Markdown editor on `body` (a MarkdownxField) — see kb/models.py.
    list_display = [
        'title', 'status', 'org_id', 'category', 'sort_order',
        'author_display_name', 'published_at', 'helpful_count',
    ]
    list_filter = ['status', 'category', 'tags']
    search_fields = ['title', 'summary', 'body', 'author_display_name']
    prepopulated_fields = {'slug': ('title',)}
    filter_horizontal = ['tags']
    inlines = [ArticleStepInline, ArticlePhotoInline]
    readonly_fields = ['created_at', 'updated_at']
    actions = ['publish_articles', 'archive_articles', 'unpublish_to_draft']

    class Media:
        # Same fix as the in-app editor (kb/templates/kb/article_form.html)
        # for the same bug -- markdownx's own JS silently swallows the
        # real upload-error message and inserts a useless placeholder
        # instead. See kb/static/kb/js/markdown-editor.js and
        # kb/views.py::markdown_image_upload's docstring for the full
        # story. Admin uses the same markdown widget on this same
        # field, so it gets the same bug and the same fix.
        js = ['kb/js/markdown-editor.js']

    fieldsets = (
        (None, {
            'fields': ('title', 'slug', 'status', 'org_id', 'category', 'sort_order', 'tags'),
        }),
        ('Content', {
            'fields': ('summary', 'body'),
        }),
        ('SEO', {
            'fields': ('meta_description',),
            'description': 'Leave blank to fall back to a truncated summary.',
        }),
        ('Authorship', {
            'fields': ('author_user_id', 'author_display_name'),
        }),
        ('Timestamps', {
            'fields': ('published_at', 'created_at', 'updated_at'),
        }),
    )

    @admin.action(description='Publish selected articles')
    def publish_articles(self, request, queryset):
        updated = 0
        for article in queryset:
            article.status = Article.STATUS_PUBLISHED
            if not article.published_at:
                article.published_at = timezone.now()
            article.save()
            updated += 1
        self.message_user(request, f'{updated} article(s) published.')

    @admin.action(description='Archive selected articles')
    def archive_articles(self, request, queryset):
        updated = queryset.update(status=Article.STATUS_ARCHIVED)
        self.message_user(request, f'{updated} article(s) archived.')

    @admin.action(description='Send back to draft (unpublish)')
    def unpublish_to_draft(self, request, queryset):
        updated = queryset.update(status=Article.STATUS_DRAFT)
        self.message_user(request, f'{updated} article(s) sent back to draft.')


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'org_id']
    list_filter = ['org_id']
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ['name']


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'org_id', 'visibility']
    list_filter = ['org_id', 'visibility']
    prepopulated_fields = {'slug': ('name',)}
    search_fields = ['name']


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ['article', 'is_helpful', 'comment', 'user_id', 'anon_token', 'created_at']
    list_filter = ['is_helpful']
    search_fields = ['comment', 'article__title']
    readonly_fields = [f.name for f in Rating._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(FeaturedArticle)
class FeaturedArticleAdmin(admin.ModelAdmin):
    list_display = ['article', 'starts_on', 'ends_on', 'curated_by_user_id']
    autocomplete_fields = ['article']


@admin.register(ProductShowcase)
class ProductShowcaseAdmin(admin.ModelAdmin):
    """Curates the cross-sell block shown on every published article on
    the site's own public KB — see models.ProductShowcase for the rationale."""
    list_display = ['title', 'preview', 'is_active', 'order', 'cta_label']
    list_editable = ['is_active', 'order']
    readonly_fields = ['preview']
    fields = ['title', 'screenshot', 'preview', 'alt_text', 'caption', 'cta_label', 'cta_url', 'is_active', 'order']

    def preview(self, obj):
        if obj.pk and obj.screenshot:
            return format_html('<img src="{}" style="max-height:120px;" />', obj.screenshot.url)
        return '—'
    preview.short_description = 'Preview'


admin.site.site_header = 'SL Deskline administration'
admin.site.site_title = 'SL Deskline admin'
admin.site.index_title = 'Knowledge Base moderation'
