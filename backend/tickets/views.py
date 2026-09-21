"""
Support desk — server-rendered Django views (+ htmx for in-place
updates). The business rules:

  - Support agents (is_staff — NOT is_superuser; an agent must not need
    platform-admin rights) see and can act on every ticket. Everyone
    else is a customer, who sees the tickets they raised themselves plus
    every ticket filed under an organisation they belong to — colleagues
    can follow each other's issues, across as many organisations as the
    customer is a member of.
  - The default list is everything except resolved; resolved tickets
    have their own screen.
  - `?status=` filters within the non-resolved statuses; `?ordering=`
    is an allowlist, never a raw field name.
  - Status/assignment changes are support-agent-only. A customer
    replying to a `waiting_on_customer` ticket moves it back to
    `in_progress`.
  - Notifications fire on the same events as before — see
    tickets/notifications.py for which template goes to whom.

Organisations
-------------
A customer may belong to any number of organisations
(`accounts.Membership`), including none. Visibility is recomputed from
their memberships on every request (`_visible_tickets`), so adding or
removing someone in Django Admin — or at their next SSO sign-in — takes
effect immediately. A ticket's `organisation` is fixed when it is raised
(`ticket_create`): no membership files it under no organisation, one
membership files it under that one, several make the customer choose.
There is no organisation switcher; a multi-organisation customer simply
sees the union.

Interactive bits (reply thread, staff controls) use htmx and degrade to
plain form posts + redirects without it. Every view here is
login-required; nothing under /tickets/ is public or crawlable
(kb/views.py::robots_txt disallows it, and tickets/base.html marks
every page noindex).
"""
import logging

from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, IntegerField, Q, When
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.models import Membership, Organisation
from branding.utils import get_site_settings
from kb.models import Article

from .forms import TicketCreateForm, TicketReplyForm, TicketStaffUpdateForm
from .models import Ticket, TicketMessage
from .notifications import (
    notify_assigned, notify_customer_reply, notify_resolved, notify_staff_new_message,
    notify_staff_new_ticket, notify_ticket_received,
)

logger = logging.getLogger(__name__)

# Severity order for ?ordering=urgency — plain alphabetical would read
# "high, low, normal", not a sensible severity order. Annotated onto the
# queryset rather than sorted in Python, so it still works if this ever
# grows pagination.
_URGENCY_RANK = Case(
    When(urgency=Ticket.Urgency.LOW, then=0),
    When(urgency=Ticket.Urgency.NORMAL, then=1),
    When(urgency=Ticket.Urgency.HIGH, then=2),
    output_field=IntegerField(),
)

# Public ?ordering= values mapped to the actual field(s) to order by —
# deliberately an allowlist rather than passing the query param straight
# to .order_by(), which would let a client sort (and therefore probe) by
# any model field, including `raised_by__password`.
#
# 'organisation' is part of the public query-string surface, so it is
# named for the model field, not for the label above the column.
# Organisation-less tickets sort by a NULL name, which lands them first
# ascending on SQLite and last on Postgres — acceptable for a column only
# agents can sort by.
_ORDERING_FIELDS = {
    'subject': ('subject',),
    'organisation': ('organisation__name',),
    'urgency': ('urgency_rank',),
    'updated_at': ('updated_at',),
}
_DEFAULT_ORDERING = '-updated_at'

# Column headers for the list table, in display order. The third item is
# "support-agent only": for most customers every row would say the same
# organisation (or none), so the column would be noise.
_SORT_COLUMNS = (
    ('subject', 'Subject', False),
    ('organisation', 'Organisation', True),
    ('urgency', 'Urgency', False),
    ('updated_at', 'Updated', False),
)

_OPEN_STATUSES = [s for s in Ticket.Status if s != Ticket.Status.RESOLVED]


