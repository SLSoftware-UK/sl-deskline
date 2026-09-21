"""
Tests for the project-level settings/middleware glue that has no app of
its own. Host routing itself is covered in tickets/tests.py
(TicketHostRootRedirectTests), next to the app it redirects into.
"""
import os
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import dj_database_url
from django.conf import settings
from django.test import SimpleTestCase

from support_core.db import configure_sqlite
from support_core.settings import (
    _configure_database_for_engine, _default_email_backend, _is_postgres, _require_ssl,
)


class IsPostgresTests(SimpleTestCase):
    """support_core/settings.py::_is_postgres.

    F-fix: DATABASE_URL="" (present but empty, as opposed to genuinely
    unset) makes dj_database_url.config() return a plain `{}` — no
    'ENGINE' key at all — because a blank string never reaches its
    `default=` and there is no URL to parse either (see the
    django-environ trap documented in .env.example's header). Indexing
    that dict with `DATABASES['default']['ENGINE']` used to crash the
    whole settings module at import time with a KeyError, before Django
    ever got a chance to raise its own clearer "settings.DATABASES is
    improperly configured" error once something actually tried to use
    the connection. `_is_postgres` must survive exactly that shape."""

    def test_true_for_the_postgres_engine(self):
        config = dj_database_url.parse('postgres://u:p@host/db')

        self.assertTrue(_is_postgres(config))

    def test_false_for_the_sqlite_engine(self):
        config = dj_database_url.parse('sqlite:///db.sqlite3')

        self.assertFalse(_is_postgres(config))

    def test_false_rather_than_a_keyerror_for_an_empty_config(self):
        # What dj_database_url.config() returns for a present-but-empty
        # DATABASE_URL — the regression case above.
        self.assertFalse(_is_postgres({}))


class ConfigureDatabaseForEngineTests(SimpleTestCase):
    """support_core/settings.py::_configure_database_for_engine — the
    CONN_MAX_AGE / CONN_HEALTH_CHECKS / sslmode-floor gating that must
    only ever do anything for Postgres, never for SQLite. Tested as its
    own function (same reasoning as _require_ssl below) rather than only
    through the two module-level DATABASES globals it produces, so each
    case doesn't require re-importing support_core.settings under a
    different DATABASE_URL/DEBUG for every combination."""

    def test_sqlite_gets_conn_max_age_zero_and_nothing_else(self):
        config = dj_database_url.parse('sqlite:///db.sqlite3')

        _configure_database_for_engine(config, debug=False)

        self.assertEqual(config['CONN_MAX_AGE'], 0)
        # dj_database_url.parse() already puts CONN_HEALTH_CHECKS: False
        # in every config it returns (its own default) — the point here
        # is that _configure_database_for_engine never turns it *on* for
        # SQLite, not that the key is absent.
        self.assertFalse(config.get('CONN_HEALTH_CHECKS'))
        self.assertNotIn('OPTIONS', config)  # _require_ssl never ran

    def test_postgres_gets_conn_max_age_600_and_health_checks(self):
        config = dj_database_url.parse('postgres://u:p@host/db')

        _configure_database_for_engine(config, debug=False)

        self.assertEqual(config['CONN_MAX_AGE'], 600)
        self.assertTrue(config['CONN_HEALTH_CHECKS'])

    def test_postgres_gets_the_sslmode_floor_when_not_debug(self):
        config = dj_database_url.parse('postgres://u:p@host/db')

        _configure_database_for_engine(config, debug=False)

        self.assertEqual(config['OPTIONS']['sslmode'], 'require')

    def test_postgres_sslmode_floor_is_skipped_under_debug(self):
        # A developer running a local Postgres for parity testing
        # shouldn't be forced onto sslmode=require against a server that
        # was never set up for it.
        config = dj_database_url.parse('postgres://u:p@host/db')

        _configure_database_for_engine(config, debug=True)

        self.assertNotIn('OPTIONS', config)

    def test_an_empty_config_is_treated_as_sqlite_not_postgres(self):
        # The present-but-empty DATABASE_URL regression case again: must
        # not crash, and must not be mistaken for a Postgres connection
        # that then gets CONN_HEALTH_CHECKS/sslmode applied to it.
        config = {}

        _configure_database_for_engine(config, debug=False)

        self.assertEqual(config['CONN_MAX_AGE'], 0)
        self.assertNotIn('CONN_HEALTH_CHECKS', config)
        self.assertNotIn('OPTIONS', config)


class DatabaseSslModeTests(SimpleTestCase):
    """The production branch used to assign
    `OPTIONS['sslmode'] = 'require'` unconditionally, which silently
    downgraded a stronger mode that arrived in DATABASE_URL's query
    string. `require` encrypts but verifies nothing; `verify-full` is
    the only mode that checks the certificate and the hostname, i.e. the
    only one that stops a MITM. An operator who upgrades the connection
    string should get what they asked for."""

    def test_a_stronger_mode_from_the_url_survives(self):
        config = dj_database_url.parse(
            'postgres://u:p@db.example.neon.tech/support?sslmode=verify-full'
        )
        # Precondition: dj_database_url really does parse it into OPTIONS.
        self.assertEqual(config['OPTIONS']['sslmode'], 'verify-full')

        _require_ssl(config)

        self.assertEqual(config['OPTIONS']['sslmode'], 'verify-full')

    def test_require_is_still_the_floor_when_the_url_says_nothing(self):
        config = dj_database_url.parse('postgres://u:p@db.example.neon.tech/support')

        _require_ssl(config)

        self.assertEqual(config['OPTIONS']['sslmode'], 'require')

    def test_a_weaker_mode_is_left_alone_rather_than_silently_forced(self):
        """Deliberately asked for, deliberately honoured — the point of
        setdefault is that the connection string is the operator's
        decision, not something this module second-guesses in either
        direction. Recorded as a test so the behaviour is a choice
        rather than a side effect."""
        config = dj_database_url.parse(
            'postgres://u:p@localhost/support?sslmode=disable'
        )

        _require_ssl(config)

        self.assertEqual(config['OPTIONS']['sslmode'], 'disable')


