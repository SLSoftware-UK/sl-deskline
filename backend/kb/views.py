"""
Public-facing Knowledge Base views — plain Django views + templates,
server-rendered on every request. No DRF here deliberately: every page
must be a complete HTML document on first response for crawlers that
don't execute JS at all (social + AI crawlers), not just Googlebot's
delayed second pass.

Scoped to the site's own public KB (org_id=None) for now — a private
org KB (nullable org_id is already on the models for this) would reuse
these same views with org_id resolved from the request's domain/
subdomain, but that routing doesn't exist yet.
"""
import json
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Case, F, FloatField, Q, Value, When
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST
from markdownx.settings import MARKDOWNX_MEDIA_PATH
from support_core.host_middleware import is_ticket_host

from branding.utils import get_site_settings

from . import search_index
from .context_processors import tickets_url
from .decorators import superuser_required
from .forms import ArticleForm, CategoryForm, TagForm
from .image_cleanup import purge_orphaned_markdown_images
from .image_processing import process_photo_image
from .models import Article, Category, Tag, Rating, FeaturedArticle, ProductShowcase
from .validators import max_photo_upload_bytes

# The site's own public KB. Hardcoded org scope for now — see module
# docstring; a private org KB will resolve this from the request instead.
PLATFORM_ORG_ID = None

# Cookie used to key anonymous 'was this helpful?' votes (see rate_article).
RATING_COOKIE_NAME = 'deskline_rating_token'


def _site_name(request):
    """The self-hoster's configured site name, for meta_title strings
    built here rather than left to base.html's own default (an article
    or category title always precedes it, so the plain "<site name> —
    Knowledge Base" fallback in kb/templates/kb/base.html's <title>
    block does not apply).

    `get_site_settings(request)`, not `SiteSettings.load()` directly:
    branding's own context processor loads the identical row again for
    this same request once the template renders (it needs it for
    og:site_name, the header brand, etc.) — `get_site_settings` caches
    on `request` so that is one query total, not two. See
    branding/utils.py."""
    return get_site_settings(request).site_name


# Home page layout (article_list): unfiltered, it shows one section per
# category capped at this many articles (in _in_display_order) plus a "See all" link, so
# the page stays roughly the same length however many articles exist.
# Any filter (search, category, tag) switches to a flat grid paginated
# at ARTICLES_PER_PAGE.
ARTICLES_PER_CATEGORY_ON_HOME = 6
ARTICLES_PER_PAGE = 12


def _can_see_members_only(request):
    """True if this visitor may see Category.VISIBILITY_MEMBERS content —
    the gate applied everywhere below (article list, featured spot,
    category filter, article detail).

    Two populations qualify:

      - a signed-in user with at least one `accounts.Membership`, i.e.
        someone an operator (or the SSO sync) has put in an organisation.
        Which organisation does not matter: members-only categories are
        part of the site's own KB, written for customers in general
        rather than for one organisation in particular.
      - a support agent (`is_staff`). Authoring members-only articles is
        superuser-only (kb/decorators.py::superuser_required), but every
        agent still needs to see them on the public pages exactly as a
        member would, or "is this article live and correct?" becomes
        unanswerable for a non-superuser agent without signing in as a
        customer.

    A signed-in user with no membership is kept out, exactly like an
    anonymous visitor: being able to sign in is not the same as being a
    customer the members-only material was written for.

    The answer is cached on the request, because the article list asks it
    up to three times per render and each ask would otherwise be its own
    `EXISTS` query.
    """
    cached = getattr(request, '_can_see_members_only', None)
    if cached is not None:
        return cached
    user = request.user
    allowed = user.is_authenticated and (user.is_staff or user.memberships.exists())
    request._can_see_members_only = allowed
    return allowed


def _published_articles(request):
    articles = Article.objects.filter(
        status=Article.STATUS_PUBLISHED, org_id=PLATFORM_ORG_ID,
    ).select_related('category')
    if not _can_see_members_only(request):
        articles = articles.exclude(category__visibility=Category.VISIBILITY_MEMBERS)
    return articles


def _in_display_order(articles):
    """Public browse order: articles given an explicit `sort_order` come
    first, lowest first, then the rest newest first. Not used for search
    results, which stay ordered by relevance.

    `-id` is a final tie-break, not a meaningful ordering signal on its
    own: articles created in the same test (or any other batch-created
    fixture) can share an identical `created_at` on SQLite, and without a
    tie-break their relative order is undefined and can flip between
    runs. `-id` is monotonic with insertion order, so ties resolve
    newest-inserted first, matching Article.Meta.ordering."""
    return articles.order_by(
        F('sort_order').asc(nulls_last=True), '-published_at', '-created_at', '-id',
    )


