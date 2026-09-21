from django.apps import AppConfig
from django.db.backends.signals import connection_created


class SupportCoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'support_core'

    def ready(self):
        # Wired here rather than at import time in db.py: AppConfig.ready()
        # is Django's documented place for signal registration, and it
        # guarantees this connects exactly once, after the app registry is
        # fully populated. See support_core/db.py for what the receiver
        # does and why.
        from support_core.db import configure_sqlite
        connection_created.connect(configure_sqlite)
