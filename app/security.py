from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Protocol

from starlette.types import ASGIApp, Receive, Scope, Send


class Authenticator(Protocol):
    def authenticate(self, token: str) -> bool: ...


class StaticBearerAuthenticator:
    def __init__(self, expected_token: str) -> None:
        self._expected = expected_token.encode("utf-8")

    def authenticate(self, token: str) -> bool:
        return hmac.compare_digest(self._expected, token.encode("utf-8"))


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw = headers.get(b"authorization", b"")
        try:
            scheme, token = raw.decode("latin-1").split(" ", 1)
        except ValueError:
            scheme, token = "", ""
        if scheme.lower() != "bearer" or not token or not self.authenticator.authenticate(token):
            body = json.dumps(
                {"error": "invalid_token", "error_description": "Authentication required"}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                        (b"www-authenticate", b'Bearer realm="awita-mail"'),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def redact_text(value: str, secrets: list[str]) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