def _active_showcases():
    return ProductShowcase.objects.filter(is_active=True)[:3]


def _search_articles(articles, query):
    """Applies `query` to `articles`, in whichever way the active
    database can actually do well.

    Plain `icontains` across title/summary/body is literal substring
    matching only: search for "summaries" and an article whose summary
    says "Summary" won't match, because neither string contains the
    other. Fixing that properly means stemming ("summaries" and
    "Summary" reducing to the same root word) -- something `icontains`
    fundamentally can't do, no matter how the query string is massaged.
    Both real paths below therefore use the database's own built-in
    full-text search, English (Porter) stemming included, rather than an
    external search service or an embeddings/AI-backed search: no extra
    infrastructure to run for a KB that is nowhere near needing it.

    Three branches, tried in this order:

      1. PostgreSQL (opt-in via DATABASE_URL):
         `django.contrib.postgres.search` -- `to_tsvector`/
         `plainto_tsquery` under the hood, plain SQL functions every
         Postgres install has, no extension needed. The vector is
         computed per-request rather than stored in an indexed column
         (the usual GIN-index setup) -- the simpler option at KB scale.
      2. SQLite with FTS5 (the default single-container deployment): the
         FTS5 index in kb/search_index.py, kept in sync by kb/signals.py.
         Keeps search inside the one SQLite file on the one volume -- no
         Postgres needed just for search, no extra service. The matching
         article ids and their bm25 scores are fetched from the index,
         then applied to `articles` as `id__in` plus a Case/When rank
         annotation; that is a CASE with one branch per match, which is
         fine at KB scale (hundreds of articles, not millions).
      3. Anything else (a SQLite build without FTS5): the substring
         `icontains` OR-match, so search still works, just unstemmed.

    Both full-text branches have the same semantics: every word of the
    query must match (AND, punctuation ignored), title is weighted above
    summary above body (Postgres A/B/C = 1.0/0.4/0.2, reproduced as the
    bm25 column weights), only real matches are returned, and they are
    ordered by relevance alone -- replacing the published-date ordering
    used everywhere else.
    """
    if connection.vendor == 'postgresql':
        vector = (
            SearchVector('title', weight='A', config='english')
            + SearchVector('summary', weight='B', config='english')
            + SearchVector('body', weight='C', config='english')
        )
        search_query = SearchQuery(query, config='english')
        # Filtering with `rank__gt=0` looks reasonable but isn't safe --
        # ts_rank can return a tiny non-zero epsilon (observed: 1e-20)
        # for documents that don't actually match the query at all, a
        # known ts_rank quirk, not a Django/psycopg issue. The correct
        # way to test "does this row match" is the `@@` operator itself
        # (`search=search_query` below, on the annotated vector) --
        # `rank` is then only ever used for ordering the real matches.
        return (
            articles.annotate(search=vector, rank=SearchRank(vector, search_query))
            .filter(search=search_query)
            .order_by('-rank')
        )
    if search_index.fts5_available(connection):
        matches = search_index.search(query, connection)
        if not matches:
            return articles.none()
        return (
            articles.filter(id__in=[article_id for article_id, _ in matches])
            .annotate(fts_rank=Case(
                *[When(id=article_id, then=Value(score)) for article_id, score in matches],
                output_field=FloatField(),
            ))
            .order_by('fts_rank')
        )
    return articles.filter(
        Q(title__icontains=query) | Q(summary__icontains=query) | Q(body__icontains=query)
    )


