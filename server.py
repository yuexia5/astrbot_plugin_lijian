"""璃笺本地写入接口：与桌面版璃笺 ai_bridge 兼容的 HTTP API。

- GET    /api/v1/health
- POST   /api/v1/diary           （Bearer 令牌）
- GET    /api/v1/notes           （Bearer 令牌，?include_archived=true）
- GET    /api/v1/notes/{id}      （Bearer 令牌）
- PATCH  /api/v1/notes/{id}      （Bearer 令牌）
- DELETE /api/v1/notes/{id}      （Bearer 令牌，归档）

在独立线程 + 独立事件循环中运行，便于 AstrBot 热重载时干净地启停。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger("astrbot_plugin_lijian")

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

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._start_error is None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._start_error = None
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
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_route("*", "/api/v1/health", self._handle_health)
        app.router.add_route("*", "/api/v1/diary", self._handle_diary)
        app.router.add_route("*", "/api/v1/notes", self._handle_notes_list)
        app.router.add_route("*", "/api/v1/notes/{note_id}", self._handle_note)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    async def _shutdown(self) -> None:
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

    # ---------- 处理 ----------

    def _authorized(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token_provider()}"
        return auth == expected

    async def _handle_health(self, request: web.Request) -> web.Response:
        return _ok(
            {
                "name": "璃笺",
                "endpoints": [
                    "POST /api/v1/diary",
                    "GET /api/v1/notes",
                    "GET /api/v1/notes/{id}",
                    "PATCH /api/v1/notes/{id}",
                    "DELETE /api/v1/notes/{id}",
                ],
            }
        )

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
            return _error(str(exc))
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
        include_archived = request.query.get("include_archived", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        try:
            notes = self.db.list_notes(include_archived)
        except Exception as exc:
            return _error(str(exc))
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
                    }
                    for note in notes
                ]
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
                patch = {
                    "title": payload.get("title"),
                    "body_md": payload.get("content_md") or payload.get("body_md"),
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
            return _error(str(exc), status=404 if "not found" in str(exc) else 400)
        return _error("method not allowed", status=405)

    def _unauthorized(self) -> web.Response:
        return _json_response({"ok": False, "error": "unauthorized"}, status=401)
