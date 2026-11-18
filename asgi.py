"""Unified ASGI application mounting Streamable HTTP MCP server alongside VaultSwap Flask app.

Enables Cloud Run and production deployments to serve the Web UI, API, and the
Model Context Protocol (/mcp) endpoint on the exact same port and HTTPS domain.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from datetime import timezone
from typing import Any, AsyncIterator, Optional
from urllib.parse import parse_qs

import anyio
from a2wsgi import WSGIMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from app import create_app
import mcp_server
from models import ApiToken, db, utc_now


def _extract_bearer_token(scope) -> Optional[str]:
    for raw_name, raw_val in scope.get("headers") or []:
        if raw_name.lower() == b"authorization":
            val = raw_val.decode("latin-1").strip()
            if val.lower().startswith("bearer "):
                tok = val[7:].strip()
                if tok:
                    return tok
    return None


def _extract_query_key(scope) -> Optional[str]:
    qs = scope.get("query_string") or b""
    if not qs:
        return None
    parsed = parse_qs(qs.decode("latin-1"), keep_blank_values=False)
    keys = parsed.get("key")
    if keys and keys[0].strip():
        return keys[0].strip()
    return None


class McpTokenAuth:
    """ASGI middleware enforcing per-user ApiToken auth on /mcp routes."""

    def __init__(self, app, flask_app):
        self.app = app
        self.flask_app = flask_app

    def _verify_token(self, raw_token: str) -> bool:
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        with self.flask_app.app_context():
            tok = ApiToken.query.filter_by(token_hash=token_hash).first()
            if tok is None or tok.revoked_at is not None:
                return False
            now = utc_now()
            last_used = tok.last_used_at
            if last_used is not None and last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=timezone.utc)
            if last_used is None or (now - last_used).total_seconds() >= 300:
                tok.last_used_at = now
                db.session.commit()
            return True

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (scope.get("path") or "").startswith("/mcp"):
            bearer = _extract_bearer_token(scope)
            query_key = None if bearer else _extract_query_key(scope)
            token = bearer or query_key
            if not token or not await anyio.to_thread.run_sync(self._verify_token, token):
                resp = JSONResponse({"error": "Invalid or missing API token"}, status_code=401)
                await resp(scope, receive, send)
                return

            if query_key:
                async def send_no_store(message):
                    if message["type"] == "http.response.start":
                        headers = [
                            (k, v)
                            for k, v in (message.get("headers") or [])
                            if k.lower() != b"cache-control"
                        ]
                        headers.append((b"cache-control", b"no-store"))
                        message = {**message, "headers": headers}
                    await send(message)

                await self.app(scope, receive, send_no_store)
                return

        await self.app(scope, receive, send)


def create_asgi_app(flask_app: Optional[Any] = None) -> Starlette:
    """Creates a unified ASGI Starlette app routing /mcp to MCPServer and everything else to Flask."""
    flask_app = flask_app or create_app()

    # Disable DNS rebinding check so Cloud Run (*.run.app) or custom domains work smoothly
    # with the incoming Gemini mobile app HTTPS handshake
    mcp_starlette = mcp_server.mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp_starlette.router.lifespan_context(mcp_starlette):
            yield

    starlette_app = Starlette(
        routes=[
            *mcp_starlette.routes,
            Mount("/", app=WSGIMiddleware(flask_app)),
        ],
        lifespan=lifespan,
    )
    return McpTokenAuth(starlette_app, flask_app)  # type: ignore[return-value]


asgi_app = create_asgi_app()

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 5055))
    uvicorn.run("asgi:asgi_app", host="0.0.0.0", port=port, log_level="info")