def article_list(request):
    """Home / browse / search — filterable by category and tag.

    The tag filter is scoped to whichever category is currently
    selected (or to all published articles if none is): only tags that
    are actually used somewhere in that set are offered. Without this,
    picking 'Billing' would still list Booking-only tags like 'Waitlist'
    as an option, pointing at zero results — confusing, and it
    undermines Category as a navigation aid. Tags aren't hard-tied to a
    single Category in the schema (a tag can still genuinely span
    categories, e.g. 'Pro feature' on both a Billing and a Booking
    article) — this is purely a display-time narrowing based on real
    usage.
    """
    articles = _published_articles(request)

    query = request.GET.get('q', '').strip()
    if query:
        articles = _search_articles(articles, query)

    category_slug = request.GET.get('category', '')
    category_scoped_articles = articles
    if category_slug:
        category_scoped_articles = category_scoped_articles.filter(category__slug=category_slug)
        articles = articles.filter(category__slug=category_slug)

    tag_slug = request.GET.get('tag', '')
    if tag_slug:
        articles = articles.filter(tags__slug=tag_slug)

    articles = articles.distinct()

    # Built from category_scoped_articles (before the tag filter itself
    # is applied), so selecting a tag doesn't make its own option
    # disappear from the list.
    available_tags = (
        Tag.objects.filter(org_id=PLATFORM_ORG_ID, articles__in=category_scoped_articles)
        .distinct()
        .order_by('name')
    )

    featured_qs = FeaturedArticle.objects.filter(
        article__status=Article.STATUS_PUBLISHED, article__org_id=PLATFORM_ORG_ID,
    )
    if not _can_see_members_only(request):
        featured_qs = featured_qs.exclude(article__category__visibility=Category.VISIBILITY_MEMBERS)
    featured = featured_qs.select_related('article').first()

    categories = Category.objects.filter(org_id=PLATFORM_ORG_ID)
    if not _can_see_members_only(request):
        categories = categories.exclude(visibility=Category.VISIBILITY_MEMBERS)

    # Browsing (no filter at all) gets one capped section per category;
    # anything filtered gets a flat paginated grid. One count + one
    # capped query per category -- categories are few, articles aren't.
    is_filtered = bool(query or category_slug or tag_slug)
    page_obj = None
    category_sections = []
    if is_filtered:
        results = articles if query else _in_display_order(articles)
        page_obj = Paginator(results, ARTICLES_PER_PAGE).get_page(request.GET.get('page'))
    else:
        for category in categories:
            in_category = _in_display_order(articles.filter(category=category))
            count = in_category.count()
            if count:
                category_sections.append({
                    'category': category,
                    'articles': in_category[:ARTICLES_PER_CATEGORY_ON_HOME],
                    'count': count,
                    'has_more': count > ARTICLES_PER_CATEGORY_ON_HOME,
                })

    # Pagination links carry the active filters but not the old page.
    pagination_params = request.GET.copy()
    pagination_params.pop('page', None)

    context = {
        'is_filtered': is_filtered,
        'page_obj': page_obj,
        'category_sections': category_sections,
        'pagination_querystring': pagination_params.urlencode(),
        'query': query,
        'categories': categories,
        'tags': available_tags,
        'selected_category': category_slug,
        'selected_tag': tag_slug,
        'featured': featured,
        'showcases': _active_showcases(),
        'meta_title': f'{_site_name(request)} — Knowledge Base',
        'meta_description': (
            f'Guides and answers from {_site_name(request)} — '
            'search by topic or browse by category.'
        ),
        'canonical_path': reverse('kb:article-list'),
    }
    return render(request, 'kb/article_list.html', context)


def absolute_media_url(url):
    """An absolute URL for a stored media file, for og:image and
    schema.org `image` — tags whose consumer fetches the image from
    somewhere else entirely, so a host-relative path is no use to it.

    Storage-shape aware. Every environment now runs FileSystemStorage
    (see STORAGES in support_core/settings.py), so `FieldFile.url` is
    always a root-relative path — `/media/articles/2026/09/hero.jpg` —
    and always needs SITE_URL prefixing to become the absolute URL a
    crawler card requires.

    The check stays even though the default storage never needs it. A storage
    backend that returns a complete absolute URL (any S3-compatible one
    does) turns unconditional prefixing into
    `https://help.example.comhttps://bucket.host/...`, which breaks
    every social and AI crawler card on the one service whose entire
    reason for being server-rendered is those crawlers. That is what
    this function is written to stop, and it would bite the day anyone
    swaps in such a backend.

    Worth knowing before anyone proposes object storage: a private
    bucket's absolute URL is typically *presigned* and expires (often
    after an hour), so a crawler fetching the image later gets a 403 and
    the card breaks anyway — fixing that means serving bucket objects
    publicly, which is an exposure decision in its own right. Serving
    media from MEDIA_ROOT makes og:image a plain, permanent, public URL,
    so the question never arises.
    """
    if not url:
        return url
    # '//host/path' is protocol-relative — already absolute as far as a
    # crawler is concerned, and SITE_URL in front of it would be
    # nonsense.
    if url.startswith(('http://', 'https://', '//')):
        return url
    return f'{settings.SITE_URL}{url}'


