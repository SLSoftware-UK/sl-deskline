"""Django Admin is where an operator manages who belongs to what: there is
no self-signup, so creating an organisation and putting users in it
happens here.

Memberships are editable from both ends, because operators arrive from
both: "set up Acme and add its three people" starts on the Organisation
page, "this new user should see Acme's tickets" starts on the User page.
Both inlines use autocomplete for the other side, which needs a
`search_fields` on the related admin -- OrganisationAdmin below for one,
Django's own UserAdmin (which this module extends rather than replaces)
for the other.
"""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin

from .models import Membership, Organisation

User = get_user_model()


class OrganisationMembershipInline(admin.TabularInline):
    model = Membership
    extra = 1
    autocomplete_fields = ('user',)


class UserMembershipInline(admin.TabularInline):
    model = Membership
    extra = 1
    autocomplete_fields = ('organisation',)
    verbose_name = 'organisation membership'
    verbose_name_plural = 'organisation memberships'


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'external_id')
    search_fields = ('name', 'slug', 'external_id')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [OrganisationMembershipInline]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'organisation', 'role')
    list_filter = ('role',)
    search_fields = ('user__username', 'user__email', 'organisation__name')
    autocomplete_fields = ('user', 'organisation')


# Re-register the stock User admin with the membership inline added.
# Subclassing keeps everything Django's UserAdmin already does (password
# change form, permissions, search_fields that the autocomplete above
# depends on); only the inline list changes.
admin.site.unregister(User)


@admin.register(User)
class UserWithMembershipsAdmin(UserAdmin):
    inlines = [*UserAdmin.inlines, UserMembershipInline]
