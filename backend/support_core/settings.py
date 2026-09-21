"""
SL Deskline — Django settings.

One Django service serving two halves:

  - `kb`       — the public, SEO-critical Knowledge Base (the help
                 centre). Server-rendered on purpose: social/AI crawlers
                 don't execute JS, see docs/help-center-roadmap.md.
  - `tickets`  — the login-required support desk. Server-rendered Django
                 + htmx like the KB, so there is one stack and one auth
                 story.
  - `accounts` — organisations and memberships (who a customer
                 belongs to), sign-in (plain Django auth by default, or
                 an optional SSO landing that turns an identity provider
                 login into a local shadow `User` with its memberships),
                 plus the separate username/password form support agents
                 use in SSO mode (see accounts/models.py and
                 accounts/views.py).
  - `branding` — the SiteSettings singleton (site name, logo, accent
                 colour, support/from addresses), editable in Django
                 Admin.

The help centre and the ticket desk can share one hostname or be given
one each (e.g. help.example.com and support.example.com) pointing at this
same service. URLs don't overlap (kb at the root, tickets under
/tickets/), so no host-based routing is needed beyond sending the bare
root of a ticket host to /tickets/ (support_core/host_middleware.py).

Note what is deliberately absent: no DRF, no JWT library, no CORS
middleware. Every page here is a server-rendered template and the only
cross-service call is the optional outbound server-to-server POST to an
SSO provider's code-exchange endpoint, so none of that machinery earns
its keep.
"""

import os
import sys
from pathlib import Path
import dj_database_url
import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
# SL_DESKLINE_ENV_FILE overrides which .env file gets read, defaulting to
# backend/.env as before -- this only exists so the test suite can point
# it at a file that cannot exist (os.devnull), guaranteeing a fresh
# interpreter sees no local .env at all. Without it, a developer's own
# backend/.env (e.g. one configured for SSO) would silently leak into any
# subprocess test that spawns a fresh interpreter to exercise "what does
# a clean checkout do by default" (see accounts/tests.py).
environ.Env.read_env(os.environ.get('SL_DESKLINE_ENV_FILE', os.path.join(BASE_DIR, '.env')))

# Defaults to False (not True) so a missing DEBUG env var in production
# fails closed instead of silently booting in debug mode (HSTS,
# SSL-redirect, and secure cookies all key off this). Defaulting it to True
# is the classic fail-open mistake: forget one variable and production
# serves tracebacks over plain HTTP.
DEBUG = env.bool('DEBUG', default=False)

# SECURITY WARNING: keep the secret key used in production secret!
# This service has its own SECRET_KEY, shared with nothing else — there is
# no cross-service signing here (an SSO code is never verified locally;
# see the SSO_BACKEND note below). Fail loud rather
# than silently booting on the git-committed insecure default in
# production.
SECRET_KEY = env('SECRET_KEY', default=None)
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-support-change-me-in-production'
    else:
        raise ImproperlyConfigured(
            'SECRET_KEY environment variable must be set in production.'
        )

# ALLOWED_HOSTS is env-configurable so each deployment lists its own
# hostnames without a code change. The default only covers local
# development; in production set it to every hostname this service
# answers on (the help host, the ticket host if separate, and any
# platform hostname your host's healthchecks use).
ALLOWED_HOSTS = env.list(
    'ALLOWED_HOSTS',
    default=['localhost', '127.0.0.1'],
)

# The one directory holding everything this service keeps on disk between
# deploys: the SQLite database file (DATABASES below) and uploaded media
# (MEDIA_ROOT below) both live under it. In production this is the single
# volume mounted into the container (documented as /data in
# backend/.env.example) — the whole point of the single-container,
# single-volume default is that provisioning one volume and pointing
# DATA_DIR at its mount path is the entire deployment story, with no
# separate database or object-storage service to set up. Defaults to
# BASE_DIR so a plain checkout with no DATA_DIR set still works, writing
# both under the repo itself, exactly as it did before this setting
# existed.
DATA_DIR = env.path('DATA_DIR', default=BASE_DIR)

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# --- Production HTTPS hardening ------------------------------------------
# SECURE_HSTS_SECONDS starts conservative (1 hour); raise once HTTPS is
# confirmed stable via SSL Labs, then consider
# SECURE_HSTS_INCLUDE_SUBDOMAINS/PRELOAD.
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = 3600 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

