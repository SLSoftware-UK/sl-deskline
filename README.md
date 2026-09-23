# SL Deskline

SL Deskline is a self-hosted knowledge base and support ticket desk in one Django service. The knowledge base is public, server-rendered and search-engine friendly: categories, tags, full-text search, "Was this helpful?" ratings, SEO metadata and a Markdown editor with image uploads. The ticket desk sits behind a login: customers in one or more organisations raise tickets, and your support agents work the queue, with email notifications both ways. Sign-in is plain Django auth out of the box, with an optional code-exchange SSO hook for plugging it into your own product's accounts. There is no JavaScript build step: pages are Django templates plus a little htmx.

**See it live:** the knowledge-base side runs in production at [help.telosgym.com](https://help.telosgym.com/).

## Self-hosting: one container, one volume

The default deployment is a single container with a single volume attached, and nothing else to provision:

- **SQLite in WAL mode, plus uploads, together under `DATA_DIR`.** The database lives at `DATA_DIR/db.sqlite3` and uploaded images at `DATA_DIR/media`. Every new SQLite connection is set to `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL` and `foreign_keys=ON` (`backend/support_core/db.py`), so the container's two gunicorn workers can read while one writes instead of hitting "database is locked".
- **No external database, object storage or Redis required.** Uploaded media is served by the app itself from `MEDIA_ROOT`, and knowledge-base search uses SQLite's built-in FTS5 index.
- **Single replica.** A volume attaches to one container, so run exactly one instance of the service.
- **Backup = copy the volume.** Everything stateful is under `DATA_DIR`. For a consistent copy of a live SQLite database, stop the service first or use `sqlite3 db.sqlite3 ".backup backup.sqlite3"`.
- **Postgres and Redis are optional.** Set `DATABASE_URL` to use Postgres instead of SQLite (search then uses Postgres full-text search), and `REDIS_URL` to keep the sign-in throttle's counters in Redis rather than in each worker's memory.

The container (`backend/Dockerfile`) is `python:3.12-slim`. It runs `collectstatic` at build time and, on start, `python manage.py migrate` followed by gunicorn on `support_core.wsgi` with 2 workers, a 120-second timeout and `--bind 0.0.0.0:${PORT:-8000}`.

### Railway recipe

1. Create a service from this repo and set its root directory to `/backend`. `backend/railway.json` selects the Dockerfile builder.
2. Attach a volume to the service, mounted at `/data`.
3. Set these variables:
   - `DATA_DIR=/data`
   - `SECRET_KEY` — a long random string
   - `ALLOWED_HOSTS` — your hostname(s), e.g. `help.example.com`
   - `CSRF_TRUSTED_ORIGINS` — the matching origin(s), e.g. `https://help.example.com`
   - `SITE_URL` — your canonical origin, e.g. `https://help.example.com`
4. Deploy, then create your first account from the service's shell: `python manage.py createsuperuser`.

Leave `DEBUG` unset in production: it defaults to `False`. Configure outbound email (see [Configuration](#configuration)) if you want ticket notifications to be delivered. Any other container host follows the same recipe: build `backend/`, mount a volume, point `DATA_DIR` at it. Whichever host you use, put it behind a TLS-terminating reverse proxy that sets (or, if the request came from outside, overwrites) `X-Forwarded-Proto` — `SECURE_PROXY_SSL_HEADER` trusts that header unconditionally (`backend/support_core/settings.py`), and Railway and most PaaS hosts do this for you already, but a container exposed directly with no such proxy in front would let a client-supplied header spoof HTTPS.

## Quickstart

Local development needs Python 3.12.

```bash
git clone https://github.com/SLSoftware-UK/sl-deskline.git
cd sl-deskline/backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows (cmd): copy .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then open <http://localhost:8000/login/> and sign in with the superuser's username (or email) and password. Go to <http://localhost:8000/admin/> and open **Site settings** to set your site name, logo, accent colour and support addresses.

The copied `.env` holds local values: `DEBUG=True`, SQLite in `backend/` (no `DATA_DIR` or `DATABASE_URL` set), and email printed to the console instead of sent. `DEBUG` defaults to `False`, so without `.env` (or `DEBUG=True` and a `SECRET_KEY` in the environment) `manage.py` refuses to start. Keep `SESSION_COOKIE_DOMAIN` blank locally: a browser will not store a parent-domain cookie on `localhost`, so sign-in would silently fail.

**There is no self-signup.** Every account is created by an operator, with `createsuperuser` or in Django Admin. To try the customer side, create an ordinary (non-staff) user in Django Admin, create an **Organisation**, and add the user to it via the Membership inline (on either the Organisation or the User page). That user signs in at `/login/` too.

To run the tests:

```bash
cd backend
DEBUG=True python manage.py test
```

On Windows PowerShell, set the variable first: `$env:DEBUG = "True"; python manage.py test`. `DEBUG=True` is only needed so settings load without a `SECRET_KEY`; Django's test runner runs the tests themselves with `DEBUG=False`.

## Features

### Knowledge base (public, no login needed)

- Articles grouped into **categories** and labelled with **tags**. The home page shows one section per category (its newest 6 articles plus a "See all" link); any search, category or tag filter switches to a flat list paginated at 12. Categories are ordered by an editable sort order, then by name.
- **Search**: stemmed full-text search across title, summary and body, with title matches ranked above summary above body and every word required. On SQLite it uses an FTS5 index (Porter stemming, bm25 column weights 1.0 / 0.4 / 0.2), kept in sync automatically and rebuildable with `python manage.py rebuild_search_index`. On Postgres it uses Postgres full-text search with the same weighting. A SQLite build without FTS5 falls back to a simple substring match.
- **Ratings**: "Was this helpful?" on every article, open to anonymous readers. One vote per article per reader, keyed to the account when signed in and to a cookie otherwise. Readers can add an optional comment after voting; admins can switch ratings and the helpful count off, and read comments at `/feedback/` (see [Reader feedback](#reader-feedback)).
- **Members-only categories**: a category can be `public` or `members`. Members-only articles are hidden from listings, navigation, the featured slot and the sitemap, and 404 on a direct URL, unless the reader is signed in and belongs to at least one organisation, or is a support agent.
- **SEO**: per-article meta description, canonical URLs, Open Graph tags, `sitemap.xml` (members-only articles excluded), a host-aware `robots.txt`, and schema.org `HowTo` structured data for articles with numbered steps.
- **Authoring**: an in-app Markdown editor with live preview (`/articles/new/`, `/articles/manage/`) and category/tag management at `/taxonomy/`, for superusers. Drag-and-drop image uploads are resized, EXIF-stripped and re-encoded rather than cropped. Articles can also carry numbered how-to steps and photos (edited in Django Admin), and Django Admin handles featured articles and product showcase cards.
- **"Raise a ticket from this article"**: signed-in readers see a "Couldn't find an answer? Raise a ticket" link on each article, which opens the ticket form and records which article they came from.

### Ticket desk (`/tickets/`, login required, never indexed)

- **Organisations**: a customer can belong to several organisations. They see the tickets they raised plus every ticket filed under any organisation they belong to, and choose the organisation when raising a ticket if they belong to more than one. Support agents see every ticket.
- **Urgency**: low, normal or high, set when the ticket is raised.
- **Statuses**: open, in progress, waiting on customer, resolved. A customer's reply to a "waiting on customer" ticket moves it back to "in progress". Resolved tickets have their own list.
- **Assignment**: support agents change status and assign tickets to agents; customers cannot.
- One message thread per ticket, with htmx replies. The open list can be filtered by status and sorted by subject, organisation (agents only), urgency or last update.
- **Email notifications**: customers get a confirmation when they raise a ticket, plus emails for agent replies and resolution; agents get emails for new tickets, customer replies and assignments. Sent over SMTP by default, or through SMTP2GO's HTTPS API for hosts that block outbound SMTP.

Not built: attachments on ticket messages.

## Reader feedback

Each article ends with "Was this article helpful?" 👍 / 👎. Once a reader votes, the buttons are replaced by an optional "Anything to add?" box (up to 1,000 characters). The comment is saved on that reader's own vote, and only once.

Superusers get two sidebar pages:

- **Settings** (`/settings/`, `kb.models.KBSettings`, a single row): switch the rating widget off (this also refuses new votes; existing ones are kept) and hide "X found this helpful" on article cards. Both are on by default.
- **Feedback** (`/feedback/`): 👍/👎 totals, then every vote that has a comment, newest first, showing the article and who left it (or Anonymous). "All votes" also shows the votes without a comment. Ratings are also in Django Admin, where comments are searchable.

## Authentication

Selected by `SSO_BACKEND`. Any value other than `local` or `code_exchange` stops the service from starting.

Two kinds of account share one user table, and Django's `is_staff` flag is the only thing that tells them apart:

- **Customers**: accounts without `is_staff`, in zero or more organisations. They raise tickets. Holding at least one membership opens members-only knowledge-base categories.
- **Support agents**: `is_staff` accounts. They work every ticket, need no memberships, and cannot raise tickets themselves. Superusers can also author knowledge-base articles and use Django Admin.

### `local` (default)

Plain Django auth. `/login/` is a username-or-email and password form for every account, customers and agents alike. `/staff/login/` is a temporary (302) redirect to `/login/`, and `/sso/callback/` returns 404. Password sign-in is throttled per client IP and submitted identifier (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_SECONDS`). `POST /logout/` ends the session.

### `code_exchange` (optional SSO)

For putting the desk behind your own product's accounts. Customers never get a password here; your identity provider vouches for them with a one-time code. Set `SSO_EXCHANGE_URL` and `SSO_PROVIDER_LOGIN_URL` too; the service will not start without both.

1. A user who is signed in to your product clicks "Help" or "Support". Your product mints a short-lived, single-use code and sends the browser to `https://<desk>/sso/callback/?code=<code>&next=<local path>` (`sso_code` is accepted as an alias for `code`; `next` is optional and must be a local path).
2. Deskline redeems the code server-to-server:

   ```http
   POST {SSO_EXCHANGE_URL}
   Content-Type: application/json

   {"code": "<the code from the callback URL>"}
   ```

3. Your endpoint answers HTTP 200 with:

   ```json
   {
     "user": {
       "email": "dana@example.com",
       "first_name": "Dana",
       "last_name": "Owner",
       "organisations": [
         {"id": 42, "name": "Northside Dental", "slug": "northside-dental", "role": "owner"}
       ]
     }
   }
   ```

Contract details:

- The code must be short-lived and single-use: Deskline never initiates this handshake, it only redeems whatever code arrives at `/sso/callback/`, so a code your identity provider would still accept a second time (or long after issuing) lets an attacker sign a victim into the *attacker's* account by getting them to open a callback URL carrying the attacker's own code (a login CSRF). Expire and consume the code on first redemption.
- Any status other than 200 is treated as an invalid or expired code. Extra top-level keys are ignored.
- `email` is required and becomes the local user's username. `first_name`, `last_name` and `organisations` are optional; a missing or empty `organisations` list is allowed.
- `organisations[].id` is your id for the organisation. It is stored as a string (at most 64 characters) in `Organisation.external_id` and used to match the same organisation on later sign-ins. `role` is `owner`, `admin` or `member`; anything else is stored as `member`.
- A malformed body (wrong types, an organisation that is not an object, a missing or over-long id) refuses the whole sign-in and writes nothing.
- **Memberships are a full replace, scoped to provider-managed organisations.** On every sign-in the user's memberships in organisations that have an `external_id` are replaced by what the response lists. Memberships in organisations an operator created by hand in Django Admin (no `external_id`) are never touched. Organisations are created and updated, never deleted.
- **`is_staff` guard:** an SSO sign-in never authenticates as, or modifies, an existing `is_staff` or superuser account; it is refused instead.
- The code is opaque to Deskline. There is no shared secret and nothing is verified locally: the exchange response is the sole source of identity.

In this mode `/login/` is an informational page linking to `SSO_PROVIDER_LOGIN_URL` (with `?next=` appended), and support agents sign in with their local passwords at `/staff/login/`, which accepts `is_staff` accounts only. `POST /logout/` ends the Deskline session only, not the provider's. Django Admin's own login works the same in both modes.

## Branding

Branding is held in a single **Site settings** record, edited in Django Admin (`/admin/`, under Branding). Nothing needs a code or template change.

| Field | Where it shows up |
|---|---|
| Site name | Header, page titles, Open Graph site name, footer, email subjects and sign-offs |
| Logo | Header, in place of the site-name text |
| Accent colour | Links, buttons and other accents (`#rrggbb`) |
| Support email | Page footer and outbound email, when set |
| Notification from-address | From-address for ticket email; falls back to `DEFAULT_FROM_EMAIL` when blank |

## Configuration

Every environment variable `backend/support_core/settings.py` reads. `backend/.env.example` lists the same set with local values and notes. A variable that is set but empty is not the same as one that is absent: the default only applies when the variable is absent, so comment out anything you do not need rather than blanking it.

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | none (required unless `DEBUG`) | Django signing key. The service refuses to start without it in production. |
| `DEBUG` | `False` | Debug mode. Leave unset in production; HTTPS redirect, HSTS and secure cookies key off it. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated hostnames the service answers on. |
| `CSRF_TRUSTED_ORIGINS` | `http://localhost:8000` | Comma-separated origins trusted for form posts, e.g. `https://help.example.com`. |
| `DATA_DIR` | `backend/` directory | Where the SQLite database and uploads live. Point it at your volume (`/data`). |
| `DATABASE_URL` | unset (SQLite at `DATA_DIR/db.sqlite3`) | Postgres connection string, to use Postgres instead of SQLite. |
| `MEDIA_ROOT` | `DATA_DIR/media` | Upload directory, if it should differ from `DATA_DIR/media`. |
| `SITE_URL` | `http://localhost:8000` | Canonical origin for canonical/Open Graph URLs, the sitemap and schema.org data. |
| `SESSION_COOKIE_DOMAIN` | unset | Parent domain for the session cookie (e.g. `.example.com`) when the help centre and ticket desk use different hostnames. Leave blank for one host and locally. |
| `TICKET_HOSTS` | empty | Comma-separated hostnames treated as the ticket desk; a bare `/` there redirects to `/tickets/`. |
| `TICKET_SITE_URL` | value of `SITE_URL` | Origin for ticket links in email and for cross-host links from the knowledge base to the desk. |
| `SSO_BACKEND` | `local` | `local` or `code_exchange`. See [Authentication](#authentication). |
| `SSO_EXCHANGE_URL` | empty | `code_exchange` only (required): full URL of the provider's code-redemption endpoint. |
| `SSO_PROVIDER_LOGIN_URL` | empty | `code_exchange` only (required): where `/login/` sends people to sign in at the provider. |
| `EMAIL_BACKEND` | console if `DEBUG`; SMTP2GO if `SMTP2GO_API_KEY` is set; otherwise SMTP | Django email backend, to override that choice. |
| `EMAIL_HOST` | `localhost` | SMTP host. |
| `EMAIL_PORT` | `25` | SMTP port. |
| `EMAIL_HOST_USER` | empty | SMTP username. |
| `EMAIL_HOST_PASSWORD` | empty | SMTP password. |
| `EMAIL_USE_TLS` | `False` | Use STARTTLS for SMTP. |
| `EMAIL_TIMEOUT` | `10` | Seconds before an SMTP send gives up. Notifications are sent inside the request, so keep it well under gunicorn's 120-second timeout. |
| `SMTP2GO_API_KEY` | empty | Send email through SMTP2GO's HTTPS API instead of SMTP, for hosts that block outbound SMTP ports. |
| `DEFAULT_FROM_EMAIL` | `noreply@localhost` | Fallback sender; the Site settings from-address wins when set. |
| `REDIS_URL` | empty (per-process memory cache) | Redis for the cache, which holds the sign-in throttle counters. Recommended in production, since the in-memory fallback is per worker and reset on every deploy. |
| `LOGIN_MAX_ATTEMPTS` | `5` | Failed password attempts per (IP, identifier) before a lockout. |
| `LOGIN_LOCKOUT_SECONDS` | `900` | Lockout length in seconds. |
| `MAX_PHOTO_UPLOAD_SIZE` | `26214400` (25 MB) | Maximum size in bytes for article photos and inline Markdown images. |
| `KB_PURGE_ORPHANED_IMAGES` | `False` | After each article save or delete, delete uploaded Markdown images no article references any more. Only turn it on when the media directory holds nothing but this database's uploads. |

`PORT` is read by the container's start command (gunicorn binds to `${PORT:-8000}`), not by Django.

## Project layout

| Path | What |
|---|---|
| `backend/support_core/` | Settings, root URLs, security headers, SQLite tuning, ticket-host root redirect, health endpoint (`/api/health/`) |
| `backend/accounts/` | Organisations and memberships, sign-in (local and `code_exchange` SSO), the sign-in throttle |
| `backend/branding/` | The Site settings record and its context processor |
| `backend/kb/` | Knowledge base: models, public pages, search, ratings, authoring, sitemap, robots.txt |
| `backend/tickets/` | Ticket desk: models, views, htmx templates, email notifications |
| `docs/help-center-roadmap.md` | Design rationale for the knowledge base and desk |
| [llms.txt](llms.txt) | A fuller description of the app and its design decisions |

## License

MIT. See [LICENSE](LICENSE).

## Support

SL Deskline is provided as-is. It is community-supported through [GitHub issues](https://github.com/SLSoftware-UK/sl-deskline/issues): bug reports, questions and pull requests are welcome there. There is no SLA, and there is no guarantee that a reported issue will be fixed.
