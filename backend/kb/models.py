"""
Knowledge Base models.

Data model per docs/help-center-roadmap.md. This app covers the KB half
of the service only — Support Tickets (the `tickets` app) are a
separate, mostly-independent data model that links back to an article
by id.

The KB is a public help centre of articles explaining the self-hoster's
own product or service: how-to guides, troubleshooting, billing
questions, and so on.

`org_id` is a first-class, nullable field on `Category` and `Article`
from day one (not bolted on later): `org_id = None` is the site's own
public KB (what this app is for today); `org_id = <org>` would be a
specific organisation's own private, branded KB — the possible
multi-tenant add-on described in the roadmap doc. Only the schema is in
place for that today; the branding/domain/authoring UI to actually
build it is future work.

Authoring identity (`author_user_id`) is stored as a plain integer
mirroring Django's own auth.User pk rather than a ForeignKey, so an
article survives its author's account being removed and the KB never
depends on how that account was created (local sign-in or SSO shadow
user). See kb/views.py's article_create/article_edit for where this is
set.
"""
from django.db import models
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from markdownx.models import MarkdownxField

from . import youtube
from .markdown_utils import render_markdown
from .validators import validate_image_size
from .youtube import YouTubeVideoIdField


class Category(models.Model):
    """Primary grouping for an article — e.g. 'Getting started',
    'Billing', 'Troubleshooting' for the site's own public KB, or
    whatever an org chooses for their own private KB ('Warranty',
    'Returns', 'Shipping').
    `org_id = None` is the site's own public KB's category set; a
    specific `org_id` is that org's own, independent of every other
    org's categories — see the roadmap's 'Multi-tenant add-on'.

    `visibility` is a second, independent axis: a members-only category
    is still part of the site's own KB (org_id=None), it is just hidden
    from anyone who is neither a signed-in organisation member nor a
    support agent. See kb/views.py::_can_see_members_only for the rule."""
    VISIBILITY_PUBLIC = 'public'
    VISIBILITY_MEMBERS = 'members'
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, 'Public — visible to every visitor'),
        (VISIBILITY_MEMBERS, 'Members only — signed-in users who belong to an organisation'),
    ]

    org_id = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Null = the site\'s own public KB. Set = a specific '
                   'org\'s private KB (add-on).',
    )
    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_PUBLIC,
        help_text='Members-only categories are hidden from anonymous visitors '
                   'and from signed-in users who belong to no organisation. '
                   'Organisation members and support agents can see them.',
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, blank=True)

    # Unlike Article.sort_order (nullable, deliberately kept out of
    # Meta.ordering so the manage list stays newest-first), this one IS
    # the category ordering everywhere: the home page groups by category,
    # so "which section comes first" is the only question a category's
    # order ever answers. Non-null with a 0 default so every category has
    # a real position and ties fall back to name -- an untouched KB keeps
    # the alphabetical behaviour this replaced.
    sort_order = models.PositiveIntegerField(
        'Order', default=0,
        help_text='Lower numbers are listed first. Leave at 0 to sort '
                   'alphabetically among the other unordered categories.',
    )

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Slug uniqueness is enforced per-org at the application level,
        # not via a DB UniqueConstraint — org_id is nullable and Postgres
        # treats each NULL as distinct for uniqueness purposes, so a DB
        # constraint on (org_id, slug) would silently fail to prevent
        # duplicate slugs among the site's own (org_id=None) categories.
        if not self.slug:
            base = slugify(self.name)[:140]
            slug = base
            n = 1
            while Category.objects.filter(
                org_id=self.org_id, slug=slug,
            ).exclude(pk=self.pk).exists():
                n += 1
                slug = f'{base}-{n}'
            self.slug = slug
        super().save(*args, **kwargs)