CSRF_TRUSTED_ORIGINS = env.list(
    'CSRF_TRUSTED_ORIGINS',
    default=['http://localhost:8000'],
)

# --- Sessions, optionally shared across the help and ticket hosts -------
# The same service can answer on a help-centre host and a ticket-desk
# host (TICKET_HOSTS). With SSO_BACKEND='code_exchange' there is no silent
# SSO -- the handshake is push-only and happens once, on whichever host
# the identity provider sent the user to (accounts/sso.py). Without a
# session cookie scoped to the parent domain, a user recognised on the
# help host would arrive anonymous at the ticket host and an article's
# "Raise a ticket" CTA would dead-end. Set SESSION_COOKIE_DOMAIN to the
# parent domain (e.g. '.example.com') when the two hosts differ; leave it
# unset for a single host or localhost.
#
# The distinct cookie NAME is load-bearing, not cosmetic: a cookie scoped
# to a parent domain is sent to every other host under that domain too.
# If this one were called Django's default 'sessionid', any other Django
# app on a sibling host (your main product, an admin tool) would receive
# two cookies of the same name and could read ours as its own — or
# overwrite it — logging users out of one app every time they touch the
# other. A product-specific name keeps the two sessions independent.
SESSION_COOKIE_NAME = 'deskline_sessionid'
SESSION_COOKIE_DOMAIN = env('SESSION_COOKIE_DOMAIN', default=None)  # e.g. '.example.com' in production

# Canonical origin for this site — used to build absolute URLs for
# canonical/OG tags, sitemap.xml entries and schema.org data. These must
# be absolute (not host-relative) for crawlers to resolve them correctly
# wherever the page is fetched from.
SITE_URL = env('SITE_URL', default='http://localhost:8000')

# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',
    'django.contrib.humanize',
    # Third party
    'django_htmx',
    'markdownx',
    # Local apps, in this order:
    #   accounts, branding, kb, tickets   (accounts before kb — kb's
    #   members-only gate reads accounts.Membership, and tickets scopes
    #   on it too; branding before kb/tickets — both extend
    #   kb/templates/kb/base.html, which reads the site_settings context
    #   processor registered below)
    'accounts',
    'branding',
    'kb',
    'tickets',
    # support_core itself: needs an AppConfig only for its ready() hook,
    # which wires up the SQLite WAL pragmas below (support_core/db.py).
    'support_core',
]

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
# Deliberately no "silent SSO" middleware (one that bounces anonymous
# visitors through the provider with prompt=none to see whether they are
# already signed in there). The code_exchange contract only requires the
# provider to expose a server-to-server exchange endpoint, not a
# browser-facing authorize endpoint that could be probed that way.
# Recognition is push-only — the provider's app sends the user to
# /sso/callback/?code=... itself.

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'support_core.security_middleware.SecurityHeadersMiddleware',  # CSP + Permissions-Policy
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    # <ticket host>/ -> /tickets/ (see TICKET_HOSTS below).
    'support_core.host_middleware.TicketHostRootRedirectMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
]

ROOT_URLCONF = 'support_core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # kb is installed (see INSTALLED_APPS above) and both of
                # these feed kb/templates/kb/base.html, which every page
                # in the service extends — including
                # tickets/base.html.
                #   site_url:    absolute canonical/OG URLs.
                #   tickets_url: the header's "Tickets" link, which must
                #                cross to the ticket host when the page
                #                is being served on the help host.
                'kb.context_processors.site_url',
                'kb.context_processors.tickets_url',
                #   user_organisations: the signed-in customer's
                #                organisations, shown next to their name
                #                in the header.
                'accounts.context_processors.user_organisations',
                #   site_settings: the self-hoster's SiteSettings
                #                singleton (name, logo, accent colour,
                #                support email) — see
                #                branding/context_processors.py. Does
                #                NOT reach render_to_string() for
                #                outbound email, which has no request;
                #                tickets/notifications.py passes it into
                #                that context explicitly.
                'branding.context_processors.site_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'support_core.wsgi.application'

# ---------------------------------------------------------------------------
# Database — SQLite on the volume by default; Postgres is opt-in
# ---------------------------------------------------------------------------
# The default production shape for this project is one container and one
# volume: DATA_DIR (below) is that volume's mount point, and with no
# DATABASE_URL set, dj_database_url.config()'s `default=` puts the SQLite
# file straight in it. That is enough for most deployments — no separate
# database service to provision, back up or pay for. DATABASE_URL stays
# fully supported for anyone who wants Postgres instead (a managed
# provider, or their own); set it and this switches over with no code
# changes needed on either side.
DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{DATA_DIR("db.sqlite3")}',
    )
}


