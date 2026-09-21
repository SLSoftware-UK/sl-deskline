"""
Tests for the SiteSettings singleton: the model/admin contract in
isolation, plus the integration points a self-hoster actually cares
about — that changing a field here really does change the rendered page
and the outbound email, not just a database row nobody reads.
"""
import io

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from PIL import Image

from tickets.models import Ticket
from tickets.notifications import notify_staff_new_ticket, notify_ticket_received

from .models import SiteSettings

User = get_user_model()


def _png_bytes():
    buffer = io.BytesIO()
    Image.new('RGB', (4, 4), (124, 58, 237)).save(buffer, format='PNG')
    return buffer.getvalue()


class LoadTests(TestCase):
    def test_load_creates_exactly_one_row(self):
        self.assertEqual(SiteSettings.objects.count(), 0)
        obj = SiteSettings.load()
        self.assertEqual(obj.pk, 1)
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_load_is_idempotent(self):
        first = SiteSettings.load()
        second = SiteSettings.load()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_second_save_does_not_create_another_row(self):
        obj = SiteSettings.load()
        obj.site_name = 'Acme Support'
        obj.save()
        # A second, distinct in-memory instance -- not obj.save() again --
        # is the case that would actually reveal a broken pk override.
        other = SiteSettings(site_name='Rogue Row')
        other.save()
        self.assertEqual(SiteSettings.objects.count(), 1)
        self.assertEqual(SiteSettings.objects.get().site_name, 'Rogue Row')

    def test_load_defaults_match_the_css_that_shipped_before_this_model(self):
        obj = SiteSettings.load()
        self.assertEqual(obj.site_name, 'SL Deskline')
        self.assertEqual(obj.accent_color, '#7c3aed')

    def test_str_is_site_name(self):
        obj = SiteSettings.load()
        obj.site_name = 'Acme Support'
        self.assertEqual(str(obj), 'Acme Support')


class AccentColourValidationTests(TestCase):
    def test_valid_hex_colour_passes(self):
        obj = SiteSettings(accent_color='#123abc')
        obj.full_clean()  # should not raise

    def test_invalid_hex_colour_is_rejected(self):
        obj = SiteSettings(accent_color='purple')
        with self.assertRaises(ValidationError):
            obj.full_clean()

    def test_short_hex_colour_is_rejected(self):
        obj = SiteSettings(accent_color='#fff')
        with self.assertRaises(ValidationError):
            obj.full_clean()


class AdminSingletonTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser('admin', 'admin@example.com', 'pw')
        self.client.force_login(self.admin_user)

    def test_changelist_redirects_to_the_pk1_change_form(self):
        response = self.client.get(reverse('admin:branding_sitesettings_changelist'))
        self.assertRedirects(response, reverse('admin:branding_sitesettings_change', args=[1]))
        # The redirect itself must have created the row via load() -- an
        # operator's very first visit to this admin page is exactly the
        # "fresh database" case load() exists for.
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_changelist_denies_a_staff_user_with_no_view_permission_rather_than_redirecting(self):
        self.client.logout()
        agent = User.objects.create_user('agent', is_staff=True)
        self.client.force_login(agent)
        response = self.client.get(reverse('admin:branding_sitesettings_changelist'))
        self.assertEqual(response.status_code, 403)
        # An unauthorised probe of this URL must not have the create-on-
        # first-visit side effect that a genuine operator's visit does.
        self.assertEqual(SiteSettings.objects.count(), 0)

    def test_no_add_permission_once_a_row_exists(self):
        SiteSettings.load()
        response = self.client.get(reverse('admin:branding_sitesettings_add'))
        self.assertEqual(response.status_code, 403)

    def test_no_delete_permission(self):
        obj = SiteSettings.load()
        response = self.client.get(reverse('admin:branding_sitesettings_delete', args=[obj.pk]))
        self.assertEqual(response.status_code, 403)

    def test_editing_the_row_persists(self):
        obj = SiteSettings.load()
        response = self.client.post(
            reverse('admin:branding_sitesettings_change', args=[obj.pk]),
            {
                'site_name': 'Acme Support',
                'accent_color': '#123abc',
                'support_email': 'help@acme.example',
                'notification_from_email': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        obj.refresh_from_db()
        self.assertEqual(obj.site_name, 'Acme Support')
        self.assertEqual(obj.accent_color, '#123abc')


class ContextProcessorTests(TestCase):
    """The context processor feeds kb/templates/kb/base.html, which
    every page in the service extends (tickets included) -- so the KB
    home page is enough to exercise it without pulling in tickets'
    login-required views."""

    def test_default_site_name_appears_in_the_title(self):
        response = self.client.get(reverse('kb:article-list'))
        self.assertContains(response, '<title>SL Deskline')

    def test_changing_site_name_changes_the_page_title(self):
        # Asserts the title/header/og:site_name; the hero and footer
        # also render site_name but are covered by their own markup.
        obj = SiteSettings.load()
        obj.site_name = 'Acme Support'
        obj.save()
        response = self.client.get(reverse('kb:article-list'))
        self.assertContains(response, '<title>Acme Support')
        self.assertContains(response, '<a class="brand" href="/">Acme Support</a>')
        self.assertContains(response, '<meta property="og:site_name" content="Acme Support">')

    def test_og_site_name_follows_site_name(self):
        obj = SiteSettings.load()
        obj.site_name = 'Acme Support'
        obj.save()
        response = self.client.get(reverse('kb:article-list'))
        self.assertContains(response, '<meta property="og:site_name" content="Acme Support">')

    def test_header_shows_site_name_when_no_logo(self):
        obj = SiteSettings.load()
        obj.site_name = 'Acme Support'
        obj.save()
        response = self.client.get(reverse('kb:article-list'))
        self.assertContains(response, '<a class="brand" href="/">Acme Support</a>')

    def test_header_shows_logo_when_set(self):
        obj = SiteSettings.load()
        obj.logo = SimpleUploadedFile('logo.png', _png_bytes(), content_type='image/png')
        obj.save()
        response = self.client.get(reverse('kb:article-list'))
        self.assertContains(response, obj.logo.url)
        self.assertContains(response, '<img')

    def test_accent_colour_is_written_as_a_css_custom_property(self):
        obj = SiteSettings.load()
        obj.accent_color = '#123abc'
        obj.save()
        response = self.client.get(reverse('kb:article-list'))
        self.assertContains(response, '--accent: #123abc')

    def test_support_email_shown_in_footer_when_set(self):
        obj = SiteSettings.load()
        obj.support_email = 'help@acme.example'
        obj.save()
        response = self.client.get(reverse('kb:article-list'))
        self.assertContains(response, 'help@acme.example')

    def test_support_email_absent_from_footer_when_blank(self):
        response = self.client.get(reverse('kb:article-list'))
        self.assertNotContains(response, 'mailto:')


class EmailBrandingTests(TestCase):
    """tickets/notifications.py builds emails with render_to_string(),
    which never runs context processors -- these confirm site_settings
    genuinely reaches the templates via the explicit context, not by
    accident."""

    def setUp(self):
        self.customer = User.objects.create_user('customer', 'customer@example.com', 'pw')
        self.ticket = Ticket.objects.create(subject='Cannot log in', raised_by=self.customer)

    def test_default_site_name_in_subject_and_body(self):
        notify_ticket_received(self.ticket, 'Help please.')
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertIn('SL Deskline', sent.subject)
        self.assertIn('SL Deskline', sent.body)
        self.assertIn('SL Deskline', sent.alternatives[0][0])

    def test_changed_site_name_in_subject_and_body(self):
        obj = SiteSettings.load()
        obj.site_name = 'Acme Support'
        obj.save()
        notify_ticket_received(self.ticket, 'Help please.')
        sent = mail.outbox[0]
        self.assertIn('Acme Support', sent.subject)
        self.assertIn('Acme Support', sent.body)
        self.assertNotIn('SL Deskline', sent.subject)

    def test_support_email_shown_in_email_footer_when_set(self):
        obj = SiteSettings.load()
        obj.support_email = 'help@acme.example'
        obj.save()
        notify_ticket_received(self.ticket, 'Help please.')
        sent = mail.outbox[0]
        self.assertIn('help@acme.example', sent.body)

    def test_support_email_absent_from_email_footer_when_blank(self):
        notify_ticket_received(self.ticket, 'Help please.')
        sent = mail.outbox[0]
        self.assertNotIn('mailto:', sent.alternatives[0][0])

    @override_settings(DEFAULT_FROM_EMAIL='fallback@example.com')
    def test_from_email_falls_back_to_default_from_email_when_blank(self):
        notify_ticket_received(self.ticket, 'Help please.')
        self.assertEqual(mail.outbox[0].from_email, 'fallback@example.com')

    @override_settings(DEFAULT_FROM_EMAIL='fallback@example.com')
    def test_from_email_uses_notification_from_email_when_set(self):
        obj = SiteSettings.load()
        obj.notification_from_email = 'tickets@acme.example'
        obj.save()
        notify_ticket_received(self.ticket, 'Help please.')
        self.assertEqual(mail.outbox[0].from_email, 'tickets@acme.example')


def _count_site_settings_queries(captured_queries):
    """How many of `captured_queries` (a CaptureQueriesContext's
    `.captured_queries`) touched the branding_sitesettings table -- used
    to pin "one SiteSettings query per request/notification", which a
    correct render/send can't tell apart from a duplicated-but-harmless
    one on its own."""
    return sum(1 for q in captured_queries if 'branding_sitesettings' in q['sql'])


class QueryCountTests(TestCase):
    """Pins a review finding: several call sites ask for SiteSettings on
    the same request or notification send -- a view building its own
    meta_title, then branding's context processor again once the
    template renders; a notify_* function building its subject, then
    _ticket_context again per staff recipient in the fan-out functions.
    branding/utils.py's get_site_settings() (request-memoised) and the
    explicit site_settings parameter threaded through
    tickets/notifications.py exist to make that one query rather than
    several. Counting queries against just this one table, rather than
    `assertNumQueries` around the whole page/send, is what actually pins
    the fix without going brittle every time an unrelated query is added
    elsewhere in the request."""

    def test_one_query_per_kb_page_render(self):
        # Pre-create the row: SiteSettings.load()'s own get_or_create()
        # is a SELECT *and* an INSERT the very first time it is ever
        # called, which would swamp the count this test exists to pin.
        # With the row already there, every load() is a single SELECT,
        # so the number of queries directly reflects how many times it
        # was actually called.
        SiteSettings.load()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('kb:article-list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_count_site_settings_queries(ctx.captured_queries), 1)

    def test_one_query_per_ticket_page_render(self):
        SiteSettings.load()
        customer = User.objects.create_user('customer2', 'customer2@example.com', 'pw')
        self.client.force_login(customer)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('tickets:list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_count_site_settings_queries(ctx.captured_queries), 1)

    def test_one_query_per_staff_fan_out_notification_send(self):
        SiteSettings.load()
        customer = User.objects.create_user('customer3', 'customer3@example.com', 'pw')
        User.objects.create_user('agent1', 'agent1@example.com', is_staff=True)
        User.objects.create_user('agent2', 'agent2@example.com', is_staff=True)
        ticket = Ticket.objects.create(subject='Printer jam', raised_by=customer)
        with CaptureQueriesContext(connection) as ctx:
            notify_staff_new_ticket(ticket)
        # Two unassigned agents -> the fan-out actually sends to both,
        # which is what would have turned into an N+1 without the fix.
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(_count_site_settings_queries(ctx.captured_queries), 1)
