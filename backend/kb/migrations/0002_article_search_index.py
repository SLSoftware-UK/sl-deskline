"""
Creates the SQLite FTS5 search index for KB articles (kb/search_index.py)
and backfills it from the articles already in the database.

RunPython rather than RunSQL: the same migration has to be a no-op on
Postgres, and on a SQLite build without FTS5 compiled in, where
`CREATE VIRTUAL TABLE ... USING fts5` would fail outright.
"""
from django.db import migrations

from kb import search_index


def create_index(apps, schema_editor):
    connection = schema_editor.connection
    if not search_index.fts5_available(connection):
        return
    search_index.create_table(connection)
    search_index.rebuild(apps.get_model('kb', 'Article'), connection)


def drop_index(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor != 'sqlite':
        return
    search_index.drop_table(connection)


class Migration(migrations.Migration):

    dependencies = [
        ('kb', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_index, drop_index),
    ]