class CacheConfigurationTests(SimpleTestCase):
    """The sign-in throttle's supporting setting. The throttle is only as good as the
    cache behind it, so assert the fallback is configured rather than
    absent — a missing CACHES would leave Django's default LocMemCache
    working by accident, and this service should say what it means."""

    def test_a_default_cache_is_configured(self):
        self.assertIn('default', settings.CACHES)
        self.assertTrue(settings.CACHES['default']['BACKEND'])

    def test_the_fallback_is_locmem_and_not_the_dummy_backend(self):
        # Dummy would make every throttle counter a no-op and the
        # lockout would silently never fire.
        self.assertNotIn('dummy', settings.CACHES['default']['BACKEND'].lower())


class SessionCookieNameTests(SimpleTestCase):
    """The session cookie may be scoped to a parent domain shared with
    other apps (SESSION_COOKIE_DOMAIN), so its name must be specific to
    this product rather than Django's default 'sessionid', or it would
    collide with any sibling Django app's session."""

    def test_session_cookie_name_is_product_specific(self):
        self.assertEqual(settings.SESSION_COOKIE_NAME, 'deskline_sessionid')


class DefaultEmailBackendTests(SimpleTestCase):
    """What EMAIL_BACKEND falls back to when the env var is absent.
    Plain SMTP is the non-DEBUG default because that is what most
    self-hosters have; SMTP2GO only when its API key is configured."""

    def test_debug_uses_the_console(self):
        self.assertEqual(
            _default_email_backend(debug=True, smtp2go_api_key='key'),
            'django.core.mail.backends.console.EmailBackend',
        )

    def test_production_without_smtp2go_key_uses_plain_smtp(self):
        self.assertEqual(
            _default_email_backend(debug=False, smtp2go_api_key=''),
            'django.core.mail.backends.smtp.EmailBackend',
        )

    def test_production_with_smtp2go_key_uses_smtp2go(self):
        self.assertEqual(
            _default_email_backend(debug=False, smtp2go_api_key='api-123'),
            'tickets.email_backend.SMTP2GOBackend',
        )


class EmailTimeoutTests(SimpleTestCase):
    """Notifications are sent synchronously in the request that saved
    the ticket, so an SMTP send must not be able to hang past gunicorn's
    worker timeout (Django's own default is no timeout at all)."""

    def test_email_timeout_defaults_to_ten_seconds(self):
        self.assertEqual(settings.EMAIL_TIMEOUT, 10)


class ConfigureSqliteTests(SimpleTestCase):
    """support_core/db.py::configure_sqlite, the connection_created
    receiver that puts a SQLite connection into WAL mode.

    Django's own SQLite test database runs in memory (`sqlite://:memory:`),
    where `PRAGMA journal_mode` always reports `memory` — WAL is a
    file-journalling mode and has nothing to attach to for an in-memory
    database, so asserting against `settings.DATABASES['default']`'s live
    connection here would not exercise the pragmas at all. Instead these
    tests call the receiver directly against a real on-disk SQLite file,
    which is exactly the shape it runs against in production.
    """

    def _connection_to_a_real_sqlite_file(self):
        fd, path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        raw = sqlite3.connect(path)
        self.addCleanup(raw.close)
        # A minimal stand-in for Django's connection wrapper: the receiver
        # only ever touches `.vendor` and `.cursor()`, and a real
        # sqlite3.Connection's `.cursor()` behaves the same way Django's
        # does for the plain `execute()`/`close()` calls configure_sqlite
        # makes.
        return SimpleNamespace(vendor='sqlite', cursor=raw.cursor)

    def _pragma(self, raw, name):
        cursor = raw.cursor()
        cursor.execute(f'PRAGMA {name};')
        value = cursor.fetchone()[0]
        cursor.close()
        return value

    def test_sets_wal_journal_mode_and_busy_timeout(self):
        connection = self._connection_to_a_real_sqlite_file()
        raw = connection.cursor.__self__  # the underlying sqlite3.Connection

        configure_sqlite(sender=None, connection=connection)

        self.assertEqual(self._pragma(raw, 'journal_mode').lower(), 'wal')
        self.assertEqual(self._pragma(raw, 'busy_timeout'), 5000)

    def test_sets_synchronous_normal_and_foreign_keys_on(self):
        connection = self._connection_to_a_real_sqlite_file()
        raw = connection.cursor.__self__

        configure_sqlite(sender=None, connection=connection)

        # SQLite reports synchronous=NORMAL as 1 and foreign_keys=ON as 1
        # via PRAGMA — there is no textual form for either.
        self.assertEqual(self._pragma(raw, 'synchronous'), 1)
        self.assertEqual(self._pragma(raw, 'foreign_keys'), 1)

    def test_is_a_no_op_for_a_non_sqlite_vendor(self):
        # Postgres has its own MVCC and row-level locking and needs none
        # of this; the receiver must not even try to open a cursor on a
        # connection it has no business touching.
        connection = Mock()
        connection.vendor = 'postgresql'

        configure_sqlite(sender=None, connection=connection)

        connection.cursor.assert_not_called()
