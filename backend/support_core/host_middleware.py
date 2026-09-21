"""
The one bit of host awareness this service has at the HTTP layer.

A deployment may give the ticket desk its own hostname (e.g.
support.example.com, listed in settings.TICKET_HOSTS) alongside the help
centre's. Both point at this one service, whose root is the KB homepage,
but someone typing the ticket hostname expects the desk. Sending a bare
`/` on a ticket host to /tickets/ gives them that — and it also covers
an SSO provider that sends the browser to /sso/callback/?code=... with
no `next`, which defaults to `/`. Every other path is served
identically on either host. With TICKET_HOSTS empty (the default,
single-host deployment) this middleware does nothing.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class TicketHostRootRedirectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == '/' and is_ticket_host(request):
            return redirect(reverse('tickets:list'))
        return self.get_response(request)


def is_ticket_host(request):
    return request.get_host().split(':')[0].lower() in {h.lower() for h in settings.TICKET_HOSTS}
