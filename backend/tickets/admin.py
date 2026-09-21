"""Tickets in Django Admin, for support agents who need a raw view.

`autocomplete_fields` needs a `search_fields` on the *related* admin:
`organisation` is covered by accounts/admin.py::OrganisationAdmin,
`raised_by` / `assigned_to` by the User admin. Django Admin here is the
support agents' tool (accounts/views.py leaves admin.site.login alone
on purpose), so this is a real working surface, not a leftover.
"""
from django.contrib import admin
from .models import Ticket, TicketMessage


class TicketMessageInline(admin.TabularInline):
    model = TicketMessage
    extra = 0
    readonly_fields = ('author', 'body', 'created_at')


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ('id', 'subject', 'organisation', 'status', 'raised_by', 'assigned_to', 'updated_at')
    list_filter = ('status', 'organisation')
    search_fields = ('subject',)
    autocomplete_fields = ('organisation', 'raised_by', 'assigned_to')
    inlines = [TicketMessageInline]


@admin.register(TicketMessage)
class TicketMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'ticket', 'author', 'created_at')
    search_fields = ('body',)
