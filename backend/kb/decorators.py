"""
Access control for the in-app authoring views (kb/views.py:
article_create, article_edit, article_manage).

Superuser-only for now — the same crude "are you logged into Django
Admin as a superuser at all" gate Django Admin itself relies on.
Expected to grow into a proper per-org/per-role check once org-scoped
authoring lands (the multi-tenant add-on this app's models already
carry a nullable org_id for), at which point an org admin should be
able to author articles under their own org_id without being a
superuser.
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login


def superuser_required(view_func):
    """Redirects anonymous or non-superuser visitors to Django Admin's
    own login page, same as hitting /admin/ while logged out.
    Deliberately not django.contrib.auth.decorators.login_required +
    user_passes_test stacked separately — this single decorator keeps
    the "must be a superuser" story in one place for every authoring
    view."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        return redirect_to_login(
            request.get_full_path(), login_url='admin:login',
        )
    return _wrapped
