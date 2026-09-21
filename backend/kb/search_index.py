"""
Full-text search index for KB articles on SQLite, using SQLite's
built-in FTS5 extension.

Why this exists: the default deployment is a single container with a
single volume holding a SQLite database. Plain `icontains` matching on
SQLite is literal substring matching only -- a search for "summaries"
misses an article that says "Summary", because neither string contains
the other. Getting that right needs stemming (both words reducing to the
same root), which substring matching can never do however the query is
massaged. FTS5 gives stemmed, weighted, ranked search inside the SQLite
file the app already uses: no Postgres needed just for search, no extra
search service to run, nothing extra to back up. Deployments that opt
into Postgres (via DATABASE_URL) use Postgres's own full-text search
instead -- see kb/views.py::_search_articles for the dispatch.

Shape of the index -- one FTS5 virtual table, `kb_articleindex`:

    title, summary, body     indexed text, copied from kb.Article
    article_id UNINDEXED     the join key back to kb_article.id

  - Tokenizer `porter unicode61`: Unicode-aware word splitting with
    Porter stemming on top -- the same stemming algorithm behind
    Postgres's `english` text-search config, so "configuring" finds
    "configure" and "summaries" finds "Summary" on either backend.
  - Ranking `bm25(kb_articleindex, 1.0, 0.4, 0.2)`: per-column weights
    for title/summary/body mirroring Postgres ts_rank's default A/B/C
    weights, so a title match outranks the same word in the summary,
    which outranks it in the body. bm25() returns lower-is-better.
  - Every article is indexed, published or not. What a visitor may see
    is decided by the queryset the view already filtered; the index only
    answers "which articles match, and how well".

The table is kept in sync from Python (kb/signals.py, on Article
post_save/post_delete) rather than with SQL triggers, so all the logic
lives in one readable place and the table can be rebuilt from scratch
at any time with `python manage.py rebuild_search_index`.

Everything here is a no-op on a database that is not SQLite, or on a
SQLite build without FTS5 compiled in: `fts5_available()` gates every
entry point, and search falls back to the older behaviour.
"""
import re

from django.db import connection as default_connection
from django.db import transaction

TABLE = 'kb_articleindex'

# bm25() weights, in column order: title, summary, body. The fourth
# column (article_id) is UNINDEXED and never contributes to the score.
TITLE_WEIGHT = 1.0
SUMMARY_WEIGHT = 0.4
BODY_WEIGHT = 0.2

CREATE_TABLE_SQL = (
    f'CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5('
    'title, summary, body, article_id UNINDEXED, '
    "tokenize = 'porter unicode61')"
)
DROP_TABLE_SQL = f'DROP TABLE IF EXISTS {TABLE}'

# A "word" for query purposes. \w is Unicode-aware in Python 3, which
# lines up with the unicode61 tokenizer closely enough: anything that is
# not a word character is punctuation to both.
_WORD_RE = re.compile(r'\w+')

# Keyed by connection alias. Whether FTS5 is compiled in cannot change
# while the process runs, so it is asked once per database connection
# alias rather than on every search/save.
_availability_cache = {}


def fts5_available(connection=None):
    """True if `connection` is SQLite with FTS5 compiled in."""
    connection = connection or default_connection
    if connection.vendor != 'sqlite':
        return False
    alias = connection.alias
    if alias not in _availability_cache:
        with connection.cursor() as cursor:
            cursor.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')")
            _availability_cache[alias] = bool(cursor.fetchone()[0])
    return _availability_cache[alias]


def create_table(connection=None):
    connection = connection or default_connection
    with connection.cursor() as cursor:
        cursor.execute(CREATE_TABLE_SQL)


def drop_table(connection=None):
    connection = connection or default_connection
    with connection.cursor() as cursor:
        cursor.execute(DROP_TABLE_SQL)


def _row(article):
    return [article.title or '', article.summary or '', article.body or '', article.pk]


def upsert_article(article, connection=None):
    """Replace this article's row in the index. Delete-then-insert rather
    than UPDATE: FTS5 rowids are its own, and keying on article_id means
    a missing row (e.g. an article saved before the index existed) is
    simply created rather than silently not updated."""
    connection = connection or default_connection
    if not fts5_available(connection):
        return
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM {TABLE} WHERE article_id = %s', [article.pk])
        cursor.execute(
            f'INSERT INTO {TABLE} (title, summary, body, article_id) VALUES (%s, %s, %s, %s)',
            _row(article),
        )


def delete_article(article_id, connection=None):
    connection = connection or default_connection
    if not fts5_available(connection):
        return
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM {TABLE} WHERE article_id = %s', [article_id])


def rebuild(article_model=None, connection=None):
    """Empty the index and repopulate it from every article. Returns the
    number of rows indexed, or None if FTS5 is unavailable.

    `article_model` lets the migration pass its historical model; normal
    callers leave it to default to kb.Article.

    The snapshot read, DELETE and inserts all run inside one
    transaction.atomic() block, so this is genuinely safe to run at any
    time as the docstring of `rebuild_search_index` promises: a search
    running concurrently sees either the old rows or the new ones, never
    the empty gap between DELETE and the inserts finishing, and a failure
    partway through (a bad row, a killed process) rolls the whole rebuild
    back instead of leaving the index half-populated."""
    connection = connection or default_connection
    if not fts5_available(connection):
        return None
    if article_model is None:
        from .models import Article as article_model
    rows = [
        _row(article)
        for article in article_model.objects.using(connection.alias).only(
            'id', 'title', 'summary', 'body',
        ).iterator()
    ]
    with transaction.atomic(using=connection.alias):
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TABLE_SQL)
            cursor.execute(f'DELETE FROM {TABLE}')
            cursor.executemany(
                f'INSERT INTO {TABLE} (title, summary, body, article_id) VALUES (%s, %s, %s, %s)',
                rows,
            )
    return len(rows)


def build_match_expression(query):
    """Turn free user input into a safe FTS5 MATCH expression, or None if
    it contains no words.

    Mirrors Postgres's plainto_tsquery (what `SearchQuery(query)` uses):
    every word must match, punctuation is ignored. Each word becomes a
    double-quoted FTS5 string, and FTS5 ANDs space-separated strings, so
    `configuring summaries` becomes `"configuring" "summaries"`. Quoting
    every token is also what neutralises FTS5's own query syntax in user
    input -- AND/OR/NOT/NEAR, `*` prefix queries, `^`, `column:` filters
    and parentheses all become plain words or disappear as punctuation.
    Embedded `"` would be doubled per FTS5's string escaping; \\w tokens
    cannot contain one, but the escaping stays so this remains safe if the
    tokenising rule ever changes."""
    tokens = _WORD_RE.findall(query or '')
    if not tokens:
        return None
    return ' '.join('"{}"'.format(token.replace('"', '""')) for token in tokens)


def search(query, connection=None):
    """Return [(article_id, bm25_rank), ...] for articles matching every
    word of `query`, best match first (lowest bm25 first). The match
    expression is always a bound parameter, never formatted into SQL."""
    connection = connection or default_connection
    expression = build_match_expression(query)
    if expression is None or not fts5_available(connection):
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT article_id, bm25({TABLE}, %s, %s, %s) AS score '
            f'FROM {TABLE} WHERE {TABLE} MATCH %s ORDER BY score',
            [TITLE_WEIGHT, SUMMARY_WEIGHT, BODY_WEIGHT, expression],
        )
        return [(int(article_id), score) for article_id, score in cursor.fetchall()]