def _howto_schema_json(article, hero_photo):
    """schema.org HowTo structured data for articles with numbered
    steps — built in Python (not hand-assembled in the template) so
    JSON escaping can't quietly break: a title/instruction containing a
    quote or `</script>` would otherwise corrupt the page rather than
    just the schema. Returns None for articles with no steps, since
    HowTo isn't a fit for a purely prose explainer."""
    steps = list(article.steps.all())
    if not steps:
        return None

    schema = {
        '@context': 'https://schema.org',
        '@type': 'HowTo',
        'name': article.title,
        'description': article.effective_meta_description,
    }
    if hero_photo:
        schema['image'] = absolute_media_url(hero_photo.image.url)
    step_entries = []
    for step in steps:
        step_data = {'@type': 'HowToStep', 'position': step.order, 'text': step.instruction}
        first_photo = step.photos.all()[0] if step.photos.all() else None
        if first_photo:
            step_data['image'] = absolute_media_url(first_photo.image.url)
        step_entries.append(step_data)
    schema['step'] = step_entries

    # json.dumps can emit "</script>" if a field contains that literal
    # text — escape the forward slash so it can't close the <script> tag
    # early no matter what an author typed into the article.
    return mark_safe(json.dumps(schema).replace('</', '<\\/'))


def _ticket_url_for(request, article):
    """The article's "Raise a ticket" CTA target, or None to hide it.

    Shown to any signed-in account and to nobody else. An anonymous
    reader gets nothing rather than a link into a login wall: this KB is
    public marketing copy read mostly by people who have no account at
    all, and /tickets/new/ is login_required, so the CTA would dead-end
    for exactly the readers who see it most.

    A *support agent* is signed in and does get the link, even though
    ticket_create refuses them with a 403 (the desk is for customers to
    report problems to agents — see tickets/views.py). That is deliberate:
    the refusal is a rendered page in the desk's own chrome that says in
    a sentence why an agent account cannot raise a ticket and what to do
    instead, so nobody is left staring at an unexplained error. Adding
    an `and not request.user.is_staff` here would instead make the CTA
    silently absent for the handful of people who most often read the KB
    while editing it, and they would have no way to tell the missing CTA
    from a broken one.

    Built from the TICKETS_URL context processor rather than
    `reverse('tickets:create')`: the KB may be served on a help host
    (help.example.com) as well as a ticket host (support.example.com),
    and reverse() only ever yields a path, so on the help host it would
    produce a same-origin link rather than one to the ticket host. The
    helper is the one place that host logic lives (see
    kb/context_processors.py) — this calls it directly instead of
    reading it out of the template context, since the guard has to be
    decided in Python anyway.
    """
    if not request.user.is_authenticated:
        return None
    # TICKETS_URL is the list page ('/tickets/' or its absolute twin);
    # the create page hangs off it. Both halves follow the mount point
    # in support_core/urls.py and are asserted in kb/tests.py.
    return f"{tickets_url(request)['TICKETS_URL']}new/?article={article.pk}"


def article_detail(request, slug):
    article = get_object_or_404(
        _published_articles(request).prefetch_related('steps__photos', 'photos', 'tags'),
        slug=slug,
    )

    # Two ways of remembering "you already rated this", because there
    # are two kinds of reader. Signed in — a customer or a support
    # agent — the vote is keyed to their user id.
    # Anonymous, which is still the common case here (the KB is public
    # and nothing about reading it requires a login), it falls back to
    # the anon cookie below. See rate_article for why anonymous voting
    # exists at all.
    already_rated = False
    if request.user.is_authenticated:
        already_rated = article.ratings.filter(user_id=request.user.id).exists()
    else:
        anon_token = request.COOKIES.get(RATING_COOKIE_NAME)
        if anon_token:
            already_rated = article.ratings.filter(anon_token=anon_token).exists()

    hero_photo = article.photos.filter(step__isnull=True).first()

    context = {
        'article': article,
        'hero_photo': hero_photo,
        # og:image, built here rather than as `{{ SITE_URL }}{{ ... }}`
        # in the template, so the one storage-shape rule lives in one
        # place — see absolute_media_url above.
        'hero_image_url': absolute_media_url(hero_photo.image.url) if hero_photo else None,
        'howto_schema_json': _howto_schema_json(article, hero_photo),
        'showcases': _active_showcases(),
        'already_rated': already_rated,
        'meta_title': f'{article.title} — {_site_name(request)}',
        'meta_description': article.effective_meta_description,
        'canonical_path': reverse('kb:article-detail', args=[article.slug]),
        # "Couldn't find an answer? Raise a ticket" — guarded in
        # article_detail.html by {% if ticket_url %}, so None here hides
        # the CTA outright for anonymous readers. The link carries
        # ?article=<id> so the ticket records which article the reader
        # came from (Ticket.linked_article_id).
        'ticket_url': _ticket_url_for(request, article),
    }
    return render(request, 'kb/article_detail.html', context)


