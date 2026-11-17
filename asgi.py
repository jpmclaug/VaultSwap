"""Unified ASGI application mounting Streamable HTTP MCP server alongside VaultSwap Flask app.

Enables Cloud Run and production deployments to serve the Web UI, API, and the
Model Context Protocol (/mcp) endpoint on the exact same port and HTTPS domain.
"""

from __future__ import annotations

import contextlib
import os
from typing import AsyncIterator

from a2wsgi import WSGIMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from app import create_app
import mcp_server


def create_asgi_app() -> Starlette:
    """Creates a unified ASGI Starlette app routing /mcp to MCPServer and everything else to Flask."""
    flask_app = create_app()

    # Disable DNS rebinding check so Cloud Run (*.run.app) or custom domains work smoothly
    # with the incoming Gemini mobile app HTTPS handshake
    mcp_starlette = mcp_server.mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp_starlette.router.lifespan_context(mcp_starlette):
            yield

    return Starlette(
        routes=[
            *mcp_starlette.routes,
            Mount("/", app=WSGIMiddleware(flask_app)),
        ],
        lifespan=lifespan,
    )


asgi_app = create_asgi_app()

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 5055))
    uvicorn.run("asgi:asgi_app", host="0.0.0.0", port=port, log_level="info")
