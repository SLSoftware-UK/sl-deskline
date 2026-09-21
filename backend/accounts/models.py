"""Who a signed-in customer belongs to.

An `Organisation` is a customer company (or team, or household -- any
group of people who should see each other's tickets). A `Membership` puts
one user in one organisation with a role, and a user may hold several:
a contractor who supports two clients, or an owner who runs two
businesses, is one login with two memberships, not two logins.

Memberships are load-bearing in both halves of the service:

  - tickets: a customer sees the tickets they raised themselves plus
    every ticket filed under an organisation they are a member of, so
    colleagues can follow each other's issues (see
    tickets/views.py::_visible_tickets). A ticket records which
    organisation it was raised for; a customer with no membership at all
    can still raise tickets, which are simply filed under no
    organisation.
  - kb: holding at least one membership is what opens members-only KB
    categories (see kb/views.py::_can_see_members_only).

Rows get here one of two ways, and nothing else creates them -- there is
no self-signup:

  - An operator adds them in Django Admin (the Organisation page has a
    Membership inline, and so does the User page), which is the whole
    story for a deployment using plain local accounts.
  - With `SSO_BACKEND='code_exchange'`, the identity provider's exchange
    response carries the user's organisations and accounts/sso.py
    re-syncs them on every sign-in: a full replace of the user's
    memberships in provider-managed organisations (those with an
    `external_id`), so one the provider no longer reports stops granting
    access at the next login. Memberships an operator added in
    hand-made organisations (no `external_id`) are left alone.

"Staff" has two unrelated meanings nearby, so to be explicit: Django's
`is_staff` flag marks a *support agent* -- someone who works the ticket
queue for every customer. Support agents do not need memberships and the
SSO sync refuses to attach any to an is_staff account, so an agent's
access never depends on, or leaks through, a customer organisation.
"""
from django.conf import settings
from django.db import models


class Organisation(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    # Set only for organisations synced from an external identity
    # provider (SSO_BACKEND='code_exchange') — the provider's own id, so a
    # re-sync updates the same row. Null for organisations created by hand
    # in Django Admin.
    external_id = models.CharField(max_length=64, null=True, blank=True, unique=True)

    class Meta:
        ordering = ('name', 'id')

    def __str__(self):
        return self.name


class Membership(models.Model):
    """One user in one organisation. `related_name='memberships'` on both
    sides is relied on by name (tickets scopes on
    `organisation__memberships__user`, the KB gate asks
    `user.memberships.exists()`), so it is part of this model's public
    surface rather than an incidental choice.

    `role` is recorded (and synced from SSO) but does not yet change what
    anyone can see: every member of an organisation sees all of its
    tickets. It is here so a later permission rule -- say, only owners
    and admins see billing tickets -- has the data it needs without
    another migration."""

    class Role(models.TextChoices):
        OWNER = 'owner', 'Owner'
        ADMIN = 'admin', 'Admin'
        MEMBER = 'member', 'Member'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships',
    )
    organisation = models.ForeignKey(
        Organisation, on_delete=models.CASCADE, related_name='memberships',
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)

    class Meta:
        unique_together = ('user', 'organisation')

    def __str__(self):
        return f'{self.user} @ {self.organisation} ({self.get_role_display()})'