@require_POST
def rate_article(request, slug):
    """'Was this helpful?' — open to every reader, logged in or not.

    A signed-in reader gets one vote per (article, user_id). Everyone
    else gets one vote per (article, anon_token), via a random token in
    a long-lived cookie — gameable, accepted as such in exchange for
    working at all without an account.

    Deliberately NOT narrowed to logged-in-only, even though real
    visitor logins exist (accounts/views.py). This KB is public,
    SEO-driven copy read mostly by people who have no account yet, and
    requiring one to say "this wasn't helpful" would silence precisely
    the readers whose confusion is most worth hearing about. A
    deployment whose KB is read mainly by signed-in customers might
    reasonably make the opposite trade."""
    article = get_object_or_404(_published_articles(request), slug=slug)
    is_helpful = request.POST.get('helpful') == 'yes'
    set_cookie = None

    if request.user.is_authenticated:
        _, created = Rating.objects.get_or_create(
            article=article,
            user_id=request.user.id,
            defaults={'is_helpful': is_helpful},
        )
    else:
        anon_token = request.COOKIES.get(RATING_COOKIE_NAME)
        if not anon_token:
            anon_token = secrets.token_urlsafe(32)
            set_cookie = anon_token
        _, created = Rating.objects.get_or_create(
            article=article,
            anon_token=anon_token,
            defaults={'is_helpful': is_helpful},
        )

    if request.htmx:
        response = render(request, 'kb/includes/rating_result.html', {
            'article': article, 'already_rated': True, 'just_voted': created,
        })
    else:
        messages.success(request, 'Thanks for the feedback!' if created else 'You already rated this article.')
        response = redirect('kb:article-detail', slug=slug)

    if set_cookie:
        response.set_cookie(
            RATING_COOKIE_NAME, set_cookie,
            max_age=60 * 60 * 24 * 365 * 5, samesite='Lax',
        )

    return response


