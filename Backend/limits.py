import asyncio
from collections import Counter

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class Window:
    def __init__(self, seconds, capacity):
        self.seconds = seconds
        self.capacity = capacity
        self.started = None
        self.counts = Counter()

    def allow(self, key, limit, now):
        if self.started is None or now < self.started or now - self.started >= self.seconds:
            self.started = now
            self.counts.clear()
        if self.counts[key] >= limit or (key not in self.counts and len(self.counts) >= self.capacity):
            return False
        self.counts[key] += 1
        return True


class APIRequestLimits:
    MAX_BODY = 16 * 1024

    def __init__(self, app, clock):
        self.app = app
        self.clock = clock
        self.budget = Window(60, 5000)
        self.registrations = Window(60, 2000)

    @staticmethod
    async def reject(scope, receive, send, status, code, message):
        response = JSONResponse({"error": {"code": code, "message": message}},
                                status_code=status, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        peer = client[0] if client else "unknown"
        now = self.clock()
        registering = scope["path"].rstrip("/") == "/api/v1/registrations" and scope["method"] == "POST"
        if (not self.budget.allow(peer, 600, now)
                or (registering and not self.registrations.allow(peer, 30, now))
                or not self.budget.allow(None, 4800, now)):
            await self.reject(scope, receive, send, 429, "RATE_LIMITED", "요청이 많습니다. 잠시 후 다시 시도해 주세요.")
            return

        if scope["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
            await self.app(scope, receive, send)
            return

        size = Headers(scope=scope).get("content-length")
        if size is not None:
            try:
                size = int(size)
            except ValueError:
                size = -1
            if size < 0:
                await self.reject(scope, receive, send, 400, "INVALID_REQUEST", "본문 길이가 올바르지 않습니다.")
                return
            if size > self.MAX_BODY:
                await self.reject(scope, receive, send, 413, "BODY_TOO_LARGE", "요청 본문이 너무 큽니다.")
                return

        body = bytearray()
        try:
            async with asyncio.timeout(8):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.MAX_BODY:
                        await self.reject(scope, receive, send, 413, "BODY_TOO_LARGE", "요청 본문이 너무 큽니다.")
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await self.reject(scope, receive, send, 408, "BODY_TIMEOUT", "요청 본문 수신 시간이 초과됐습니다.")
            return

        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)
