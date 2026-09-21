"""
Keeps the SQLite FTS5 search index (kb/search_index.py) in step with
kb.Article. Connected in kb/apps.py::KbConfig.ready().

Sync is done here in Python rather than with SQL triggers so the whole
index lifecycle -- create, sync, rebuild, query -- is one readable module
plus these two receivers. Both are no-ops on databases without FTS5
(Postgres, or a SQLite build lacking it): search_index checks that on
every call.

Bulk `QuerySet.update()` calls bypass post_save, as with any Django
signal. The only ones in this codebase (kb/admin.py's archive/unpublish
actions) change `status`, which is not indexed; if the index ever does
drift, `python manage.py rebuild_search_index` repairs it.
"""
from django.db import connections
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from . import search_index
from .models import Article


@receiver(post_save, sender=Article, dispatch_uid='kb_article_search_index_save')
def index_article_on_save(sender, instance, using, **kwargs):
    search_index.upsert_article(instance, connections[using])


@receiver(post_delete, sender=Article, dispatch_uid='kb_article_search_index_delete')
def unindex_article_on_delete(sender, instance, using, **kwargs):
    search_index.delete_article(instance.pk, connections[using])
