"""
Root-mounted, not namespaced under /accounts/.

`/login/` and `/logout/` are what people type, and `/sso/callback/` is
the URL an identity provider is configured to send browsers to, so all
of them sit at the root; the two /staff/ routes sit beside them. What
each one does depends on `settings.SSO_BACKEND` -- see accounts/views.py.

`/logout/` and `/staff/logout/` are the same view: the second exists so
older links and templates that post to it keep working.

`support_core/urls.py` includes this with an empty prefix, above
`kb.urls` (which owns the root and would otherwise shadow these).
"""
from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('sso/callback/', views.sso_callback, name='sso-callback'),
    path('logout/', views.logout_view, name='logout'),
    path('staff/login/', views.staff_login_view, name='staff-login'),
    path('staff/logout/', views.logout_view, name='staff-logout'),
]
