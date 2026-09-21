"""
Support desk data model: a ticket, raised by a customer on behalf of one
of their organisations (or of none), and a flat thread of messages on it.

Scoping is a nullable FK to `accounts.Organisation` rather than anything
per-user, because the unit a customer shares issues with is their
organisation: everyone who holds a Membership in it sees its tickets.
See accounts/models.py for the Organisation/Membership model and
tickets/views.py::_visible_tickets for the visibility rule itself.
"""
from django.conf import settings
from django.db import models

from accounts.models import Organisation


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        IN_PROGRESS = 'in_progress', 'In progress'
        WAITING_ON_CUSTOMER = 'waiting_on_customer', 'Waiting on customer'
        RESOLVED = 'resolved', 'Resolved'

    class Urgency(models.TextChoices):
        LOW = 'low', 'Low'
        NORMAL = 'normal', 'Normal'
        HIGH = 'high', 'High'

    # Which organisation this ticket was raised for — the scoping field
    # that lets the raiser's colleagues see it (tickets/views.py::
    # _visible_tickets). Nullable on purpose: a customer who belongs to
    # no organisation (an individual end customer, say) can still raise
    # tickets, and those are visible to the raiser and to support agents
    # only. PROTECT so deleting an organisation in Admin cannot silently
    # take its ticket history with it. raised_by is never null:
    # anonymous ticket submission is out of scope.
    organisation = models.ForeignKey(
        Organisation, on_delete=models.PROTECT, null=True, blank=True, related_name='tickets',
    )
    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='tickets_raised',
    )
    # Staff only (is_staff — support-agent accounts, see
    # accounts/models.py's module docstring) — enforced in views/forms,
    # not at the model layer. tickets/forms.py's TicketStaffUpdateForm
    # narrows the assignable queryset to is_staff/is_active accounts.
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tickets_assigned',
    )
    subject = models.CharField(max_length=255)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.OPEN)
    # Set by the raiser on the creation form and fixed thereafter (not
    # editable by anyone). This is the customer's signal of how urgent
    # it is for *them*; there is deliberately no staff-facing priority
    # field, staff order their own workload.
    urgency = models.CharField(max_length=10, choices=Urgency.choices, default=Urgency.NORMAL)

    # The KB article a ticket was raised *from*, if any. The ticket form
    # accepts `?article=<id>` (views.py::ticket_create), the detail page
    # shows the article back (templates/tickets/includes/ticket_meta.html),
    # and the KB's "Raise a ticket" CTA on each article populates it
    # (kb/views.py::_ticket_url_for).
    #
    # Still a bare PositiveIntegerField rather than an FK to kb.Article:
    # a ticket must survive its article being deleted or unpublished
    # (the conversation is the record, the article is context), and an
    # FK would either block the delete or null the field out. Callers
    # resolve it defensively — see views.py::_linked_article.
    linked_article_id = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-updated_at',)

    def __str__(self):
        return f'#{self.pk} {self.subject}'


class TicketMessage(models.Model):
    """One flat thread per ticket — no internal-notes/customer-visible
    split for v1 (explicitly decided, see kickoff brief). Cheap to add
    an `is_internal` boolean later if that's ever asked for."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='messages')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='ticket_messages')
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('created_at',)

    def __str__(self):
        return f'Message on #{self.ticket_id} by {self.author}'