def _is_postgres(db_config):
    """True when db_config (a DATABASES['default']-shaped dict) resolved
    to the Postgres backend.

    `.get()`, not `[...]`: dj_database_url.config() returns a plain `{}`
    for a *present-but-empty* DATABASE_URL (`DATABASE_URL=` with nothing
    after the `=`, as opposed to the variable being genuinely absent —
    see the django-environ trap documented in .env.example's header),
    since a blank string never reaches `default=` and there is no
    `sqlite://` URL to parse either. `{}` has no 'ENGINE' key at all, and
    this function is called while DATABASES is still being built at
    import time, so indexing with `[...]` here would crash the entire
    settings module before Django ever got a chance to raise its own,
    much clearer "settings.DATABASES is improperly configured. Please
    supply the ENGINE value" once something actually tries to use that
    connection — which is the documented behaviour .env.example promises
    for this exact case.

    A function so it can be tested — see support_core/tests.py.
    """
    return db_config.get('ENGINE') == 'django.db.backends.postgresql'


def _require_ssl(db_config):
    """Make sure a production Postgres connection asks for TLS, without
    ever *weakening* one that already asks for more.

    `setdefault`, not assignment: dj_database_url parses a `?sslmode=`
    query string out of DATABASE_URL straight into OPTIONS, so an
    operator who deliberately upgrades the connection string to
    `sslmode=verify-full` (full certificate + hostname verification, the
    only mode that actually stops a MITM) had it silently overwritten
    back down to `require` (encrypt, but verify nothing). The floor is
    `require`; anything stronger already present wins.

    A function so it can be tested — see support_core/tests.py.
    """
    db_config.setdefault('OPTIONS', {}).setdefault('sslmode', 'require')
    return db_config


def _configure_database_for_engine(db_config, *, debug):
    """Apply the settings that differ between SQLite and Postgres to
    db_config (a DATABASES['default']-shaped dict), in place, and return
    it. Kept as one function — rather than three separate module-level
    `if` blocks — so the gating itself, not just _require_ssl in
    isolation, can be exercised directly in tests without having to
    re-import support_core.settings under different environment
    variables for every combination. See support_core/tests.py.

    - CONN_MAX_AGE controls how long Django keeps a database connection
      open and reuses it across requests, instead of opening a fresh one
      every time. For SQLite that has nothing to buy: opening a
      connection to a local file is effectively free, and — more
      importantly — a long-lived connection is exactly what the WAL
      pragmas in support_core/db.py are applied to once, on open, so
      keeping a pool of already-configured connections around just adds
      bookkeeping. For Postgres a fresh TCP handshake plus TLS
      negotiation per request is genuinely expensive, so a 10-minute
      persistent connection is worth having.
    - CONN_HEALTH_CHECKS: a managed Postgres provider can silently close
      an idle connection server-side well within CONN_MAX_AGE's
      10-minute window. Without this, Django reuses the dead connection
      on the next request and psycopg2 raises "SSL connection has been
      closed unexpectedly" instead of just reconnecting. This makes
      Django check a connection's health before reusing it and
      transparently open a fresh one if it has gone. Meaningless for
      SQLite, where there is no server to close anything out from under
      the process.
    - The sslmode floor (_require_ssl) is gated on the resolved engine,
      not on "DATABASE_URL happens to be set": SQLite needs no TLS at
      all, so this only ever has anything to do once DATABASE_URL
      actually points at Postgres. Also gated on `not debug` so a
      developer running a local Postgres for parity testing isn't forced
      onto sslmode=require against a server that was never set up for
      it.
    """
    is_postgres = _is_postgres(db_config)
    db_config['CONN_MAX_AGE'] = 600 if is_postgres else 0
    if is_postgres:
        db_config['CONN_HEALTH_CHECKS'] = True
        if not debug:
            _require_ssl(db_config)
    return db_config


