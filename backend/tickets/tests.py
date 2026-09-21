"""
Ticket behaviour: who can see which ticket, who can raise and change
them, and which notifications fire.

Most classes patch the `notify_*` names as they are bound inside
tickets.views, so a test can assert *which* notification fired without
rendering or sending anything. `NotificationTests` is the deliberate
exception: it lets the real functions run against the locmem email
backend and asserts on `django.core.mail.outbox`, which is the only way
to prove the templates render and the link shape is right.

Terminology, because this database holds two unrelated kinds of "staff"
(see accounts/models.py): `agent` below is a support agent
(`is_staff`, no memberships, works tickets); `customer`/`colleague`/
`outsider` are this desk's customers, each a member of an organisation.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Membership, Organisation
from kb.models import Article, Category

from .models import Ticket, TicketMessage

User = get_user_model()

NOTIFY = [
    'notify_ticket_received', 'notify_staff_new_ticket', 'notify_customer_reply',
    'notify_staff_new_message', 'notify_assigned', 'notify_resolved',
]


class TicketTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organisation.objects.create(name='Riverside Dental', slug='riverside')
        cls.other_org = Organisation.objects.create(name='Hilltop Accounting', slug='hilltop')

        cls.customer = User.objects.create_user('owner@riverside.example', email='owner@riverside.example', first_name='Cara')
        cls.colleague = User.objects.create_user('staff@riverside.example', email='staff@riverside.example')
        cls.outsider = User.objects.create_user('owner@hilltop.example', email='owner@hilltop.example')
        # Support agents: is_staff, and deliberately no memberships —
        # their access comes from the flag, never from an organisation.
        cls.agent = User.objects.create_user('agent', email='support@example.com', is_staff=True)
        cls.agent2 = User.objects.create_user('jay', email='jay@example.com', is_staff=True)

        Membership.objects.create(user=cls.customer, organisation=cls.org, role=Membership.Role.OWNER)
        Membership.objects.create(user=cls.colleague, organisation=cls.org)
        Membership.objects.create(user=cls.outsider, organisation=cls.other_org)

        cls.colleague_ticket = cls._ticket(cls.colleague, cls.org, 'Printer jam', urgency='high')
        cls.outsider_ticket = cls._ticket(cls.outsider, cls.other_org, 'Other organisation issue', urgency='low')
        cls.resolved_ticket = cls._ticket(cls.customer, cls.org, 'Old problem', status=Ticket.Status.RESOLVED)

    @staticmethod
    def _ticket(user, organisation, subject, **kwargs):
        ticket = Ticket.objects.create(raised_by=user, organisation=organisation, subject=subject, **kwargs)
        TicketMessage.objects.create(ticket=ticket, author=user, body=f'{subject} details')
        return ticket

    def setUp(self):
        self.notify = {}
        for name in NOTIFY:
            patcher = mock.patch(f'tickets.views.{name}')
            self.notify[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def notified(self):
        return {name for name, m in self.notify.items() if m.called}


class VisibilityTests(TicketTestCase):
    def test_anonymous_is_sent_to_the_login_page(self):
        """Nothing under /tickets/ is reachable signed out —
        settings.LOGIN_URL is accounts:login, i.e. /login/."""
        response = self.client.get('/tickets/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login/?next=/tickets/'))

    def test_customer_sees_the_whole_organisation_but_not_another_one(self):
        self.client.force_login(self.customer)
        response = self.client.get('/tickets/')
        self.assertContains(response, 'Printer jam')          # a colleague's, same organisation
        self.assertNotContains(response, 'Other organisation issue')
        self.assertNotContains(response, 'Old problem')       # resolved is its own screen
        self.assertContains(response, 'noindex, nofollow')

    def test_a_member_of_two_organisations_sees_both(self):
        """The multi-org case: one login, two memberships, and the list
        is the union of both organisations' tickets."""
        both = User.objects.create_user('consultant@example.com', email='consultant@example.com')
        Membership.objects.create(user=both, organisation=self.org)
        Membership.objects.create(user=both, organisation=self.other_org)
        self.client.force_login(both)
        subjects = {t.subject for t in self.client.get('/tickets/').context['tickets']}
        self.assertEqual(subjects, {'Printer jam', 'Other organisation issue'})
        self.assertEqual(self.client.get(f'/tickets/{self.outsider_ticket.pk}/').status_code, 200)

    def test_raised_by_and_member_of_overlapping_does_not_duplicate_rows(self):
        """Raised-by and member-of both match a customer's own ticket in
        their own organisation; the visibility filter must not return
        that row once per membership of the organisation, which a plain
        `Q(raised_by=...) | Q(organisation__memberships__user=...)` JOIN
        would (the organisation here has two members).

        The list alone would not catch it: its `Count('messages')`
        annotation adds a GROUP BY that folds the duplicate rows back
        together, and the damage shows up instead as a doubled message
        count. The detail view has no annotation, so there a duplicate
        is a MultipleObjectsReturned from get_object_or_404 -- a 500."""
        self.client.force_login(self.customer)
        response = self.client.get('/tickets/resolved/')
        self.assertEqual(
            [(t.subject, t.message_count) for t in response.context['tickets']], [('Old problem', 1)],
        )
        self.assertEqual(self.client.get(f'/tickets/{self.resolved_ticket.pk}/').status_code, 200)

    def test_an_organisation_less_ticket_is_visible_to_its_raiser_only(self):
        loner = User.objects.create_user('loner@example.com', email='loner@example.com')
        mine = self._ticket(loner, None, 'Personal account issue')

        self.client.force_login(loner)
        self.assertContains(self.client.get('/tickets/'), 'Personal account issue')
        self.assertEqual(self.client.get(f'/tickets/{mine.pk}/').status_code, 200)

        # A customer who happens to share no organisation with the raiser
        # -- which, for a ticket with no organisation, is everyone.
        self.client.force_login(self.customer)
        self.assertNotContains(self.client.get('/tickets/'), 'Personal account issue')
        self.assertEqual(self.client.get(f'/tickets/{mine.pk}/').status_code, 404)

        self.client.force_login(self.agent)
        self.assertContains(self.client.get('/tickets/'), 'Personal account issue')

    def test_leaving_an_organisation_keeps_your_own_tickets_but_not_theirs(self):
        """Visibility is recomputed from memberships on every request, so
        removing someone from an organisation in Admin (or at their next
        SSO sync) cuts them off from its tickets at once -- except the
        ones they raised themselves, which stay theirs."""
        own = self._ticket(self.customer, self.org, 'Raised while a member')
        Membership.objects.filter(user=self.customer).delete()

        self.client.force_login(self.customer)
        subjects = {t.subject for t in self.client.get('/tickets/').context['tickets']}
        self.assertEqual(subjects, {'Raised while a member'})
        self.assertEqual(self.client.get(f'/tickets/{own.pk}/').status_code, 200)
        self.assertEqual(self.client.get(f'/tickets/{self.colleague_ticket.pk}/').status_code, 404)

    def test_customer_cannot_open_another_organisations_ticket(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(f'/tickets/{self.outsider_ticket.pk}/').status_code, 404)
        response = self.client.post(f'/tickets/{self.outsider_ticket.pk}/messages/', {'body': 'hi'})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.notified())

    def test_customer_cannot_open_another_organisations_ticket_via_htmx(self):
        """The same two 404s with an HX-Request header on.

        Coverage rather than a live hole: in both views the
        `get_object_or_404(_visible_tickets(...))` line runs before
        anything looks at `request.htmx`, so the htmx path cannot
        diverge without the full-page one diverging too. Pinned anyway,
        because the obvious future refactor here is to give the htmx
        branch its own early return, and that is exactly the edit that
        would move a gate below it.
        """
        self.client.force_login(self.customer)
        self.assertEqual(
            self.client.get(
                f'/tickets/{self.outsider_ticket.pk}/', HTTP_HX_REQUEST='true',
            ).status_code,
            404,
        )
        response = self.client.post(
            f'/tickets/{self.outsider_ticket.pk}/messages/', {'body': 'hi'}, HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.outsider_ticket.messages.count(), 1)
        self.assertFalse(self.notified())

    def test_customer_can_open_a_colleagues_ticket(self):
        self.client.force_login(self.customer)
        response = self.client.get(f'/tickets/{self.colleague_ticket.pk}/')
        self.assertContains(response, 'Printer jam details')
        self.assertNotContains(response, 'staff-controls')

    def test_customer_without_a_membership_sees_an_empty_list(self):
        loner = User.objects.create_user('no-org@example.com')
        self.client.force_login(loner)
        response = self.client.get('/tickets/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No tickets yet.')

    def test_customer_without_a_membership_cannot_reach_an_organisations_ticket(self):
        loner = User.objects.create_user('no-org-2@example.com')
        self.client.force_login(loner)
        self.assertEqual(self.client.get(f'/tickets/{self.colleague_ticket.pk}/').status_code, 404)

    def test_agent_sees_every_organisation(self):
        self.client.force_login(self.agent)
        response = self.client.get('/tickets/')
        self.assertContains(response, 'Printer jam')
        self.assertContains(response, 'Other organisation issue')
        self.assertContains(response, 'Hilltop Accounting')  # organisation column is agent-only
        detail = self.client.get(f'/tickets/{self.outsider_ticket.pk}/')
        self.assertContains(detail, 'staff-controls')

    def test_organisation_column_is_hidden_from_customers(self):
        self.client.force_login(self.customer)
        response = self.client.get('/tickets/')
        self.assertEqual([c['key'] for c in response.context['columns']],
                         ['subject', 'urgency', 'updated_at'])
        # Scoped to the table, not the whole page: kb/base.html's header
        # auth block legitimately prints the signed-in customer's
        # organisation name next to their own, so a page-wide
        # assertNotContains would fail against correct markup.
        table = response.content.decode().split('<table class="ticket-table">')[1]
        self.assertNotIn('Riverside Dental', table)

    def test_ticket_detail_shows_the_organisation_or_a_dash(self):
        self.client.force_login(self.agent)
        self.assertContains(
            self.client.get(f'/tickets/{self.colleague_ticket.pk}/'),
            '<div><dt>Organisation</dt><dd>Riverside Dental</dd></div>', html=True,
        )
        orphan = self._ticket(self.customer, None, 'No organisation')
        self.assertContains(
            self.client.get(f'/tickets/{orphan.pk}/'),
            '<div><dt>Organisation</dt><dd>—</dd></div>', html=True,
        )

    def test_resolved_has_its_own_screen(self):
        self.client.force_login(self.customer)
        response = self.client.get('/tickets/resolved/')
        self.assertContains(response, 'Old problem')
        self.assertNotContains(response, 'Printer jam')

    def test_urgency_ordering_is_by_severity_not_alphabet(self):
        """Alphabetically 'high' < 'low' < 'normal'; the _URGENCY_RANK
        annotation is what makes ascending mean low -> normal -> high."""
        self._ticket(self.customer, self.org, 'Middling', urgency='normal')
        self.client.force_login(self.agent)
        response = self.client.get('/tickets/', {'ordering': 'urgency'})
        self.assertEqual(
            [t.subject for t in response.context['tickets']],
            ['Other organisation issue', 'Middling', 'Printer jam'],
        )
        response = self.client.get('/tickets/', {'ordering': '-urgency'})
        self.assertEqual(
            [t.subject for t in response.context['tickets']],
            ['Printer jam', 'Middling', 'Other organisation issue'],
        )

    def test_status_filter_is_applied(self):
        self._ticket(self.customer, self.org, 'Waiting one', status=Ticket.Status.WAITING_ON_CUSTOMER)
        self.client.force_login(self.agent)
        response = self.client.get('/tickets/', {'status': 'waiting_on_customer'})
        self.assertEqual([t.subject for t in response.context['tickets']], ['Waiting one'])

    def test_unknown_ordering_and_status_fall_back_to_defaults(self):
        """The ?ordering= allowlist exists so a client cannot sort (and
        therefore probe) by an arbitrary field; ?status=resolved on the
        open list belongs on the resolved screen, not here."""
        self.client.force_login(self.agent)
        response = self.client.get('/tickets/', {'ordering': 'raised_by__password', 'status': 'resolved'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['ordering'], '-updated_at')
        self.assertNotIn('Old problem', [t.subject for t in response.context['tickets']])

    def test_ordering_by_organisation_is_allowed_and_sorts_by_name(self):
        self.client.force_login(self.agent)
        response = self.client.get('/tickets/', {'ordering': 'organisation'})
        self.assertEqual(response.context['ordering'], 'organisation')
        self.assertEqual(
            [t.subject for t in response.context['tickets']],
            ['Other organisation issue', 'Printer jam'],  # Hilltop before Riverside
        )


class CreateTests(TicketTestCase):
    def test_a_single_membership_files_under_that_organisation_with_a_first_message(self):
        self.client.force_login(self.customer)
        # Exactly one organisation: nothing to choose, so no field.
        form_page = self.client.get('/tickets/new/')
        self.assertNotIn('organisation', form_page.context['form'].fields)
        self.assertNotContains(form_page, 'name="organisation"')

        response = self.client.post('/tickets/new/', {
            'subject': 'Bookings not syncing', 'body': 'Nothing arrives.', 'urgency': 'high',
        })
        ticket = Ticket.objects.get(subject='Bookings not syncing')
        self.assertRedirects(response, f'/tickets/{ticket.pk}/', fetch_redirect_response=False)
        self.assertEqual(ticket.organisation, self.org)
        self.assertEqual(ticket.raised_by, self.customer)
        self.assertEqual(ticket.urgency, 'high')
        self.assertEqual(ticket.status, Ticket.Status.OPEN)
        self.assertEqual(list(ticket.messages.values_list('body', flat=True)), ['Nothing arrives.'])
        self.assertEqual(self.notified(), {'notify_ticket_received', 'notify_staff_new_ticket'})
        self.notify['notify_ticket_received'].assert_called_once_with(ticket, 'Nothing arrives.')

    def test_agent_account_gets_a_403(self):
        """Ticket creation is customer-only: the desk is for customers
        to report problems to agents, so an agent raising one would put a
        ticket in the queue that no customer can see."""
        self.client.force_login(self.agent)
        get = self.client.get('/tickets/new/')
        self.assertEqual(get.status_code, 403)
        self.assertContains(get, 'agent accounts cannot raise tickets', status_code=403)
        response = self.client.post('/tickets/new/', {
            'subject': 'Should be rejected', 'body': 'x', 'urgency': 'normal',
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Ticket.objects.filter(subject='Should be rejected').exists())
        self.assertFalse(self.notified())

    def test_customer_without_a_membership_raises_an_organisation_less_ticket(self):
        """Zero memberships is a normal state, not an error: an
        individual customer of a self-hosted desk belongs to no
        organisation and still needs to report problems."""
        loner = User.objects.create_user('no-org-3@example.com', email='no-org-3@example.com')
        self.client.force_login(loner)
        form_page = self.client.get('/tickets/new/')
        self.assertEqual(form_page.status_code, 200)
        self.assertNotIn('organisation', form_page.context['form'].fields)

        response = self.client.post('/tickets/new/', {'subject': 'x', 'body': 'y', 'urgency': 'normal'})
        ticket = Ticket.objects.get(subject='x')
        self.assertRedirects(response, f'/tickets/{ticket.pk}/', fetch_redirect_response=False)
        self.assertIsNone(ticket.organisation)
        self.assertEqual(ticket.raised_by, loner)
        self.assertEqual(self.notified(), {'notify_ticket_received', 'notify_staff_new_ticket'})

    def test_a_multi_member_must_choose_among_their_own_organisations(self):
        both = User.objects.create_user('consultant@example.com', email='consultant@example.com')
        Membership.objects.create(user=both, organisation=self.org)
        Membership.objects.create(user=both, organisation=self.other_org)
        stranger_org = Organisation.objects.create(name='Stranger Ltd', slug='stranger')
        self.client.force_login(both)

        form_page = self.client.get('/tickets/new/')
        field = form_page.context['form'].fields['organisation']
        self.assertTrue(field.required)
        self.assertEqual(set(field.queryset), {self.org, self.other_org})
        self.assertContains(form_page, 'name="organisation"')
        self.assertNotContains(form_page, 'Stranger Ltd')

        # Not choosing is an error, not a silent pick of the first one.
        response = self.client.post('/tickets/new/', {'subject': 'Unfiled', 'body': 'x', 'urgency': 'normal'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'field-error')
        self.assertFalse(Ticket.objects.filter(subject='Unfiled').exists())

        response = self.client.post('/tickets/new/', {
            'subject': 'For Hilltop', 'body': 'x', 'urgency': 'normal', 'organisation': self.other_org.pk,
        })
        ticket = Ticket.objects.get(subject='For Hilltop')
        self.assertRedirects(response, f'/tickets/{ticket.pk}/', fetch_redirect_response=False)
        self.assertEqual(ticket.organisation, self.other_org)

    def test_a_posted_organisation_that_is_not_yours_is_rejected(self):
        """The choice is validated server-side against the user's own
        memberships -- the select only offering their organisations is
        presentation, not a control. A crafted POST naming someone
        else's organisation would otherwise file a ticket that the
        other organisation's members could then read."""
        both = User.objects.create_user('consultant@example.com', email='consultant@example.com')
        Membership.objects.create(user=both, organisation=self.org)
        Membership.objects.create(user=both, organisation=self.other_org)
        stranger_org = Organisation.objects.create(name='Stranger Ltd', slug='stranger')
        self.client.force_login(both)

        response = self.client.post('/tickets/new/', {
            'subject': 'Sneaky', 'body': 'x', 'urgency': 'normal', 'organisation': stranger_org.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.filter(subject='Sneaky').exists())
        self.assertFalse(self.notified())

    def test_a_single_member_cannot_redirect_a_ticket_by_posting_an_organisation(self):
        """With one membership there is no field at all, so a posted
        `organisation` is ignored and the ticket still goes to the
        member's own organisation."""
        self.client.force_login(self.customer)
        self.client.post('/tickets/new/', {
            'subject': 'Posted elsewhere', 'body': 'x', 'urgency': 'normal', 'organisation': self.other_org.pk,
        })
        self.assertEqual(Ticket.objects.get(subject='Posted elsewhere').organisation, self.org)

    def test_create_from_an_article_records_the_linked_article(self):
        article = self._published_article('Setting up classes', 'setting-up-classes')
        self.client.force_login(self.customer)
        form_page = self.client.get(f'/tickets/new/?article={article.pk}')
        self.assertContains(form_page, 'Setting up classes')
        self.client.post('/tickets/new/', {
            'subject': 'Still stuck', 'body': 'Read it.', 'urgency': 'normal', 'article': article.pk,
        })
        ticket = Ticket.objects.get(subject='Still stuck')
        self.assertEqual(ticket.linked_article_id, article.pk)
        detail = self.client.get(f'/tickets/{ticket.pk}/')
        self.assertContains(detail, f'/articles/{article.slug}/')

    def test_unpublished_or_bogus_article_is_dropped(self):
        draft = Article.objects.create(
            title='Draft guide', category=Category.objects.create(org_id=None, name='Billing'),
            summary='s', status=Article.STATUS_DRAFT, author_user_id=1, author_display_name='Alex Author',
        )
        self.client.force_login(self.customer)
        self.client.post('/tickets/new/', {
            'subject': 'Bogus', 'body': 'x', 'urgency': 'normal', 'article': '99999',
        })
        self.assertIsNone(Ticket.objects.get(subject='Bogus').linked_article_id)
        self.client.post('/tickets/new/', {
            'subject': 'Drafty', 'body': 'x', 'urgency': 'normal', 'article': draft.pk,
        })
        self.assertIsNone(Ticket.objects.get(subject='Drafty').linked_article_id)

    def test_the_form_carries_the_article_through_a_failed_submit(self):
        """The hidden field is the whole reason `article` is read from
        POST as well as GET: a validation error re-renders the form at
        /tickets/new/ with no query string, so without it the link back
        to the article would be lost on the second attempt."""
        article = self._published_article('Booking rules', 'booking-rules')
        self.client.force_login(self.customer)
        response = self.client.post('/tickets/new/', {
            'subject': '', 'body': '', 'urgency': 'normal', 'article': article.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<input type="hidden" name="article" value="{article.pk}">')
        self.assertContains(response, 'Booking rules')

    def test_a_non_numeric_article_is_dropped_rather_than_erroring_the_form(self):
        """Separate from the bogus-id case because it takes a different
        branch: `.isdecimal()` keeps a value like 'abc' out of the int()
        entirely. Either way the ticket is still created - someone
        reporting a problem should not be told their URL is malformed."""
        self.client.force_login(self.customer)
        response = self.client.post('/tickets/new/', {
            'subject': 'Wonky link', 'body': 'x', 'urgency': 'normal', 'article': 'abc',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Ticket.objects.get(subject='Wonky link').linked_article_id)

    def test_a_superscript_digit_article_is_dropped_rather_than_500ing(self):
        """The guard used to be `.isdigit()`, which is True for digit
        characters int() will not parse - '²' (superscript two) being
        the everyday one, since it is a single keystroke on several
        European keyboard layouts and survives a copy-paste out of a
        rendered document. int('²') raises ValueError, so the view
        500ed on exactly the input the surrounding comment promises to
        drop in silence. `.isdecimal()` is the set int() accepts.

        Asserted on both the GET (the form must render) and the POST
        (the ticket must still be created, unlinked), because the
        parameter is read on both.
        """
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get('/tickets/new/?article=²').status_code, 200)
        response = self.client.post('/tickets/new/', {
            'subject': 'Superscript link', 'body': 'x', 'urgency': 'normal', 'article': '²',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Ticket.objects.get(subject='Superscript link').linked_article_id)

    def test_deleting_the_linked_article_does_not_break_the_ticket(self):
        """linked_article_id is a PositiveIntegerField, deliberately not
        an FK to kb.Article (the two apps share no FKs, so neither's
        migrations depend on the other's and deleting an article can
        never cascade into the support desk). The cost of that is a
        dangling id, which views.py::_linked_article has to absorb."""
        article = self._published_article('Doomed guide', 'doomed-guide')
        self.client.force_login(self.customer)
        self.client.post('/tickets/new/', {
            'subject': 'About the guide', 'body': 'x', 'urgency': 'normal', 'article': article.pk,
        })
        ticket = Ticket.objects.get(subject='About the guide')
        article_pk = article.pk
        article.delete()

        ticket.refresh_from_db()
        self.assertEqual(ticket.linked_article_id, article_pk)   # not nulled: no FK, no cascade
        response = self.client.get(f'/tickets/{ticket.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<dt>Article</dt>')

    def test_an_unpublished_linked_article_is_shown_without_a_link(self):
        """It was published when the ticket was raised; archiving it
        later must not leave a link that 404s for the customer (the KB
        detail view only serves published articles)."""
        article = self._published_article('Later archived', 'later-archived')
        self.client.force_login(self.customer)
        self.client.post('/tickets/new/', {
            'subject': 'Archived one', 'body': 'x', 'urgency': 'normal', 'article': article.pk,
        })
        ticket = Ticket.objects.get(subject='Archived one')
        Article.objects.filter(pk=article.pk).update(status=Article.STATUS_DRAFT)

        response = self.client.get(f'/tickets/{ticket.pk}/')
        self.assertContains(response, 'Later archived')
        self.assertNotContains(response, f'/articles/{article.slug}/')

    def test_invalid_form_rerenders_without_notifying(self):
        self.client.force_login(self.customer)
        response = self.client.post('/tickets/new/', {'subject': '', 'body': '', 'urgency': 'normal'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'field-error')
        self.assertFalse(self.notified())

    @staticmethod
    def _published_article(title, slug):
        return Article.objects.create(
            title=title, slug=slug, category=Category.objects.create(org_id=None, name='Classes'),
            summary='s', status=Article.STATUS_PUBLISHED, published_at=timezone.now(),
            author_user_id=1, author_display_name='Alex Author',
        )


class ReplyTests(TicketTestCase):
    def test_customer_reply_reopens_waiting_on_customer(self):
        self.colleague_ticket.status = Ticket.Status.WAITING_ON_CUSTOMER
        self.colleague_ticket.save()
        self.client.force_login(self.customer)
        response = self.client.post(f'/tickets/{self.colleague_ticket.pk}/messages/', {'body': 'Here you go'})
        self.assertRedirects(response, f'/tickets/{self.colleague_ticket.pk}/#reply', fetch_redirect_response=False)
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.status, Ticket.Status.IN_PROGRESS)
        self.assertEqual(self.notified(), {'notify_staff_new_message'})

    def test_customer_reply_leaves_every_other_status_alone(self):
        self.client.force_login(self.customer)
        self.client.post(f'/tickets/{self.resolved_ticket.pk}/messages/', {'body': 'Back again'})
        self.resolved_ticket.refresh_from_db()
        self.assertEqual(self.resolved_ticket.status, Ticket.Status.RESOLVED)

    def test_agent_reply_notifies_the_customer_and_keeps_the_status(self):
        self.colleague_ticket.status = Ticket.Status.WAITING_ON_CUSTOMER
        self.colleague_ticket.save()
        self.client.force_login(self.agent)
        self.client.post(f'/tickets/{self.colleague_ticket.pk}/messages/', {'body': 'Any update?'})
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.status, Ticket.Status.WAITING_ON_CUSTOMER)
        self.assertEqual(self.notified(), {'notify_customer_reply'})
        self.assertEqual(self.colleague_ticket.messages.last().author, self.agent)

    def test_reply_bumps_updated_at(self):
        before = self.colleague_ticket.updated_at
        self.client.force_login(self.customer)
        self.client.post(f'/tickets/{self.colleague_ticket.pk}/messages/', {'body': 'ping'})
        self.colleague_ticket.refresh_from_db()
        self.assertGreater(self.colleague_ticket.updated_at, before)

    def test_blank_reply_is_rejected(self):
        self.client.force_login(self.customer)
        response = self.client.post(f'/tickets/{self.colleague_ticket.pk}/messages/', {'body': '   '})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.colleague_ticket.messages.count(), 1)
        self.assertFalse(self.notified())

    def test_htmx_reply_returns_the_conversation_partial_with_an_oob_status(self):
        self.colleague_ticket.status = Ticket.Status.WAITING_ON_CUSTOMER
        self.colleague_ticket.save()
        self.client.force_login(self.customer)
        response = self.client.post(
            f'/tickets/{self.colleague_ticket.pk}/messages/', {'body': 'Via htmx'}, HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('<section id="conversation"', content)
        self.assertIn('Via htmx', content)
        self.assertIn('hx-swap-oob="true"', content)
        self.assertIn('In progress', content)
        self.assertNotIn('<html', content)

    def test_get_is_not_allowed(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(f'/tickets/{self.colleague_ticket.pk}/messages/').status_code, 405)


class StaffUpdateTests(TicketTestCase):
    def url(self, ticket=None):
        return f'/tickets/{(ticket or self.colleague_ticket).pk}/update/'

    def test_customer_cannot_change_status_or_assignment(self):
        self.client.force_login(self.customer)
        response = self.client.post(self.url(), {'status': 'resolved', 'assigned_to': self.agent.pk})
        self.assertEqual(response.status_code, 403)
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.status, Ticket.Status.OPEN)
        self.assertIsNone(self.colleague_ticket.assigned_to)
        self.assertFalse(self.notified())

    def test_customer_cannot_change_status_or_assignment_via_htmx(self):
        """The 403 with an HX-Request header on - same reasoning as
        VisibilityTests' htmx variant: the `if not request.user.is_staff`
        guard is the first line of ticket_update, above every
        `request.htmx` branch. The assertion that no `#ticket-meta`
        markup comes back matters as much as the status code: htmx swaps
        on a 403 only if told to, but a partial in the body would mean
        the view had rendered the controls it just refused."""
        self.client.force_login(self.customer)
        response = self.client.post(
            self.url(), {'status': 'resolved', 'assigned_to': self.agent.pk}, HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('ticket-meta', response.content.decode())
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.status, Ticket.Status.OPEN)
        self.assertIsNone(self.colleague_ticket.assigned_to)
        self.assertFalse(self.notified())

    def test_agent_resolves_once(self):
        self.client.force_login(self.agent)
        self.client.post(self.url(), {'status': 'resolved', 'assigned_to': ''})
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.status, Ticket.Status.RESOLVED)
        self.assertEqual(self.notified(), {'notify_resolved'})

        # Re-posting the form (e.g. reassigning a resolved ticket) must
        # not re-send the resolved email.
        self.notify['notify_resolved'].reset_mock()
        self.client.post(self.url(), {'status': 'resolved', 'assigned_to': self.agent2.pk})
        self.assertFalse(self.notify['notify_resolved'].called)
        self.assertTrue(self.notify['notify_assigned'].called)

    def test_assign_notifies_only_on_an_actual_change(self):
        self.client.force_login(self.agent)
        self.client.post(self.url(), {'status': 'open', 'assigned_to': self.agent.pk})
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.assigned_to, self.agent)
        self.assertEqual(self.notified(), {'notify_assigned'})

        self.notify['notify_assigned'].reset_mock()
        self.client.post(self.url(), {'status': 'in_progress', 'assigned_to': self.agent.pk})
        self.assertFalse(self.notify['notify_assigned'].called)

    def test_unassigning_works_and_notifies_the_change(self):
        """A full form post always sends `assigned_to`, so an empty
        select must still clear the assignment (and notify the
        change)."""
        self.colleague_ticket.assigned_to = self.agent
        self.colleague_ticket.save()
        self.client.force_login(self.agent)
        self.client.post(self.url(), {'status': 'open', 'assigned_to': ''})
        self.colleague_ticket.refresh_from_db()
        self.assertIsNone(self.colleague_ticket.assigned_to)

    def test_only_support_agents_are_assignable(self):
        self.client.force_login(self.agent)
        response = self.client.post(self.url(), {'status': 'open', 'assigned_to': self.customer.pk})
        self.assertEqual(response.status_code, 400)
        self.colleague_ticket.refresh_from_db()
        self.assertIsNone(self.colleague_ticket.assigned_to)

    def test_a_deactivated_assignee_is_not_silently_unassigned(self):
        """Deactivating an agent must not quietly drop their tickets.

        The assignable queryset is `is_staff=True, is_active=True`. Without
        the `| Q(pk=self.instance.assigned_to_id)` half, a deactivated
        assignee's id is not in the queryset, so the bound select
        renders with nothing selected - i.e. "Unassigned" - and the next
        status change an agent makes posts that empty value and clears
        the assignment without anyone having touched the field. Both
        halves are asserted: what the page shows, and what posting what
        it shows does."""
        self.colleague_ticket.assigned_to = self.agent2
        self.colleague_ticket.save()
        self.agent2.is_active = False
        self.agent2.save()

        self.client.force_login(self.agent)
        detail = self.client.get(f'/tickets/{self.colleague_ticket.pk}/').content.decode()
        self.assertIn(f'value="{self.agent2.pk}" selected', detail)

        self.client.post(self.url(), {'status': 'in_progress', 'assigned_to': self.agent2.pk})
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.assigned_to, self.agent2)
        self.assertEqual(self.colleague_ticket.status, Ticket.Status.IN_PROGRESS)
        self.assertFalse(self.notify['notify_assigned'].called)

    def test_a_deactivated_agent_is_still_unassignable_elsewhere(self):
        """The exception is scoped to the ticket they already hold - a
        deactivated agent must not reappear as a choice everywhere."""
        self.agent2.is_active = False
        self.agent2.save()
        self.client.force_login(self.agent)
        detail = self.client.get(f'/tickets/{self.colleague_ticket.pk}/').content.decode()
        self.assertNotIn(f'<option value="{self.agent2.pk}"', detail)
        response = self.client.post(self.url(), {'status': 'open', 'assigned_to': self.agent2.pk})
        self.assertEqual(response.status_code, 400)
        self.colleague_ticket.refresh_from_db()
        self.assertIsNone(self.colleague_ticket.assigned_to)

    def test_invalid_status_is_rejected(self):
        """An unknown status persisted silently would wedge the ticket
        in the open queue. A ModelForm enforces Status.choices for free."""
        self.client.force_login(self.agent)
        response = self.client.post(self.url(), {'status': 'closed', 'assigned_to': ''})
        self.assertEqual(response.status_code, 400)
        self.colleague_ticket.refresh_from_db()
        self.assertEqual(self.colleague_ticket.status, Ticket.Status.OPEN)
        self.assertFalse(self.notified())

    def test_htmx_update_returns_the_meta_partial(self):
        self.client.force_login(self.agent)
        response = self.client.post(
            self.url(), {'status': 'waiting_on_customer', 'assigned_to': self.agent.pk}, HTTP_HX_REQUEST='true',
        )
        content = response.content.decode()
        self.assertIn('id="ticket-meta"', content)
        self.assertIn('Waiting on customer', content)
        self.assertNotIn('hx-swap-oob', content)
        self.assertNotIn('<html', content)

    def test_superuser_counts_as_an_agent(self):
        boss = User.objects.create_user('boss', is_staff=True, is_superuser=True)
        self.client.force_login(boss)
        response = self.client.post(self.url(self.outsider_ticket), {'status': 'in_progress', 'assigned_to': ''})
        self.assertEqual(response.status_code, 302)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    TICKET_SITE_URL='https://support.example.com',
    DEFAULT_FROM_EMAIL='noreply@example.com',
)
class NotificationTests(TestCase):
    """The real notifications, end to end through the templates and
    django.core.mail. Unlike the classes above, nothing is patched —
    this is what proves the 12 email templates render and that links
    are built from TICKET_SITE_URL."""

    @classmethod
    def setUpTestData(cls):
        cls.org = Organisation.objects.create(name='Riverside Dental', slug='riverside')
        cls.customer = User.objects.create_user(
            'owner@riverside.example', email='owner@riverside.example', first_name='Cara',
        )
        Membership.objects.create(user=cls.customer, organisation=cls.org, role=Membership.Role.OWNER)
        cls.agent = User.objects.create_user('agent', email='support@example.com', is_staff=True)
        cls.agent2 = User.objects.create_user('jay', email='jay@example.com', is_staff=True)
        cls.ticket = Ticket.objects.create(
            raised_by=cls.customer, organisation=cls.org, subject='Bookings not syncing',
        )

    def setUp(self):
        mail.outbox = []

    def test_ticket_link_is_built_from_ticket_site_url(self):
        from . import notifications

        self.assertTrue(notifications.notify_customer_reply(self.ticket))
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['owner@riverside.example'])
        self.assertEqual(message.from_email, 'noreply@example.com')
        self.assertEqual(message.subject, 'New reply on your ticket: Bookings not syncing — SL Deskline')
        expected = f'https://support.example.com/tickets/{self.ticket.pk}/'
        self.assertIn(expected, message.body)
        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, 'text/html')
        self.assertIn(expected, html)

    @override_settings(TICKET_SITE_URL='https://support.example.com/')
    def test_trailing_slash_on_the_setting_does_not_double_up(self):
        from . import notifications

        notifications.notify_resolved(self.ticket)
        self.assertIn(f'https://support.example.com/tickets/{self.ticket.pk}/', mail.outbox[0].body)
        self.assertNotIn('//tickets/', mail.outbox[0].body)

    def test_unassigned_new_ticket_fans_out_to_every_agent(self):
        from . import notifications

        notifications.notify_staff_new_ticket(self.ticket)
        self.assertEqual(
            sorted(sum((m.to for m in mail.outbox), [])),
            ['jay@example.com', 'support@example.com'],
        )
        self.assertTrue(all('Riverside Dental' in m.body for m in mail.outbox))

    def test_an_organisation_less_ticket_renders_cleanly_in_agent_emails(self):
        """The agent-facing templates name the organisation in brackets;
        with none they must drop the brackets, not print '()' or
        'None'."""
        from . import notifications

        loner = User.objects.create_user('loner@example.com', email='loner@example.com')
        ticket = Ticket.objects.create(raised_by=loner, subject='Personal account issue')
        ticket.assigned_to = self.agent
        notifications.notify_staff_new_ticket(ticket)
        notifications.notify_staff_new_message(ticket)
        notifications.notify_assigned(ticket)
        self.assertEqual(len(mail.outbox), 3)
        for message in mail.outbox:
            html, _ = message.alternatives[0]
            for rendered in (message.body, html):
                self.assertIn('Personal account issue', rendered)
                self.assertNotIn('()', rendered)
                self.assertNotIn('None', rendered)

    def test_assigned_ticket_notifies_only_the_assignee(self):
        from . import notifications

        self.ticket.assigned_to = self.agent
        self.ticket.save()
        notifications.notify_staff_new_message(self.ticket)
        self.assertEqual([m.to for m in mail.outbox], [['support@example.com']])

    def test_a_deactivated_agent_is_dropped_from_the_fan_out(self):
        """tickets/forms.py's assignable queryset filters
        is_active; _staff_recipients did not, so an agent retired by
        deactivation (rather than deletion, which would take their
        ticket history with it) vanished from the dropdown but kept
        getting every new-ticket email indefinitely."""
        from . import notifications

        self.agent2.is_active = False
        self.agent2.save()

        notifications.notify_staff_new_ticket(self.ticket)
        self.assertEqual([m.to for m in mail.outbox], [['support@example.com']])

    def test_a_deactivated_assignee_falls_back_to_the_team(self):
        """The assigned branch filters is_active too — otherwise the
        one account guaranteed to be notified is the one that has
        stopped reading its mail. Nobody is watching the ticket, so it
        fans out exactly as an unassigned one."""
        from . import notifications

        self.ticket.assigned_to = self.agent
        self.ticket.save()
        self.agent.is_active = False
        self.agent.save()

        notifications.notify_staff_new_message(self.ticket)
        self.assertEqual([m.to for m in mail.outbox], [['jay@example.com']])

    def test_a_send_failure_never_raises(self):
        """Callers in views.py deliberately do not wrap these — a broken
        mail server must not roll back a ticket."""
        from . import notifications

        # assertLogs doubles as the assertion that the failure is logged
        # rather than swallowed, and keeps the expected traceback out of
        # the test run's output.
        with mock.patch('tickets.notifications.EmailMultiAlternatives.send', side_effect=OSError('down')):
            with self.assertLogs('tickets.notifications', level='ERROR'):
                self.assertFalse(notifications.notify_resolved(self.ticket))
        self.assertEqual(mail.outbox, [])

    def test_creating_a_ticket_sends_both_sides_exactly_one_email_each(self):
        self.client.force_login(self.customer)
        self.client.post('/tickets/new/', {
            'subject': 'Card reader offline', 'body': 'It will not pair.', 'urgency': 'normal',
        })
        subjects = sorted(m.subject for m in mail.outbox)
        self.assertEqual(subjects, [
            'New ticket: Card reader offline — SL Deskline',            # to each agent...
            'New ticket: Card reader offline — SL Deskline',
            "We've received your ticket: Card reader offline — SL Deskline",  # ...and the raiser
        ])
        self.assertIn('It will not pair.', next(
            m.body for m in mail.outbox if m.subject.startswith("We've received")
        ))


@override_settings(
    TICKET_HOSTS=['support.example.com'],
    ALLOWED_HOSTS=['help.example.com', 'support.example.com', 'testserver'],
)
class TicketHostRootRedirectTests(TestCase):
    """support_core/host_middleware.py has always reversed
    `tickets:list` for a bare `/` on a ticket host; until this app
    existed that was a NoReverseMatch waiting to happen and nothing
    could assert it. It is load-bearing: an SSO provider may send the
    browser to /sso/callback/?code=... with no `next`, which defaults to
    `/`."""

    def test_bare_root_on_a_ticket_host_redirects_to_the_ticket_list(self):
        response = self.client.get('/', headers={'host': 'support.example.com'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/tickets/')

    def test_bare_root_on_the_help_host_is_left_alone(self):
        response = self.client.get('/', headers={'host': 'help.example.com'})
        self.assertEqual(response.status_code, 200)
