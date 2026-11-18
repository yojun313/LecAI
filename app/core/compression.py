"""텍스트 응답(HTML · JSON · JS · CSS) gzip 압축.

nginx 는 프록시 응답을 압축하지 않게 설정돼 있어(gzip_proxied 꺼짐) 문서 내용(수백 KB 마크다운) · 페이지 HTML ·
작업 목록 JSON 이 그대로 전송된다. 여기서 압축한다.

일부러 좁게 적용한다:
- 한 번에 끝나는 응답(본문 1조각)만 — 파일 다운로드 · 영상 · 음성(Range) · SSE 스트리밍은 건드리지 않는다
- 압축할 만한 형식 · 크기(MIN_SIZE 이상) · 200 응답 · 이미 인코딩되지 않은 것만
"""

import gzip

from starlette.datastructures import Headers, MutableHeaders

MIN_SIZE = 1400
COMPRESSIBLE = (
    "text/html",
    "text/plain",
    "text/css",
    "text/markdown",
    "application/json",
    "application/javascript",
    "text/javascript",
    "image/svg+xml",
)


class TextGzipMiddleware:
    def __init__(self, app, minimum_size: int = MIN_SIZE, level: int = 6):
        self.app = app
        self.minimum_size = minimum_size
        self.level = level

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get(
            "accept-encoding", ""
        ):
            await self.app(scope, receive, send)
            return

        start = None

        async def wrapped(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message  # 본문 첫 조각을 보고 정한다
                return
            if message["type"] == "http.response.body" and start is not None:
                head, start = start, None
                headers = MutableHeaders(raw=head["headers"])
                body = message.get("body", b"")
                ctype = headers.get("content-type", "").split(";")[0].strip().lower()
                if (
                    head["status"] == 200
                    and not message.get("more_body", False)
                    and len(body) >= self.minimum_size
                    and ctype in COMPRESSIBLE
                    and "content-encoding" not in headers
                    and "content-range" not in headers
                ):
                    packed = gzip.compress(body, compresslevel=self.level)
                    headers["content-encoding"] = "gzip"
                    headers["content-length"] = str(len(packed))
                    headers.add_vary_header("Accept-Encoding")
                    await send(head)
                    await send({"type": "http.response.body", "body": packed})
                    return
                await send(head)
            await send(message)

        await self.app(scope, receive, wrapped)
