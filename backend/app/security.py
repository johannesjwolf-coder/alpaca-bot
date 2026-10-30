"""Zugriffsschutz für eine nur lokal gebundene Anwendung.

* Host-Header-Prüfung (TrustedHost) gegen DNS-Rebinding.
* Mutierende API-Aufrufe brauchen den Header ``X-Requested-With: alpaca-bot-ui``.
  Browser senden diesen Header bei fremden Seiten nur nach einem CORS-Preflight,
  den diese Anwendung nie erlaubt (kein CORS) → wirksamer CSRF-Schutz.
* Ist ein Origin-Header vorhanden, muss er in der erlaubten Liste stehen.
* Restriktive Sicherheitsheader für alle Antworten.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_HEADER = b"x-requested-with"
CSRF_VALUE = b"alpaca-bot-ui"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    ),
    (b"cache-control", b"no-store"),
]


class OriginCsrfMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed = {o.rstrip("/").lower() for o in allowed_origins}

    async def _reject(self, send: Send, detail: str) -> None:
        body = ('{"detail": "%s"}' % detail).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json"), *SECURITY_HEADERS],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if scope["method"] in MUTATING and scope["path"].startswith("/api/"):
            if headers.get(CSRF_HEADER) != CSRF_VALUE:
                await self._reject(send, "CSRF-Schutz: Header X-Requested-With fehlt oder ist falsch.")
                return
            origin = headers.get(b"origin")
            if origin is not None and origin.decode("latin-1").rstrip("/").lower() not in self.allowed:
                await self._reject(send, "Origin nicht erlaubt.")
                return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in SECURITY_HEADERS if k not in existing]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)
