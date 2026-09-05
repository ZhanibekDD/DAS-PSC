"""Local-only default, optional shared pilot password, same-origin writes and body limits."""
import base64
import binascii
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException


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

        if scope["path"] != "/health":
            if self.password:
                try:
                    token = headers.get(b"authorization", b"").split(b" ", 1)
                    if token[0].lower() != b"basic":
                        raise ValueError
                    raw = base64.b64decode(token[1], validate=True).decode("utf-8")
                    user, password = raw.split(":", 1)
                    valid = secrets.compare_digest(user.encode(), b"psc") & secrets.compare_digest(password.encode(), self.password.encode())
                except (ValueError, IndexError, binascii.Error, UnicodeError):
                    valid = False
                if not valid:
                    return await reject(401, "Нужна авторизация ПСК", {"WWW-Authenticate": 'Basic realm="DAS-PSC"'})
            elif (scope.get("client") or ("",))[0] not in {"127.0.0.1", "::1", "testclient"}:
                return await reject(403, "Без пароля доступ разрешен только локально")
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
