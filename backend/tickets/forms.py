"""
The three forms behind the support desk: raising a ticket, replying to
one, and a support agent's status/assignment change.

Worth noting what ModelForms buy us for free: writing `status` straight
onto the model from request data would bypass `choices` entirely, so an
invalid status string would persist silently — leaving the ticket stuck
in the open queue forever (list views exclude only the real RESOLVED
value). A `ModelForm` with `fields = ('status', ...)` validates against
`Status.choices` as a matter of course, so that class of bug cannot
happen here.
"""
from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from accounts.models import Organisation

from .models import Ticket

User = get_user_model()


class TicketCreateForm(forms.ModelForm):
    """The customer's "raise a ticket" form.

    Which organisation the ticket is filed under depends on how many the
    raiser belongs to, passed in as `organisations` (their own, never
    anyone else's):

      - none: the ticket gets `organisation=None` — visible to the raiser
        and to support agents only. No field is shown.
      - exactly one: that organisation, again with no field — there is
        nothing to choose, and asking would only invite a wrong click.
      - several: a required `organisation` choice. Its queryset IS the
        raiser's own organisations, so the server-side check that a
        posted id is one of theirs is the ModelChoiceField's own
        validation: a crafted POST naming someone else's organisation is
        "not one of the available choices", never a ticket filed where
        that organisation's members could read it.

    The ticket's organisation is never taken from a model field on the
    form (it is not in Meta.fields), so none of this can be bypassed by
    posting `organisation` to a form that shows no such field — call
    `chosen_organisation()` after `is_valid()` for the answer.
    """
    body = forms.CharField(
        label="What's going on?",
        widget=forms.Textarea(attrs={'rows': 6}),
        help_text='Your first message on the ticket.',
    )

    class Meta:
        model = Ticket
        # urgency is chosen here at creation only — nothing else ever
        # edits it (see Ticket.urgency). raised_by is set by the view
        # from the signed-in user, and organisation via
        # chosen_organisation() below.
        fields = ('subject', 'body', 'urgency')
        widgets = {'urgency': forms.RadioSelect}
        labels = {'urgency': 'How urgent is this for you?'}

    def __init__(self, *args, organisations, **kwargs):
        super().__init__(*args, **kwargs)
        self._organisations = list(organisations)
        if len(self._organisations) > 1:
            self.fields['organisation'] = forms.ModelChoiceField(
                queryset=Organisation.objects.filter(pk__in=[o.pk for o in self._organisations]),
                label='Which organisation is this for?',
                empty_label='Choose an organisation…',
            )
            # Placed first, so it reads as "who is this for", then "what".
            self.order_fields(['organisation'])

    def chosen_organisation(self):
        if 'organisation' in self.fields:
            return self.cleaned_data['organisation']
        return self._organisations[0] if self._organisations else None


class TicketReplyForm(forms.Form):
    body = forms.CharField(
        label='Reply',
        widget=forms.Textarea(attrs={'rows': 4, 'placeholder': 'Write a reply…'}),
    )

    def clean_body(self):
        """Whitespace-only is empty. This matters more than it looks: a
        CharField with required=True happily accepts '   '."""
        body = self.cleaned_data['body'].strip()
        if not body:
            raise forms.ValidationError('Write a message first.')
        return body


class TicketStaffUpdateForm(forms.ModelForm):
    """Support-agent-only status/assignment controls on the detail page.

    Only support-agent accounts are assignable — enforced in the field's
    queryset so an out-of-range id is a form error (400) rather than a
    404 on a page the agent is already looking at.

    The `is_active=True` filter needs the current assignee OR'd back in, or
    it silently destroys data: deactivate an agent who still has tickets
    assigned and their id drops out of the queryset, the select renders
    as "Unassigned" because the bound value no longer matches an option,
    and the next status change an agent makes posts that empty value and
    clears the assignment without anyone touching the field. Keeping the
    instance's own assignee in the queryset means the select still shows
    who owns the ticket; they simply cannot be picked for a *different*
    one.
    """

    class Meta:
        model = Ticket
        fields = ('status', 'assigned_to')
        labels = {'assigned_to': 'Assigned to'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields['assigned_to']
        field.queryset = User.objects.filter(
            Q(is_staff=True, is_active=True) | Q(pk=self.instance.assigned_to_id)
        ).order_by('first_name', 'email')
        # Support agents are createsuperuser-made, so plenty of them
        # have no first/last name and some have no email either —
        # fall through to the username rather than rendering a blank
        # option label.
        field.label_from_instance = lambda u: u.get_full_name() or u.email or u.username
        field.empty_label = 'Unassigned'
