"""
Mounted at /tickets/ by support_core/urls.py, above the root `kb`
include.

Paths (/tickets/new, /tickets/resolved, /tickets/<id>) are what
notification emails link to, so treat them as stable — APPEND_SLASH
covers a missing trailing slash on GETs.
The bare ticket host root is sent here too, by
support_core/host_middleware.py, which reverses `tickets:list`.
"""
from django.urls import path

from . import views

app_name = 'tickets'

urlpatterns = [
    path('', views.ticket_list, name='list'),
    path('resolved/', views.ticket_list_resolved, name='resolved'),
    path('new/', views.ticket_create, name='create'),
    path('<int:pk>/', views.ticket_detail, name='detail'),
    path('<int:pk>/messages/', views.ticket_reply, name='reply'),
    path('<int:pk>/update/', views.ticket_update, name='update'),
]