def _visible_tickets(user):
    """Support agents see everything; a customer sees the tickets they
    raised plus every ticket filed under an organisation they are a
    member of.

    The raised-by half is what keeps organisation-less tickets visible to
    the person who raised them, and keeps a customer's own tickets theirs
    after they leave the organisation they filed them under.

    The membership half is a subquery (`organisation_id IN (SELECT ...)`)
    rather than a join through `organisation__memberships__user`. A join
    would produce one row per member of the organisation for every
    ticket that also matches on raised_by, duplicating rows in the list
    (and inflating the `Count('messages')` annotation) unless every
    caller remembered `.distinct()`."""
    qs = Ticket.objects.select_related('organisation', 'raised_by', 'assigned_to')
    if user.is_staff:
        return qs
    member_of = Membership.objects.filter(user=user).values('organisation_id')
    return qs.filter(Q(raised_by=user) | Q(organisation_id__in=member_of))


def _page_context(request, page_title, **extra):
    """Bits kb/base.html expects (meta/canonical) for a page that is
    noindex anyway — supplied so the shared shell renders a sensible
    <title> rather than falling back to the KB's default.

    `get_site_settings(request)`, not `SiteSettings.load()`: branding's
    context processor loads the same row again for this request once
    the template renders, and caching it on `request` keeps that to one
    query total (see branding/utils.py)."""
    return {
        'meta_title': f'{page_title} — {get_site_settings(request).site_name}',
        'canonical_path': request.path,
        **extra,
    }


def _ticket_list(request, resolved):
    qs = _visible_tickets(request.user).annotate(
        urgency_rank=_URGENCY_RANK, message_count=Count('messages'),
    )

    status_filter = ''
    if resolved:
        qs = qs.filter(status=Ticket.Status.RESOLVED)
    else:
        # Only the non-resolved statuses are selectable here; resolved
        # has its own screen, so `?status=resolved` on this one falls
        # back to the default rather than quietly showing a list the
        # tabs say lives elsewhere.
        status_filter = request.GET.get('status', '')
        if status_filter in _OPEN_STATUSES:
            qs = qs.filter(status=status_filter)
        else:
            status_filter = ''
            qs = qs.exclude(status=Ticket.Status.RESOLVED)

    ordering = request.GET.get('ordering', _DEFAULT_ORDERING)
    field = ordering.lstrip('-')
    if field not in _ORDERING_FIELDS:
        ordering, field = _DEFAULT_ORDERING, _DEFAULT_ORDERING.lstrip('-')
    descending = ordering.startswith('-')
    prefix = '-' if descending else ''
    qs = qs.order_by(*[f'{prefix}{f}' for f in _ORDERING_FIELDS[field]])

    # Clicking the active column flips direction; any other column
    # starts ascending.
    columns = []
    for key, label, staff_only in _SORT_COLUMNS:
        if staff_only and not request.user.is_staff:
            continue
        active = key == field
        next_ordering = key if (active and descending) or not active else f'-{key}'
        columns.append({
            'key': key, 'label': label, 'active': active,
            'descending': descending, 'next_ordering': next_ordering,
        })

    if resolved:
        title = 'Resolved tickets'
    else:
        title = 'All tickets' if request.user.is_staff else 'Your tickets'

    return render(request, 'tickets/ticket_list.html', _page_context(
        request, title,
        title=title,
        tickets=qs,
        resolved=resolved,
        columns=columns,
        ordering=ordering,
        status_filter=status_filter,
        status_choices=[(s.value, s.label) for s in _OPEN_STATUSES],
    ))


@login_required
def ticket_list(request):
    return _ticket_list(request, resolved=False)


@login_required
def ticket_list_resolved(request):
    return _ticket_list(request, resolved=True)


def _linked_article(article_id):
    """Resolve Ticket.linked_article_id defensively — it is a bare
    integer, not an FK (see models.py), so the article may have been
    deleted or unpublished since the ticket was raised."""
    if not article_id:
        return None
    return Article.objects.filter(pk=article_id).only('id', 'title', 'slug', 'status').first()


