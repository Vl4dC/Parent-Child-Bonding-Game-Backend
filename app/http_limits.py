from fastapi import HTTPException


class BodyLimit:
    """Bound actual received bytes, even without an honest Content-Length header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = 61 * 2**20 if scope["path"] == "/api/materials" else 4 * 2**20
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > limit:
                raise HTTPException(413, "Request body exceeds the allowed size.")
            return message

        await self.app(scope, bounded_receive, send)
