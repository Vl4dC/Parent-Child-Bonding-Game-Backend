from fastapi import HTTPException

MIB = 2**20
LIMITS = {
    "/api/materials": 61 * MIB,
    "/api/chat/image": 36 * MIB,  # up to four 8 MiB pictures plus the form fields
}


class BodyLimit:
    """Bound actual received bytes, even without an honest Content-Length header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = LIMITS.get(scope["path"], 4 * MIB)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > limit:
                raise HTTPException(413, "Request body exceeds the allowed size.")
            return message

        await self.app(scope, bounded_receive, send)