class Tag(models.Model):
    """Free-form tags for finer-grained search within a category."""
    org_id = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Null = the site\'s own public KB. Set = a specific org\'s private KB.',
    )
    name = models.CharField(max_length=60)
    slug = models.SlugField(max_length=80, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Same per-org application-level uniqueness reasoning as Category.
        if not self.slug:
            base = slugify(self.name)[:80]
            slug = base
            n = 1
            while Tag.objects.filter(
                org_id=self.org_id, slug=slug,
            ).exclude(pk=self.pk).exists():
                n += 1
                slug = f'{base}-{n}'
            self.slug = slug
        super().save(*args, **kwargs)


class Article(models.Model):
    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHED = 'published'
    STATUS_ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_PUBLISHED, 'Published'),
        (STATUS_ARCHIVED, 'Archived'),
    ]

    # Null = the site's own public help centre. Set = a specific org's
    # private KB (multi-tenant add-on) — see module docstring and
    # docs/help-center-roadmap.md's "Multi-tenant add-on".
    org_id = models.PositiveIntegerField(null=True, blank=True)

    title = models.CharField(max_length=200)
    # Slug-based canonical URLs, not ID-based — see roadmap SEO section.
    slug = models.SlugField(max_length=220, blank=True)

    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name='articles',
    )
    tags = models.ManyToManyField(Tag, related_name='articles', blank=True)

    summary = models.TextField(
        help_text='Short standalone summary of what this article covers — '
                   'shown in listings and used as a search-result snippet.',
    )
    # Optional supporting video, shown between the summary and the body.
    # A dedicated field rather than anything inside `body` -- see
    # kb/youtube.py for why (bleach strips iframes from the body, and
    # only a bare video ID is ever stored).
    VIDEO_DISPLAY_EMBED = 'embed'
    VIDEO_DISPLAY_THUMBNAIL = 'thumbnail'
    VIDEO_DISPLAY_BUTTON = 'button'
    VIDEO_DISPLAY_CHOICES = [
        (VIDEO_DISPLAY_EMBED, 'Embedded player'),
        (VIDEO_DISPLAY_THUMBNAIL, 'Thumbnail linking to YouTube'),
        (VIDEO_DISPLAY_BUTTON, '"Watch on YouTube" button'),
    ]
    youtube_video_id = YouTubeVideoIdField(
        'YouTube video', blank=True, default='',
        help_text='Optional. Paste the video\u2019s YouTube link (or its ID).',
    )
    video_display = models.CharField(
        'Show video as', max_length=10,
        choices=VIDEO_DISPLAY_CHOICES, default=VIDEO_DISPLAY_EMBED,
    )

    # Markdown source — MarkdownxField is a plain TextField at the DB
    # level (same column type as before) with a live-preview editor
    # widget in Django Admin (see kb/admin.py, kb/markdown_utils.py).
    body = MarkdownxField(
        blank=True,
        default='',
        help_text='Main article content, written in Markdown (prose '
                   'explaining a feature or answering a question). '
                   'Optional if the article is purely a numbered '
                   'how-to — see ArticleStep.',
    )

    # SEO — server-rendered from day one, not injected client-side (see
    # roadmap: Facebook's crawler only parses ~60KB of raw HTML and
    # doesn't run JS at all, so this has to be in the initial response).
    meta_description = models.CharField(
        max_length=160, blank=True,
        help_text='Falls back to a truncated summary if left blank.',
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    # Optional reading order within a category on the public list
    # (kb/views.py::_in_display_order): numbered articles first, lowest
    # first, then unnumbered ones newest first. Deliberately not added to
    # Meta.ordering -- the manage list, sitemap and admin stay newest-first.
    sort_order = models.PositiveIntegerField(
        'Order', null=True, blank=True,
        help_text='Optional. Lower numbers are listed first within their '
                   'category; leave blank to list by newest.',
    )

    # Mirrors Django's own auth.User pk — see module docstring.
    author_user_id = models.PositiveIntegerField()
    author_display_name = models.CharField(
        max_length=150,
        help_text='Cached at publish time from the authoring user — avoids a '
                   'lookup against auth.User on every '
                   'page render.',
    )

    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # `-id` is a final tie-break, not a meaningful ordering signal on
        # its own: on SQLite (and in any batch-created fixture/test data)
        # several articles can share the same `created_at` down to the
        # microsecond, and without a tie-break their relative order is
        # undefined and can flip between runs. `-id` is monotonic with
        # insertion order, so ties resolve newest-inserted first, matching
        # what "newest first" already means for everything else.
        ordering = ['-published_at', '-created_at', '-id']
        indexes = [
            models.Index(fields=['status', '-published_at']),
            models.Index(fields=['org_id', 'status']),
        ]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:200]
            slug = base
            n = 1
            while Article.objects.filter(
                org_id=self.org_id, slug=slug,
            ).exclude(pk=self.pk).exists():
                n += 1
                slug = f'{base}-{n}'
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def effective_meta_description(self):
        if self.meta_description:
            return self.meta_description
        return (self.summary or '')[:157].rstrip() + (
            '...' if len(self.summary or '') > 157 else ''
        )

    @property
    def body_html(self):
        """Sanitised HTML rendering of `body` for the public article
        page — see kb/markdown_utils.py. Computed on read rather than
        cached on write, since this is low-traffic content, not a
        hot path worth the cache-invalidation complexity."""
        return mark_safe(render_markdown(self.body))

    @property
    def youtube_embed_url(self):
        return youtube.embed_url(self.youtube_video_id) if self.youtube_video_id else ''

    @property
    def youtube_watch_url(self):
        return youtube.watch_url(self.youtube_video_id) if self.youtube_video_id else ''

    @property
    def youtube_thumbnail_url(self):
        return youtube.thumbnail_url(self.youtube_video_id) if self.youtube_video_id else ''

    @property
    def helpful_count(self):
        return self.ratings.filter(is_helpful=True).count()

    @property
    def not_helpful_count(self):
        return self.ratings.filter(is_helpful=False).count()


class ArticleStep(models.Model):
    """Optional numbered steps for a how-to article (e.g. 'How to invite
    a team member: 1... 2... 3...'). Not every article needs these — a
    conceptual explainer can rely on `Article.body` alone."""
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='steps')
    order = models.PositiveIntegerField()
    instruction = models.TextField()

    class Meta:
        ordering = ['order']
        constraints = [
            models.UniqueConstraint(fields=['article', 'order'], name='unique_step_order_per_article'),
        ]

    def __str__(self):
        return f'{self.article.title} — step {self.order}'


