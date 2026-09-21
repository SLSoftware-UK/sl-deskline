"""
Empties the KB full-text search index and repopulates it from every
article (see kb/search_index.py).

The index is normally kept current by kb/signals.py, so this is only
needed after something bypassed the model's save/delete signals -- a raw
SQL import, a restored database file from before the index existed, a
bulk `QuerySet.update()` touching title/summary/body -- or as a harmless
"make sure" after any migration or restore. Safe to run at any time --
search_index.rebuild() does the empty-and-repopulate inside one
transaction.atomic(), so a search running concurrently never sees an
empty index mid-rebuild and a failure partway through rolls back instead
of leaving the index half-populated; on a database without SQLite FTS5
(e.g. Postgres) it does nothing.
"""
from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections

from kb import search_index


class Command(BaseCommand):
    help = 'Rebuild the KB full-text search index (SQLite FTS5) from every article.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--database', default=DEFAULT_DB_ALIAS,
            help='Database alias to rebuild the index in (default: "default").',
        )

    def handle(self, *args, **options):
        connection = connections[options['database']]
        count = search_index.rebuild(connection=connection)
        if count is None:
            self.stdout.write(
                'Search index not in use on this database (it needs SQLite with FTS5); '
                'nothing to do.'
            )
            return
        noun = 'article' if count == 1 else 'articles'
        self.stdout.write(self.style.SUCCESS(f'Indexed {count} {noun}.'))