@login_required
def ticket_create(request):
    """Raise a ticket. Customer-only.

    A support agent (is_staff) is *refused*: the desk is for customers
    to report problems to agents, and a ticket an agent raised would sit
    in the queue with no customer able to see it. Rendered as a 403 page
    rather than a raised PermissionDenied so the agent gets the desk's own
    chrome and a sentence explaining why, not Django's bare error page —
    and 403 rather than 200 so a crawler or a monitoring check cannot
    mistake it for a working form.

    Every customer can raise a ticket, whatever their memberships; how
    many organisations they belong to only decides where it is filed
    (see TicketCreateForm for the none / one / several rules). The
    organisations handed to the form are the user's own, looked up here,
    so the choice it offers — and validates against — can never include
    someone else's.
    """
    if request.user.is_staff:
        return render(request, 'tickets/ticket_form.html', _page_context(
            request, 'Raise a ticket', staff_cannot_raise=True,
        ), status=403)

    organisations = Organisation.objects.filter(memberships__user=request.user).order_by('name', 'id')

    # ?article=<id> from a KB article's "Raise a ticket" CTA. The link
    # itself is built in kb/views.py::_ticket_url_for; the parameter
    # handling belongs here. Kept on the ticket as linked_article_id.
    # Only a published article counts; anything else is silently
    # dropped rather than erroring the form — the person came here to
    # report a problem, not to have their URL validated at them. Read
    # from POST as well as GET so it survives the round trip through a
    # re-rendered form.
    #
    # `.isdecimal()`, NOT `.isdigit()`: isdigit() is also True for
    # non-decimal digit characters such as the superscript '²', which
    # int() then rejects with ValueError — so `?article=²` was a 500 on
    # the one path that promises never to error over a bad value.
    # isdecimal() is exactly the set int() accepts.
    raw_article = request.POST.get('article') or request.GET.get('article') or ''
    article = None
    if raw_article.isdecimal():
        article = Article.objects.filter(pk=int(raw_article), status=Article.STATUS_PUBLISHED).first()

    if request.method == 'POST':
        form = TicketCreateForm(request.POST, organisations=organisations)
        if form.is_valid():
            body = form.cleaned_data['body']
            ticket = form.save(commit=False)
            ticket.organisation = form.chosen_organisation()
            ticket.raised_by = request.user
            ticket.linked_article_id = article.pk if article else None
            ticket.save()
            TicketMessage.objects.create(ticket=ticket, author=request.user, body=body)
            # Both sides get notified of the same event, each in their
            # own template — the customer gets a confirmation
            # (ticket_received, not "new reply"), agents get the
            # new-ticket alert (staff_new_ticket, not staff_new_message,
            # which is for follow-up messages only). See
            # tickets/notifications.py.
            notify_ticket_received(ticket, body)
            notify_staff_new_ticket(ticket)
            return redirect('tickets:detail', pk=ticket.pk)
    else:
        form = TicketCreateForm(organisations=organisations)

    return render(request, 'tickets/ticket_form.html', _page_context(
        request, 'Raise a ticket', form=form, article=article,
    ))


def _detail_context(request, ticket, reply_form=None, staff_form=None):
    ctx = {
        'ticket': ticket,
        'thread': ticket.messages.select_related('author'),
        'reply_form': reply_form or TicketReplyForm(),
        'linked_article': _linked_article(ticket.linked_article_id),
    }
    if request.user.is_staff:
        ctx['staff_form'] = staff_form or TicketStaffUpdateForm(instance=ticket)
    return ctx


@login_required
def ticket_detail(request, pk):
    """A single ticket, at any status. Scoped by `_visible_tickets` but
    deliberately NOT by status: the resolved/not-resolved split is a
    list concern, and a resolved ticket must stay directly viewable (and
    repliable) from an old email link."""
    ticket = get_object_or_404(_visible_tickets(request.user), pk=pk)
    return render(request, 'tickets/ticket_detail.html', _page_context(
        request, ticket.subject, **_detail_context(request, ticket),
    ))


