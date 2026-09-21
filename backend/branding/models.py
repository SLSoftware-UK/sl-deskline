"""
Self-hoster branding: one row, editable in Django Admin, that lets
whoever runs this service put their own name, logo, colour and support
address on it without touching code or templates.

This is a *singleton* model — exactly one row, always primary key 1 — not
a per-organisation setting. `accounts.Organisation` scopes tickets and
members-only KB categories between a deployment's own customers; this
model is one level up, styling the deployment itself (its KB, its ticket
desk, its outbound email) for whoever is self-hosting it. A multi-tenant
white-label product, where different organisations see different
branding on the same install, is a different, bigger feature this does
not attempt.

`SiteSettings.load()` is the one supported way to read this: it always
returns the pk=1 row, creating it with the field defaults below on first
call so every code path — the context processor, a view building an
email, `python manage.py shell` on a brand new clone — gets a real
instance without every caller having to handle "does the row exist yet".
"""
from django.core.validators import RegexValidator
from django.db import models

#: `#` followed by exactly six hex digits — CSS's `#rrggbb` form.
#: Deliberately not `#rgb` or named colours (`rebeccapurple`): a single
#: fixed shape is enough for a colour picker to write and a CSS custom
#: property to consume, and keeping the format to one thing means the
#: field never needs to normalise what it was given before using it in a
#: template.
hex_colour_validator = RegexValidator(
    regex=r'^#[0-9a-fA-F]{6}$',
    message='Enter a hex colour code in the form #rrggbb.',
)

# The accent colour help.css/tickets.css shipped with before this model
# existed (their :root block's --accent — see those files' comments).
# Using it as the default, rather than some other colour, is what keeps
# an unconfigured deployment looking exactly as it did before
# self-hoster branding existed: SiteSettings.load() on a fresh database
# returns this same value, base.html writes it into --accent, and the
# CSS renders identically to when the value was hardcoded.
DEFAULT_ACCENT_COLOUR = '#7c3aed'


class SiteSettings(models.Model):
    site_name = models.CharField(
        max_length=100,
        default='SL Deskline',
        help_text='Shown in the header, page titles, social previews and outbound email.',
    )
    logo = models.ImageField(
        upload_to='branding/',
        blank=True,
        help_text='Shown in the header in place of the site name text when set.',
    )
    accent_color = models.CharField(
        max_length=7,
        default=DEFAULT_ACCENT_COLOUR,
        validators=[hex_colour_validator],
        help_text='Hex colour (e.g. #7c3aed) used for links, buttons and other accents.',
    )
    support_email = models.EmailField(
        blank=True,
        help_text='Shown in the KB/ticket footer and outbound email when set.',
    )
    notification_from_email = models.EmailField(
        blank=True,
        help_text='From-address for outbound ticket email. Falls back to DEFAULT_FROM_EMAIL when blank.',
    )

    class Meta:
        verbose_name = 'site settings'
        verbose_name_plural = 'site settings'

    def __str__(self):
        return self.site_name

    @classmethod
    def load(cls):
        """The one row, created on first call with the field defaults
        above if this is a fresh database. `get_or_create` rather than a
        migration-time data load so a database restored from a backup
        taken before this model existed, or a test that truncates
        between runs, still gets a usable row the first time anything
        asks for one — no separate seeding step to remember."""
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        """Force pk=1 on every save, so `SiteSettings()` (or a stray
        `SiteSettings(pk=2)`) can never create a second row that
        `load()` would then not find — the Admin (see admin.py, which
        also blocks add/delete at the permission level) and any other
        caller always end up editing the same one."""
        self.pk = 1
        super().save(*args, **kwargs)
