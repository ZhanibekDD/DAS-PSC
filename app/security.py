"""Login sessions (or local-only without a password), same-origin writes and body limits."""
import base64
import binascii
import functools
import ipaddress
import os
from urllib.parse import quote, urlsplit

from starlette.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException

from app.auth import SESSION_COOKIE, LoginRejected

PUBLIC_PATHS = {"/health", "/login", "/api/login"}
ACCOUNT_PATHS = {"/account", "/api/account/password", "/api/logout"}
LOCAL_USER = {"id": 0, "username": "local", "display_name": "Локальный режим", "role": "admin",
              "active": True, "must_change": False, "last_login_at": None}


@functools.lru_cache(maxsize=4)
def default_gateway(route_file: str = "/proc/net/route") -> str | None:
    """The container's IPv4 default gateway: the host side of its Docker bridge."""
    try:
        with open(route_file) as fh:
            for line in fh.read().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 7 and fields[1] == "00000000" and fields[7] == "00000000":
                    return str(ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1]))
    except (OSError, ValueError):
        pass
    return None


def trusted_proxies(raw: str | None = None, route_file: str = "/proc/net/route") -> tuple:
    """Networks whose X-Real-IP is believed: PSC_TRUSTED_PROXIES (comma-separated IPs/CIDRs).

    Default is loopback only. A private peer is not enough: through the loopback-published Docker
    port every local client reaches the app from the bridge address and could rotate the header to
    dodge the per-address limit, so the deployment names its proxy explicitly. `gateway` means the
    container's default gateway: in the pilot the host nginx reaches the app through the published
    loopback port, i.e. from the bridge gateway, whose address Docker assigns per network.
    """
    raw = os.environ.get("PSC_TRUSTED_PROXIES", "127.0.0.1/32,::1/128") if raw is None else raw
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if part == "gateway":
            part = default_gateway(route_file) or ""
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def from_proxy(scope, trusted=None) -> bool:
    trusted = trusted_proxies() if trusted is None else trusted
    try:
        peer = ipaddress.ip_address((scope.get("client") or ("",))[0])
    except ValueError:
        return False
    return any(peer in network for network in trusted)


def client_address(scope, headers, trusted=None) -> str:
    """The proxy's X-Real-IP is trusted only when the connection comes from a configured proxy."""
    peer = (scope.get("client") or ("",))[0]
    forwarded = headers.get(b"x-real-ip", b"").decode("latin-1").strip()
    try:
        if forwarded and from_proxy(scope, trusted):
            return str(ipaddress.ip_address(forwarded))
    except ValueError:
        pass
    return peer


def behind_https(scope, headers, trusted=None) -> bool:
    """HTTPS to the app itself, or X-Forwarded-Proto: https from a configured proxy (as for X-Real-IP)."""
    if scope.get("scheme") == "https":
        return True
    proto = headers.get(b"x-forwarded-proto", b"").decode("latin-1").strip().lower()
    return proto == "https" and from_proxy(scope, trusted)


def session_token(headers) -> str:
    for part in headers.get(b"cookie", b"").decode("latin-1").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            return value
    return ""


class PayloadTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "Запрос превышает лимит загрузки")


class GuardMiddleware:
    def __init__(self, app, password: str, max_body: int):
        self.app, self.password, self.max_body = app, password, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}

        async def reject(status, detail, extra=None):
            await JSONResponse({"detail": detail}, status_code=status, headers=extra)(scope, receive, send)

        path = scope["path"]
        public = path in PUBLIC_PATHS or path.startswith("/static/")
        if self.password:
            auth = scope["app"].state.auth
            address = client_address(scope, headers)
            user = auth.session_user(session_token(headers))
            basic = headers.get(b"authorization", b"").split(b" ", 1)
            if user is None and basic[0].lower() == b"basic":
                # Scripts (pilot verify, CI smoke) may still send a login and password in the header.
                try:
                    login, password = base64.b64decode(basic[1], validate=True).decode("utf-8").split(":", 1)
                    user = auth.authenticate(login, password, address)
                except LoginRejected as exc:
                    return await reject(401, str(exc))
                except (ValueError, IndexError, binascii.Error, UnicodeError):
                    return await reject(401, "Нужно войти в ПСК")
            if user is None and not public:
                accept = headers.get(b"accept", b"").decode("latin-1")
                if scope["method"] in {"GET", "HEAD"} and "text/html" in accept:
                    target = path + ("?" + scope["query_string"].decode("latin-1") if scope.get("query_string") else "")
                    return await RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)(scope, receive, send)
                return await reject(401, "Нужно войти в ПСК")
            if user is not None and user["must_change"] and not public and path not in ACCOUNT_PATHS:
                if scope["method"] in {"GET", "HEAD"} and "text/html" in headers.get(b"accept", b"").decode("latin-1"):
                    return await RedirectResponse("/account", status_code=303)(scope, receive, send)
                return await reject(403, "Сначала смените временный пароль")
        elif path != "/health" and (scope.get("client") or ("",))[0] not in {"127.0.0.1", "::1", "testclient"}:
            return await reject(403, "Без пароля доступ разрешен только локально")
        else:
            user = LOCAL_USER
        if (path == "/users" or path.startswith("/api/users")) and (user or {}).get("role") != "admin":
            return await reject(403, "Раздел доступен только администратору")
        scope.setdefault("state", {})["user"] = user
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            if headers.get(b"x-psc-request") != b"1":
                return await reject(403, "Нет заголовка подтверждения запроса")
            origin = headers.get(b"origin")
            if origin:
                try:
                    parsed = urlsplit(origin.decode("latin-1"))
                except ValueError:
                    return await reject(403, "Некорректный Origin")
                if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != headers.get(b"host", b"").decode("latin-1").lower():
                    return await reject(403, "Межсайтовый запрос запрещен")
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await reject(400, "Некорректный размер запроса")
        if length < 0:
            return await reject(400, "Некорректный размер запроса")
        if length > self.max_body:
            return await reject(413, "Запрос превышает лимит загрузки")
        total, started = 0, False

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body:
                    raise PayloadTooLarge
            return message

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"), (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                ]
            await send(message)
        try:
            await self.app(scope, limited_receive, secure_send)
        except PayloadTooLarge:
            if started:
                raise
            await reject(413, "Запрос превышает лимит загрузки")