def robots_txt(request):
    # Served on both hostnames, and it must not say the same thing on
    # both. The help host is the canonical, indexable site; a ticket
    # host (settings.TICKET_HOSTS) serves the identical URL space (see
    # support_core/host_middleware.py) but is the login-gated support
    # desk, and nothing there should be in an index at all. A single
    # answer built from request.get_host() would advertise a sitemap of
    # every KB article under the ticket hostname and invite crawling of
    # the whole duplicate tree — with only the canonical tag standing
    # between that and double-indexing.
    if is_ticket_host(request):
        lines = [
            'User-agent: *',
            'Disallow: /',
            '',
        ]
        return HttpResponse('\n'.join(lines), content_type='text/plain')

    lines = [
        'User-agent: *',
        'Allow: /',
        'Disallow: /admin/',
        # The support desk lives in this same service rather than a
        # separate app, so its routes need disallowing here. All of
        # these are either login-gated or a redirect landing strip:
        # nothing behind them is public content, and crawling them only
        # burns crawl budget and fills the logs with 302s/403s.
        'Disallow: /tickets/',
        'Disallow: /staff/',
        'Disallow: /sso/',
        # /login/ is the sign-in page (or the SSO hand-off to it). Same
        # reasoning as /staff/ next to it: no public content, and it is
        # a sign-in page, which is not something to have indexed.
        'Disallow: /login/',
        '',
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse('\n'.join(lines), content_type='text/plain')


# ---------------------------------------------------------------------------
# In-app authoring — replaces the old "New Article" -> Django Admin
# redirect (kb/templates/kb/base.html). Superuser-only for now (see
# kb/decorators.py); Django Admin (kb/admin.py) remains the place for
# publish/archive bulk actions and the ArticleStep/ArticlePhoto inlines,
# which aren't covered by this pass. All views below are scoped to
# the site's own public KB (org_id=PLATFORM_ORG_ID) — an org-scoped
# private KB's own authoring UI is future work, see docs/help-center-
# roadmap.md's "Multi-tenant add-on".
# ---------------------------------------------------------------------------

def _author_display_name(user):
    return user.get_full_name() or user.email or user.username


def _authoring_org_id(user):
    """The org_id every server-authored Article/Category/Tag gets —
    NEVER taken from a form field or request data. An earlier version
    of CategoryForm/TagForm exposed org_id as a free-text number input,
    which was exactly the hole it looks like: anyone signed in could
    type in *any* org's id and write content straight into that org's
    private KB (or the site's own public one) regardless of which org
    they actually belong to.

    Right now this always returns PLATFORM_ORG_ID, because the only
    identity that can author here at all is a superuser — there's no
    such thing yet as a
    logged-in org member with their own org_id to derive this from.
    Once the multi-tenant add-on's own auth path exists (an org member
    signing in to author their own org's KB), this is the one place
    that needs to change: resolve `user`'s own org_id here (from their
    accounts.Membership rows) instead of
    hardcoding PLATFORM_ORG_ID, and article_create/category_create/
    tag_create/etc all pick it up for free — none of them, or their
    forms, need to change."""
    return PLATFORM_ORG_ID


def _cleanup_suffix(deleted_image_count):
    """Small ' (N unused image(s) cleaned up)' addition for the
    success message after a create/edit/delete that triggered
    purge_orphaned_markdown_images() -- see kb/image_cleanup.py. Silent
    (empty string) when there was nothing to clean up, so the common
    case doesn't get a noisy "(0 unused images cleaned up)" tacked on
    every single save."""
    if not deleted_image_count:
        return ''
    noun = 'image' if deleted_image_count == 1 else 'images'
    return f' ({deleted_image_count} unused {noun} cleaned up.)'


@superuser_required
def article_manage(request):
    """List of every article in the site's own KB (any status), with
    edit links — the replacement for having to open Django Admin just
    to find a draft to continue writing. Admin is still linked per-row
    for steps/photos/publish actions not yet covered here."""
    articles = (
        Article.objects.filter(org_id=PLATFORM_ORG_ID)
        .select_related('category')
        .order_by('-updated_at')
    )
    paginator = Paginator(articles, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'page_obj': page_obj,
        'meta_title': f'Manage articles — {_site_name(request)}',
        'canonical_path': reverse('kb:article-manage'),
    }
    return render(request, 'kb/article_manage.html', context)


@superuser_required
def article_create(request):
    if request.method == 'POST':
        form = ArticleForm(request.POST)
        if form.is_valid():
            article = form.save(commit=False)
            article.org_id = _authoring_org_id(request.user)
            article.author_user_id = request.user.id
            article.author_display_name = _author_display_name(request.user)
            if article.status == Article.STATUS_PUBLISHED and not article.published_at:
                article.published_at = timezone.now()
            article.save()
            form.save_m2m()
            deleted_images = purge_orphaned_markdown_images()
            messages.success(request, f'"{article.title}" created.{_cleanup_suffix(deleted_images)}')
            return redirect('kb:article-manage')
        messages.error(request, 'Please fix the errors below.')
    else:
        form = ArticleForm()

    context = {
        'form': form,
        'is_new': True,
        'meta_title': f'New article — {_site_name(request)}',
        'canonical_path': reverse('kb:article-create'),
    }
    return render(request, 'kb/article_form.html', context)


@superuser_required
def article_edit(request, slug):
    article = get_object_or_404(Article, slug=slug, org_id=PLATFORM_ORG_ID)

    if request.method == 'POST':
        form = ArticleForm(request.POST, instance=article)
        if form.is_valid():
            article = form.save(commit=False)
            if article.status == Article.STATUS_PUBLISHED and not article.published_at:
                article.published_at = timezone.now()
            article.save()
            form.save_m2m()
            deleted_images = purge_orphaned_markdown_images()
            messages.success(request, f'"{article.title}" saved.{_cleanup_suffix(deleted_images)}')
            return redirect('kb:article-manage')
        messages.error(request, 'Please fix the errors below.')
    else:
        form = ArticleForm(instance=article)

    context = {
        'form': form,
        'is_new': False,
        'article': article,
        'meta_title': f'Editing — {article.title} — {_site_name(request)}',
        'canonical_path': reverse('kb:article-edit', args=[article.slug]),
    }
    return render(request, 'kb/article_form.html', context)


@superuser_required
@require_POST
def article_delete(request, slug):
    """No structured DB relationship stops a delete here the way
    Category's PROTECT does — an article is always safe to delete on
    its own terms, nothing else references it. The two things that
    genuinely need cleaning up by hand are storage files, which
    Django's cascade delete never touches just because the row
    pointing at them is gone:

    1. ArticlePhoto rows cascade-delete fine (on_delete=CASCADE), but
       each one's actual image file would otherwise be left behind in
       storage — deleted explicitly below before the article itself
       goes.
    2. Any inline markdown image dropped into this article's body has
       no DB row at all tying it to the article (see kb/image_cleanup.py)
       — purge_orphaned_markdown_images() after the delete picks those
       up the same way it does after every create/edit.
    """
    article = get_object_or_404(Article, slug=slug, org_id=PLATFORM_ORG_ID)
    title = article.title

    for photo in article.photos.all():
        photo.image.delete(save=False)

    article.delete()
    deleted_images = purge_orphaned_markdown_images()
    messages.success(request, f'"{title}" deleted.{_cleanup_suffix(deleted_images)}')
    return redirect('kb:article-manage')


# ---------------------------------------------------------------------------
# Category / Tag authoring — same motivation as the Article authoring
# block above: these had no screen of their own, only Django Admin
# (kb/admin.py's CategoryAdmin/TagAdmin) or a direct DB insert. One
# combined page since both models are simple (name + org_id, slug
# auto-derived by Category.save()/Tag.save()) and were always managed
# together in practice. Superuser-only, same gate as everything else
# in this block — see kb/decorators.py.
# ---------------------------------------------------------------------------

@superuser_required
def taxonomy_manage(request):
    # Mirrors Category.Meta.ordering within each org, so this list reads
    # in the same order the public home page does.
    categories = Category.objects.all().order_by('org_id', 'sort_order', 'name')
    tags = Tag.objects.all().order_by('org_id', 'name')

    context = {
        'categories': categories,
        'tags': tags,
        'category_form': CategoryForm(),
        'tag_form': TagForm(),
        'meta_title': f'Categories & Tags — {_site_name(request)}',
        'canonical_path': reverse('kb:taxonomy-manage'),
    }
    return render(request, 'kb/taxonomy_manage.html', context)


@superuser_required
@require_POST
def category_create(request):
    form = CategoryForm(request.POST)
    if form.is_valid():
        # org_id is never taken from the form -- see _authoring_org_id.
        category = form.save(commit=False)
        category.org_id = _authoring_org_id(request.user)
        category.save()
        messages.success(request, f'Category "{category.name}" created.')
    else:
        messages.error(request, f'Could not create category: {form.errors.as_text()}')
    return redirect(reverse('kb:taxonomy-manage') + '#categories')


@superuser_required
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)

    if request.method == 'POST':
        form = CategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, f'Category "{category.name}" saved.')
            return redirect(reverse('kb:taxonomy-manage') + '#categories')
        messages.error(request, 'Please fix the errors below.')
    else:
        form = CategoryForm(instance=category)

    context = {
        'form': form,
        'item': category,
        'item_type': 'category',
        'cancel_url': reverse('kb:taxonomy-manage') + '#categories',
        'meta_title': f'Editing category — {category.name} — {_site_name(request)}',
        'canonical_path': reverse('kb:category-edit', args=[category.pk]),
    }
    return render(request, 'kb/taxonomy_item_form.html', context)


