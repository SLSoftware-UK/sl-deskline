# SL Deskline help centre — design rationale & roadmap

Why the knowledge base (`kb/`) and the support desk (`tickets/`) are built the way they are.
Several modules cite this document by path (`kb/admin.py`, `kb/models.py`, `kb/sitemaps.py`,
`kb/markdown_utils.py`, `kb/views.py`, `support_core/settings.py`), so the section names below
are referenced from code comments — keep them stable.

## What this is (and isn't)

A public knowledge base **about your own product or service** — how-to guides,
troubleshooting, billing questions and so on — plus a login-required support ticket desk,
served by one Django project. It is not a general content-management system, and it has no
content-intake API: articles are written in the built-in editor or Django Admin.

The help centre and the ticket desk share templates, session and identity layer. They can run
on one hostname (the desk lives at `/tickets/`) or on two (e.g. `help.example.com` and
`support.example.com`) pointing at the same service.

## Why public

A public, indexable KB is discoverable by prospective customers via Google and AI search
before they ever talk to you, and it reduces support load by letting existing customers
self-serve answers. Gating the KB behind a login would trade away that discoverability for a
support-deflection benefit that public articles deliver just as well. Content that genuinely
should not be public goes in a members-only category (see "Members-only content").

## Why plain Django views, not an API + SPA

Every public-facing page must be a complete HTML document on first response. Googlebot's JS
rendering pass is a second, delayed pass; social crawlers (Facebook, X, LinkedIn) and AI
crawlers (GPTBot, PerplexityBot, ClaudeBot) don't execute JavaScript at all — a
client-rendered article would be invisible to them. Interactivity (ratings, tag filtering,
ticket replies) is layered on with htmx/`fetch()` over server-rendered HTML.

This is the whole service's rule, not just the KB's: the support desk is server-rendered
Django + htmx too, so there is one stack, one template tree and one auth story. There is no
DRF, JWT library or CORS middleware — nothing here is a cross-origin JSON client.

## SEO

- `sitemap.xml`, clean canonical URLs per article — slug-based, not ID-based, and an
  article's slug does not change when its title is edited, so inbound links keep working.
- Open Graph / Twitter card / schema.org data is server-rendered into the initial response.
  Facebook's crawler only parses roughly the first 60KB of raw HTML and runs no JS, so none of
  it can be injected client-side.
- `robots.txt` differs per host: the help host is indexable; a separate ticket host
  disallows everything (it serves the same URL space, and none of it should be indexed twice).

## Auth

Two unrelated kinds of account live in the same database:

- **Support agents** — local Django accounts with `is_staff`/`is_superuser`, created with
  `createsuperuser` or in Django Admin. They work every organisation's tickets. Superusers
  also author KB articles (`kb/decorators.py::superuser_required`).
- **Customers** — everyone else, each in zero or more organisations via
  `accounts.Membership`. Their memberships scope which tickets they see and unlock
  members-only KB categories.

How customers sign in is chosen by `SSO_BACKEND`:

- `local` (default) — plain Django auth; `/login/` is a username-or-email + password form.
  A fresh clone can `migrate`, `createsuperuser` and sign in with no external service.
- `code_exchange` — an external identity provider sends the browser to
  `/sso/callback/?code=...`; this service redeems the one-time code server-to-server at
  `SSO_EXCHANGE_URL` and creates or re-syncs a local shadow `User` plus its memberships from
  the response. Support agents keep local passwords and sign in at `/staff/login/`. See
  `accounts/sso.py` for the exchange contract.

Known simplification: an SSO user's memberships are re-synced only when they next come
through `/sso/callback/`, so someone removed from an organisation at the provider keeps that
organisation's visibility until their session expires or they log out. Acceptable while
members-only content is operational documentation rather than sensitive data — revisit if
that changes.

## Members-only content

`Category.visibility` (`public` default / `members`) — a members-only category's articles
are hidden from anonymous visitors and from signed-in users who belong to no organisation,
everywhere they could otherwise appear (listing, category nav, the featured slot, search,
`sitemap.xml`, and the direct article URL, which 404s rather than merely being unlisted) via
`kb/views.py::_published_articles`. Organisation members and support agents see them.

Independent of the `org_id` multi-tenant concept below — see that section for why the two
shouldn't be conflated.

## Moderation

Could genuinely start as Django Admin customisations rather than bespoke screens, since this
is internal-only and low-volume at first: publish/archive happen via admin actions
(`kb/admin.py`), and the in-app editor (`/articles/new/`, `/articles/manage/`) covers
day-to-day writing.

## Multi-tenant add-on (not started)

`Article`, `Category` and `Tag` carry a nullable `org_id` from the first migration — `null` is
the site's own public KB (the only thing in use). The idea is that each organisation could
one day get its own private, branded KB. It is not being pursued near-term; `org_id` stays on
the schema regardless since a nullable column costs nothing unused. If it is ever picked up,
`org_id` must be resolved server-side from the signed-in user's own memberships — never from
a form field (`kb/views.py::_authoring_org_id` is the one place to change).

Deliberately unrelated to `Category.visibility`: `org_id` would mean "this org's own private
KB, invisible to every other org", a full per-tenant split of the content set. `visibility`
means something narrower — "this public-KB article is for signed-in organisation members
only" — with no tenancy involved: every organisation's members see the same members-only
content. Don't conflate the two.

## Ratings

"Was this helpful?" — a signed-in reader gets one vote per (article, user). Anonymous readers
get one vote per (article, cookie token) (`Rating.anon_token`) — accepted as gameable in
exchange for working without an account. Anonymous voting is kept on purpose: the KB's
typical reader has no account, and requiring one to say "this wasn't helpful" would silence
precisely the readers whose confusion is most worth hearing about.

## Support CTA

Every article ends with "Couldn't find an answer? Raise a ticket", built by
`kb/views.py::_ticket_url_for` — absolute (on `TICKET_SITE_URL`) when the page is read on the
help host, relative on a ticket host — carrying the article id
(`/tickets/new/?article=<id>`), which the desk records on the ticket as `linked_article_id`.
It is shown only to signed-in accounts: `/tickets/new/` is login-required, and an
unconditional link would dead-end most of the people who see it.

## Search

The database's own full-text search, with English stemming — no Elasticsearch or embeddings,
deliberately the simpler option at this scale:

- SQLite (the default): an FTS5 index (`kb/search_index.py`), kept in sync by signals.
- PostgreSQL (optional, via `DATABASE_URL`): `django.contrib.postgres.search`.
- A SQLite build without FTS5: a plain `icontains` fallback, unstemmed.

## Roadmap

1. **Done** — public KB, search, ratings, superuser authoring, members-only categories,
   support desk, optional SSO, per-deployment branding (`SiteSettings`).
2. Accent-colour shades derived from a custom accent (today only the primary accent is
   configurable; hover/tint shades stay fixed — see `kb/static/kb/css/help.css`).
3. Multi-tenant private KBs per organisation — not started, deprioritised (see above).
