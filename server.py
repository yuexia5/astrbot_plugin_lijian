"""璃笺本地写入接口：与桌面版璃笺 ai_bridge 兼容的 HTTP API。

- GET    /api/v1/health
- POST   /api/v1/diary           （Bearer 令牌）
- GET    /api/v1/notes           （Bearer 令牌，?include_archived=true&include_body=true）
- POST   /api/v1/notes           （Bearer 令牌，新建便签）
- GET    /api/v1/notes/{id}      （Bearer 令牌）
- PATCH  /api/v1/notes/{id}      （Bearer 令牌）
- DELETE /api/v1/notes/{id}      （Bearer 令牌，归档）

在独立线程 + 独立事件循环中运行，便于 AstrBot 热重载时干净地启停。
"""

from __future__ import annotations

import asyncio
import hmac
import threading
from typing import Any, Callable, Optional

from astrbot.api import logger

import aiohttp
from aiohttp import web

JSON = dict[str, Any]


def _json_response(data: JSON, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda v: _to_json(v))


def _to_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _ok(payload: JSON | None = None) -> web.Response:
    body: JSON = {"ok": True}
    if payload:
        body.update(payload)
    return _json_response(body)


def _error(message: str, status: int = 400) -> web.Response:
    return _json_response({"ok": False, "error": message}, status=status)


@web.middleware
async def _security_headers(request: web.Request, handler):
    """给所有响应当上几顶安全帽。纯 JSON 接口也值当，成本几乎为零。"""
    response = await handler(request)
    # SSE 在 handler 内部就已经 prepare 并发头了，这时再改 headers 会炸。
    if not response.prepared:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cache-Control", "no-store")
    return response