_configure_database_for_engine(DATABASES['default'], debug=DEBUG)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
# Two unrelated kinds of "staff" live in this database and must never be
# conflated:
#   - Customers — this desk's end users, each in zero or more
#     organisations via accounts.Membership. Their memberships scope
#     which tickets they see and open KB categories whose visibility is
#     'members'.
#   - Support agents — local Django accounts with is_staff/is_superuser,
#     created by `createsuperuser`, needing no membership. They work
#     every organisation's tickets and author KB articles.

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Named URL, not a path — resolved lazily via accounts.urls at redirect
# time. What /login/ shows depends on SSO_BACKEND below.
LOGIN_URL = 'accounts:login'

# How customers sign in (accounts/views.py, accounts/sso.py):
#   - 'local' (default): plain Django auth. /login/ is a username-or-email
#     + password form for every account; accounts are created by an
#     operator in Django Admin or with `createsuperuser`. Needs no
#     external service at all, which is what lets a fresh clone run
#     `migrate`, `createsuperuser` and sign in.
#   - 'code_exchange': customers arrive from an external identity
#     provider at /sso/callback/?code=..., and the one-time code is
#     redeemed server-to-server at SSO_EXCHANGE_URL. The code is opaque
#     here -- there is deliberately no shared secret to verify it with;
#     the exchange response is the sole source of truth for the shadow
#     User and its memberships. Support agents keep local passwords and
#     sign in at /staff/login/. See accounts/sso.py for the contract.
#
# Validated here, at import, so a typo fails the boot rather than
# silently falling back to one mode or the other. Views read the setting
# at request time, so tests can switch it with override_settings.
SSO_BACKEND = env('SSO_BACKEND', default='local')
_SSO_BACKENDS = ('local', 'code_exchange')
if SSO_BACKEND not in _SSO_BACKENDS:
    raise ImproperlyConfigured(
        f'SSO_BACKEND must be one of {", ".join(_SSO_BACKENDS)}; got {SSO_BACKEND!r}.'
    )

# code_exchange only. SSO_EXCHANGE_URL is the full URL of the provider's
# code-redemption endpoint (this service POSTs {"code": ...} to it).
# SSO_PROVIDER_LOGIN_URL is where /login/ sends a visitor to sign in at
# the provider, with ?next=<path> appended. Both are required in that
# mode -- an empty exchange URL would turn every sign-in into a
# confusing "could not reach" error, and an empty login link would
# leave /login/ with a button to nowhere.
SSO_EXCHANGE_URL = env('SSO_EXCHANGE_URL', default='')
SSO_PROVIDER_LOGIN_URL = env('SSO_PROVIDER_LOGIN_URL', default='')
if SSO_BACKEND == 'code_exchange':
    _missing = [
        name for name, value in (
            ('SSO_EXCHANGE_URL', SSO_EXCHANGE_URL),
            ('SSO_PROVIDER_LOGIN_URL', SSO_PROVIDER_LOGIN_URL),
        ) if not value
    ]
    if _missing:
        raise ImproperlyConfigured(
            f"SSO_BACKEND='code_exchange' requires {' and '.join(_missing)} to be set."
        )

# ---------------------------------------------------------------------------
# Static & Media
# ---------------------------------------------------------------------------

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
# Uploads live on the filesystem, under DATA_DIR by default so they sit on
# the same volume as the database and survive redeploys without a second
# resource to provision. MEDIA_ROOT can still be pointed elsewhere with its
# own env var if a deployment wants uploads on different storage from the
# database — but read DATA_DIR's docstring above before doing that.
# support_core/urls.py serves them in every environment.
#
# A single mounted volume attaches to exactly one container, so this
# service must stay at a single replica. That is the trade this default
# makes against object storage: one fewer resource and no presigned-URL
# machinery, in exchange for giving up horizontal scaling. For a
# knowledge base and ticket desk of this size, that is the right way
# round; a deployment that outgrows it can move MEDIA_ROOT (and, via
# DATABASE_URL, the database) onto something else without a code change.
MEDIA_ROOT = env.path('MEDIA_ROOT', default=DATA_DIR('media'))