class ArticlePhoto(models.Model):
    """Photo attached to an article (a hero image, or one tied to a
    step). Every upload goes through the resize/EXIF-strip/re-encode
    pipeline in kb/image_processing.py and the size ceiling in
    kb/validators.py before it reaches storage."""
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='photos')
    step = models.ForeignKey(
        ArticleStep, on_delete=models.CASCADE, related_name='photos',
        null=True, blank=True,
        help_text='Left blank for an article-level (e.g. hero) photo.',
    )
    image = models.ImageField(upload_to='articles/%Y/%m/', validators=[validate_image_size])
    # Alt text is SEO/accessibility load-bearing, not decorative.
    alt_text = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f'Photo for {self.article.title}'

    def save(self, *args, **kwargs):
        # Resize/EXIF-strip/re-encode on every save, regardless of
        # upload path — see class docstring.
        if self.image and hasattr(self.image, 'file'):
            from .image_processing import process_photo_image
            processed = process_photo_image(self.image.file)
            if processed is not None:
                self.image.save(processed.name, processed, save=False)
        super().save(*args, **kwargs)


RATING_COMMENT_MAX_LENGTH = 1000


class Rating(models.Model):
    """'Was this helpful?' — one per (user_id, article) for logged-in
    readers to prevent repeat voting; a lighter cookie-based token for
    anonymous readers (accepted as gameable, per the roadmap — reading
    the KB requires no account, so a hard per-user constraint isn't
    available for that path)."""
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='ratings')
    user_id = models.PositiveIntegerField(null=True, blank=True)
    anon_token = models.CharField(
        max_length=64, null=True, blank=True,
        help_text='Random token stored in a long-lived cookie for anonymous readers.',
    )
    is_helpful = models.BooleanField()
    # Optional free text the reader can add straight after voting
    # (views.rate_comment). One per vote, never overwritten.
    comment = models.TextField(blank=True, max_length=RATING_COMMENT_MAX_LENGTH)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['article', 'user_id'],
                condition=models.Q(user_id__isnull=False),
                name='unique_rating_per_user',
            ),
            models.UniqueConstraint(
                fields=['article', 'anon_token'],
                condition=models.Q(anon_token__isnull=False),
                name='unique_rating_per_anon_token',
            ),
        ]

    def __str__(self):
        return f'{"Helpful" if self.is_helpful else "Not helpful"} — {self.article.title}'


class FeaturedArticle(models.Model):
    """Backs a 'Featured article' spot — superuser-curated for the site's
    own KB, org-admin-curated for a private org KB (see roadmap)."""
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name='featured_runs')
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    curated_by_user_id = models.PositiveIntegerField()
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-starts_on']

    def __str__(self):
        return f'Featured: {self.article.title} from {self.starts_on}'


class ProductShowcase(models.Model):
    """Lets the KB double as SEO-driven advertising for the product it
    documents. Superuser-curated cross-sell screenshots of that product
    (e.g. an article about one feature promoting another), rendered as a
    block on every published article and the homepage — see
    kb/templates/kb/includes/showcase_block.html. Only meaningful on the
    site's own public KB (org_id=None articles) — not shown on a private
    org KB. Entirely optional: with no active rows the block is simply
    not rendered. Deliberately its own model
    rather than fields on Article: the same handful of screenshots rotate
    across every article rather than being authored per-article, and it
    can be edited/rotated from Django Admin without touching article
    content at all.
    """
    title = models.CharField(
        max_length=120,
        help_text='e.g. "Track every request in one place".',
    )
    screenshot = models.ImageField(upload_to='showcase/', validators=[validate_image_size])
    alt_text = models.CharField(max_length=200, blank=True)
    caption = models.CharField(max_length=200, blank=True)
    cta_label = models.CharField(max_length=60, default='Learn more')
    cta_url = models.URLField()
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', '-created_at']

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.screenshot and hasattr(self.screenshot, 'file'):
            from .image_processing import process_photo_image
            processed = process_photo_image(self.screenshot.file)
            if processed is not None:
                self.screenshot.save(processed.name, processed, save=False)
        super().save(*args, **kwargs)


class KBSettings(models.Model):
    """Superuser-editable switches for the KB's reader feedback
    (views.kb_settings). A singleton: always pk=1, fetched via load().
    Both default to on, which is how the KB behaved before this existed."""
    ratings_enabled = models.BooleanField(
        default=True,
        verbose_name='Ask "Was this article helpful?" on articles',
        help_text='Off hides the Yes/No buttons and the comment box, and stops new votes. '
                  'Existing feedback is kept.',
    )
    show_helpful_count = models.BooleanField(
        default=True,
        verbose_name='Show "X found this helpful" on article cards',
    )

    class Meta:
        verbose_name = 'KB settings'
        verbose_name_plural = 'KB settings'

    def __str__(self):
        return 'KB settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
