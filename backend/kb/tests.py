import io
import json
import re
import shutil
import tempfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import get_template
from django.template.loader_tags import BlockNode
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from markdownx.settings import MARKDOWNX_MEDIA_PATH
from PIL import Image

from accounts.models import Membership, Organisation
from kb.context_processors import help_url, tickets_url
from kb.image_cleanup import purge_orphaned_markdown_images
from kb.models import Article, ArticlePhoto, ArticleStep, Category, FeaturedArticle
from kb.validators import validate_image_size
from kb.views import ARTICLES_PER_CATEGORY_ON_HOME, ARTICLES_PER_PAGE, absolute_media_url


class AuthoringSidebarTests(TestCase):
    """Superuser authoring sidebar in kb/base.html."""

    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            'editor', 'editor@example.com', 'pw'
        )
        self.client.force_login(self.user)

    def _active_hrefs(self, url):
        html = self.client.get(url).content.decode()
        hrefs = {
            'create': reverse('kb:article-create'),
            'manage': reverse('kb:article-manage'),
            'taxonomy': reverse('kb:taxonomy-manage'),
        }
        return {
            key for key, href in hrefs.items()
            if f'class="side-nav-link active" href="{href}"' in html
        }

    def test_active_link_follows_current_page(self):
        self.assertEqual(self._active_hrefs(reverse('kb:article-create')), {'create'})
        self.assertEqual(self._active_hrefs(reverse('kb:article-manage')), {'manage'})
        self.assertEqual(self._active_hrefs(reverse('kb:taxonomy-manage')), {'taxonomy'})
        self.assertEqual(self._active_hrefs(reverse('kb:article-list')), set())

    def test_sidebar_links_only_to_this_service(self):
        # The sidebar has no cross-links out to other services. The one
        # absolute link is Tickets (TICKETS_URL), which is this same
        # service on its ticket origin — TICKET_SITE_URL.
        html = self.client.get(reverse('kb:article-manage')).content.decode()
        sidebar = html[html.index('<aside id="sideNav"'):html.index('</aside>')]
        absolute = re.findall(r'href="(https?://[^"]*)"', sidebar)
        own = (settings.SITE_URL.rstrip('/') + '/', settings.TICKET_SITE_URL.rstrip('/') + '/')
        self.assertEqual([u for u in absolute if not u.startswith(own)], [])

    def test_logout_is_a_post_form(self):
        # Django 5+ LogoutView rejects GET with 405, so the sidebar must POST.
        html = self.client.get(reverse('kb:article-manage')).content.decode()
        self.assertIn(
            f'<form class="side-nav-logout-form" method="post" action="{reverse("admin:logout")}">',
            html,
        )

    def test_logout_post_logs_out_and_redirects_home(self):
        response = self.client.post(
            reverse('admin:logout'), {'next': reverse('kb:article-list')}
        )
        self.assertRedirects(response, reverse('kb:article-list'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)


def _make_article(category, title, slug):
    return Article.objects.create(
        org_id=None,
        title=title,
        slug=slug,
        category=category,
        summary=f'Summary for {title}.',
        body='Body copy.',
        status=Article.STATUS_PUBLISHED,
        author_user_id=1,
        author_display_name='Acme Ltd',
        published_at=timezone.now(),
    )


class MembersOnlyCategoryVisibilityTests(TestCase):
    """The `Category.visibility == 'members'` gate
    (kb/views.py::_can_see_members_only).

    Two populations get in: signed-in users with at least one
    organisation membership, and support agents (`is_staff`), who author
    these articles and need to see them on the public pages like any
    reader would. A signed-in user who belongs to no organisation is
    kept out exactly like an anonymous visitor.
    """

    @classmethod
    def setUpTestData(cls):
        cls.public_category = Category.objects.create(
            org_id=None, name='Billing', visibility=Category.VISIBILITY_PUBLIC,
        )
        cls.members_category = Category.objects.create(
            org_id=None, name='Internal Runbooks', visibility=Category.VISIBILITY_MEMBERS,
        )
        cls.public_article = _make_article(cls.public_category, 'Paying by card', 'paying-by-card')
        cls.members_article = _make_article(cls.members_category, 'Refund runbook', 'refund-runbook')

        # Featured deliberately points at the members-only article: the
        # featured spot is its own queryset with its own .exclude(), so
        # it can leak independently of the article list.
        FeaturedArticle.objects.create(
            article=cls.members_article, starts_on=timezone.now().date(), curated_by_user_id=1,
        )

        User = get_user_model()
        cls.agent = User.objects.create_user('agent', 'agent@example.com', 'pw', is_staff=True)
        cls.member = User.objects.create_user('owner@member.test', 'owner@member.test', 'pw')
        organisation = Organisation.objects.create(name='Riverside Dental', slug='riverside')
        Membership.objects.create(user=cls.member, organisation=organisation)
        cls.non_member = User.objects.create_user('someone@example.com', 'someone@example.com', 'pw')

    def _list_html(self):
        return self.client.get(reverse('kb:article-list')).content.decode()

    def assertMembersContentHidden(self):
        html = self._list_html()
        self.assertIn('Paying by card', html)
        self.assertNotIn('Refund runbook', html)    # article list + featured spot
        self.assertNotIn('Internal Runbooks', html)  # category filter
        self.assertEqual(
            self.client.get(
                reverse('kb:article-detail', args=[self.members_article.slug])
            ).status_code,
            404,
        )

    def assertMembersContentShown(self):
        html = self._list_html()
        self.assertIn('Paying by card', html)
        self.assertIn('Refund runbook', html)
        self.assertIn('Internal Runbooks', html)
        self.assertEqual(
            self.client.get(
                reverse('kb:article-detail', args=[self.members_article.slug])
            ).status_code,
            200,
        )

    def test_anonymous_visitor_sees_no_members_content(self):
        self.assertMembersContentHidden()

    def test_signed_in_user_without_a_membership_sees_no_members_content(self):
        self.client.force_login(self.non_member)
        self.assertMembersContentHidden()

    def test_organisation_member_sees_members_content(self):
        self.client.force_login(self.member)
        self.assertMembersContentShown()

    def test_support_agent_sees_members_content_without_any_membership(self):
        """Agents write these articles; hiding them from the people who
        author them would make "is my article live?" unanswerable from
        the public pages."""
        self.client.force_login(self.agent)
        self.assertFalse(self.agent.memberships.exists())
        self.assertMembersContentShown()


@override_settings(
    TICKET_HOSTS=['support.example.com'],
    TICKET_SITE_URL='https://support.example.com',
    ALLOWED_HOSTS=['help.example.com', 'support.example.com', 'testserver'],
)
class TicketsUrlContextProcessorTests(TestCase):
    """The header's Tickets link has to cross hosts from help. to
    ticket., and must never be built with a `tickets:` named-URL lookup
    -- `reverse()` only ever yields a path, so it cannot name the other
    origin."""

    def test_absolute_on_the_help_host(self):
        request = RequestFactory(headers={'host': 'help.example.com'}).get('/')
        self.assertEqual(
            tickets_url(request), {'TICKETS_URL': 'https://support.example.com/tickets/'},
        )

    def test_relative_on_a_ticket_host(self):
        request = RequestFactory(headers={'host': 'support.example.com'}).get('/')
        self.assertEqual(tickets_url(request), {'TICKETS_URL': '/tickets/'})

    def test_the_hardcoded_paths_still_match_the_tickets_urlconf(self):
        """The one test in this file that talks to `tickets.urls`.

        Everything else here -- kb/context_processors.py, kb/views.py::
        _ticket_url_for, and the assertions above -- spells the desk's
        paths as string literals, because reverse() yields a path and
        cannot express the cross-host half of the link (see the class
        docstring). That means a rename in tickets/urls.py would break
        every KB "Tickets" header link and every article's "Raise a
        ticket" CTA while leaving this whole suite green: the literals
        would agree with each other and with nothing else.

        So pin them to the URLconf here, once, on the ticket host where
        the helper's own output *is* a plain path. If tickets/urls.py
        moves, this is the test that says so.
        """
        request = RequestFactory(headers={'host': 'support.example.com'}).get('/')
        self.assertEqual(tickets_url(request)['TICKETS_URL'], reverse('tickets:list'))
        self.assertEqual(reverse('tickets:list'), '/tickets/')
        # _ticket_url_for appends 'new/' to TICKETS_URL; that join is
        # only correct while tickets:create is TICKETS_URL + 'new/'.
        self.assertEqual(reverse('tickets:create'), '/tickets/new/')

    @override_settings(TICKET_SITE_URL='https://support.example.com/')
    def test_trailing_slash_on_the_setting_does_not_double_up(self):
        request = RequestFactory(headers={'host': 'help.example.com'}).get('/')
        self.assertEqual(
            tickets_url(request), {'TICKETS_URL': 'https://support.example.com/tickets/'},
        )

    def test_rendered_header_link_crosses_hosts(self):
        """End to end through a real template render, so this also fails
        if the context processor is ever dropped from settings."""
        category = Category.objects.create(org_id=None, name='Billing')
        article = _make_article(category, 'Paying by card', 'paying-by-card')
        User = get_user_model()
        self.client.force_login(
            User.objects.create_user('agent', 'a@example.com', 'pw', is_staff=True)
        )
        path = reverse('kb:article-detail', args=[article.slug])

        on_help = self.client.get(path, headers={'host': 'help.example.com'}).content.decode()
        self.assertIn('href="https://support.example.com/tickets/">Tickets</a>', on_help)

        # Not '/' on the ticket host: its root is redirected to /tickets/
        # by TicketHostRootRedirectMiddleware, so an article path is the
        # only way to exercise the header on that host.
        on_ticket = self.client.get(path, headers={'host': 'support.example.com'}).content.decode()
        self.assertIn('href="/tickets/">Tickets</a>', on_ticket)


@override_settings(
    TICKET_HOSTS=['support.example.com'],
    TICKET_SITE_URL='https://support.example.com',
    SITE_URL='https://help.example.com',
    ALLOWED_HOSTS=['help.example.com', 'support.example.com', 'testserver'],
)
class HelpUrlCrossLinkTests(TestCase):
    """The way back from a ticket host to the KB. Its bare `/` is
    redirected to /tickets/, so every "go to Help" link on that host --
    brand, search, header button, sidebar -- has to be absolute."""

    def test_relative_on_the_help_host(self):
        request = RequestFactory(headers={'host': 'help.example.com'}).get('/')
        self.assertEqual(help_url(request), {'HELP_URL': '/'})

    def test_absolute_on_a_ticket_host(self):
        request = RequestFactory(headers={'host': 'support.example.com'}).get('/')
        self.assertEqual(help_url(request), {'HELP_URL': 'https://help.example.com/'})

    def test_superuser_sidebar_links_both_halves_from_either_host(self):
        self.client.force_login(
            get_user_model().objects.create_superuser('ed', 'ed@example.com', 'pw')
        )
        on_help = self.client.get(
            reverse('kb:article-manage'), headers={'host': 'help.example.com'},
        ).content.decode()
        self.assertIn('class="side-nav-link" href="https://support.example.com/tickets/"', on_help)

        on_ticket = self.client.get(
            reverse('tickets:list'), headers={'host': 'support.example.com'},
        ).content.decode()
        self.assertIn('class="side-nav-link active" href="/tickets/"', on_ticket)
        self.assertIn('class="side-nav-link" href="https://help.example.com/"', on_ticket)

    def test_search_and_brand_on_ticket_host_go_to_the_help_host(self):
        self.client.force_login(
            get_user_model().objects.create_user('agent', 'a@example.com', 'pw', is_staff=True)
        )
        html = self.client.get(
            reverse('tickets:list'), headers={'host': 'support.example.com'},
        ).content.decode()
        self.assertIn('<a class="brand" href="https://help.example.com/">', html)
        self.assertIn('<form class="search-form" action="https://help.example.com/"', html)

    def test_header_button_offers_the_other_half(self):
        self.client.force_login(
            get_user_model().objects.create_user('agent', 'a@example.com', 'pw', is_staff=True)
        )
        on_tickets = self.client.get(
            reverse('tickets:list'), headers={'host': 'support.example.com'},
        ).content.decode()
        self.assertIn('href="https://help.example.com/">Help</a>', on_tickets)
        self.assertNotIn('>Tickets</a>', on_tickets)

        on_kb = self.client.get(
            reverse('kb:article-list'), headers={'host': 'help.example.com'},
        ).content.decode()
        self.assertIn('href="https://support.example.com/tickets/">Tickets</a>', on_kb)
        self.assertNotIn('>Help</a>', on_kb)


class HeaderAuthBlockTests(TestCase):
    """The three header states in kb/base.html."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.agent = User.objects.create_user(
            'agent', 'agent@example.com', 'pw',
            is_staff=True, first_name='Sam', last_name='Agent',
        )
        cls.member = User.objects.create_user(
            'owner@member.test', 'owner@member.test', 'pw',
            first_name='Dana', last_name='Owner',
        )
        organisation = Organisation.objects.create(name='Riverside Dental', slug='riverside')
        Membership.objects.create(user=cls.member, organisation=organisation, role=Membership.Role.OWNER)

    def _header(self, path=None):
        return self.client.get(path or reverse('kb:article-list')).content.decode()

    def test_anonymous_gets_a_log_in_link_carrying_next(self):
        html = self._header(reverse('kb:article-list') + '?category=billing')
        # Django's `urlencode` filter leaves '/' alone by default (it is
        # safe in a query value), so only the '?' and '=' are escaped.
        self.assertIn(
            f'href="{reverse("accounts:login")}?next=/%3Fcategory%3Dbilling">Log in</a>',
            html,
        )
        self.assertNotIn('>Tickets</a>', html)
        self.assertNotIn('Log out', html)

    def _assert_post_log_out(self, html):
        # POST-only for every signed-in account: a GET logout link is
        # CSRF-able and pre-fetchable.
        self.assertIn(
            f'<form class="nav-auth-logout-form" method="post" '
            f'action="{reverse("accounts:logout")}">',
            html,
        )
        self.assertIn('csrfmiddlewaretoken', html)
        self.assertNotIn(f'href="{reverse("accounts:logout")}"', html)

    def test_a_member_sees_their_name_organisation_tickets_and_a_post_log_out(self):
        self.client.force_login(self.member)
        html = self._header()
        self.assertIn('Dana Owner &middot; Riverside Dental', html)
        self.assertIn('>Tickets</a>', html)
        self._assert_post_log_out(html)

    def test_a_member_of_several_organisations_sees_them_all(self):
        second = Organisation.objects.create(name='Hilltop Accounting', slug='hilltop')
        Membership.objects.create(user=self.member, organisation=second)
        self.client.force_login(self.member)
        self.assertIn('Dana Owner &middot; Hilltop Accounting, Riverside Dental', self._header())

    def test_a_customer_with_no_organisation_is_still_a_customer(self):
        """Zero memberships is a valid customer state (they can raise
        organisation-less tickets), so they get the customer header --
        just with no organisation after their name -- not the agent
        one."""
        User = get_user_model()
        loner = User.objects.create_user('loner@example.com', 'loner@example.com', 'pw', first_name='Lee')
        self.client.force_login(loner)
        html = self._header()
        self.assertIn('<span class="nav-username">Lee</span>', html)
        self.assertIn('>Tickets</a>', html)
        self._assert_post_log_out(html)

    def test_support_agent_gets_a_post_log_out_and_no_organisation_name(self):
        self.client.force_login(self.agent)
        html = self._header()
        self.assertIn('Sam Agent', html)
        self.assertNotIn('Riverside Dental', html)
        self.assertIn('>Tickets</a>', html)
        self._assert_post_log_out(html)

    def test_page_nav_block_exists_for_the_tickets_app_to_fill(self):
        """tickets/base.html extends this template and overrides
        {% block page_nav %}. Assert the block is there rather than that
        the KB renders anything into it -- it is empty here by design, so
        nothing else in the suite would notice it going missing until the
        ticket desk's nav strip silently vanished."""
        nodes = get_template('kb/base.html').template.nodelist.get_nodes_by_type(BlockNode)
        self.assertIn('page_nav', {node.name for node in nodes})


@override_settings(
    TICKET_HOSTS=['support.example.com'],
    TICKET_SITE_URL='https://support.example.com',
    ALLOWED_HOSTS=['help.example.com', 'support.example.com', 'testserver'],
)
class ArticleTicketCtaTests(TestCase):
    """The article footer's "Couldn't find an answer? Raise a ticket"
    CTA (kb/views.py::_ticket_url_for).

    Three populations, two of which are easy to get wrong:

      anonymous     -- no CTA at all. /tickets/new/ is login_required
                       and this KB's typical reader has no
                       account, so a link would dead-end in a login wall.
      customer      -- the CTA, pointing at the *ticket* host when read
                       on the help host (one service, two hostnames).
      support agent -- the CTA too, even though ticket_create refuses
                       them: the refusal is a page that explains itself.
                       Asserted here end to end, because "show a link
                       that 403s" is only defensible while the 403 keeps
                       saying why.
    """

    @classmethod
    def setUpTestData(cls):
        category = Category.objects.create(org_id=None, name='Classes')
        cls.article = _make_article(category, 'Setting up classes', 'setting-up-classes')
        cls.path = reverse('kb:article-detail', args=[cls.article.slug])

        User = get_user_model()
        cls.agent = User.objects.create_user('agent', 'agent@example.com', 'pw', is_staff=True)
        cls.member = User.objects.create_user('owner@member.test', 'owner@member.test', 'pw')
        organisation = Organisation.objects.create(name='Riverside Dental', slug='riverside')
        Membership.objects.create(user=cls.member, organisation=organisation)

    def _html(self, host='help.example.com'):
        return self.client.get(self.path, headers={'host': host}).content.decode()

    def test_anonymous_reader_gets_no_cta(self):
        html = self._html()
        self.assertNotIn('ticket-cta', html)
        self.assertNotIn('Raise a ticket', html)

    def test_a_customer_gets_an_absolute_cross_host_link_on_the_help_host(self):
        self.client.force_login(self.member)
        self.assertIn(
            f'<a href="https://support.example.com/tickets/new/?article={self.article.pk}">'
            f'Raise a ticket</a>',
            self._html(),
        )

    def test_the_same_link_is_relative_on_the_ticket_host(self):
        """Same page, same service, other hostname -- a relative path,
        so it keeps working on a preview/staging domain where
        TICKET_SITE_URL still names production."""
        self.client.force_login(self.member)
        self.assertIn(
            f'<a href="/tickets/new/?article={self.article.pk}">Raise a ticket</a>',
            self._html(host='support.example.com'),
        )

    def test_support_agent_sees_the_cta_and_its_target_explains_the_refusal(self):
        self.client.force_login(self.agent)
        self.assertIn('Raise a ticket</a>', self._html(host='support.example.com'))
        response = self.client.get(
            f'/tickets/new/?article={self.article.pk}', headers={'host': 'support.example.com'},
        )
        self.assertContains(response, 'agent accounts cannot raise tickets', status_code=403)

    def test_copy_is_raise_a_ticket_not_the_old_contact_support(self):
        """The CTA raises a ticket in this service's own desk, not a
        generic "Contact support" link out to somewhere else."""
        self.client.force_login(self.member)
        self.assertNotIn('Contact support', self._html())


class ArticleSitemapTests(TestCase):
    """/sitemap.xml is fetched anonymously by definition, so it must
    apply the same visibility gate the detail view does. Before this it
    published every members-only article's slug and lastmod -- no body
    text, but the titles live in the slugs, on a document whose whole
    purpose is to be crawled and archived."""

    @classmethod
    def setUpTestData(cls):
        public_category = Category.objects.create(
            org_id=None, name='Billing', visibility=Category.VISIBILITY_PUBLIC,
        )
        members_category = Category.objects.create(
            org_id=None, name='Internal Runbooks', visibility=Category.VISIBILITY_MEMBERS,
        )
        cls.public_article = _make_article(public_category, 'Paying by card', 'paying-by-card')
        cls.members_article = _make_article(members_category, 'Refund runbook', 'refund-runbook')

    def test_members_only_slugs_are_absent_while_public_ones_are_listed(self):
        xml = self.client.get('/sitemap.xml').content.decode()
        self.assertIn(reverse('kb:article-detail', args=[self.public_article.slug]), xml)
        self.assertNotIn(reverse('kb:article-detail', args=[self.members_article.slug]), xml)
        self.assertNotIn('refund-runbook', xml)

    def test_signing_in_does_not_change_the_sitemap(self):
        """A crawler is anonymous, but the gate is in the queryset, not
        in the request -- so this is a fixed document, not a
        per-visitor one. (A member's session seeing extra URLs here
        would be the same leak with more steps: sitemaps get shared,
        cached and archived.)"""
        User = get_user_model()
        user = User.objects.create_user('owner@member.test', 'owner@member.test', 'pw')
        organisation = Organisation.objects.create(name='Hilltop Accounting', slug='hilltop')
        Membership.objects.create(user=user, organisation=organisation)
        self.client.force_login(user)
        xml = self.client.get('/sitemap.xml').content.decode()
        self.assertNotIn('refund-runbook', xml)


def _png_bytes(size=(20, 12), colour=(200, 40, 90)):
    """A real, tiny PNG — ArticlePhoto.save() runs the whole
    resize/EXIF-strip/re-encode pipeline on every save, so a fake byte
    string would be silently dropped rather than stored."""
    buffer = io.BytesIO()
    Image.new('RGB', size, colour).save(buffer, format='PNG')
    return buffer.getvalue()


class OrphanedImagePurgeGuardTests(TestCase):
    """purge_orphaned_markdown_images() works out what is
    "orphaned" from Article.objects in *this* database and deletes
    every other file under markdownx/ in storage. So against storage
    that already holds content this database never saw — a previous
    install, or a directory shared with another site — the first
    article save would delete all of it, reported only as
    "(40 unused images cleaned up.)".

    Hence settings.KB_PURGE_ORPHANED_IMAGES, default False. The first
    two tests are the whole contract: it does its job when switched on,
    and it touches nothing at all when it is not.
    """

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        # Stands in for an image already in the bucket that no article
        # in this (empty) database has ever referenced.
        self.stored_path = default_storage.save(
            MARKDOWNX_MEDIA_PATH + 'someone_elses_screenshot.png', ContentFile(_png_bytes()),
        )

    @override_settings(KB_PURGE_ORPHANED_IMAGES=True)
    def test_purges_when_the_setting_is_on(self):
        self.assertEqual(purge_orphaned_markdown_images(), 1)
        self.assertFalse(default_storage.exists(self.stored_path))

    @override_settings(KB_PURGE_ORPHANED_IMAGES=False)
    def test_deletes_nothing_when_the_setting_is_off(self):
        self.assertEqual(purge_orphaned_markdown_images(), 0)
        self.assertTrue(default_storage.exists(self.stored_path))

    @override_settings(KB_PURGE_ORPHANED_IMAGES=True)
    def test_a_referenced_image_is_never_touched_even_when_on(self):
        """The guard is about which *database* we are running against;
        the original safety property still has to hold once it is on."""
        category = Category.objects.create(org_id=None, name='Classes')
        article = _make_article(category, 'Using the door', 'using-the-door')
        article.body = '![](/media/' + self.stored_path + ')'
        article.save()

        self.assertEqual(purge_orphaned_markdown_images(), 0)
        self.assertTrue(default_storage.exists(self.stored_path))

    @override_settings(KB_PURGE_ORPHANED_IMAGES=False)
    def test_saving_an_article_through_the_editor_deletes_nothing(self):
        """The end-to-end version of the same thing: the three call
        sites are in kb/views.py, and this is the one an operator would
        actually trip over on day one."""
        User = get_user_model()
        author = User.objects.create_superuser('author', 'author@example.com', 'pw')
        self.client.force_login(author)
        category = Category.objects.create(org_id=None, name='Billing')

        response = self.client.post(reverse('kb:article-create'), {
            'title': 'Paying by card', 'slug': 'paying-by-card', 'category': category.pk,
            'summary': 'How to pay.', 'body': 'No images at all.',
            'status': Article.STATUS_PUBLISHED,
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Article.objects.filter(slug='paying-by-card').exists())
        self.assertTrue(default_storage.exists(self.stored_path))


@override_settings(SITE_URL='https://help.example.com')
class AbsoluteMediaUrlTests(SimpleTestCase):
    """`FieldFile.url` is a root-relative path on FileSystemStorage (the
    default, and every test) but a complete absolute URL on an
    S3-compatible backend, so a naive `SITE_URL + photo.image.url` would
    be correct under the default and broken the day someone swaps the
    storage backend."""

    def test_a_storage_relative_path_is_joined_onto_site_url(self):
        self.assertEqual(
            absolute_media_url('/media/articles/2026/09/hero.jpg'),
            'https://help.example.com/media/articles/2026/09/hero.jpg',
        )

    def test_an_already_absolute_url_is_returned_unchanged(self):
        s3_url = 'https://bucket.example.com/articles/2026/09/hero.jpg?X-Amz-Signature=abc'
        self.assertEqual(absolute_media_url(s3_url), s3_url)
        self.assertNotIn('comhttps', absolute_media_url(s3_url))

    def test_a_protocol_relative_url_is_returned_unchanged(self):
        self.assertEqual(
            absolute_media_url('//bucket.example.com/hero.jpg'),
            '//bucket.example.com/hero.jpg',
        )

    def test_empty_input_passes_straight_through(self):
        self.assertEqual(absolute_media_url(''), '')
        self.assertIsNone(absolute_media_url(None))


@override_settings(SITE_URL='https://help.example.com')
class ArticleCrawlerImageTagTests(TestCase):
    """End to end, through the three call sites that build a
    crawler-facing image URL: og:image in
    kb/templates/kb/article_detail.html and the two schema.org `image`
    keys in kb/views.py::_howto_schema_json.

    The S3 case is faked by pointing MEDIA_URL at an absolute origin,
    which gives FileSystemStorage's `.url` the exact shape an
    S3-compatible backend's has — no bucket required, and it fails
    loudly against a naive `SITE_URL + url` implementation."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)

        category = Category.objects.create(org_id=None, name='Classes')
        self.article = _make_article(category, 'Setting up classes', 'setting-up-classes')
        self.hero = ArticlePhoto.objects.create(
            article=self.article,
            image=SimpleUploadedFile('hero.png', _png_bytes(), content_type='image/png'),
            alt_text='The classes screen',
        )
        step = ArticleStep.objects.create(article=self.article, order=1, instruction='Open Classes.')
        self.step_photo = ArticlePhoto.objects.create(
            article=self.article, step=step,
            image=SimpleUploadedFile('step.png', _png_bytes(), content_type='image/png'),
        )
        self.path = reverse('kb:article-detail', args=[self.article.slug])

    def _og_image(self, html):
        marker = '<meta property="og:image" content="'
        start = html.index(marker) + len(marker)
        return html[start:html.index('"', start)]

    def _schema(self, html):
        opening = '<script type="application/ld+json">'
        start = html.index(opening) + len(opening)
        return json.loads(html[start:html.index('</script>', start)])

    def test_filesystem_storage_urls_are_joined_onto_site_url(self):
        html = self.client.get(self.path).content.decode()

        self.assertEqual(
            self._og_image(html), 'https://help.example.com' + self.hero.image.url,
        )
        schema = self._schema(html)
        self.assertEqual(schema['image'], 'https://help.example.com' + self.hero.image.url)
        self.assertEqual(
            schema['step'][0]['image'],
            'https://help.example.com' + self.step_photo.image.url,
        )

    @override_settings(MEDIA_URL='https://bucket.example.com/')
    def test_s3_shaped_absolute_urls_are_not_prefixed_again(self):
        html = self.client.get(self.path).content.decode()

        og_image = self._og_image(html)
        self.assertTrue(og_image.startswith('https://bucket.example.com/'), og_image)
        # The actual production symptom, asserted literally.
        self.assertNotIn('help.example.comhttps://', html)

        schema = self._schema(html)
        self.assertTrue(schema['image'].startswith('https://bucket.example.com/'))
        self.assertTrue(schema['step'][0]['image'].startswith('https://bucket.example.com/'))


@override_settings(
    TICKET_HOSTS=['support.example.com'],
    ALLOWED_HOSTS=['help.example.com', 'support.example.com', 'testserver'],
)
class RobotsTxtTests(TestCase):
    """One service, two hostnames, one robots.txt view — and it
    built its answer from request.get_host(), so the ticket host served
    `Allow: /` plus a Sitemap: line listing every KB article under a
    hostname they should not be indexed on. Only the canonical tag
    stood between that and double-indexing."""

    def test_the_help_host_is_crawlable_and_advertises_the_sitemap(self):
        body = self.client.get('/robots.txt', headers={'host': 'help.example.com'}).content.decode()

        self.assertIn('Allow: /', body)
        self.assertIn('Sitemap: http://help.example.com/sitemap.xml', body)
        self.assertIn('Disallow: /tickets/', body)
        self.assertIn('Disallow: /staff/', body)

    def test_the_ticket_host_is_disallowed_entirely_and_advertises_nothing(self):
        body = self.client.get('/robots.txt', headers={'host': 'support.example.com'}).content.decode()

        self.assertIn('Disallow: /', body)
        self.assertNotIn('Allow: /', body)
        self.assertNotIn('Sitemap:', body)

    def test_the_login_page_is_disallowed(self):
        """/login/ is disallowed alongside /staff/."""
        body = self.client.get('/robots.txt', headers={'host': 'help.example.com'}).content.decode()

        self.assertIn('Disallow: /login/', body)


class PhotoUploadSizeSettingTests(TestCase):
    """settings.MAX_PHOTO_UPLOAD_SIZE is documented in .env.example as
    the enforced ceiling, so both call sites must actually read it — a
    hardcoded constant would make setting the env var do nothing."""

    class _FakeUpload:
        def __init__(self, size):
            self.size = size

    @override_settings(MAX_PHOTO_UPLOAD_SIZE=1024)
    def test_the_validator_honours_a_lowered_setting(self):
        validate_image_size(self._FakeUpload(1024))  # exactly at the limit, fine
        with self.assertRaises(ValidationError):
            validate_image_size(self._FakeUpload(1025))

    @override_settings(MAX_PHOTO_UPLOAD_SIZE=50 * 1024 * 1024)
    def test_the_validator_honours_a_raised_setting(self):
        validate_image_size(self._FakeUpload(30 * 1024 * 1024))

    def test_the_default_is_unchanged_at_25mb(self):
        """The point of the default is that wiring the setting up
        changes nothing until someone configures it."""
        from django.conf import settings as django_settings

        self.assertEqual(django_settings.MAX_PHOTO_UPLOAD_SIZE, 25 * 1024 * 1024)
        with self.assertRaises(ValidationError):
            validate_image_size(self._FakeUpload(25 * 1024 * 1024 + 1))

    @override_settings(MAX_PHOTO_UPLOAD_SIZE=1024)
    def test_the_inline_markdown_upload_endpoint_honours_it_too(self):
        User = get_user_model()
        author = User.objects.create_superuser('author', 'author@example.com', 'pw')
        self.client.force_login(author)

        oversized = SimpleUploadedFile(
            'big.png', _png_bytes(size=(400, 400)), content_type='image/png',
        )
        self.assertGreater(oversized.size, 1024)

        response = self.client.post('/markdownx/upload/', {'image': oversized})

        self.assertEqual(response.status_code, 400)
        self.assertIn('the limit for inline article images is', response.json()['error'])


class ArticleListLayoutTests(TestCase):
    """The home page groups articles by category, showing only the
    newest few per category with a "See all" link, so it stays roughly
    the same length however many articles are published. Any filter
    (search, category or tag) switches to a flat, paginated grid.
    """

    @classmethod
    def setUpTestData(cls):
        cls.getting_started = Category.objects.create(org_id=None, name='Getting Started')
        cls.technical = Category.objects.create(org_id=None, name='Technical')
        cls.empty = Category.objects.create(org_id=None, name='Nothing Here Yet')
        for i in range(15):
            _make_article(cls.getting_started, f'Setup guide {i:02d}', f'setup-guide-{i:02d}')
        for i in range(2):
            _make_article(cls.technical, f'Embed guide {i}', f'embed-guide-{i}')

    def _get(self, **params):
        return self.client.get(reverse('kb:article-list'), params)

    def test_home_groups_articles_into_one_section_per_category(self):
        response = self._get()
        sections = response.context['category_sections']
        self.assertEqual(
            [s['category'].name for s in sections], ['Getting Started', 'Technical'],
        )
        self.assertFalse(response.context['is_filtered'])

    def test_home_hides_categories_with_no_published_articles(self):
        response = self._get()
        names = [s['category'].name for s in response.context['category_sections']]
        self.assertNotIn('Nothing Here Yet', names)
        self.assertNotContains(response, '<h2 class="category-section-title">Nothing Here Yet</h2>', html=False)

    def test_home_caps_each_category_and_links_to_the_rest(self):
        response = self._get()
        busy = response.context['category_sections'][0]
        self.assertEqual(busy['count'], 15)
        self.assertEqual(len(busy['articles']), ARTICLES_PER_CATEGORY_ON_HOME)
        self.assertContains(response, 'See all 15 articles')
        self.assertContains(response, f'href="?category={self.getting_started.slug}"')

    def test_home_shows_newest_articles_first_within_a_category(self):
        response = self._get()
        titles = [a.title for a in response.context['category_sections'][0]['articles']]
        self.assertEqual(titles[0], 'Setup guide 14')

    def test_small_category_gets_no_see_all_link(self):
        response = self._get()
        small = response.context['category_sections'][1]
        self.assertFalse(small['has_more'])
        self.assertNotContains(response, 'See all 2 articles')

    def test_category_filter_is_paginated(self):
        page_one = self._get(category=self.getting_started.slug)
        self.assertTrue(page_one.context['is_filtered'])
        self.assertEqual(len(page_one.context['page_obj']), ARTICLES_PER_PAGE)
        self.assertEqual(page_one.context['page_obj'].paginator.count, 15)
        self.assertContains(page_one, 'Page 1 of 2')

        page_two = self._get(category=self.getting_started.slug, page=2)
        self.assertEqual(len(page_two.context['page_obj']), 15 - ARTICLES_PER_PAGE)

    def test_pagination_links_keep_the_active_filters(self):
        response = self._get(category=self.getting_started.slug)
        self.assertContains(
            response, f'href="?category={self.getting_started.slug}&amp;page=2"',
        )

    def test_out_of_range_page_falls_back_instead_of_404(self):
        response = self._get(category=self.getting_started.slug, page=99)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['page_obj'].number, 2)

    def test_search_results_are_a_flat_list_not_grouped(self):
        response = self._get(q='Embed')
        self.assertTrue(response.context['is_filtered'])
        self.assertEqual(
            {a.title for a in response.context['page_obj']}, {'Embed guide 0', 'Embed guide 1'},
        )

    def test_empty_knowledge_base_shows_the_empty_message(self):
        Article.objects.all().delete()
        response = self._get()
        self.assertEqual(response.context['category_sections'], [])
        self.assertContains(response, 'No articles match yet')


class ArticleSortOrderTests(TestCase):
    """An optional per-article `sort_order` lets an author pin reading
    order (e.g. a Getting Started sequence). Numbered articles come
    first, lowest number first; unnumbered ones follow, newest first --
    so a category nobody has numbered behaves exactly as before."""

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(org_id=None, name='Getting Started')
        # Created oldest -> newest, so newest-first alone would reverse them.
        cls.hub = _make_article(cls.category, 'Hub', 'hub')
        cls.second = _make_article(cls.category, 'Second', 'second')
        cls.unnumbered_old = _make_article(cls.category, 'Unnumbered old', 'unnumbered-old')
        cls.unnumbered_new = _make_article(cls.category, 'Unnumbered new', 'unnumbered-new')
        Article.objects.filter(pk=cls.hub.pk).update(sort_order=1)
        Article.objects.filter(pk=cls.second.pk).update(sort_order=2)

    EXPECTED = ['Hub', 'Second', 'Unnumbered new', 'Unnumbered old']

    def test_home_section_follows_sort_order_then_newest(self):
        response = self.client.get(reverse('kb:article-list'))
        titles = [a.title for a in response.context['category_sections'][0]['articles']]
        self.assertEqual(titles, self.EXPECTED)

    def test_category_view_uses_the_same_order(self):
        response = self.client.get(reverse('kb:article-list'), {'category': self.category.slug})
        self.assertEqual([a.title for a in response.context['page_obj']], self.EXPECTED)

    def test_article_form_accepts_a_blank_or_positive_order(self):
        from kb.forms import ArticleForm

        base = {
            'title': 'New one', 'category': self.category.pk, 'summary': 's',
            'body': 'b', 'status': Article.STATUS_DRAFT,
        }
        self.assertTrue(ArticleForm(data={**base, 'sort_order': ''}).is_valid())
        self.assertTrue(ArticleForm(data={**base, 'sort_order': '3'}).is_valid())
        self.assertIn('sort_order', ArticleForm(data={**base, 'sort_order': '-1'}).errors)


class CategorySortOrderTests(TestCase):
    """Categories are ordered by an explicit sort_order, falling back to
    name — the KB home page groups by category, so without this the
    section order was purely alphabetical and 'Classes & Scheduling'
    sorted above 'Getting Started'."""

    def setUp(self):
        # Created out of both alphabetical and sort_order sequence, so a
        # passing assertion can't just be insertion order.
        self.technical = Category.objects.create(name='Technical', sort_order=6)
        self.getting_started = Category.objects.create(name='Getting Started', sort_order=1)
        self.classes = Category.objects.create(name='Classes & Scheduling', sort_order=2)

    def test_sort_order_beats_alphabetical(self):
        self.assertEqual(
            [c.name for c in Category.objects.all()],
            ['Getting Started', 'Classes & Scheduling', 'Technical'],
        )

    def test_categories_sharing_an_order_fall_back_to_name(self):
        Category.objects.all().update(sort_order=0)
        self.assertEqual(
            [c.name for c in Category.objects.all()],
            ['Classes & Scheduling', 'Getting Started', 'Technical'],
        )

    def test_default_is_zero_so_new_categories_sort_alphabetically_first(self):
        new = Category.objects.create(name='Aardvarks')
        self.assertEqual(new.sort_order, 0)
        self.assertEqual(Category.objects.first(), new)

    def test_home_page_sections_follow_sort_order(self):
        author = get_user_model().objects.create_user('sorter', password='x')
        for category in (self.technical, self.getting_started, self.classes):
            Article.objects.create(
                title=f'{category.name} article', category=category,
                summary='s', body='b', status=Article.STATUS_PUBLISHED,
                published_at=timezone.now(), author_user_id=author.id,
                author_display_name='Sorter',
            )

        response = self.client.get(reverse('kb:article-list'))
        names = [s['category'].name for s in response.context['category_sections']]
        self.assertEqual(names, ['Getting Started', 'Classes & Scheduling', 'Technical'])

    def test_category_form_exposes_sort_order(self):
        from kb.forms import CategoryForm

        self.assertIn('sort_order', CategoryForm().fields)
        form = CategoryForm(data={'name': 'Ordered', 'visibility': Category.VISIBILITY_PUBLIC, 'sort_order': '4'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().sort_order, 4)

    def test_category_form_defaults_to_zero_when_left_blank(self):
        from kb.forms import CategoryForm

        form = CategoryForm(data={'name': 'Unordered', 'visibility': Category.VISIBILITY_PUBLIC, 'sort_order': ''})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().sort_order, 0)


class MediaServingTests(TestCase):
    """`/media/<path>` is served by this app, off MEDIA_ROOT.

    Production runs the same filesystem storage as local dev, with
    MEDIA_ROOT on the mounted data volume, so the route is
    unconditional — and untested-but-unconditional is how a help centre
    silently stops rendering every article image.
    """

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.stored_path = default_storage.save(
            'articles/hero.png', ContentFile(_png_bytes()),
        )

    def test_serves_a_stored_image(self):
        response = self.client.get('/media/' + self.stored_path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), _png_bytes())

    def test_sets_a_cache_header(self):
        response = self.client.get('/media/' + self.stored_path)
        self.assertEqual(response['Cache-Control'], 'public, max-age=86400')

    def test_a_missing_file_404s(self):
        self.assertEqual(self.client.get('/media/articles/nope.png').status_code, 404)

    def test_a_path_escaping_media_root_is_refused(self):
        """The property that makes serving user uploads from a public
        route safe at all. Django rejects the traversal before it ever
        reaches the view (400), rather than serve()'s own 403/404 —
        assert the refusal, not which layer refused, so a future Django
        moving the check does not fail this test spuriously."""
        response = self.client.get('/media/../support_core/settings.py')
        self.assertIn(response.status_code, (400, 403, 404))
        self.assertNotIn(b'SECRET_KEY', response.content)

    def test_only_safe_methods_are_allowed(self):
        self.assertEqual(self.client.post('/media/' + self.stored_path).status_code, 405)


def _make_search_article(category, title, summary='Placeholder overview.', body='Placeholder text.',
                         status=Article.STATUS_PUBLISHED):
    """Article with explicit title/summary/body, so a search test controls
    exactly which column a word appears in (`_make_article` above puts the
    word "Summary" in every summary, which would pollute stemming tests)."""
    return Article.objects.create(
        org_id=None,
        title=title,
        category=category,
        summary=summary,
        body=body,
        status=status,
        author_user_id=1,
        author_display_name='Support',
        published_at=timezone.now() if status == Article.STATUS_PUBLISHED else None,
    )


class Fts5SearchTests(TestCase):
    """KB search on SQLite's FTS5 index (kb/search_index.py). The test
    database is SQLite with FTS5 compiled in, so this exercises the live
    path, not a mock."""

    @classmethod
    def setUpTestData(cls):
        from django.db import connection
        from kb import search_index
        assert search_index.fts5_available(connection), 'test DB must have FTS5 for these tests'
        cls.category = Category.objects.create(org_id=None, name='Guides')
        cls.body_hit = _make_search_article(
            cls.category, 'Account settings',
            body='Open the page and look for the gadget panel near the top.',
        )
        cls.title_hit = _make_search_article(cls.category, 'Gadget basics')
        cls.summary_hit = _make_search_article(
            cls.category, 'Getting around', summary='Covers the gadget in brief.',
        )
        # Unrelated rows so the term is not in every document (keeps
        # bm25's IDF meaningful, as in a real KB).
        for n in range(4):
            _make_search_article(cls.category, f'Unrelated topic {n}')

    def _search(self, query):
        from kb.views import _search_articles
        return list(_search_articles(Article.objects.all(), query))

    def test_title_beats_summary_beats_body(self):
        self.assertEqual(
            self._search('gadget'), [self.title_hit, self.summary_hit, self.body_hit],
        )

    def test_stemmed_variants_match(self):
        summary_only = _make_search_article(
            self.category, 'Reading reports', summary='A quick Summary of the monthly report.',
        )
        configure_only = _make_search_article(
            self.category, 'Printers', body='How to configure a printer.',
        )
        self.assertEqual(self._search('summaries'), [summary_only])
        self.assertEqual(self._search('configuring'), [configure_only])

    def test_body_only_term_matches(self):
        self.assertEqual(self._search('panel'), [self.body_hit])

    def test_multi_word_query_requires_every_word(self):
        self.assertEqual(self._search('gadget panel'), [self.body_hit])
        self.assertEqual(self._search('gadget nonexistentword'), [])

    def test_fts_syntax_in_user_input_is_neutralised(self):
        for query in ['"', '*', 'AND', 'NEAR(', 'title:foo', '-', '"gadget', 'gadget*',
                      'gadget AND', 'OR gadget', 'gadget NOT panel', '(gadget', '^gadget']:
            with self.subTest(query=query):
                self._search(query)  # must not raise
        # Column filters are not honoured: "title:gadget" is the two words
        # "title" and "gadget", ANDed, and no article has both.
        self.assertEqual(self._search('title:gadget'), [])
        # "NOT" is a plain word too, not an operator that would drop the
        # body hit; no article contains the word "not", so nothing matches.
        self.assertEqual(self._search('gadget NOT panel'), [])
        self.assertIn(self.title_hit, self._search('gadget*'))

    def test_input_with_no_word_tokens_returns_nothing(self):
        for query in ['!!!', '"', '*', '-', '   ', '']:
            with self.subTest(query=query):
                self.assertEqual(self._search(query), [])

    def test_editing_an_article_updates_the_index(self):
        self.title_hit.title = 'Widget basics'
        self.title_hit.save()
        self.assertNotIn(self.title_hit, self._search('gadget'))
        self.assertEqual(self._search('widget'), [self.title_hit])

    def test_deleting_an_article_removes_it_from_the_index(self):
        from django.db import connection
        article_id = self.title_hit.id
        self.title_hit.delete()
        with connection.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM kb_articleindex WHERE article_id = %s', [article_id])
            self.assertEqual(cursor.fetchone()[0], 0)
        self.assertEqual(self._search('gadget'), [self.summary_hit, self.body_hit])

    def test_rebuild_command_repopulates_an_emptied_index(self):
        from django.core.management import call_command
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM kb_articleindex')
        self.assertEqual(self._search('gadget'), [])
        out = io.StringIO()
        call_command('rebuild_search_index', stdout=out)
        self.assertIn(f'Indexed {Article.objects.count()} article', out.getvalue())
        self.assertEqual(
            self._search('gadget'), [self.title_hit, self.summary_hit, self.body_hit],
        )

    def test_fallback_to_icontains_when_fts5_unavailable(self):
        from unittest import mock
        # "gadg" is a substring, not a word: FTS5 does not match it,
        # icontains does -- which proves which path ran.
        self.assertEqual(self._search('gadg'), [])
        with mock.patch('kb.search_index.fts5_available', return_value=False):
            self.assertCountEqual(
                self._search('gadg'), [self.title_hit, self.summary_hit, self.body_hit],
            )


class Fts5SearchVisibilityTests(TestCase):
    """The FTS5 index holds every article, published or not; the view's
    visibility filters must still decide what a visitor sees."""

    @classmethod
    def setUpTestData(cls):
        public = Category.objects.create(
            org_id=None, name='Billing', visibility=Category.VISIBILITY_PUBLIC,
        )
        members = Category.objects.create(
            org_id=None, name='Internal Runbooks', visibility=Category.VISIBILITY_MEMBERS,
        )
        _make_search_article(public, 'Public zebra guide')
        _make_search_article(public, 'Draft zebra guide', status=Article.STATUS_DRAFT)
        _make_search_article(members, 'Members zebra runbook')
        User = get_user_model()
        cls.member = User.objects.create_user('member@example.com', 'member@example.com', 'pw')
        organisation = Organisation.objects.create(name='Acme', slug='acme')
        Membership.objects.create(user=cls.member, organisation=organisation)

    def _results_html(self):
        return self.client.get(reverse('kb:article-list'), {'q': 'zebra'}).content.decode()

    def test_anonymous_visitor_sees_only_public_published_matches(self):
        html = self._results_html()
        self.assertIn('Public zebra guide', html)
        self.assertNotIn('Draft zebra guide', html)
        self.assertNotIn('Members zebra runbook', html)

    def test_member_also_sees_members_only_matches_but_not_drafts(self):
        self.client.force_login(self.member)
        html = self._results_html()
        self.assertIn('Public zebra guide', html)
        self.assertIn('Members zebra runbook', html)
        self.assertNotIn('Draft zebra guide', html)
