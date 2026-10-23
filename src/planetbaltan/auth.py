import hmac
from starlette.responses import JSONResponse


class TokenGate:
    """ASGI gate also protects mounted MCP routes."""
    def __init__(self, app, token: str, protected):
        self.app, self.token, self.protected = app, token, protected

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token and self.protected(scope["path"], scope["method"]):
            headers = dict(scope["headers"])
            supplied = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(supplied, "Bearer " + self.token):
                await JSONResponse({"detail": "authentication required"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def mcp_security(public_url: str):
    from urllib.parse import urlsplit
    from mcp.server.transport_security import TransportSecuritySettings
    origin = urlsplit(public_url)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", origin.netloc],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", f"{origin.scheme}://{origin.netloc}"],
    )
