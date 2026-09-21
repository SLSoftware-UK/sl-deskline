"""
Tests for the shared identity layer, in both SSO_BACKEND modes.

Settings validation happens when settings.py is imported, but the views
read `settings.SSO_BACKEND` at request time, so each mode is exercised
here with `override_settings` (the `CODE_EXCHANGE` decorator below); the
import-time validation itself is tested in a subprocess, where a fresh
import is the only honest way to see it.

`requests.post` is mocked in every SSO test -- nothing here ever touches
the network, so the suite is deterministic and runnable offline, and a
typo in SSO_EXCHANGE_URL can never turn into a real outbound call from a
test run.
"""
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import Membership, Organisation

User = get_user_model()

EXCHANGE_URL = 'https://idp.example.com/api/sso/exchange/'
PROVIDER_LOGIN_URL = 'https://idp.example.com/login'

CODE_EXCHANGE = override_settings(
    SSO_BACKEND='code_exchange',
    SSO_EXCHANGE_URL=EXCHANGE_URL,
    SSO_PROVIDER_LOGIN_URL=PROVIDER_LOGIN_URL,
)
LOCAL = override_settings(SSO_BACKEND='local')

# The default-origin every DEFAULT_ORIGIN-assuming test below pins itself
# to via override_settings, so a developer's own backend/.env (e.g. one
# configured with a different SITE_URL for real SSO testing) cannot make
# an assertion about "what a fresh install looks like" fail.
DEFAULT_ORIGIN = 'http://localhost:8000'

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _run_in_fresh_interpreter(code, **env):
    """Run `code` in a brand-new Python process that imports
    support_core.settings, for tests that need either a genuinely fresh
    module import (settings validation runs at import time -- see
    SSOSettingsValidationTests) or a genuinely clean environment
    (DefaultModeTests, below).

    Strips any SSO_* variable this test process itself inherited, and
    points SL_DESKLINE_ENV_FILE at os.devnull so settings.py's read_env()
    finds nothing there -- otherwise a developer's own backend/.env
    (e.g. one configured for `code_exchange` SSO, with SITE_URL and
    TICKET_HOSTS set to match) would leak into the subprocess and make
    these "what does a clean checkout do" tests lie. `env` overrides
    still work normally: read_env() only fills variables that are not
    already set, so anything a test passes here wins regardless."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith('SSO_')}
    clean.update({'DEBUG': 'True', 'SL_DESKLINE_ENV_FILE': os.devnull, **env})
    return subprocess.run(
        [sys.executable, '-c', code],
        cwd=BACKEND_DIR, env=clean, capture_output=True, text=True, timeout=60,
    )

# Every throttle-aware test class gets its own clean in-process cache, so
# a counter left by one test can never lock out the next.
THROTTLE_CACHE = override_settings(
    CACHES={'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'login-throttle-tests',
    }},
    LOGIN_MAX_ATTEMPTS=3,
    LOGIN_LOCKOUT_SECONDS=600,
)

POST_PATCH = 'accounts.sso.requests.post'

# The exchange response body: {'user': {...}}, where `user.organisations`
# is the full list of organisations the user belongs to at the identity
# provider, each with the provider's own id and the user's role there.
# The extra `token` key is deliberately present and deliberately ignored
# (this service keeps its own session), so keeping it here documents that.
EXCHANGE_PAYLOAD = {
    'token': 'ignored-provider-token',
    'user': {
        'email': 'owner@member.example',
        'first_name': 'Dana',
        'last_name': 'Owner',
        'organisations': [
            {'id': 42, 'name': 'Northside Dental', 'slug': 'northside-dental', 'role': 'owner'},
            {'id': 43, 'name': 'Southside Dental', 'slug': 'southside-dental', 'role': 'member'},
        ],
    },
}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else EXCHANGE_PAYLOAD
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError('not json')
        return self._payload


def _callback_url(**params):
    url = reverse('accounts:sso-callback')
    if params:
        url = f'{url}?{urlencode(params)}'
    return url


def _with_user(**changes):
    return {'user': {**EXCHANGE_PAYLOAD['user'], **changes}}


# ---------------------------------------------------------------------------
# SSO_BACKEND='local' (the default)
# ---------------------------------------------------------------------------

class DefaultModeTests(SimpleTestCase):
    def test_local_is_the_default_backend(self):
        # settings.py is only imported once for this whole test run, and
        # by the time that happened it may already have read a
        # developer's own backend/.env (e.g. one configured with
        # SSO_BACKEND=code_exchange for testing SSO by hand) -- in that
        # case `settings.SSO_BACKEND` in this process is whatever that
        # file said, not necessarily the shipped default. A fresh
        # interpreter with SL_DESKLINE_ENV_FILE pointed at nothing (see
        # _run_in_fresh_interpreter) is the only way to see the real one.
        result = _run_in_fresh_interpreter(
            'import support_core.settings as s; print(s.SSO_BACKEND)',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'local')

    def test_login_url_is_the_login_route(self):
        self.assertEqual(settings.LOGIN_URL, 'accounts:login')
        self.assertEqual(reverse(settings.LOGIN_URL), '/login/')


@LOCAL
@THROTTLE_CACHE
class LocalLoginTests(TestCase):
    """/login/ in local mode: one password form for every account."""

    @classmethod
    def setUpTestData(cls):
        cls.agent = User.objects.create_user(
            'agentsam', email='support@example.com', password='correct-horse', is_staff=True,
        )
        cls.customer = User.objects.create_user(
            'dana', email='dana@member.example', password='battery-staple',
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse('accounts:login')

    def _post(self, username, password, url=None, **extra):
        return self.client.post(url or self.url, {'username': username, 'password': password}, **extra)

    def test_get_renders_the_password_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_login.html')
        self.assertContains(response, 'name="password"')

    def test_customer_signs_in_by_username_and_lands_on_the_kb_home(self):
        response = self._post('dana', 'battery-staple')
        self.assertRedirects(response, reverse('kb:article-list'), fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.customer.pk))

    def test_customer_signs_in_by_email(self):
        response = self._post('dana@member.example', 'battery-staple')
        self.assertRedirects(response, reverse('kb:article-list'), fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.customer.pk))

    def test_agent_signs_in_by_email_and_lands_on_the_ticket_list(self):
        # The point of the email -> username lookup: the agent's username
        # ('agentsam') is nothing like their email address.
        response = self._post('support@example.com', 'correct-horse')
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.agent.pk))

    def test_agent_signs_in_by_username(self):
        response = self._post('agentsam', 'correct-horse')
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)

    def test_email_lookup_is_case_insensitive(self):
        response = self._post('Support@Example.COM', 'correct-horse')
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)

    def test_wrong_password_gets_the_generic_error(self):
        response = self._post('dana@member.example', 'nope')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Those credentials were not recognised.')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_unknown_account_gets_the_same_generic_error(self):
        """Same message, same status for "no such account" as for "wrong
        password" -- the form must not confirm which accounts exist."""
        wrong_password = self._post('dana@member.example', 'nope')
        unknown = self._post('nobody@nowhere.example', 'nope')
        self.assertEqual(wrong_password.status_code, unknown.status_code)
        self.assertEqual(
            list(wrong_password.context['form'].non_field_errors()),
            list(unknown.context['form'].non_field_errors()),
        )

    def test_an_account_with_an_unusable_password_cannot_sign_in(self):
        """An SSO shadow user left over from a code_exchange deployment
        has no password; the form must not let one in with any."""
        shadow = User.objects.create_user('shadow@member.example', email='shadow@member.example')
        self.assertFalse(shadow.has_usable_password())
        response = self._post('shadow@member.example', '')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(response.status_code, 200)

    def test_inactive_account_cannot_sign_in(self):
        self.customer.is_active = False
        self.customer.save()
        self._post('dana', 'battery-staple')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_repeated_failures_lock_the_pair_out(self):
        for _ in range(2):
            self.assertContains(self._post('dana', 'wrong'), 'Those credentials were not recognised.')
        # The third failure trips it, and says so immediately.
        self.assertContains(self._post('dana', 'wrong'), 'Please try again in 10 minutes.')
        # Even the right password is refused now.
        response = self._post('dana', 'battery-staple')
        self.assertContains(response, 'Too many failed sign-in attempts')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_local_next_is_honoured(self):
        response = self._post('dana', 'battery-staple', url=f'{self.url}?next=/tickets/12/')
        self.assertRedirects(response, '/tickets/12/', fetch_redirect_response=False)

    def test_offsite_next_is_ignored(self):
        for bad in ('https://evil.com/', '//evil.com/x', '/\\evil.com/x'):
            with self.subTest(next=bad):
                self.client.logout()
                response = self._post('dana', 'battery-staple', url=f'{self.url}?{urlencode({"next": bad})}')
                self.assertRedirects(response, reverse('kb:article-list'), fetch_redirect_response=False)

    def test_offsite_next_for_an_agent_falls_back_to_the_ticket_list(self):
        response = self._post('agentsam', 'correct-horse', url=f'{self.url}?next=https://evil.com/')
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)

    def test_login_required_pages_send_anonymous_visitors_here(self):
        response = self.client.get('/tickets/')
        self.assertTrue(response['Location'].startswith('/login/?next=/tickets/'))
        self.assertEqual(self.client.get(response['Location']).status_code, 200)

    def test_a_signed_in_visitor_is_shown_the_form_not_bounced(self):
        """A view gated on something stricter than "signed in" can send
        an authenticated user to LOGIN_URL; redirecting them straight
        back to `next` would loop."""
        self.client.force_login(self.customer)
        response = self.client.get(f'{self.url}?next=/tickets/')
        self.assertEqual(response.status_code, 200)