# ---------------------------------------------------------------------------
# Markdown authoring (Article.body) — django-markdownx
# ---------------------------------------------------------------------------
# Editor widget + live preview, in Django Admin and in the in-app editor
# (kb/views.py: article_create/article_edit) alike; public rendering goes
# through the same render function (kb.markdown_utils.render_markdown) via
# MARKDOWNX_MARKDOWNIFY_FUNCTION below, so what an author sees while writing is
# exactly what a reader gets — including HTML sanitisation.
#
# Drag-and-drop image uploads from the editor land in MEDIA_ROOT/markdownx/
# (same default_storage as ArticlePhoto), separate from the structured
# ArticlePhoto/hero-photo flow.
# The upload endpoint itself (/markdownx/upload/) is our own view —
# kb/views.py::markdown_image_upload — not markdownx's stock one: the
# stock view crops every upload to a fixed 500x500 square, which is wrong
# for an inline article screenshot (needs its own aspect ratio, and to
# actually be readable). Ours reuses kb/image_processing.py's resize/
# EXIF-strip/re-encode pipeline instead, same as ArticlePhoto, so a large
# phone photo gets shrunk automatically rather than rejected or cropped.
# See support_core/urls.py and kb/static/kb/js/markdown-editor.js (which
# fixes the upload widget's own error-message UX, a separate bug from the
# crop behaviour) for the rest of this story.
MARKDOWNX_MARKDOWN_EXTENSIONS = ['extra', 'nl2br', 'sane_lists']
MARKDOWNX_MARKDOWNIFY_FUNCTION = 'kb.markdown_utils.render_markdown'

# Max upload size for article photos and inline markdown images (bytes).
# Read by kb/validators.py (the ArticlePhoto.image field validator) and by
# kb/views.py::markdown_image_upload — both call sites must read it from
# here rather than hardcoding a constant, or setting the env var silently
# does nothing. Defaults to 25MB.
MAX_PHOTO_UPLOAD_SIZE = env.int('MAX_PHOTO_UPLOAD_SIZE', default=25 * 1024 * 1024)  # 25MB

# Housekeeping: after every article create/edit/delete, delete any file
# under markdownx/ in default_storage that no Article.body references any
# more (kb/image_cleanup.py).
#
# DEFAULT False, AND IT MUST STAY FALSE unless you know storage holds
# nothing this database has never seen. The purge derives "still in use"
# from Article.objects in *this* database and deletes everything else it
# finds in storage. So if MEDIA_ROOT is ever pointed at storage that
# already has content from elsewhere (a previous install, a restored
# backup whose database was not restored with it, another site sharing
# the directory), the very first article saved would delete every inline
# image this database doesn't know about, reported only as "…and 40
# orphaned images cleaned up".
#
# Turning it on is safe once storage holds only what this database knows
# about. It is a
# housekeeping nicety (it stops unused uploads accumulating), never a
# correctness requirement: with it off, orphaned files simply stay.
KB_PURGE_ORPHANED_IMAGES = env.bool('KB_PURGE_ORPHANED_IMAGES', default=False)

# Configure storage backends through this dict and nothing else. Django 5.1
# removed the DEFAULT_FILE_STORAGE/STATICFILES_STORAGE backward-compat shim
# outright (4.2 deprecated them in favour of this dict and kept translating
# the old settings for a while; that translation is gone), so setting either
# of those names here would be silently ignored rather than raise — which is
# exactly how a deployment ends up writing uploads to the container's
# ephemeral disk while believing they are going to a bucket. The default
# is the filesystem (see MEDIA_ROOT above), but the trap is the same for
# any future backend change.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# The test runner forces DEBUG=False, which makes the manifest storage
# demand a collectstatic manifest that a test run never has.
if len(sys.argv) > 1 and sys.argv[1] == 'test':
    STORAGES['staticfiles'] = {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------

LANGUAGE_CODE = 'en-gb'
TIME_ZONE = 'Europe/London'
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Email (tickets app notifications)
# ---------------------------------------------------------------------------
# Under DEBUG, mail goes to the console. Otherwise the default is Django's
# plain SMTP backend, configured with the standard EMAIL_HOST / EMAIL_PORT
# / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD / EMAIL_USE_TLS variables —
# what most self-hosters already have. The exception is a host that
# blocks outbound SMTP ports (some PaaS plans do): setting SMTP2GO_API_KEY
# switches the default to tickets.email_backend.SMTP2GOBackend, which
# sends through SMTP2GO's HTTPS API instead. An explicit EMAIL_BACKEND
# always wins over both.
# Tests override this to the locmem backend and assert on
# django.core.mail.outbox; see tickets/tests.py.
SMTP2GO_API_KEY = env('SMTP2GO_API_KEY', default='')


def _default_email_backend(*, debug, smtp2go_api_key):
    """The EMAIL_BACKEND used when the env var is absent. A function so
    the choice can be tested without re-importing settings — see
    support_core/tests.py."""
    if debug:
        return 'django.core.mail.backends.console.EmailBackend'
    if smtp2go_api_key:
        return 'tickets.email_backend.SMTP2GOBackend'
    return 'django.core.mail.backends.smtp.EmailBackend'


EMAIL_BACKEND = env(
    'EMAIL_BACKEND',
    default=_default_email_backend(debug=DEBUG, smtp2go_api_key=SMTP2GO_API_KEY),
)
EMAIL_HOST = env('EMAIL_HOST', default='localhost')
EMAIL_PORT = env.int('EMAIL_PORT', default=25)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=False)
# Seconds before an SMTP connection/send gives up. Django's own default is
# no timeout at all, and notifications are sent synchronously inside the
# request that saved the ticket: on a host that silently drops outbound
# SMTP, connect() would block for minutes, gunicorn would kill the worker
# (--timeout 120), the user would see a 502 for a ticket that WAS saved,
# and a resubmit would create a duplicate. A short timeout turns that
# into a logged send failure instead (tickets/notifications.py never
# lets a failed email break the workflow).
EMAIL_TIMEOUT = env.int('EMAIL_TIMEOUT', default=10)
# Fallback sender. The SiteSettings singleton's notification from-address
# (branding app, editable in Django Admin) takes precedence when set.
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', default='noreply@localhost')

