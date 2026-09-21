"""Admin for the SiteSettings singleton.

A changelist, an "Add" button and a delete action all make sense for a
table of many rows; this table only ever has one. Rather than trust an
operator to notice that and always edit the existing row, the admin
below removes the ways of getting it wrong: no add link once the row
exists (there is only ever one to add), no delete action ever, and the
changelist itself redirects straight to the row's change form instead of
showing a one-item list an operator would have to click through.
"""
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse

from .models import SiteSettings


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    fields = ('site_name', 'logo', 'accent_color', 'support_email', 'notification_from_email')

    def has_add_permission(self, request):
        # SiteSettings.save() forces pk=1 regardless, so a second row
        # can never actually be created -- this just stops the Admin
        # offering an "Add" button that would look like it works and
        # then silently overwrite the one row instead.
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Deleting the only row would just mean the next load() call
        # recreates it with the defaults, silently discarding whatever
        # branding was configured -- worse than useless as a delete
        # action, so it is not offered at all.
        return False

    def changelist_view(self, request, extra_context=None):
        # There is nothing worth showing in a list of one -- go straight
        # to the row an operator actually wants, creating it first if
        # this is a fresh database. The permission check has to happen
        # here explicitly, before that create-on-first-visit side
        # effect: the default changelist_view we would otherwise inherit
        # only checks it once it starts building the list, and this
        # override replaces that whole method rather than extending it.
        # Falling through to `super()` for an unauthorised request keeps
        # Django's own 403 page (and its permission error, not a
        # confusing redirect) intact.
        if not self.has_view_permission(request):
            return super().changelist_view(request, extra_context)
        obj = SiteSettings.load()
        return redirect(reverse('admin:branding_sitesettings_change', args=[obj.pk]))
