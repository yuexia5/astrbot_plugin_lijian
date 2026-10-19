"""璃笺 AstrBot 插件：无头环境下的日记与便签书库。

把桌面版璃笺（Tauri）的存储与写入接口移植进 AstrBot：
- 指令：/lijian_status /lijian_write /lijian_list /lijian_read /lijian_delete
- LLM 工具：write_lijian_diary / list_lijian_notes / read_lijian_note /
  update_lijian_note / delete_lijian_note
- 本地写入接口：监听 127.0.0.1:27176，与桌面版璃笺 API 兼容
- WebUI 页面：书架 (pages/bookshelf)
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .db import (
        Database,
        DatabaseError,
        clean_tags,
        date_prefix,
        local_today,
        markdown_for_note,
        normalize_kind,
        safe_filename,
    )
    from .server import BridgeServer
except ImportError:
    from db import (
        Database,
        DatabaseError,
        clean_tags,
        date_prefix,
        local_today,
        markdown_for_note,
        normalize_kind,
        safe_filename,
    )
    from server import BridgeServer

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # noqa: BLE001
    get_astrbot_plugin_data_path = None

try:
    from astrbot.api.web import error_response, file_response, json_response, request
    HAS_WEB_API = True
except Exception:  # noqa: BLE001
    HAS_WEB_API = False

PLUGIN_NAME = "astrbot_plugin_lijian"
# 单篇 Markdown 导入上限。日记再长也到不了这个量级，超过基本是误传大文件。
MAX_IMPORT_BYTES = 8 * 1024 * 1024

# 本地图片：存在插件数据目录的 media/ 下，正文里写 media/<文件名>。
MEDIA_DIR_NAME = "media"
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MEDIA_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# 上传时文件名是 uuid4 的 32 位十六进制 + 白名单扩展名，读的时候也照这个卡一遍，
# 挡住 ../ 之类的路径穿越。
MEDIA_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|jpeg|gif|webp)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 新建日记时套的模板。{date} 会替换成本地日期。
DEFAULT_DIARY_TEMPLATE = (
    "**天气**：\n"
    "**心情**：\n\n"
    "## 今天做了什么\n\n- \n\n"
    "## 三件小事\n\n1. \n2. \n3. \n"
)
PLUGIN_VERSION = "1.1.1"


@register(
    PLUGIN_NAME,
    "璃笺",
    "无头环境下运行的璃笺日记库：AI 写入、指令管理、书架页面。",
    PLUGIN_VERSION,
)
class LijianPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._db: Database | None = None
        self._bridge: BridgeServer | None = None
        self._data_dir = self._resolve_data_dir()
        try:
            self._open_database()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"璃笺数据库初始化失败: {exc}")
        if self._db is not None:
            self._start_bridge()
        if self._db is not None and HAS_WEB_API:
            self._register_web_apis()

    # ---------- 生命周期 ----------

    def _resolve_data_dir(self) -> Path:
        base: Path | None = None
        if get_astrbot_plugin_data_path is not None:
            try:
                base = Path(get_astrbot_plugin_data_path())
            except Exception:  # noqa: BLE001
                base = None
        if base is None:
            base = Path("data/plugins")
        return base / PLUGIN_NAME

    def _open_database(self) -> None:
        configured = str(self.config.get("db_path", "") or "").strip()
        if configured:
            db_path = configured
        else:
            db_path = self._data_dir / "lijian.sqlite3"
        self._db = Database(db_path)
        self._db.ensure_ai_token(str(self.config.get("token", "") or ""))
        # 只有库彻底为空时才放第一篇。用 include_archived=True 判断，
        # 否则用户把笔记全部归档后重载插件，又会冒出一篇「第一笺」。
        if not self._db.list_notes(True):
            self._seed_first_note()
        logger.info(f"璃笺数据库已打开: {db_path}")

    def _seed_first_note(self) -> None:
        try:
            self._db.create_note(
                {
                    "title": "第一笺",
                    "body_md": (
                        "# 第一笺\n\n"
                        "今天想记下什么？\n\n"
                        "这是一本会自己长高的日记书。\n"
                        "试着对 AI 说「把今天的总结写进日记」，或打开 WebUI 里的书架页面。"
                    ),
                    "kind": "diary",
                    "diary_date": local_today(),
                    "tags": ["璃笺"],
                    "source": "local",
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"创建第一笺失败: {exc}")

    def _start_bridge(self) -> None:
        try:
            enabled = bool(self.config.get("bridge_enabled", True))
        except Exception:  # noqa: BLE001
            enabled = True
        if not enabled:
            logger.info("璃笺本地写入接口未启用（bridge_enabled=false）")
            return
        host = str(self.config.get("bridge_host", "127.0.0.1"))
        try:
            port = int(self.config.get("bridge_port", 27176))
        except (TypeError, ValueError):
            port = 27176
        if host not in ("127.0.0.1", "::1", "localhost"):
            # 绑到 0.0.0.0 之后，同一网段的人拿着令牌就能读写日记。
            # 这是用户自己选的，但得让他清楚现在是什么状态。
            logger.warning(
                f"璃笺本地接口监听在 {host}:{port}，"
                "已超出本机范围，同网段可访问。若不需要请把 bridge_host 改回 127.0.0.1。"
            )
        self._bridge = BridgeServer(self._db, host=host, port=port)
        if not self._bridge.start():
            logger.warning(
                f"璃笺本地接口启动失败（端口 {host}:{port} 可能被占用），"
                "写入仅能通过指令 / LLM 工具 / 书架页面进行。"
            )

    def _register_web_apis(self) -> None:
        register = self.context.register_web_api
        register(f"/{PLUGIN_NAME}/notes", self.api_list_notes, ["GET"], "书架：列出条目")
        register(f"/{PLUGIN_NAME}/notes/<note_id>", self.api_get_note, ["GET"], "书架：读取条目")
        register(f"/{PLUGIN_NAME}/notes", self.api_create_note, ["POST"], "书架：新建条目")
        register(f"/{PLUGIN_NAME}/notes/<note_id>", self.api_update_note, ["PATCH", "POST"], "书架：修改条目")
        register(f"/{PLUGIN_NAME}/notes/<note_id>", self.api_archive_note, ["DELETE"], "书架：归档条目")
        register(f"/{PLUGIN_NAME}/notes/<note_id>/archive", self.api_archive_note, ["POST"], "书架：归档条目")
        register(f"/{PLUGIN_NAME}/notes/<note_id>/restore", self.api_restore_note, ["POST"], "书架：恢复条目")
        register(f"/{PLUGIN_NAME}/notes/<note_id>/pin", self.api_pin_note, ["POST"], "书架：置顶条目")
        register(f"/{PLUGIN_NAME}/notes/<note_id>/unpin", self.api_unpin_note, ["POST"], "书架：取消置顶")
        register(f"/{PLUGIN_NAME}/diary", self.api_write_diary, ["POST"], "书架：写入今日日记")
        register(f"/{PLUGIN_NAME}/notes/<note_id>/markdown", self.api_note_markdown, ["GET"], "书架：导出单篇")
        register(f"/{PLUGIN_NAME}/export/all", self.api_export_all, ["GET"], "书架：导出全部 (zip)")
        register(f"/{PLUGIN_NAME}/export/json", self.api_export_json, ["GET"], "书架：全量 JSON 备份")
        register(f"/{PLUGIN_NAME}/import/json", self.api_import_json, ["POST"], "书架：从 JSON 备份恢复")
        register(f"/{PLUGIN_NAME}/random", self.api_random_note, ["GET"], "书架：随手翻一篇")
        register(f"/{PLUGIN_NAME}/weekly", self.api_weekly, ["GET"], "书架：周报素材")
        register(f"/{PLUGIN_NAME}/signature", self.api_signature, ["GET"], "书架：内容签名（轮询用）")
        register(f"/{PLUGIN_NAME}/media", self.api_upload_media, ["POST"], "书架：上传图片")
        register(f"/{PLUGIN_NAME}/media/<name>", self.api_get_media, ["GET"], "书架：读取图片")
        register(f"/{PLUGIN_NAME}/import", self.api_import, ["POST"], "书架：导入 Markdown")
        register(f"/{PLUGIN_NAME}/stats", self.api_stats, ["GET"], "书架：统计")
        register(f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "书架：运行状态")

    async def terminate(self):
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        if self._db is not None:
            self._db.close()
            self._db = None
        logger.info("璃笺插件已卸载。")

    # ---------- 内部工具 ----------

    def _require_db(self) -> Database:
        if self._db is None:
            raise RuntimeError("璃笺数据库不可用")
        return self._db

    def _bridge_running(self) -> bool:
        return bool(self._bridge and self._bridge.running)

    def _tool_error(self, prefix: str, exc: Exception) -> str:
        return json.dumps(
            {"ok": False, "error": str(exc), "message": f"{prefix}：{exc}"},
            ensure_ascii=False,
        )

    @staticmethod
    def _summary(note: dict, include_body: bool = False) -> dict:
        summary = {
            "id": note["id"],
            "title": note["title"],
            "kind": note["kind"],
            "diary_date": note["diary_date"],
            "tags": note["tags"],
            "source": note["source"],
            "pinned": note["pinned"],
            "archived": note["archived"],
            "created_at": note["created_at"],
            "updated_at": note["updated_at"],
        }
        if include_body:
            summary["body_md"] = note.get("body_md", "")
        return summary

    # ---------- 指令 ----------

    @filter.command("lijian_status")
    async def lijian_status(self, event: AstrMessageEvent):
        """查看璃笺运行状态。"""
        if self._db is None:
            yield event.plain_result("璃笺数据库不可用，请检查插件配置中的 db_path。")
            return
        try:
            stats = self._db.stats()
        except Exception as exc:
            stats = {"notes": 0, "words": 0, "archived": 0}
            logger.warning(f"璃笺统计失败: {exc}")
        bridge_state = (
            f"接口已启动：{self._bridge.host}:{self._bridge.port}"
            if self._bridge_running()
            else ("接口未启用" if self._bridge is None else "接口启动失败（端口占用？）")
        )
        token = self._db.settings()["ai_token"]
        yield event.plain_result(
            f"璃笺状态：正常\n"
            f"数据库：{self._db.path}\n"
            f"{bridge_state}\n"
            f"写入令牌：{'已配置' if token else '未配置'}（可在书架页面复制）\n"
            f"共 {stats.get('notes', 0)} 篇 · {stats.get('words', 0)} 字 · "
            f"归档 {stats.get('archived', 0)} 篇"
        )

    @filter.command("lijian_write")
    async def lijian_write(self, event: AstrMessageEvent, content: str):
        """手动写入一篇日记（追加到今日）。"""
        if self._db is None:
            yield event.plain_result("璃笺数据库不可用。")
            return
        try:
            note = self._db.write_diary_from_ai(
                {
                    "content_md": content,
                    "title": "AstrBot 手动写入",
                    "tags": ["AstrBot"],
                    "mode": self.config.get("default_mode", "append_today"),
                }
            )
        except Exception as exc:
            yield event.plain_result(f"璃笺写入失败：{exc}")
            return
        yield event.plain_result(
            f"已写入璃笺：{note['title']}（{note.get('diary_date') or '无日期'}）"
        )

    @filter.command("lijian_list")
    async def lijian_list(self, event: AstrMessageEvent, limit: int = 20):
        """列出最近的日记和便签摘要。"""
        if self._db is None:
            yield event.plain_result("璃笺数据库不可用。")
            return
        try:
            notes = self._db.list_notes(False)[: max(1, min(int(limit), 50))]
        except Exception as exc:
            yield event.plain_result(f"璃笺列表读取失败：{exc}")
            return
        if not notes:
            yield event.plain_result("书架上还没有条目，试试 /lijian_write 或打开书架页面。")
            return
        lines = []
        for index, note in enumerate(notes, start=1):
            date_str = note.get("diary_date") or note["created_at"][:10]
            kind = "日记" if note["kind"] == "diary" else "便签"
            lines.append(f"{index}. [{kind} {date_str}] {note['title']} (id: {note['id'][:8]}…)")
        yield event.plain_result(f"璃笺书架（共 {len(notes)} 条）：\n" + "\n".join(lines))

    @filter.command("lijian_read")
    async def lijian_read(self, event: AstrMessageEvent, note_id: str):
        """读取某一篇日记或便签的完整内容。"""
        if self._db is None:
            yield event.plain_result("璃笺数据库不可用。")
            return
        try:
            note = self._db.get_note(note_id)
        except Exception as exc:
            yield event.plain_result(f"璃笺读取失败：{exc}")
            return
        excerpt = note["body_md"].strip()[:1500]
        if len(note["body_md"].strip()) > 1500:
            excerpt += "\n…（内容过长已截断）"
        tags = "、".join(note["tags"]) or "无"
        yield event.plain_result(
            f"《{note['title']}》\n"
            f"日期：{note.get('diary_date') or '无'} · 标签：{tags}\n"
            f"---\n{excerpt}"
        )

    @filter.command("lijian_delete")
    async def lijian_delete(self, event: AstrMessageEvent, note_id: str):
        """把某一篇日记或便签移入归档。"""
        if self._db is None:
            yield event.plain_result("璃笺数据库不可用。")
            return
        try:
            note = self._db.archive_note(note_id, True)
        except Exception as exc:
            yield event.plain_result(f"璃笺归档失败：{exc}")
            return
        yield event.plain_result(f"已移入归档：《{note['title']}》")

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="write_lijian_diary")
    async def write_lijian_diary(
        self,
        event: AstrMessageEvent,
        content_md: str,
        title: str = "",
        diary_date: str = "",
        tags: str = "",
        mode: str = "",
    ):
        """写入或追加一篇璃笺日记。

        Args:
            content_md(string): 要写入璃笺的 Markdown 正文。
            title(string): 日记标题，可为空。
            diary_date(string): 日记日期，格式为 YYYY-MM-DD，可为空表示今天。
            tags(string): 标签，多个标签用英文逗号分隔。
            mode(string): 写入模式，append_today 表示追加到当日，create 表示创建新日记。
        """
        if self._db is None:
            return json.dumps(
                {"ok": False, "error": "db unavailable", "message": "璃笺数据库不可用"},
                ensure_ascii=False,
            )
        mode = mode or self.config.get("default_mode", "append_today")
        try:
            note = self._db.write_diary_from_ai(
                {
                    "content_md": content_md,
                    "title": title or None,
                    "diary_date": diary_date or None,
                    "tags": split_tags(tags),
                    "mode": mode,
                }
            )
        except Exception as exc:
            logger.warning(f"璃笺写入失败: {exc}")
            return self._tool_error("璃笺写入失败", exc)
        return json.dumps(
            {
                "ok": True,
                "message": "已写入璃笺",
                "note_id": note["id"],
                "title": note["title"],
                "diary_date": note["diary_date"],
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="list_lijian_notes")
    async def list_lijian_notes(
        self,
        event: AstrMessageEvent,
        include_archived: bool = False,
    ):
        """列出璃笺中的便签和日记摘要。

        Args:
            include_archived(bool): 是否包含已归档条目，默认 false。
        """
        if self._db is None:
            return self._tool_error("璃笺列表读取失败", RuntimeError("数据库不可用"))
        try:
            notes = self._db.list_notes(include_archived=include_archived)
        except Exception as exc:
            logger.warning(f"璃笺列表读取失败: {exc}")
            return self._tool_error("璃笺列表读取失败", exc)
        return json.dumps(
            {"ok": True, "notes": [self._summary(note) for note in notes]},
            ensure_ascii=False,
        )

    @filter.llm_tool(name="lijian_weekly")
    async def lijian_weekly(
        self,
        event: AstrMessageEvent,
        start_date: str = "",
        end_date: str = "",
    ):
        """把一段时间的璃笺日记聚合成周报素材，供你进一步总结成周记。

        只返回原文聚合，不会替你下结论。拿到 digest_md 之后自行归纳即可；
        如果用户要求存档，可再调用 write_lijian_diary 把总结写回去。

        Args:
            start_date(string): 起始日期 YYYY-MM-DD，留空表示本周一。
            end_date(string): 结束日期 YYYY-MM-DD，留空表示今天。
        """
        if self._db is None:
            return self._tool_error("璃笺周报读取失败", RuntimeError("数据库不可用"))
        default_start, default_end = local_week_range()
        start = (start_date or default_start).strip()
        end = (end_date or default_end).strip()
        if not (DATE_RE.match(start) and DATE_RE.match(end)):
            return self._tool_error(
                "璃笺周报读取失败", ValueError("日期格式应为 YYYY-MM-DD")
            )
        if start > end:
            start, end = end, start
        try:
            notes = self._db.notes_in_range(start, end)
        except Exception as exc:
            logger.warning(f"璃笺周报读取失败: {exc}")
            return self._tool_error("璃笺周报读取失败", exc)
        return json.dumps(
            {
                "ok": True,
                "start": start,
                "end": end,
                "count": len(notes),
                "digest_md": build_weekly_digest(notes, start, end),
            },
            ensure_ascii=False,
        )

    @filter.llm_tool(name="read_lijian_note")
    async def read_lijian_note(
        self,
        event: AstrMessageEvent,
        note_id: str,
    ):
        """读取璃笺中某一篇便签或日记的完整内容。

        Args:
            note_id(string): 要读取的璃笺 note_id。通常先调用 list_lijian_notes 获取。
        """
        if self._db is None:
            return self._tool_error("璃笺读取失败", RuntimeError("数据库不可用"))
        try:
            note = self._db.get_note(note_id)
        except Exception as exc:
            logger.warning(f"璃笺读取失败: {exc}")
            return self._tool_error("璃笺读取失败", exc)
        return json.dumps({"ok": True, "note": note}, ensure_ascii=False)

    @filter.llm_tool(name="delete_lijian_note")
    async def delete_lijian_note(
        self,
        event: AstrMessageEvent,
        note_id: str,
    ):
        """删除璃笺中某一篇便签或日记。

        实际执行的是归档隐藏，不会永久删除磁盘数据。

        Args:
            note_id(string): 要删除的璃笺 note_id。通常先调用 list_lijian_notes 获取。
        """
        if self._db is None:
            return self._tool_error("璃笺删除失败", RuntimeError("数据库不可用"))
        try:
            note = self._db.archive_note(note_id, True)
        except Exception as exc:
            logger.warning(f"璃笺删除失败: {exc}")
            return self._tool_error("璃笺删除失败", exc)
        result = {"ok": True, "note": self._summary(note)}
        result["message"] = f"已移入归档：{note['title']}"
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="update_lijian_note")
    async def update_lijian_note(
        self,
        event: AstrMessageEvent,
        note_id: str,
        content_md: str = "",
        title: str = "",
        diary_date: str = "",
        tags: str = "",
        kind: str = "",
    ):
        """修改璃笺中某一篇便签或日记。

        Args:
            note_id(string): 要修改的璃笺 note_id。通常先调用 list_lijian_notes 或 read_lijian_note 获取。
            content_md(string): 新的 Markdown 正文；为空则不修改正文。
            title(string): 新标题；为空则不修改标题。
            diary_date(string): 新日期，格式 YYYY-MM-DD；为空则不修改日期。
            tags(string): 新标签，多个标签用英文逗号分隔；为空则不修改标签。
            kind(string): 条目类型，note 或 diary；为空则不修改类型。
        """
        if self._db is None:
            return self._tool_error("璃笺修改失败", RuntimeError("数据库不可用"))
        try:
            note = self._db.update_note(
                note_id,
                {
                    **({"content_md": content_md} if content_md else {}),
                    **({"title": title} if title else {}),
                    **({"diary_date": diary_date} if diary_date else {}),
                    **({"tags": split_tags(tags)} if tags else {}),
                    **({"kind": kind} if kind else {}),
                },
            )
        except Exception as exc:
            logger.warning(f"璃笺修改失败: {exc}")
            return self._tool_error("璃笺修改失败", exc)
        result = {"ok": True, "note": self._summary(note)}
        result["message"] = f"已修改璃笺：{note['title']}"
        return json.dumps(result, ensure_ascii=False)

    # ---------- 书架 Web API ----------

    async def api_list_notes(self):
        if self._db is None:
            return error_response("数据库不可用")
        include_archived = request.query.get("include_archived", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # 书架页要在本地搜正文，所以允许把 body_md 一起带回来。
        include_body = request.query.get("include_body", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # light=1：只回摘要，不带正文。首屏和筛选都不需要正文。
        light = request.query.get("light", "false").lower() in ("1", "true", "yes")
        kind = request.query.get("kind")
        q = (request.query.get("q") or "").strip()
        # 搜索下沉到 SQL：前端不用把整库正文拉下来再过滤。
        if q:
            notes = self._db.search_notes(q, include_archived)
        else:
            notes = self._db.list_notes(include_archived, light=light)
        if kind in ("diary", "note"):
            notes = [note for note in notes if note["kind"] == kind]
        return json_response(
            {"notes": [self._summary(note, include_body) for note in notes]}
        )

    async def api_get_note(self, note_id: str):
        if self._db is None:
            return error_response("数据库不可用")
        try:
            return json_response({"note": self._db.get_note(note_id)})
        except Exception as exc:
            return error_response(str(exc), status_code=404)

    async def api_create_note(self):
        if self._db is None:
            return error_response("数据库不可用")
        payload = await request.json(default={})
        title = payload.get("title") or ""
        body_md = payload.get("body_md") or ""
        kind_raw = payload.get("kind") or ("diary" if payload.get("diary_date") else "note")
        try:
            kind = normalize_kind(kind_raw)
        except DatabaseError as exc:
            return error_response(str(exc))
        try:
            note = self._db.create_note(
                {
                    "title": title,
                    "body_md": body_md,
                    "kind": kind,
                    "diary_date": payload.get("diary_date") or None,
                    "tags": payload.get("tags") or [],
                    "source": payload.get("source") or "page",
                }
            )
        except Exception as exc:
            return error_response(str(exc))
        return json_response({"note": note})

    async def api_update_note(self, note_id: str):
        if self._db is None:
            return error_response("数据库不可用")
        payload = await request.json(default={})
        patch: dict = {}
        if payload.get("title") is not None:
            patch["title"] = payload["title"]
        if payload.get("body_md") is not None:
            patch["body_md"] = payload["body_md"]
        if payload.get("kind") is not None:
            patch["kind"] = payload["kind"]
        if payload.get("tags") is not None:
            patch["tags"] = payload["tags"]
        if "diary_date" in payload:
            patch["diary_date"] = payload["diary_date"] or None
        if not patch:
            return error_response("没有要修改的字段")
        try:
            note = self._db.update_note(note_id, patch)
        except Exception as exc:
            return error_response(str(exc), status_code=404 if "not found" in str(exc) else 400)
        return json_response({"note": note})

    async def api_archive_note(self, note_id: str):
        if self._db is None:
            return error_response("数据库不可用")
        try:
            note = self._db.archive_note(note_id, True)
        except Exception as exc:
            return error_response(str(exc), status_code=404 if "not found" in str(exc) else 400)
        return json_response({"note": self._summary(note), "message": "已归档"})

    async def api_restore_note(self, note_id: str):
        if self._db is None:
            return error_response("数据库不可用")
        try:
            note = self._db.archive_note(note_id, False)
        except Exception as exc:
            return error_response(str(exc), status_code=404 if "not found" in str(exc) else 400)
        return json_response({"note": self._summary(note), "message": "已恢复"})

    async def api_pin_note(self, note_id: str):
        return self._toggle_pin(note_id, True)

    async def api_unpin_note(self, note_id: str):
        return self._toggle_pin(note_id, False)

    def _toggle_pin(self, note_id: str, pinned: bool):
        if self._db is None:
            return error_response("数据库不可用")
        try:
            note = self._db.pin_note(note_id, pinned)
        except Exception as exc:
            return error_response(
                str(exc), status_code=404 if "not found" in str(exc) else 400
            )
        return json_response(
            {"note": self._summary(note), "message": "已置顶" if pinned else "已取消置顶"}
        )

    async def api_random_note(self):
        if self._db is None:
            return error_response("数据库不可用")
        kind = request.query.get("kind")
        exclude = request.query.get("exclude") or None
        try:
            note = self._db.random_note(
                kind if kind in ("note", "diary") else None, exclude
            )
        except Exception as exc:
            return error_response(str(exc))
        if note is None:
            return error_response("书架上还没有可翻的条目", status_code=404)
        return json_response({"note": note})

    async def api_signature(self):
        """给书架页轮询用：只回一行签名，比每次拉全表便宜得多。"""
        if self._db is None:
            return error_response("数据库不可用")
        try:
            return json_response({"signature": self._db.note_signature()})
        except Exception as exc:
            return error_response(str(exc))

    async def api_weekly(self):
        """把一段时间的日记聚成 Markdown 素材，人工看或交给 AI 总结都行。"""
        if self._db is None:
            return error_response("数据库不可用")
        default_start, default_end = local_week_range()
        start = (request.query.get("start") or default_start).strip()
        end = (request.query.get("end") or default_end).strip()
        if not (DATE_RE.match(start) and DATE_RE.match(end)):
            return error_response("日期格式应为 YYYY-MM-DD")
        if start > end:
            start, end = end, start
        try:
            notes = self._db.notes_in_range(start, end)
        except Exception as exc:
            return error_response(str(exc))
        return json_response(
            {
                "start": start,
                "end": end,
                "count": len(notes),
                "digest_md": build_weekly_digest(notes, start, end),
                "notes": [self._summary(note) for note in notes],
            }
        )

    async def api_upload_media(self):
        """图片存到插件数据目录的 media/ 下，正文里引用 media/<文件名>。"""
        if self._db is None:
            return error_response("数据库不可用")
        files = await request.files()
        upload = files.get("file") if files else None
        if upload is None:
            return error_response("缺少图片文件（字段名 file）")
        original = str(getattr(upload, "filename", "") or "")
        suffix = Path(original).suffix.lower()
        if suffix not in MEDIA_SUFFIXES:
            return error_response("只支持 png、jpg、jpeg、gif、webp")

        media_dir = self._data_dir / MEDIA_DIR_NAME
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
            # 图片是日记配图，跟数据库一个待遇：同机其他用户不该能列目录。
            os.chmod(media_dir, 0o700)
        except OSError as exc:
            return error_response(f"创建图片目录失败：{exc}")

        filename = f"{uuid4().hex}{suffix}"
        target = media_dir / filename
        try:
            target.touch(mode=0o600, exist_ok=True)
            secure_temp_file(target)
            from astrbot.api.web import PluginUploadFile

            if isinstance(upload, PluginUploadFile):
                await upload.save(target)
            else:
                target.write_bytes(upload.file.read())
            secure_temp_file(target)
            size = target.stat().st_size
            if size > MAX_MEDIA_BYTES:
                target.unlink(missing_ok=True)
                return error_response(
                    f"图片太大（{size // 1024} KB），上限 {MAX_MEDIA_BYTES // 1024} KB"
                )
        except Exception as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            return error_response(f"保存图片失败：{exc}")

        return json_response(
            {
                "url": f"{MEDIA_DIR_NAME}/{filename}",
                "name": filename,
                "size": size,
            }
        )

    async def api_get_media(self, name: str):
        if not MEDIA_NAME_RE.match(str(name or "")):
            return error_response("非法的文件名", status_code=400)
        path = self._data_dir / MEDIA_DIR_NAME / name
        if not path.is_file():
            return error_response("图片不存在", status_code=404)
        return file_response(
            str(path),
            filename=name,
            content_type=MEDIA_MIME.get(path.suffix.lower(), "application/octet-stream"),
        )

    async def api_write_diary(self):
        if self._db is None:
            return error_response("数据库不可用")
        payload = await request.json(default={})
        content_md = payload.get("content_md") or ""
        if not content_md.strip():
            return error_response("content_md 不能为空")
        try:
            note = self._db.write_diary_from_ai(
                {
                    "content_md": content_md,
                    "title": payload.get("title") or None,
                    "diary_date": payload.get("diary_date") or None,
                    "tags": payload.get("tags") or [],
                    "mode": payload.get("mode") or "append_today",
                }
            )
        except Exception as exc:
            return error_response(str(exc))
        return json_response({"note": note, "message": "已写入今日日记"})

    async def api_note_markdown(self, note_id: str):
        if self._db is None:
            return error_response("数据库不可用")
        try:
            note = self._db.get_note(note_id)
        except Exception as exc:
            return error_response(str(exc), status_code=404)
        filename = f"{date_prefix(note)}-{safe_filename(note['title'])}.md"
        return json_response({"filename": filename, "content": markdown_for_note(note)})

    async def api_export_all(self):
        if self._db is None:
            return error_response("数据库不可用")
        # 导出后的 zip 要留到浏览器下载完，不能立刻删；顺手清掉一小时前的旧包。
        cleanup_temp_files("lijian-export-*.zip")
        tmp = Path(tempfile.gettempdir()) / f"lijian-export-{uuid4().hex}.zip"
        notes = self._db.list_notes(False)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                for note in notes:
                    archive.writestr(
                        f"{date_prefix(note)}-{safe_filename(note['title'])}.md",
                        markdown_for_note(note),
                    )
            secure_temp_file(tmp)
        except Exception as exc:
            return error_response(str(exc))
        return file_response(str(tmp), filename="lijian-export.zip", content_type="application/zip")

    async def api_export_json(self):
        """全量备份：id、时间、标签、置顶全在里面，换机器能原样搬回去。

        直接把 JSON 文本回给前端下载，不落临时文件，也就没有清理问题。
        """
        if self._db is None:
            return error_response("数据库不可用")
        try:
            data = self._db.export_json()
        except Exception as exc:
            return error_response(str(exc))
        return json_response(
            {
                "filename": f"lijian-backup-{local_today()}.json",
                "content": json.dumps(data, ensure_ascii=False, indent=2),
                "count": data.get("count", 0),
            }
        )

    async def api_import_json(self):
        if self._db is None:
            return error_response("数据库不可用")
        payload = await request.json(default={})
        try:
            result = self._db.import_json(payload)
        except Exception as exc:
            return error_response(str(exc))
        return json_response(
            {
                **result,
                "message": f"新建 {result['created']}，覆盖 {result['updated']}，跳过 {result['skipped']}",
            }
        )

    async def api_import(self):
        if self._db is None:
            return error_response("数据库不可用")
        files = await request.files()
        upload = files.get("file") if files else None
        if upload is None:
            return error_response("缺少上传文件（字段名 file）")
        tmp = Path(tempfile.gettempdir()) / f"lijian-import-{uuid4().hex}.md"
        try:
            from astrbot.api.web import PluginUploadFile

            # 先落一个空文件再收紧权限，避免内容以 0644 落盘。
            tmp.touch(mode=0o600, exist_ok=True)
            secure_temp_file(tmp)
            if isinstance(upload, PluginUploadFile):
                await upload.save(tmp)
            else:
                tmp.write_bytes(upload.file.read())
            secure_temp_file(tmp)
            size = tmp.stat().st_size
            if size > MAX_IMPORT_BYTES:
                return error_response(
                    f"文件太大（{size // 1024} KB），单篇导入上限 "
                    f"{MAX_IMPORT_BYTES // 1024} KB"
                )
            notes = self._db.import_markdown_files([str(tmp)])
        except Exception as exc:
            return error_response(str(exc))
        finally:
            # 已经读进数据库了，临时文件立刻删掉，别在 /tmp 里越攒越多。
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return json_response({"notes": [self._summary(note) for note in notes], "count": len(notes)})

    async def api_stats(self):
        if self._db is None:
            return error_response("数据库不可用")
        stats = self._db.stats()
        stats["words"] = stats.get("words", 0)
        return json_response(stats)

    async def api_status(self):
        if self._db is None:
            return error_response("数据库不可用")
        template = str(self.config.get("diary_template", "") or "")
        if not template.strip():
            template = DEFAULT_DIARY_TEMPLATE
        return json_response(
            {
                "ok": True,
                "name": "璃笺",
                "version": PLUGIN_VERSION,
                "db_path": self._db.path,
                "bridge": {
                    "enabled": self._bridge is not None,
                    "running": self._bridge_running(),
                    "host": getattr(self._bridge, "host", None),
                    "port": getattr(self._bridge, "port", None),
                },
                "token": self._db.settings()["ai_token"],
                "stats": self._db.stats(),
                "diary_template": template,
                # 前端拼图片地址时要用；拿不到就让它退回按 location 推。
                "api_base": self._api_base(),
            }
        )

    def _api_base(self) -> str:
        """当前插件 API 的前缀，形如 /api/v1/plugins/extensions/<name>。"""
        try:
            path = str(request.path or "")
        except Exception:  # noqa: BLE001
            return ""
        marker = f"/{PLUGIN_NAME}"
        index = path.find(marker)
        if index <= 0:
            return ""
        return path[: index + len(marker)]


def cleanup_temp_files(pattern: str, max_age_seconds: int = 3600) -> None:
    """删掉临时目录里超过 max_age_seconds 的旧文件，失败就跳过。"""
    cutoff = time.time() - max_age_seconds
    try:
        candidates = list(Path(tempfile.gettempdir()).glob(pattern))
    except OSError:
        return
    for item in candidates:
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            continue


def secure_temp_file(path: Path) -> None:
    """把临时文件收紧到 0600。

    tempfile.gettempdir() 下建的文件默认跟随 umask，常见是 0644，
    同机其他用户能直接读日记内容。导出包和导入中转都走这里。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def split_tags(value: str | list[str] | None) -> list[str]:
    """标签解析交给数据层，统一支持中英文逗号、顿号、分号。"""
    return clean_tags(value)


def local_week_range(today: datetime | None = None) -> tuple[str, str]:
    """本周一到现在（本地时区，闭区间）。"""
    now_local = today or datetime.now()
    monday = now_local - timedelta(days=now_local.weekday())
    return monday.date().isoformat(), now_local.date().isoformat()


def build_weekly_digest(notes: list[dict], start: str, end: str) -> str:
    """把区间内的日记拼成一份 Markdown 素材。

    只做聚合，不做总结——总结该由人或者 AI 来写，
    插件擅自拟一段反而会盖掉真正想记的东西。
    """
    lines = [f"# {start} ~ {end} 的日记", ""]
    if not notes:
        lines.append("（这段时间没有日记。）")
        return "\n".join(lines)
    for note in notes:
        date = note.get("diary_date") or (note.get("created_at") or "")[:10]
        body = str(note.get("body_md") or "").strip()
        # 正文开头往往自带「# 日记 2026-10-18」，这里去掉，免得标题重复两遍
        body = re.sub(r"^#\s+[^\n]*\n?", "", body, count=1).strip()
        lines.append(f"## {date}　{note.get('title') or '未命名'}")
        lines.append("")
        lines.append(body or "（空）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