@superuser_required
@require_POST
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk)
    name = category.name
    try:
        category.delete()
        messages.success(request, f'Category "{name}" deleted.')
    except ProtectedError:
        count = category.articles.count()
        messages.error(
            request,
            f'Can\'t delete "{name}" — {count} article(s) still use it. '
            'Move them to another category first (Manage Articles, or Admin).',
        )
    return redirect(reverse('kb:taxonomy-manage') + '#categories')


@superuser_required
@require_POST
def tag_create(request):
    form = TagForm(request.POST)
    if form.is_valid():
        # org_id is never taken from the form -- see _authoring_org_id.
        tag = form.save(commit=False)
        tag.org_id = _authoring_org_id(request.user)
        tag.save()
        messages.success(request, f'Tag "{tag.name}" created.')
    else:
        messages.error(request, f'Could not create tag: {form.errors.as_text()}')
    return redirect(reverse('kb:taxonomy-manage') + '#tags')


@superuser_required
def tag_edit(request, pk):
    tag = get_object_or_404(Tag, pk=pk)

    if request.method == 'POST':
        form = TagForm(request.POST, instance=tag)
        if form.is_valid():
            form.save()
            messages.success(request, f'Tag "{tag.name}" saved.')
            return redirect(reverse('kb:taxonomy-manage') + '#tags')
        messages.error(request, 'Please fix the errors below.')
    else:
        form = TagForm(instance=tag)

    context = {
        'form': form,
        'item': tag,
        'item_type': 'tag',
        'cancel_url': reverse('kb:taxonomy-manage') + '#tags',
        'meta_title': f'Editing tag — {tag.name} — {_site_name(request)}',
        'canonical_path': reverse('kb:tag-edit', args=[tag.pk]),
    }
    return render(request, 'kb/taxonomy_item_form.html', context)