# ---------------------------------------------------------------------------
# Cache — currently only the /staff/login/ throttle
# ---------------------------------------------------------------------------
# Django's own Redis backend (django.core.cache.backends.redis, built in
# since 4.0), not django-redis: nothing here needs the extra features
# that package exists for. It does need redis-py as its client library,
# which is the one line added to requirements.txt for this.
#
# REDIS_URL is the standard connection string most hosted Redis offerings
# hand you. If you already run a Redis for another app, pointing this
# service at it (ideally a separate database number) is enough; it does
# not need its own instance. Set it in production.
#
# Without it this falls back to LocMemCache, which is PER PROCESS: with
# gunicorn's 2 workers, a throttle counter kept there is effectively
# halved (an attacker's attempts land in whichever worker takes the
# request), and it is wiped on every deploy. It is enough to keep the
# suite and local dev honest, and better than nothing in production, but
# it is not the real thing — set REDIS_URL.
REDIS_URL = env('REDIS_URL', default='')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
    } if REDIS_URL else {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'deskline-locmem',
    }
}

# Password sign-in brute-force throttle (accounts/views.py) — /login/ in
# local mode, /staff/login/ in SSO mode. Either way the form is the door
# to support-agent accounts, which see every organisation's tickets and
# hold Django Admin plus KB authoring, and it is a plain HTML form at a
# guessable URL, so it needs a lockout of its own.
#
# Counted per (client IP, submitted identifier). Small numbers on
# purpose: people know their own passwords, so five tries is generous,
# and fifteen minutes is short enough that a locked-out user is not
# stuck for the afternoon.
LOGIN_MAX_ATTEMPTS = env.int('LOGIN_MAX_ATTEMPTS', default=5)
LOGIN_LOCKOUT_SECONDS = env.int('LOGIN_LOCKOUT_SECONDS', default=15 * 60)

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Hostnames that are "the ticket desk" rather than "the help centre". A
# host-only distinction — the URL space itself is shared. Two things key
# off it, both through support_core/host_middleware.py::is_ticket_host:
# that module's own root redirect to /tickets/, and
# kb/context_processors.py::tickets_url, which uses it to choose between
# a relative path and an absolute URL built on TICKET_SITE_URL for the
# header's "Tickets" link and the article "Raise a ticket" CTA
# (kb/views.py::_ticket_url_for builds the CTA from that same helper).
# Empty by default: a single-host deployment serves the ticket desk at
# /tickets/ on the same hostname as the help centre, with no redirect.
TICKET_HOSTS = env.list('TICKET_HOSTS', default=[])

# Absolute origin for ticket links in outbound emails (an email has no
# request to build an absolute URL from) and for cross-host links from
# the KB, which the same service may serve on a different hostname.
# Defaults to SITE_URL, i.e. the ticket desk lives on the same origin as
# the help centre; set it when TICKET_HOSTS names a separate host.
TICKET_SITE_URL = env('TICKET_SITE_URL', default=SITE_URL)