@login_required
@require_POST
def ticket_reply(request, pk):
    """Append to the flat thread and notify the other side: an agent
    replying notifies the customer; the customer replying notifies the
    assigned agent (or all agents if unassigned)."""
    ticket = get_object_or_404(_visible_tickets(request.user), pk=pk)
    form = TicketReplyForm(request.POST)
    if form.is_valid():
        TicketMessage.objects.create(ticket=ticket, author=request.user, body=form.cleaned_data['body'])

        if request.user.is_staff:
            notify_customer_reply(ticket)
        else:
            notify_staff_new_message(ticket)
            # A customer replying implicitly means it is no longer purely
            # "waiting on customer" — bump back to in_progress so it
            # reappears on the agents' active queue instead of looking
            # idle. Every other status is left exactly as it was,
            # including resolved: replying to a resolved ticket does not
            # silently reopen it.
            if ticket.status == Ticket.Status.WAITING_ON_CUSTOMER:
                ticket.status = Ticket.Status.IN_PROGRESS
        # Always saved, so a new message counts as activity for the
        # list's "Updated" ordering (bumping updated_at only on a status
        # change would make a busy thread look stale).
        # update_fields keeps this to the two columns that can have
        # changed; `updated_at` has to be named explicitly or auto_now
        # would be skipped.
        ticket.save(update_fields=['status', 'updated_at'])
        form = None

    if request.htmx:
        # Re-render the whole conversation panel: the thread, a fresh
        # (or errored) reply form, and — out of band — the status badge,
        # which a customer reply may just have changed.
        return render(request, 'tickets/includes/conversation.html', _detail_context(request, ticket, reply_form=form))
    if form is not None:
        return render(request, 'tickets/ticket_detail.html', _page_context(
            request, ticket.subject, **_detail_context(request, ticket, reply_form=form),
        ), status=400)
    return redirect(f"{reverse('tickets:detail', args=[ticket.pk])}#reply")


@login_required
@require_POST
def ticket_update(request, pk):
    """Support-agent-only status/assignment changes — a customer cannot
    reassign or resolve their own ticket (403).

    Notifications only fire on an *actual* change. The form always posts
    both fields, so without the before/after comparison, reassigning an
    already-resolved ticket would re-send the "resolved" email and
    re-saving an unchanged assignment would re-ping the assignee. A full
    form post always includes `assigned_to`, so "field not sent" (leave
    it alone) can't be told apart from "sent empty" (explicitly
    unassign) by presence; comparing values instead covers both, and
    unassigning still works: an empty select posts '', which the
    ModelChoiceField cleans to None.

    TicketStaffUpdateForm is a ModelForm, so `status` is validated
    against `Status.choices` for free and an unknown status is a 400.

    `Ticket.objects` and not `_visible_tickets`: only agents reach this
    line, and they see every organisation's tickets anyway.
    """
    if not request.user.is_staff:
        return HttpResponseForbidden('Staff only.')
    ticket = get_object_or_404(Ticket, pk=pk)
    previous_status = ticket.status
    previous_assignee_id = ticket.assigned_to_id

    form = TicketStaffUpdateForm(request.POST, instance=ticket)
    if form.is_valid():
        ticket = form.save()
        if ticket.assigned_to_id != previous_assignee_id:
            notify_assigned(ticket)
        if ticket.status == Ticket.Status.RESOLVED and previous_status != Ticket.Status.RESOLVED:
            notify_resolved(ticket)
        form = None
    else:
        # A bound ModelForm mutates its instance while cleaning, so the
        # in-memory ticket already carries the rejected values — reload
        # before re-rendering, or an invalid post would display as if it
        # had been applied.
        ticket.refresh_from_db()

    if request.htmx:
        return render(request, 'tickets/includes/ticket_meta.html', _detail_context(request, ticket, staff_form=form))
    if form is not None:
        return render(request, 'tickets/ticket_detail.html', _page_context(
            request, ticket.subject, **_detail_context(request, ticket, staff_form=form),
        ), status=400)
    return redirect('tickets:detail', pk=ticket.pk)