@superuser_required
@require_POST
def tag_delete(request, pk):
    tag = get_object_or_404(Tag, pk=pk)
    name = tag.name
    article_count = tag.articles.count()
    tag.delete()
    suffix = f' (removed from {article_count} article{"s" if article_count != 1 else ""}).' if article_count else '.'
    messages.success(request, f'Tag "{name}" deleted{suffix}')
    return redirect(reverse('kb:taxonomy-manage') + '#tags')


# ---------------------------------------------------------------------------
# Markdown inline-image upload — replaces django-markdownx's own
# ImageUploadView (see support_core/urls.py) for the drag-and-drop /
# paste-to-upload flow on the Markdown editor widget (Article.body via
# kb/forms.py's ArticleForm, and the same widget in Django Admin).
#
# Two things wrong with the stock behaviour, both surfaced by dropping a
# ~14MB phone photo into the editor and seeing "Invalid response" with no
# indication why:
#
# 1. No real image processing on this path. markdownx's own upload
#    view *does* run every image through PIL, but only to crop it to a
#    fixed 500x500 square (MARKDOWNX_IMAGE_MAX_SIZE) — fine for a small
#    inline thumbnail, actively wrong for an illustrative article
#    screenshot, which needs to keep its own aspect ratio and be large
#    enough to actually read. Below, every upload instead goes through
#    kb/image_processing.py::process_photo_image — the same resize/
#    EXIF-strip/re-encode pipeline ArticlePhoto already uses — so a
#    14MB phone photo becomes a same-aspect-ratio JPEG capped at
#    MAX_DIMENSION px and re-encoded at JPEG_QUALITY, typically well
#    under 1MB, automatically, on every upload — inline images get the
#    same treatment as every other photo in this app.
# 2. markdownx's own JS (the vendored markdownx.js, not something to
#    patch directly) treats any JSON response lacking `image_code`/
#    `image_path` identically, regardless of what it actually says: it
#    inserts the literal, unhelpful string "Invalid response" into the
#    editor at the cursor and only logs the real reason to the browser
#    console — nobody has devtools open while writing a help article.
#    Can't change that library behaviour from here, so
#    kb/static/kb/js/markdown-editor.js listens for the
#    `markdownx.fileUploadError` event markdownx *does* dispatch (with
#    the real response as event detail), removes the inserted
#    placeholder, and shows the actual message from this view instead.
# ---------------------------------------------------------------------------

@superuser_required
@require_POST
def markdown_image_upload(request):
    image = request.FILES.get('image')
    if not image:
        return JsonResponse({'error': 'No file was uploaded.'}, status=400)

    # Read per request, not once at import: settings.MAX_PHOTO_UPLOAD_SIZE
    # is the configurable ceiling and a module-level constant would have
    # frozen it (and made override_settings a no-op in tests).
    max_bytes = max_photo_upload_bytes()
    if image.size > max_bytes:
        return JsonResponse({
            'error': (
                f'That image is {image.size / 1024 / 1024:.1f}MB — the limit for '
                f'inline article images is {max_bytes // (1024 * 1024)}MB. '
                'Try a smaller export, or crop/compress it first (it will be '
                'resized further automatically once uploaded).'
            ),
        }, status=400)

    processed = process_photo_image(image.file)
    if processed is None:
        return JsonResponse({
            'error': (
                "That doesn't look like a supported image (JPEG, PNG, GIF or WEBP). "
                "If it's a HEIC/HEIF photo straight from an iPhone, re-export or "
                "screenshot it as JPEG/PNG first — this app can't decode HEIC."
            ),
        }, status=400)

    saved_path = default_storage.save(f'{MARKDOWNX_MEDIA_PATH}{processed.name}', processed)
    # Store the bare storage path, not default_storage.url(saved_path):
    # on the S3 backend that's a *presigned* URL good for
    # AWS_QUERYSTRING_EXPIRE seconds (1hr default) — baking that into
    # Article.body would leave every inline image broken an hour after
    # upload. kb/markdown_utils.py::render_markdown resolves this path to
    # a fresh URL on every render instead, same as ArticlePhoto's
    # FileField already does via its own .url property.
    return JsonResponse({'image_code': f'![]({saved_path})'})