@LOCAL
class LocalModeOtherRoutesTests(TestCase):
    def test_sso_callback_is_a_404(self):
        with patch(POST_PATCH) as mock_post:
            response = self.client.get(_callback_url(code='anything'))
        self.assertEqual(response.status_code, 404)
        mock_post.assert_not_called()

    def test_staff_login_temporarily_redirects_to_login_keeping_next(self):
        # 302, never 301: a cached permanent redirect would outlive a
        # later switch to code_exchange and lock agents out of the form.
        response = self.client.get(f'{reverse("accounts:staff-login")}?next=/tickets/12/')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/?next=%2Ftickets%2F12%2F')

    def test_staff_login_without_next_redirects_to_bare_login(self):
        response = self.client.get(reverse('accounts:staff-login'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/')


class LogoutTests(TestCase):
    """POST /logout/ and POST /staff/logout/ -- the same view, either mode."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('dana', 'dana@member.example', 'pw')

    def test_post_logs_out_and_lands_on_the_kb_home_with_a_message(self):
        for name in ('accounts:logout', 'accounts:staff-logout'):
            with self.subTest(route=name):
                self.client.force_login(self.user)
                response = self.client.post(reverse(name), follow=True)
                self.assertRedirects(response, reverse('kb:article-list'))
                self.assertNotIn('_auth_user_id', self.client.session)
                self.assertContains(response, 'You have been signed out.')

    def test_get_is_rejected(self):
        # POST-only: a GET logout is CSRF-able and pre-fetchable.
        for name in ('accounts:logout', 'accounts:staff-logout'):
            with self.subTest(route=name):
                self.client.force_login(self.user)
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 405)
                self.assertIn('_auth_user_id', self.client.session)

    @CODE_EXCHANGE
    @patch(POST_PATCH)
    def test_sso_logout_ends_the_local_session_only(self, mock_post):
        mock_post.return_value = _FakeResponse()
        self.client.get(_callback_url(code='good-code'))
        self.assertIn('_auth_user_id', self.client.session)

        response = self.client.post(reverse('accounts:logout'))

        self.assertRedirects(response, reverse('kb:article-list'))
        self.assertNotIn('_auth_user_id', self.client.session)
        # The shadow user survives -- logging out is not deleting the
        # account.
        self.assertTrue(User.objects.filter(username='owner@member.example').exists())


# ---------------------------------------------------------------------------
# SSO_BACKEND='code_exchange'
# ---------------------------------------------------------------------------

@CODE_EXCHANGE
class SSOCallbackTests(TestCase):
    """The push handshake: /sso/callback/?code=..."""

    @patch(POST_PATCH)
    def test_successful_exchange_creates_user_organisations_memberships_and_logs_in(self, mock_post):
        mock_post.return_value = _FakeResponse()

        response = self.client.get(_callback_url(code='good-code'))

        # Posted server-to-server to the configured URL, code in the body.
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], EXCHANGE_URL)
        self.assertEqual(kwargs['json'], {'code': 'good-code'})
        self.assertEqual(kwargs['timeout'], 10)

        user = User.objects.get(username='owner@member.example')
        self.assertEqual(user.email, 'owner@member.example')
        self.assertEqual(user.first_name, 'Dana')
        self.assertEqual(user.last_name, 'Owner')
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)

        # Keyed on the provider's id via external_id, not on the local pk.
        organisation = Organisation.objects.get(external_id='42')
        self.assertEqual(organisation.name, 'Northside Dental')
        self.assertEqual(organisation.slug, 'northside-dental')

        self.assertEqual(
            set(Membership.objects.filter(user=user).values_list('organisation__external_id', 'role')),
            {('42', 'owner'), ('43', 'member')},
        )

        # Logged in, and landed on the default target.
        self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    @patch(POST_PATCH)
    def test_sso_code_alias_is_accepted(self, mock_post):
        mock_post.return_value = _FakeResponse()
        self.client.get(_callback_url(sso_code='good-code'))
        self.assertTrue(User.objects.filter(username='owner@member.example').exists())

    @patch(POST_PATCH)
    def test_second_login_is_a_full_replace_of_memberships(self, mock_post):
        """Sync is a full replace, not a merge: an organisation the
        provider no longer reports for this user loses its membership at
        the next sign-in (so it stops exposing that organisation's
        tickets), a changed role is updated, and a renamed organisation
        is updated in place rather than duplicated."""
        mock_post.return_value = _FakeResponse()
        self.client.get(_callback_url(code='good-code'))
        northside_pk = Organisation.objects.get(external_id='42').pk

        mock_post.return_value = _FakeResponse(payload=_with_user(
            first_name='Danielle',
            organisations=[
                {'id': 42, 'name': 'Northside Dental & Co', 'slug': 'northside-dental', 'role': 'admin'},
            ],
        ))
        self.client.get(_callback_url(code='another-code'))

        self.assertEqual(User.objects.filter(username='owner@member.example').count(), 1)
        user = User.objects.get(username='owner@member.example')
        self.assertEqual(user.first_name, 'Danielle')

        northside = Organisation.objects.get(external_id='42')
        self.assertEqual(northside.pk, northside_pk)
        self.assertEqual(northside.name, 'Northside Dental & Co')
        self.assertEqual(
            list(Membership.objects.filter(user=user).values_list('organisation__external_id', 'role')),
            [('42', 'admin')],
        )
        # The organisation row itself survives -- other users may still
        # belong to it, and its tickets reference it.
        self.assertTrue(Organisation.objects.filter(external_id='43').exists())

    @patch(POST_PATCH)
    def test_the_replace_leaves_memberships_in_hand_made_organisations_alone(self, mock_post):
        """The provider only knows about the organisations it manages
        (external_id set). A membership an operator added in Django Admin
        to a hand-made organisation (external_id NULL) is not the
        provider's to remove, so it survives every sign-in -- while a
        provider-managed membership the provider stopped reporting is
        still removed."""
        mock_post.return_value = _FakeResponse()
        self.client.get(_callback_url(code='good-code'))
        user = User.objects.get(username='owner@member.example')
        partner = Organisation.objects.create(name='Partner Clinic', slug='partner-clinic')
        Membership.objects.create(user=user, organisation=partner, role=Membership.Role.ADMIN)

        mock_post.return_value = _FakeResponse(payload=_with_user(organisations=[
            {'id': 42, 'name': 'Northside Dental', 'slug': 'northside-dental', 'role': 'owner'},
        ]))
        self.client.get(_callback_url(code='another-code'))

        self.assertEqual(
            set(Membership.objects.filter(user=user).values_list('organisation__slug', 'role')),
            {('northside-dental', 'owner'), ('partner-clinic', 'admin')},
        )

        # And an empty list clears the provider-managed ones only.
        mock_post.return_value = _FakeResponse(payload=_with_user(organisations=[]))
        self.client.get(_callback_url(code='third-code'))
        self.assertEqual(
            list(Membership.objects.filter(user=user).values_list('organisation__slug', flat=True)),
            ['partner-clinic'],
        )

    @patch(POST_PATCH)
    def test_an_unknown_role_is_stored_as_member(self, mock_post):
        mock_post.return_value = _FakeResponse(payload=_with_user(organisations=[
            {'id': 'abc', 'name': 'Acme', 'slug': 'acme', 'role': 'superadmin'},
            {'id': 'def', 'name': 'Beta', 'slug': 'beta'},
        ]))
        self.client.get(_callback_url(code='good-code'))
        self.assertEqual(
            set(Membership.objects.values_list('organisation__external_id', 'role')),
            {('abc', 'member'), ('def', 'member')},
        )

    @patch(POST_PATCH)
    def test_id_zero_is_a_valid_organisation_id(self, mock_post):
        mock_post.return_value = _FakeResponse(payload=_with_user(organisations=[
            {'id': 0, 'name': 'Zero Corp', 'slug': 'zero', 'role': 'owner'},
        ]))
        self.client.get(_callback_url(code='good-code'))
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(
            list(Membership.objects.values_list('organisation__external_id', 'role')),
            [('0', 'owner')],
        )

    @patch(POST_PATCH)
    def test_a_slug_already_taken_locally_does_not_break_the_sync(self, mock_post):
        """Organisation.slug is unique, and an operator may already have
        created an organisation by hand with the slug the provider
        sends. The synced row gets a suffixed slug rather than the whole
        sign-in failing on an IntegrityError."""
        Organisation.objects.create(name='Hand-made', slug='northside-dental')
        mock_post.return_value = _FakeResponse()

        self.client.get(_callback_url(code='good-code'))

        synced = Organisation.objects.get(external_id='42')
        self.assertNotEqual(synced.slug, 'northside-dental')
        self.assertTrue(synced.slug.startswith('northside-dental'))
        self.assertIn('_auth_user_id', self.client.session)

    @patch(POST_PATCH)
    def test_payload_with_no_organisations_signs_in_an_organisation_less_customer(self, mock_post):
        """Belonging to no organisation is a valid customer state (they
        can still raise tickets, filed under no organisation), so an empty
        or missing `organisations` list is not a reason to refuse the
        sign-in -- it just leaves the user with no memberships."""
        for payload in (_with_user(organisations=[]), {'user': {'email': 'owner@member.example'}}):
            with self.subTest(payload=payload):
                self.client.logout()
                mock_post.return_value = _FakeResponse(payload=payload)
                self.client.get(_callback_url(code='good-code'))
                user = User.objects.get(username='owner@member.example')
                self.assertEqual(self.client.session['_auth_user_id'], str(user.pk))
                self.assertFalse(Membership.objects.filter(user=user).exists())

    @patch(POST_PATCH)
    def test_payload_missing_email_is_refused(self, mock_post):
        mock_post.return_value = _FakeResponse(payload=_with_user(email=''))

        response = self.client.get(_callback_url(code='good-code'), follow=True)

        self.assertRedirects(response, reverse('kb:article-list'))
        self.assertContains(response, 'did not return an email address')
        self.assertFalse(User.objects.exists())
        self.assertFalse(Organisation.objects.exists())
        self.assertNotIn('_auth_user_id', self.client.session)


@CODE_EXCHANGE
class SSOProviderErrorTests(TestCase):
    """Every way the exchange can go wrong ends on the KB home with a
    customer-safe message, signed out, and with nothing written."""

    def _assert_refused(self, response, message=None):
        self.assertRedirects(response, reverse('kb:article-list'))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(User.objects.exists())
        self.assertFalse(Organisation.objects.exists())
        self.assertFalse(Membership.objects.exists())
        if message:
            self.assertContains(response, message)

    @patch(POST_PATCH)
    def test_non_200_exchange(self, mock_post):
        mock_post.return_value = _FakeResponse(status_code=400, payload={'detail': 'Invalid or expired code.'})
        response = self.client.get(_callback_url(code='stale-code'), follow=True)
        self._assert_refused(response, 'invalid or has expired')

    @patch(POST_PATCH)
    def test_unreachable_provider(self, mock_post):
        mock_post.side_effect = requests.RequestException('boom')
        # assertLogs both asserts the failure is recorded for ops and
        # keeps the expected traceback out of the test run's output.
        with self.assertLogs('accounts.sso', level='ERROR'):
            response = self.client.get(_callback_url(code='good-code'), follow=True)
        self._assert_refused(response, 'Could not reach the sign-in service')

    @patch(POST_PATCH)
    def test_non_json_body(self, mock_post):
        mock_post.return_value = _FakeResponse(json_error=True)
        with self.assertLogs('accounts.sso', level='ERROR'):
            response = self.client.get(_callback_url(code='good-code'), follow=True)
        self._assert_refused(response, 'unexpected response')

    def test_missing_code(self):
        with patch(POST_PATCH) as mock_post:
            response = self.client.get(_callback_url(), follow=True)
        mock_post.assert_not_called()
        self._assert_refused(response)

    @patch(POST_PATCH)
    def test_malformed_payloads_are_refused_not_500(self, mock_post):
        """The provider is trusted about *who* the user is, but not to
        get the shape right. Each of these used to be a 500 (TypeError,
        AttributeError, DataError) or a silent partial sync."""
        good_org = {'id': 1, 'name': 'Good', 'slug': 'good', 'role': 'owner'}
        cases = {
            'body is a list': ['not', 'an', 'object'],
            'user missing': {'token': 't'},
            'user is a string': {'user': 'owner@member.example'},
            'organisations is a dict': _with_user(organisations={'id': 1}),
            'organisations is a string': _with_user(organisations='acme'),
            'entry is not an object': _with_user(organisations=[good_org, 'acme']),
            'id missing': _with_user(organisations=[good_org, {'name': 'No id'}]),
            'id null': _with_user(organisations=[{'id': None, 'name': 'Null'}]),
            'id empty string': _with_user(organisations=[{'id': '  ', 'name': 'Blank'}]),
            'id is an object': _with_user(organisations=[{'id': {'x': 1}, 'name': 'Obj'}]),
            'id is a bool': _with_user(organisations=[{'id': True, 'name': 'Bool'}]),
            'id too long': _with_user(organisations=[{'id': 'x' * 65, 'name': 'Long'}]),
            'name not a string': _with_user(organisations=[{'id': 1, 'name': ['a']}]),
            'email not a string': _with_user(email=['owner@member.example']),
        }
        for label, payload in cases.items():
            with self.subTest(label):
                mock_post.return_value = _FakeResponse(payload=payload)
                # A wrong-typed email gets the same "no email" answer as a
                # missing one, which is not an ops-worthy error; every
                # other case is logged for whoever runs the provider.
                logs = (
                    nullcontext() if label == 'email not a string'
                    else self.assertLogs('accounts.sso', level='ERROR')
                )
                with logs:
                    response = self.client.get(_callback_url(code='good-code'), follow=True)
                self._assert_refused(response)

    @patch(POST_PATCH)
    def test_a_64_character_id_is_accepted(self, mock_post):
        mock_post.return_value = _FakeResponse(payload=_with_user(organisations=[
            {'id': 'x' * 64, 'name': 'Max', 'slug': 'max'},
        ]))
        self.client.get(_callback_url(code='good-code'))
        self.assertTrue(Organisation.objects.filter(external_id='x' * 64).exists())


@CODE_EXCHANGE
class SSOStaffAccountGuardTests(TestCase):
    """A local support-agent account must never be taken over -- or
    damaged -- by an SSO login that happens to carry the same address."""

    @patch(POST_PATCH)
    def test_existing_staff_account_is_refused_and_left_untouched(self, mock_post):
        agent = User.objects.create_user(
            username='owner@member.example', email='owner@member.example',
            password='agent-password', first_name='Sam', last_name='Agent', is_staff=True,
        )
        mock_post.return_value = _FakeResponse()

        response = self.client.get(_callback_url(code='good-code'), follow=True)

        self.assertRedirects(response, reverse('kb:article-list'))
        self.assertNotIn('_auth_user_id', self.client.session)

        agent.refresh_from_db()
        # Password NOT wiped by set_unusable_password(), names not synced.
        self.assertTrue(agent.check_password('agent-password'))
        self.assertEqual(agent.first_name, 'Sam')
        self.assertEqual(agent.last_name, 'Agent')
        self.assertTrue(agent.is_staff)
        # And no memberships were attached, nor organisations created --
        # an agent's access comes from is_staff, never from a customer
        # organisation.
        self.assertFalse(Membership.objects.filter(user=agent).exists())
        self.assertFalse(Organisation.objects.exists())

    @patch(POST_PATCH)
    def test_existing_superuser_account_is_refused(self, mock_post):
        admin = User.objects.create_superuser(
            'owner@member.example', 'owner@member.example', 'admin-password'
        )
        mock_post.return_value = _FakeResponse()

        self.client.get(_callback_url(code='good-code'), follow=True)

        admin.refresh_from_db()
        self.assertTrue(admin.check_password('admin-password'))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertFalse(Membership.objects.exists())


@CODE_EXCHANGE
class SafeNextPathTests(TestCase):
    """`next` arrives via a redirect chain through the identity provider,
    so it is attacker-influenced -- an open redirect here would be a
    phishing hop."""

    @patch(POST_PATCH)
    def _callback_with_next(self, next_value, mock_post):
        mock_post.return_value = _FakeResponse()
        return self.client.get(_callback_url(code='good-code', next=next_value))

    def test_local_path_is_honoured(self):
        response = self._callback_with_next('/articles/foo/')
        self.assertRedirects(response, '/articles/foo/', fetch_redirect_response=False)

    def test_local_path_with_query_string_is_honoured(self):
        response = self._callback_with_next('/tickets/new/?article=7')
        self.assertRedirects(response, '/tickets/new/?article=7', fetch_redirect_response=False)

    def test_protocol_relative_next_is_ignored(self):
        response = self._callback_with_next('//evil.com/pwned')
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_absolute_url_next_is_ignored(self):
        response = self._callback_with_next('https://evil.com/pwned')
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    # The three below are why this uses Django's
    # url_has_allowed_host_and_scheme rather than a startswith() pair:
    # browsers normalise `\` to `/` and strip tab/CR/LF before parsing
    # the authority, so each of these reads as off-site to a browser
    # while sailing past a naive prefix check.
    def test_backslash_authority_next_is_ignored(self):
        response = self._callback_with_next('/\\evil.com/pwned')
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_embedded_tab_next_is_ignored(self):
        response = self._callback_with_next('/\t/evil.com/pwned')
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    def test_percent_encoded_tab_next_stays_a_same_origin_path(self):
        # The one variant of the three that url_has_allowed_host_and_scheme
        # deliberately does NOT reject:
        #
        #   >>> url_has_allowed_host_and_scheme('/%09//evil.com/pwned',
        #   ...     allowed_hosts={'testserver'}, require_https=False)
        #   True
        #
        # and that is correct, not a gap. urlsplit strips *literal* tab
        # and CR/LF before parsing the authority, which is why
        # test_embedded_tab_next_is_ignored passes -- but `%09` is an
        # escaped tab inside the path, and no browser percent-decodes a
        # path before it parses the authority. So this value is a
        # same-origin path to every client that sees it, and honouring it
        # is the right behaviour. Asserted here so a future reader does
        # not "fix" it into the reject list on the strength of how it
        # looks.
        response = self._callback_with_next('/%09//evil.com/pwned')
        self.assertRedirects(response, '/%09//evil.com/pwned', fetch_redirect_response=False)


@CODE_EXCHANGE
class SSOLoginPageTests(TestCase):
    """/login/ in SSO mode -- informational only; this service cannot
    initiate SSO."""

    def test_renders_and_points_at_the_provider_carrying_next(self):
        response = self.client.get(f'{reverse("accounts:login")}?next=/tickets/new/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/login.html')
        self.assertContains(response, f'href="{PROVIDER_LOGIN_URL}?next=%2Ftickets%2Fnew%2F"')
        # The other door, for support agents.
        self.assertContains(response, reverse('accounts:staff-login'))

    def test_an_unsafe_next_is_not_passed_to_the_provider(self):
        response = self.client.get(f'{reverse("accounts:login")}?next=https://evil.com/')
        # Not a bare assertNotContains('evil.com'): the header's own Log
        # in link carries the current URL as its `next`, query and all.
        self.assertContains(response, f'href="{PROVIDER_LOGIN_URL}"')
        self.assertNotContains(response, f'{PROVIDER_LOGIN_URL}?next=')

    @override_settings(SSO_PROVIDER_LOGIN_URL='https://idp.example.com/login?app=desk')
    def test_a_provider_url_with_a_query_string_gets_an_ampersand(self):
        response = self.client.get(f'{reverse("accounts:login")}?next=/tickets/')
        self.assertContains(response, 'href="https://idp.example.com/login?app=desk&amp;next=%2Ftickets%2F"')

    def test_post_is_not_allowed(self):
        response = self.client.post(reverse('accounts:login'), {'username': 'x', 'password': 'y'})
        self.assertEqual(response.status_code, 405)


@CODE_EXCHANGE
@THROTTLE_CACHE
class SSOModeStaffLoginTests(TestCase):
    """/staff/login/ in SSO mode -- local support-agent accounts only."""

    @classmethod
    def setUpTestData(cls):
        cls.agent = User.objects.create_user(
            username='agentsam', email='support@example.com', password='correct-horse', is_staff=True,
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse('accounts:staff-login')

    def _post(self, username, password, url=None, **extra):
        return self.client.post(url or self.url, {'username': username, 'password': password}, **extra)

    def test_get_renders_the_agent_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/password_login.html')
        self.assertContains(response, 'Support agent sign in')

    def test_login_by_email_resolves_the_username(self):
        response = self._post('support@example.com', 'correct-horse')
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.agent.pk))

    def test_login_by_username_works_too(self):
        response = self._post('agentsam', 'correct-horse')
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)

    def test_wrong_password_is_refused(self):
        response = self._post('agentsam', 'nope')
        self.assertContains(response, 'Those credentials were not recognised.')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_non_staff_account_is_refused_even_with_the_right_password(self):
        User.objects.create_user(username='customer', email='customer@member.example', password='pw')
        for identifier in ('customer', 'customer@member.example'):
            with self.subTest(identifier=identifier):
                response = self._post(identifier, 'pw')
                self.assertContains(response, 'Those credentials were not recognised.')
                self.assertNotIn('_auth_user_id', self.client.session)

    def test_next_is_honoured_when_local(self):
        response = self._post('agentsam', 'correct-horse', url=f'{self.url}?next=/tickets/12/')
        self.assertRedirects(response, '/tickets/12/', fetch_redirect_response=False)

    def test_offsite_next_falls_back_to_the_ticket_list(self):
        response = self._post('agentsam', 'correct-horse', url=f'{self.url}?next=https://evil.com/')
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)

    def test_a_signed_in_agent_is_sent_straight_on(self):
        self.client.force_login(self.agent)
        response = self.client.get(self.url)
        self.assertRedirects(response, '/tickets/', fetch_redirect_response=False)


@LOCAL
@THROTTLE_CACHE
class LoginThrottleTests(TestCase):
    """The password form is a plain HTML form at a guessable URL, and
    the accounts behind it include support agents, who see every
    organisation's tickets and hold Django Admin plus KB authoring.

    Exercised on local-mode /login/; SSO-mode /staff/login/ runs the same
    `_password_login`. MAX_ATTEMPTS is overridden to 3 here purely to
    keep the tests short; the shipped default is 5 / 15 minutes.

    Pinned to `local` mode explicitly (@LOCAL): most of these tests hit
    plain /login/ and assume the local password form renders there, which
    is only true in local mode -- a deployment's own backend/.env could
    otherwise default this to SSO and turn /login/ into the informational
    SSO page instead. The one test that wants SSO mode overrides it back
    per-method with @CODE_EXCHANGE.
    """

    @classmethod
    def setUpTestData(cls):
        cls.agent = User.objects.create_user(
            'agentsam', email='support@example.com', password='correct-horse', is_staff=True,
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse('accounts:login')

    def _attempt(self, password='wrong', ip='10.0.0.1', identifier='support@example.com'):
        return self.client.post(
            self.url, {'username': identifier, 'password': password}, REMOTE_ADDR=ip,
        )

    def test_repeated_failures_lock_the_pair_out(self):
        for _ in range(3):
            response = self._attempt()
            self.assertEqual(response.status_code, 200)

        # Even the right password is refused now -- which is the whole
        # point: an attacker who gets there on attempt 4 gets nothing.
        response = self._attempt(password='correct-horse')

        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertContains(response, 'Too many failed sign-in attempts')

    @CODE_EXCHANGE
    def test_the_sso_mode_agent_form_is_throttled_too(self):
        url = reverse('accounts:staff-login')
        for _ in range(3):
            self.client.post(url, {'username': 'agentsam', 'password': 'wrong'})
        response = self.client.post(url, {'username': 'agentsam', 'password': 'correct-horse'})
        self.assertContains(response, 'Too many failed sign-in attempts')

    def test_the_lockout_message_says_how_long_and_replaces_the_generic_one(self):
        for _ in range(2):
            response = self._attempt()
            self.assertContains(response, 'Those credentials were not recognised.')

        # The third failure is the one that trips it, so it says so
        # immediately rather than making them guess again to find out.
        response = self._attempt()

        self.assertContains(response, 'Please try again in 10 minutes.')
        self.assertNotContains(response, 'Those credentials were not recognised.')

    def test_a_successful_login_clears_the_counter(self):
        self._attempt()
        self._attempt()

        response = self._attempt(password='correct-horse')
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

        # Two failures were already banked; if they had survived the
        # success, this next one would lock the account out.
        self.client.logout()
        response = self._attempt()
        self.assertContains(response, 'Those credentials were not recognised.')
        self.assertNotContains(response, 'Too many failed sign-in attempts')

    def test_two_different_ips_do_not_share_a_counter(self):
        for _ in range(3):
            self._attempt(ip='10.0.0.1')

        # Locked from the first address...
        self.assertContains(self._attempt(ip='10.0.0.1'), 'Too many failed sign-in attempts')
        # ...and untouched from another, so one noisy network cannot
        # lock everyone out.
        response = self._attempt(password='correct-horse', ip='10.0.0.2')
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_two_different_identifiers_from_one_ip_do_not_share_a_counter(self):
        for _ in range(3):
            self._attempt(identifier='someone.else@example.com')

        response = self._attempt(password='correct-horse')

        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)

    def test_the_forwarded_client_ip_is_what_counts_behind_the_proxy(self):
        """Behind a reverse proxy every request has the same REMOTE_ADDR.
        Counting on it would mean the first few failures anywhere locked
        out the world."""
        for _ in range(3):
            self.client.post(
                self.url, {'username': 'support@example.com', 'password': 'wrong'},
                REMOTE_ADDR='172.16.0.1', HTTP_X_FORWARDED_FOR='203.0.113.9, 172.16.0.1',
            )

        blocked = self.client.post(
            self.url, {'username': 'support@example.com', 'password': 'correct-horse'},
            REMOTE_ADDR='172.16.0.1', HTTP_X_FORWARDED_FOR='203.0.113.9, 172.16.0.1',
        )
        self.assertContains(blocked, 'Too many failed sign-in attempts')

        # Same proxy, different real client: unaffected.
        allowed = self.client.post(
            self.url, {'username': 'support@example.com', 'password': 'correct-horse'},
            REMOTE_ADDR='172.16.0.1', HTTP_X_FORWARDED_FOR='198.51.100.4, 172.16.0.1',
        )
        self.assertEqual(allowed.status_code, 302)


class AdminLoginIsUntouchedTests(TestCase):
    """Django Admin is the support agents' tool and those accounts have
    real passwords in either mode, so its own login form must keep
    working."""

    def test_admin_login_still_shows_djangos_own_form(self):
        response = self.client.get('/admin/login/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="password"')


@CODE_EXCHANGE
@override_settings(
    TICKET_HOSTS=['desk.example.com'],
    ALLOWED_HOSTS=['help.example.com', 'desk.example.com', 'testserver'],
    SITE_URL='https://help.example.com',
)
class SSOCallbackNextSlashDisambiguationTests(TestCase):
    """Two individually-correct decisions collide at `/`.

    support_core/host_middleware.py sends a bare `/` on a ticket host to
    /tickets/, which is what makes a no-`next` callback land on the
    queue. A provider that builds every callback on the ticket host and
    sends `next=/` to mean the knowledge-base home would, without special
    handling, end up at the queue too: callback on the ticket host -> log
    in -> 302 to `/` -> middleware -> /tickets/.

    An explicit `next=/` means the KB home on either hostname, via an
    absolute redirect to SITE_URL. A parent-domain session cookie
    (SESSION_COOKIE_DOMAIN) carries across the hop.
    """

    @patch(POST_PATCH)
    def test_ticket_host_with_next_slash_crosses_to_the_help_origin(self, mock_post):
        mock_post.return_value = _FakeResponse()

        response = self.client.get(
            _callback_url(code='good-code', next='/'),
            headers={'host': 'desk.example.com'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'https://help.example.com')
        # And they are signed in on arrival, not bounced to a login.
        self.assertIn('_auth_user_id', self.client.session)

    @patch(POST_PATCH)
    def test_ticket_host_with_a_real_next_stays_local(self, mock_post):
        mock_post.return_value = _FakeResponse()

        response = self.client.get(
            _callback_url(code='good-code', next='/tickets/new/'),
            headers={'host': 'desk.example.com'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/tickets/new/')

    @patch(POST_PATCH)
    def test_help_host_with_next_slash_stays_local(self, mock_post):
        mock_post.return_value = _FakeResponse()

        response = self.client.get(
            _callback_url(code='good-code', next='/'),
            headers={'host': 'help.example.com'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')

    @patch(POST_PATCH)
    def test_a_callback_with_no_next_at_all_lands_on_the_queue(self, mock_post):
        """Deliberately NOT the same case: a callback carrying no `next`
        keeps redirecting to a local `/` for the middleware to turn into
        /tickets/."""
        mock_post.return_value = _FakeResponse()

        response = self.client.get(
            _callback_url(code='good-code'), headers={'host': 'desk.example.com'},
        )
        self.assertEqual(response['Location'], '/')

        followed = self.client.get('/', headers={'host': 'desk.example.com'})
        self.assertEqual(followed['Location'], '/tickets/')


@LOCAL
@override_settings(SITE_URL=DEFAULT_ORIGIN)
class LoginPageMetaTagsTests(TestCase):
    """Every login page renders kb/base.html, which builds
    <link rel="canonical">, og:url, og:title and og:description from the
    view context. Without `canonical_path` a page tells crawlers it *is*
    the home page, with empty og:title/og:description.

    Pinned to `local` mode and SITE_URL=DEFAULT_ORIGIN explicitly: the
    assertions below hard-code http://localhost:8000, which is only the
    real SITE_URL default -- a deployment's own backend/.env (e.g. one
    pointed at a real hostname for SSO testing) could otherwise set both
    differently and make these fail for reasons that have nothing to do
    with the templates under test. The two SSO-page tests override
    SSO_BACKEND back with @CODE_EXCHANGE but keep the class-level
    SITE_URL pin.
    """

    def test_the_local_login_page_is_canonical_to_itself(self):
        html = self.client.get(reverse('accounts:login')).content.decode()
        self.assertIn('<link rel="canonical" href="http://localhost:8000/login/">', html)
        self.assertNotIn('<link rel="canonical" href="http://localhost:8000">', html)
        self.assertIn('<meta property="og:title" content="Sign in — SL Deskline">', html)
        self.assertNotIn('<meta property="og:description" content="">', html)
        self.assertInHTML('<title>Sign in — SL Deskline</title>', html)

    @CODE_EXCHANGE
    def test_the_sso_login_page_is_canonical_to_itself(self):
        html = self.client.get(reverse('accounts:login')).content.decode()
        self.assertIn('<link rel="canonical" href="http://localhost:8000/login/">', html)
        self.assertIn('<meta property="og:title" content="Sign in — SL Deskline">', html)
        self.assertNotIn('<meta property="og:description" content="">', html)

    @CODE_EXCHANGE
    def test_the_staff_login_page_is_canonical_to_itself(self):
        html = self.client.get(reverse('accounts:staff-login')).content.decode()
        self.assertIn('<link rel="canonical" href="http://localhost:8000/staff/login/">', html)
        self.assertIn(
            '<meta property="og:title" content="Support agent sign in — SL Deskline">', html,
        )
        self.assertNotIn('<meta property="og:description" content="">', html)
        self.assertInHTML('<title>Support agent sign in — SL Deskline</title>', html)


class SSOSettingsValidationTests(SimpleTestCase):
    """SSO_BACKEND and its companions are validated when settings.py is
    imported, so a misconfiguration fails the boot instead of silently
    picking a mode. A fresh interpreter is the only way to re-run that
    import, so these spawn one (via _run_in_fresh_interpreter, which also
    keeps a developer's own backend/.env from leaking values these tests
    don't explicitly pass)."""

    def _import_settings(self, **env):
        return _run_in_fresh_interpreter('import support_core.settings', **env)

    def test_default_is_local_and_needs_nothing_else(self):
        result = self._import_settings()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_unknown_backend_is_refused(self):
        result = self._import_settings(SSO_BACKEND='signed_token')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ImproperlyConfigured', result.stderr)
        self.assertIn('SSO_BACKEND', result.stderr)

    def test_code_exchange_requires_both_urls(self):
        for env, missing in (
            ({}, 'SSO_EXCHANGE_URL and SSO_PROVIDER_LOGIN_URL'),
            ({'SSO_EXCHANGE_URL': EXCHANGE_URL}, 'SSO_PROVIDER_LOGIN_URL'),
            ({'SSO_PROVIDER_LOGIN_URL': PROVIDER_LOGIN_URL}, 'SSO_EXCHANGE_URL'),
        ):
            with self.subTest(env=env):
                result = self._import_settings(SSO_BACKEND='code_exchange', **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('ImproperlyConfigured', result.stderr)
                self.assertIn(f'requires {missing} to be set', result.stderr)

    def test_code_exchange_with_both_urls_boots(self):
        result = self._import_settings(
            SSO_BACKEND='code_exchange',
            SSO_EXCHANGE_URL=EXCHANGE_URL, SSO_PROVIDER_LOGIN_URL=PROVIDER_LOGIN_URL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class MembershipAdminTests(TestCase):
    """There is no self-signup: an operator creates organisations and puts
    users in them from Django Admin, from either end."""

    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser('admin', 'admin@example.com', 'pw')
        cls.customer = User.objects.create_user('customer@example.com', 'customer@example.com', 'pw')
        cls.organisation = Organisation.objects.create(name='Riverside Dental', slug='riverside')
        Membership.objects.create(user=cls.customer, organisation=cls.organisation)

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_the_organisation_page_has_a_membership_inline(self):
        response = self.client.get(
            reverse('admin:accounts_organisation_change', args=[self.organisation.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'memberships-TOTAL_FORMS')
        self.assertContains(response, 'customer@example.com')

    def test_the_user_page_has_a_membership_inline(self):
        response = self.client.get(reverse('admin:auth_user_change', args=[self.customer.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'memberships-TOTAL_FORMS')
        self.assertContains(response, 'Riverside Dental')

    def test_an_operator_can_add_a_user_to_an_organisation_from_the_user_page(self):
        other = Organisation.objects.create(name='Hilltop Accounting', slug='hilltop')
        url = reverse('admin:auth_user_change', args=[self.customer.pk])
        existing = Membership.objects.get(user=self.customer)
        data = {
            'username': self.customer.username,
            'email': self.customer.email,
            'is_active': 'on',
            'date_joined_0': '2026-01-01', 'date_joined_1': '00:00:00',
            'initial-date_joined_0': '2026-01-01', 'initial-date_joined_1': '00:00:00',
            'memberships-TOTAL_FORMS': '2',
            'memberships-INITIAL_FORMS': '1',
            'memberships-MIN_NUM_FORMS': '0',
            'memberships-MAX_NUM_FORMS': '1000',
            'memberships-0-id': existing.pk,
            'memberships-0-user': self.customer.pk,
            'memberships-0-organisation': self.organisation.pk,
            'memberships-0-role': 'member',
            'memberships-1-user': self.customer.pk,
            'memberships-1-organisation': other.pk,
            'memberships-1-role': 'admin',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context.get('errors'))
        self.assertEqual(
            set(self.customer.memberships.values_list('organisation__slug', 'role')),
            {('riverside', 'member'), ('hilltop', 'admin')},
        )
