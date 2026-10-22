import ipaddress

from django.conf import settings
from django.contrib.auth.forms import UsernameField
from django.http import HttpResponse
from django.utils.translation import gettext

from core.limits import consume


def client_address(request):
    peer = request.META.get("REMOTE_ADDR", "")
    if peer in settings.TRUSTED_PROXY_IPS:
        try:
            return str(ipaddress.ip_address(request.META.get("HTTP_X_REAL_IP", "")))
        except ValueError:
            pass
    return peer


class ProxyHeadersMiddleware:
    """Validate the peer before Django's security and CSRF middleware run."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        trusted = request.META.get("REMOTE_ADDR", "") in settings.TRUSTED_PROXY_IPS
        protocol = request.META.get("HTTP_X_FORWARDED_PROTO")
        if not trusted or protocol not in {"http", "https"}:
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
        if not trusted:
            request.META.pop("HTTP_X_REAL_IP", None)
        return self.get_response(request)


class SecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = None
        if request.method == "POST" and request.path.startswith("/accounts/"):
            # Only an explicitly trusted proxy can supply a rewritten client IP.
            if not consume("account-ip", client_address(request), 30, 900):
                response = HttpResponse(gettext("操作过于频繁，请 15 分钟后重试。"), status=429)
                response["Retry-After"] = "900"
            # Match authentication's whitespace stripping and NFKC conversion:
            # equivalent spellings must not create fresh per-account quotas.
            username = (
                UsernameField(max_length=150)
                .to_python(request.POST.get("username", ""))
                .casefold()[:150]
            )
            if response is None and username and not consume("account-name", username, 15, 900):
                response = HttpResponse(gettext("操作过于频繁，请稍后重试。"), status=429)
        response = response or self.get_response(request)
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'"
        )
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.user.is_authenticated or request.path.startswith(("/accounts/", "/api/")):
            response["Cache-Control"] = "no-store"
        return response
