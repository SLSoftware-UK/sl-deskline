from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'
    # Deliberately no ready() hook patching admin.site.login to kick off
    # SSO instead of showing Django's own form. That is the right call for
    # a service with no local-password accounts at all; here it would be
    # actively wrong: Django Admin is the support agents' tool, and a
    # support agent is a local createsuperuser account with a real
    # password (see settings.py's "Auth" note). Leave admin.site.login
    # alone.