class BridgeServer:
    """在后台线程中运行的 aiohttp 桥接服务。"""

    def __init__(
        self,
        db: Any,
        host: str = "127.0.0.1",
        port: int = 27176,
        token_provider: Optional[Callable[[], str]] = None,
    ):
        self.db = db
        self.host = host
        self.port = port
        self.token_provider = token_provider or (lambda: db.settings()["ai_token"])
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._ready = threading.Event()
        self._start_error: Optional[str] = None
        self._sse_clients: set[asyncio.Queue] = set()
        self._sse_last_seen: Optional[str] = None
        self._sse_seeded = False
        self._watcher_task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._start_error is None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._start_error = None
        # 重启用同一个 BridgeServer 时必须清掉上一次的就绪标记，
        # 否则第二次启动会在服务真正起来之前就返回成功。
        self._ready.clear()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="lijian-bridge", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            self._start_error = "启动超时"
            return False
        if self._start_error:
            return False
        logger.info(f"璃笺本地接口已启动: http://{self.host}:{self.port}")
        return True

    def stop(self) -> None:
        loop = self._loop
        if loop is None or self._thread is None:
            return
        if self._start_error is None:
            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
                future.result(timeout=4)
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"璃笺本地接口停止时出错: {exc}")
        self._thread.join(timeout=5)
        self._thread = None
        self._loop = None

    def _run(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
            self._ready.set()
        except Exception as exc:  # noqa: BLE001
            self._start_error = str(exc)
            self._ready.set()
            logger.error(f"璃笺本地接口启动失败: {exc}")
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _serve(self) -> None:
        app = web.Application(
            client_max_size=16 * 1024 * 1024,
            middlewares=[_security_headers],
        )
        app.router.add_route("*", "/api/v1/health", self._handle_health)
        app.router.add_route("*", "/api/v1/diary", self._handle_diary)
        app.router.add_route("*", "/api/v1/notes", self._handle_notes_list)
        app.router.add_route("*", "/api/v1/notes/{note_id}", self._handle_note)
        app.router.add_route("GET", "/api/v1/events", self._handle_events)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # 热重载时旧实例的 socket 常常还没落气，头一次 bind 会撞 EADDRINUSE。
        # 等一会儿再试比直接放弃划算得多——桥接挂了，外部脚本就写不进日记了。
        last_exc: Optional[BaseException] = None
        for attempt in range(4):
            try:
                self._site = web.TCPSite(self._runner, self.host, self.port)
                await self._site.start()
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                self._site = None
                if attempt < 3:
                    await asyncio.sleep(0.5 * (attempt + 1))
        if last_exc is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._runner = None
            raise last_exc
        self._watcher_task = asyncio.create_task(self._watch_notes())

    async def _shutdown(self) -> None:
        if self._watcher_task:
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watcher_task = None
        for queue in list(self._sse_clients):
            queue.put_nowait(None)
        self._sse_clients.clear()
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

    # ---------- 处理 ----------

    def _authorized(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token_provider()}"
        return hmac.compare_digest(auth, expected)

    def _is_loopback(self, request: web.Request) -> bool:
        host = request.remote or ""
        return host in ("127.0.0.1", "::1", "localhost")

    @staticmethod
    def _behind_proxy(request: web.Request) -> bool:
        """带着转发头说明前面还有一层反代。

        这时 remote 往往是 127.0.0.1（反代自己），单看它会把外网请求误判成本机，
        query 里的令牌就会一路写进反代日志。所以有转发头一律不认 query 令牌。
        """
        return any(
            request.headers.get(name)
            for name in ("X-Forwarded-For", "X-Real-IP", "Forwarded")
        )

    def _events_token_ok(self, request: web.Request) -> bool:
        """SSE 认证：优先 header；query 令牌只接受本机直连。

        EventSource 不能带自定义头，所以本机脚本会用 ?token=。
        但令牌落在 URL 里会进反代和访问日志，因此只在回环地址、且没经过反代时放行。
        """
        if self._authorized(request):
            return True
        query_token = request.query.get("token")
        if not query_token:
            return False
        if not self._is_loopback(request) or self._behind_proxy(request):
            return False
        return hmac.compare_digest(str(query_token), self.token_provider())

    async def _handle_health(self, request: web.Request) -> web.Response:
        return _ok(
            {
                "name": "璃笺",
                "endpoints": [
                    "POST /api/v1/diary",
                    "GET /api/v1/notes",
                    "POST /api/v1/notes",
                    "GET /api/v1/notes/{id}",
                    "PATCH /api/v1/notes/{id}",
                    "DELETE /api/v1/notes/{id}",
                    "GET /api/v1/events",
                ],
            }
        )

    # ---------- SSE 实时推送 ----------

    async def _watch_notes(self) -> None:
        """每 2 秒取一次书架签名，变化就广播给所有 SSE 客户端。

        用条数 + 最新更新时间 + 归档数做签名，比逐条拉 id/updated_at 轻。
        """
        while True:
            try:
                signature = self.db.note_signature()
            except Exception:  # noqa: BLE001
                signature = None
            if self._sse_seeded and signature != self._sse_last_seen:
                await self._broadcast_sse({"type": "notes_changed"})
            self._sse_last_seen = signature
            self._sse_seeded = True
            # 没客户端时放慢轮询，别白烧 CPU
            await asyncio.sleep(2 if self._sse_clients else 5)

    async def _broadcast_sse(self, data: dict[str, Any]) -> None:
        if not self._sse_clients:
            return
        import json

        payload = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        for queue in list(self._sse_clients):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        if not self._events_token_ok(request):
            return self._unauthorized()
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._sse_clients.add(queue)
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                if message is None:
                    break
                await resp.write(message.encode("utf-8"))
        except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
            pass
        finally:
            self._sse_clients.discard(queue)
        return resp

    async def _handle_diary(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid json body")
        if not isinstance(payload, dict):
            return _error("invalid json body")
        content_md = payload.get("content_md")
        if not isinstance(content_md, str) or not content_md.strip():
            return _error("content_md is required")
        try:
            note = self.db.write_diary_from_ai(
                {
                    "content_md": content_md,
                    "title": payload.get("title") or None,
                    "diary_date": payload.get("diary_date") or None,
                    "tags": payload.get("tags") or [],
                    "mode": payload.get("mode") or "append_today",
                }
            )
        except Exception as exc:
            return self._db_error(exc)
        return _ok(
            {
                "note_id": note["id"],
                "title": note["title"],
                "diary_date": note["diary_date"],
            }
        )

    async def _handle_notes_list(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        method = request.method.upper()
        if method == "POST":
            return await self._handle_notes_create(request)
        include_archived = request.query.get("include_archived", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        include_body = request.query.get("include_body", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        try:
            notes = self.db.list_notes(include_archived)
        except Exception as exc:
            return self._db_error(exc)
        return _ok(
            {
                "notes": [
                    {
                        "id": note["id"],
                        "title": note["title"],
                        "kind": note["kind"],
                        "diary_date": note["diary_date"],
                        "tags": note["tags"],
                        "source": note["source"],
                        "archived": note["archived"],
                        "created_at": note["created_at"],
                        "updated_at": note["updated_at"],
                        **({"body_md": note["body_md"]} if include_body else {}),
                    }
                    for note in notes
                ]
            }
        )

    async def _handle_notes_create(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid json body")
        if not isinstance(payload, dict):
            return _error("invalid json body")
        body_md = payload.get("body_md") or payload.get("content_md")
        if not isinstance(body_md, str):
            return _error("body_md is required")
        try:
            note = self.db.create_note(
                {
                    "body_md": body_md,
                    "title": payload.get("title") or None,
                    "kind": payload.get("kind") or "note",
                    "diary_date": payload.get("diary_date") or None,
                    "tags": payload.get("tags") or [],
                    "source": payload.get("source") or "astrbot",
                }
            )
        except Exception as exc:
            return self._db_error(exc)
        return _ok(
            {
                "action": "created",
                "note": note,
            }
        )

    async def _handle_note(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        note_id = request.match_info["note_id"]
        method = request.method.upper()
        try:
            if method == "GET":
                note = self.db.get_note(note_id)
                return _ok({"note": note})
            if method == "DELETE":
                note = self.db.archive_note(note_id, True)
                return _ok(
                    {
                        "action": "archived",
                        "note_id": note["id"],
                        "title": note["title"],
                        "diary_date": note["diary_date"],
                    }
                )
            if method == "PATCH":
                payload = await request.json()
                if not isinstance(payload, dict):
                    return _error("invalid json body")
                if not payload:
                    return _error("at least one field is required")
                body_value = payload.get("content_md")
                if body_value is None:
                    body_value = payload.get("body_md")
                patch = {
                    "title": payload.get("title"),
                    "body_md": body_value,
                    "kind": payload.get("kind"),
                    "tags": payload.get("tags"),
                }
                if "diary_date" in payload and payload["diary_date"] is not None:
                    patch["diary_date"] = payload["diary_date"]
                patch = {key: value for key, value in patch.items() if value is not None}
                if not patch:
                    return _error("at least one field is required")
                note = self.db.update_note(note_id, patch)
                return _ok(
                    {
                        "action": "updated",
                        "note_id": note["id"],
                        "title": note["title"],
                        "diary_date": note["diary_date"],
                    }
                )
        except Exception as exc:
            return self._db_error(exc)
        return _error("method not allowed", status=405)

    def _unauthorized(self) -> web.Response:
        return _json_response({"ok": False, "error": "unauthorized"}, status=401)

    def _db_error(self, exc: Exception) -> web.Response:
        """把异常转成对外消息。

        数据层抛的 DatabaseError 和参数错误都是业务口径，照实回；
        其余（sqlite 打不开、磁盘满之类）只记日志，对外统一 internal error，
        免得把数据库路径、表结构这些东西回给调用方。
        """
        text = str(exc) or exc.__class__.__name__
        is_business = exc.__class__.__name__ == "DatabaseError" or isinstance(
            exc, (ValueError, KeyError)
        )
        if not is_business:
            logger.warning("璃笺本地接口内部错误 %s: %s", exc.__class__.__name__, text)
            return _error("internal error", status=500)
        return _error(text, status=404 if "not found" in text else 400)
