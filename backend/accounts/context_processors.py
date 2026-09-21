from django.utils.functional import SimpleLazyObject

from .models import Organisation


def user_organisations(request):
    """`user_organisations`: the signed-in user's organisations, by name.

    kb/base.html's header prints these next to a customer's name, because
    they are what that customer's tickets are scoped to — someone who
    belongs to two organisations needs to see both before raising a
    ticket. One ordered query rather than walking `user.memberships` in
    the template, which would cost a query per membership and come out in
    membership order rather than alphabetically.

    Lazy, so pages that never render the header (htmx partials, emails)
    and anonymous visitors never pay for the query.
    """
    def load():
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return []
        return list(Organisation.objects.filter(memberships__user=user).order_by('name', 'id'))

    return {'user_organisations': SimpleLazyObject(load)}
