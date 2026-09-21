"""
tickets -> outbound email, through whatever settings.EMAIL_BACKEND is
(plain SMTP by default, or tickets/email_backend.py's SMTP2GOBackend —
see settings.py). Each message is rendered from a text + HTML template
pair with render_to_string and sent as EmailMultiAlternatives.

Six trigger points (the ticket-creation pair — ticket_received vs
staff_new_ticket — and the follow-up-message pair — customer_reply vs
staff_new_message — serve the same event to two audiences with slightly
different context):
  - ticket raised          -> notify_ticket_received (the customer who
    raised it — confirmation, not a "reply") AND notify_staff_new_ticket
    (staff)
  - new customer message   -> notify_staff_new_message (assigned staff,
    or all staff if nobody's assigned yet) — follow-up messages only,
    never the ticket's own opening message
  - new staff reply        -> notify_customer_reply
  - status -> resolved     -> notify_resolved
  - ticket assigned        -> notify_assigned

Every notify_* function catches its own send errors and logs rather
than raising — a failed/missing email must never block the ticket
workflow itself, so callers in views.py don't need to wrap these in try/except.

Links are built from `settings.TICKET_SITE_URL`, the same setting
kb/context_processors.py::tickets_url uses for the KB header's
cross-host Tickets link, so an email link and an in-app link can never
disagree about where the desk lives. The URL shape is
`{TICKET_SITE_URL}/tickets/<pk>/`.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from branding.models import SiteSettings

logger = logging.getLogger(__name__)

User = get_user_model()


def _ticket_site_url(path):
    """Absolute URL on the ticket host.

    Absolute, not `reverse()`: an email has no request to build a host
    from, and the recipient opens it away from any session — so the
    origin has to come from configuration. `getattr` rather than a bare
    `settings.TICKET_SITE_URL` keeps a missing setting from turning a
    notification into a 500 inside a ticket save; the default matches
    settings.py's own.
    """
    base = getattr(settings, 'TICKET_SITE_URL', settings.SITE_URL).rstrip('/')
    return f'{base}{path}'


def _ticket_context(ticket, recipient, site_settings, **extra):
    """`site_settings` is a required parameter, not another
    `SiteSettings.load()` call in here: every notify_* function below
    already loads it once (it needs the site name for the subject
    before this is even called), and the fan-out ones
    (notify_staff_new_ticket/notify_staff_new_message) call this once
    per staff recipient -- loading it again per recipient here would
    turn one notification send into an N+1 (see branding/utils.py's
    docstring for the same reasoning applied to page views)."""
    context = {
        'ticket': ticket,
        'recipient': recipient,
        'ticket_url': _ticket_site_url(f'/tickets/{ticket.pk}/'),
        # render_to_string() has no request, so branding's context
        # processor (branding/context_processors.py) never runs for
        # these templates — the caller passes the same SiteSettings
        # instance it already loaded for the subject, so an email and
        # the page it links back to can never show two different site
        # names, and so this row is loaded at most once per
        # notification send.
        'site_settings': site_settings,
    }
    context.update(extra)
    return context


def _send(template_base, subject, context, recipient_email):
    if not recipient_email:
        logger.warning('Ticket email skipped, no recipient email: template=%s ticket=%s', template_base, context['ticket'].pk)
        return False
    try:
        text_body = render_to_string(f'tickets/email/{template_base}.txt', context)
        html_body = render_to_string(f'tickets/email/{template_base}.html', context)
        # notification_from_email lets a self-hoster send ticket mail
        # "from" their own support address instead of the service-wide
        # DEFAULT_FROM_EMAIL — useful when that default is a bare
        # no-reply box nobody reads replies at. Blank (the default) just
        # falls back to the existing behaviour.
        from_email = context['site_settings'].notification_from_email or settings.DEFAULT_FROM_EMAIL
        msg = EmailMultiAlternatives(
            subject=subject, body=text_body, from_email=from_email, to=[recipient_email],
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send()
        return True
    except Exception:
        logger.exception('Ticket email failed: template=%s recipient=%s', template_base, recipient_email)
        return False


def _staff_recipients(ticket):
    """Assigned staff member if there is one, else every staff account
    with an email address (the fan-out for unassigned tickets). "Staff"
    here is a support agent (is_staff), never a customer; see
    accounts/models.py.

    `is_active` is filtered on both branches, matching
    tickets/forms.py's assignable queryset, so the two halves of the app
    agree about who counts as a working agent. Otherwise an agent who
    left, deactivated in Admin (the correct way to retire an account —
    deleting it would take their ticket history with it), would
    disappear from the assignment dropdown but keep receiving every
    new-ticket and new-message email for the rest of the service's life.
    Deactivating an account has to mean it stops being used, and mail is
    a use.

    A deactivated *assignee* falls through to the fan-out rather than
    returning an empty list: the ticket has nobody actually watching it,
    so the whole team should see it, exactly as an unassigned one."""
    if ticket.assigned_to_id and ticket.assigned_to.is_active and ticket.assigned_to.email:
        return [ticket.assigned_to]
    return list(User.objects.filter(is_staff=True, is_active=True).exclude(email=''))


def notify_ticket_received(ticket, body):
    """Ticket just raised -> confirmation to the customer who raised it.
    Deliberately separate from notify_customer_reply: this fires on the
    customer's *own* first message, not a staff reply. Fire-and-forget
    alongside notify_staff_new_ticket, not instead of it — both sides
    get a notification for the same event, each in their own
    template."""
    recipient = ticket.raised_by
    site_settings = SiteSettings.load()
    subject = f"We've received your ticket: {ticket.subject} — {site_settings.site_name}"
    context = _ticket_context(ticket, recipient, site_settings, body=body)
    return _send('ticket_received', subject, context, recipient.email)


def notify_staff_new_ticket(ticket):
    """A brand new ticket was just raised -> notify the assigned staff
    member, or all staff if unassigned. Call this only from ticket
    creation; call notify_staff_new_message for every follow-up
    customer message after that."""
    site_settings = SiteSettings.load()
    subject = f'New ticket: {ticket.subject} — {site_settings.site_name}'
    results = [
        _send('staff_new_ticket', subject, _ticket_context(ticket, staff, site_settings), staff.email)
        for staff in _staff_recipients(ticket)
    ]
    return any(results)


def notify_customer_reply(ticket):
    """New staff reply -> notify the customer who raised the ticket."""
    recipient = ticket.raised_by
    site_settings = SiteSettings.load()
    subject = f'New reply on your ticket: {ticket.subject} — {site_settings.site_name}'
    context = _ticket_context(ticket, recipient, site_settings)
    return _send('customer_reply', subject, context, recipient.email)


def notify_staff_new_message(ticket):
    """A customer added a new message to an existing ticket -> notify
    the assigned staff member, or all staff if unassigned. Do NOT call
    this for a ticket's own opening message -- use
    notify_staff_new_ticket for that."""
    site_settings = SiteSettings.load()
    subject = f'New message on ticket #{ticket.pk}: {ticket.subject} — {site_settings.site_name}'
    results = [
        _send('staff_new_message', subject, _ticket_context(ticket, staff, site_settings), staff.email)
        for staff in _staff_recipients(ticket)
    ]
    return any(results)


def notify_assigned(ticket):
    """Ticket assigned to a staff member -> let them know."""
    if not ticket.assigned_to_id or not ticket.assigned_to.email:
        return False
    recipient = ticket.assigned_to
    site_settings = SiteSettings.load()
    subject = f'Ticket assigned to you: {ticket.subject} — {site_settings.site_name}'
    context = _ticket_context(ticket, recipient, site_settings)
    return _send('assigned', subject, context, recipient.email)


def notify_resolved(ticket):
    """Status -> resolved -> notify the customer."""
    recipient = ticket.raised_by
    site_settings = SiteSettings.load()
    subject = f'Your ticket has been resolved: {ticket.subject} — {site_settings.site_name}'
    context = _ticket_context(ticket, recipient, site_settings)
    return _send('resolved', subject, context, recipient.email)
