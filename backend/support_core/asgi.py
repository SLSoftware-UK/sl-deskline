"""ASGI config for support_core."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'support_core.settings')

application = get_asgi_application()
