from django.apps import AppConfig


class KbConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'kb'
    verbose_name = 'Knowledge Base'

    def ready(self):
        # Registers the Article post_save/post_delete receivers that keep
        # the SQLite FTS5 search index in sync (kb/signals.py).
        from . import signals  # noqa: F401
