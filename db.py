"""璃笺数据层：与桌面版璃笺 (Tauri/Rust) 相同 schema 的 SQLite 存储。

线程安全：全部操作经由同一把 RLock 串行化（对应 Rust 版 Database 的 Mutex 用法），
连接以 check_same_thread=False 打开，供 AstrBot 事件循环与桥接线程共用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger("astrbot_plugin_lijian")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',
    diary_date TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'local',
    pinned INTEGER NOT NULL DEFAULT 0,
    tile_rect_json TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_updated_at ON notes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_archived ON notes(archived);
CREATE INDEX IF NOT EXISTS idx_notes_diary_date ON notes(diary_date);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS write_events (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
);
"""


def now() -> str:
    """UTC RFC3339（秒级），与 chrono SecondsFormat::Secs 输出一致。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_today() -> str:
    """本地时区的今天（YYYY-MM-DD）。

    日记的默认日期必须按本地时区算：UTC 日期在东八区凌晨 0-8 点会落到前一天，
    直接用 UTC 会让「今天的日记」写进昨天那一篇。
    """
    return datetime.now().astimezone().date().isoformat()


class DatabaseError(Exception):
    pass


class Database:
    def __init__(self, path: Optional[Path | str] = None, memory: bool = False):
        self._lock = threading.RLock()
        if memory:
            self.path: Optional[str] = None
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            db_path = Path(path or "lijian.sqlite3")
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(db_path)
            self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    # ---------- 基础 ----------

    def _persist(self) -> None:
        """提交并顺手收紧文件权限。

        sqlite 建库、建 WAL 都按 umask 走，常见是 0644，同机其他用户能直接读，
        而 WAL/SHM 并不跟随主库权限，必须每次提交后重新设一遍。
        """
        self.conn.commit()
        self._harden_permissions()

    def _harden_permissions(self) -> None:
        if not self.path:
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(f"{self.path}{suffix}", 0o600)
            except OSError:
                continue

    def migrate(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value_json) VALUES ('theme', ?)",
                (json.dumps("light"),),
            )
            self._persist()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    def list_note_timestamps(self) -> list[dict[str, str]]:
        with self._lock:
            return [
                {"id": row["id"], "updated_at": row["updated_at"]}
                for row in self.conn.execute(
                    "SELECT id, updated_at FROM notes"
                ).fetchall()
            ]

    def note_signature(self) -> str:
        """书架内容签名（条数 + 最新更新时间 + 归档数），供 SSE 轮询比对。

        比逐条拉 id/updated_at 便宜得多，编辑、归档、删除都会改变签名。
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(MAX(updated_at), '') AS latest, "
                "COALESCE(SUM(archived), 0) AS archived FROM notes"
            ).fetchone()
            return f"{row['total']}|{row['latest']}|{row['archived']}"

    # ---------- 设置 ----------

    def ensure_ai_token(self, seed: str = "") -> str:
        with self._lock:
            current = self._get_setting("ai_token")
            if seed:
                token = seed.strip()
                if len(token) < 16:
                    logger.warning(
                        "璃笺写入令牌只有 %d 个字符，容易被猜中；"
                        "建议换成 24 位以上随机串（留空可自动生成）。",
                        len(token),
                    )
                if current != token:
                    self._set_setting("ai_token", token)
                return token
            if current:
                return current
            token = f"lijian-{uuid4()}"
            self._set_setting("ai_token", token)
            return token

    def settings(self) -> dict[str, Any]:
        with self._lock:
            show_quick = self._get_setting("show_quick_hotkey") or self._get_setting(
                "hotkey"
            ) or "Ctrl+Space"
            return {
                "hotkey": show_quick,
                "show_quick_hotkey": show_quick,
                "hide_quick_hotkey": self._get_setting("hide_quick_hotkey")
                or "Ctrl+Shift+Space",
                "main_hotkey": self._get_setting("main_hotkey") or "Alt+Space",
                "start_on_login": bool(self._get_setting_json("start_on_login") or False),
                "editor_font_size": int(self._get_setting_json("editor_font_size") or 18),
                "quick_font_size": int(self._get_setting_json("quick_font_size") or 20),
                "tile_font_size": int(self._get_setting_json("tile_font_size") or 18),
                "theme": self._get_setting("theme") or "light",
                "quick_rect": self._get_setting_json("quick_rect"),
                "ai_token": self._get_setting("ai_token") or "",
                "api_url": "http://127.0.0.1:27176/api/v1/diary",
            }

    def set_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for key in (
                "show_quick_hotkey",
                "hide_quick_hotkey",
                "main_hotkey",
            ):
                if settings.get(key):
                    self._set_setting(key, str(settings[key]))
            self._set_setting("hotkey", str(settings.get("show_quick_hotkey") or "Ctrl+Space"))
            for key in ("start_on_login", "editor_font_size", "quick_font_size", "tile_font_size"):
                if key in settings and settings[key] is not None:
                    self._set_setting_json(key, settings[key])
            if settings.get("theme"):
                self._set_setting("theme", str(settings["theme"]))
            if settings.get("quick_rect") is not None:
                self._set_setting_json("quick_rect", settings["quick_rect"])
            return self.settings()

    # ---------- 便签 / 日记 ----------

    def list_notes(self, include_archived: bool = False, light: bool = False) -> list[dict[str, Any]]:
        """列出条目。light=True 时不带 body_md，书架首屏省带宽。"""
        with self._lock:
            if include_archived:
                rows = self.conn.execute(
                    "SELECT * FROM notes ORDER BY pinned DESC, updated_at DESC"
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM notes WHERE archived = 0 ORDER BY pinned DESC, updated_at DESC"
                ).fetchall()
            return [self._note_from_row(row, light=light) for row in rows]

    def search_notes(
        self,
        q: str,
        include_archived: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """在标题、正文、标签、日期里找，大小写不敏感。

        没上 FTS5 是有意的：trigram 分词最小三字符，「牡丹」「曹州」这类两字词
        直接搜不到；unicode61 又不切中文。日记这个量级 LIKE 全表扫是毫秒级，
        换成 FTS 反而要额外维护同步表，还换来一个中文查不准的坑。
        """
        q = str(q or "").strip()
        if not q:
            return self.list_notes(include_archived)

        escaped = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        pattern = f"%{escaped}%"
        sql = (
            "SELECT * FROM notes WHERE ("
            "title LIKE ?1 ESCAPE '\\' OR body_md LIKE ?1 ESCAPE '\\' "
            "OR tags_json LIKE ?1 ESCAPE '\\' OR diary_date LIKE ?1 ESCAPE '\\'"
            ")"
        )
        if not include_archived:
            sql += " AND archived = 0"
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?2"
        with self._lock:
            rows = self.conn.execute(sql, (pattern, max(1, int(limit)))).fetchall()
            return [self._note_from_row(row) for row in rows]

    def random_note(
        self,
        kind: Optional[str] = None,
        exclude_id: Optional[str] = None,
        include_archived: bool = False,
    ) -> Optional[dict[str, Any]]:
        """随手翻一篇。exclude_id 用来避免连点两次翻到同一本。"""
        sql = "SELECT * FROM notes WHERE 1 = 1"
        params: list[Any] = []
        if not include_archived:
            sql += " AND archived = 0"
        if kind in ("note", "diary"):
            sql += " AND kind = ?"
            params.append(kind)
        if exclude_id:
            sql += " AND id <> ?"
            params.append(exclude_id)
        sql += " ORDER BY RANDOM() LIMIT 1"
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return self._note_from_row(row) if row is not None else None

    def notes_in_range(
        self,
        start: str,
        end: str,
        kind: Optional[str] = "diary",
    ) -> list[dict[str, Any]]:
        """按日期区间取条目（闭区间），供周报聚合用。

        日期优先用 diary_date；没有的退回 created_at 的本地日期，
        这样导入进来的旧日记也不会漏。
        """
        sql = (
            "SELECT * FROM notes WHERE archived = 0 AND "
            "COALESCE(diary_date, date(created_at, 'localtime')) BETWEEN ?1 AND ?2"
        )
        params: list[Any] = [start, end]
        if kind in ("note", "diary"):
            sql += " AND kind = ?3"
            params.append(kind)
        sql += " ORDER BY COALESCE(diary_date, date(created_at, 'localtime')) ASC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
            return [self._note_from_row(row) for row in rows]

    # ---------- 全量备份 ----------

    # 只导出这些设置项；令牌之类绝不进备份包。
    BACKUP_SETTINGS_KEYS = ("theme", "editor_font_size", "quick_font_size", "tile_font_size")

    def export_json(self) -> dict[str, Any]:
        """完整备份：保留 id、创建时间、来源、置顶状态，换机器也搬得回去。"""
        with self._lock:
            notes = self.list_notes(include_archived=True)
            settings = {
                key: self._get_setting(key)
                for key in self.BACKUP_SETTINGS_KEYS
                if self._get_setting(key) is not None
            }
            return {
                "format": "lijian-backup",
                "version": 1,
                "exported_at": now(),
                "count": len(notes),
                "settings": settings,
                "notes": notes,
            }

    def import_json(self, payload: Any) -> dict[str, int]:
        """从备份恢复。同 id 的就地覆盖，没有的新建，坏条目跳过并计数。"""
        if not isinstance(payload, dict):
            raise DatabaseError("备份文件不是 JSON 对象")
        fmt = payload.get("format")
        if fmt is not None and fmt != "lijian-backup":
            raise DatabaseError("不是璃笺的备份文件")
        notes = payload.get("notes")
        if not isinstance(notes, list):
            raise DatabaseError("备份里没有 notes 列表")

        created = updated = skipped = 0
        with self._lock:
            for item in notes:
                if not isinstance(item, dict):
                    skipped += 1
                    continue
                note_id = str(item.get("id") or "").strip()
                body = item.get("body_md")
                if not note_id or not isinstance(body, str):
                    skipped += 1
                    continue
                title = normalize_title(item.get("title"), body)
                kind = normalize_kind(item.get("kind"))
                tags = clean_tags(item.get("tags") or [])
                diary_date = item.get("diary_date") or None
                source = str(item.get("source") or "import")
                pinned = 1 if item.get("pinned") else 0
                archived = 1 if item.get("archived") else 0
                created_at = str(item.get("created_at") or now())
                updated_at = str(item.get("updated_at") or created_at)
                exists = self.conn.execute(
                    "SELECT 1 FROM notes WHERE id = ?1", (note_id,)
                ).fetchone()
                if exists:
                    self.conn.execute(
                        "UPDATE notes SET title=?1, body_md=?2, kind=?3, diary_date=?4, "
                        "tags_json=?5, source=?6, pinned=?7, archived=?8, updated_at=?9 "
                        "WHERE id=?10",
                        (
                            title,
                            body,
                            kind,
                            diary_date,
                            json.dumps(tags, ensure_ascii=False),
                            source,
                            pinned,
                            archived,
                            updated_at,
                            note_id,
                        ),
                    )
                    updated += 1
                else:
                    self.conn.execute(
                        "INSERT INTO notes (id, title, body_md, kind, diary_date, tags_json, "
                        "source, pinned, archived, created_at, updated_at) "
                        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                        (
                            note_id,
                            title,
                            body,
                            kind,
                            diary_date,
                            json.dumps(tags, ensure_ascii=False),
                            source,
                            pinned,
                            archived,
                            created_at,
                            updated_at,
                        ),
                    )
                    created += 1
            self._persist()
        return {"created": created, "updated": updated, "skipped": skipped}

    def get_note(self, note_id: str) -> dict[str, Any]:
        """按 id 读取；也接受唯一的 id 前缀（指令里只显示 8 位）。"""
        note_id = str(note_id or "").strip()
        if not note_id:
            raise DatabaseError("note id 不能为空")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM notes WHERE id = ?1", (note_id,)
            ).fetchone()
            if row is None and len(note_id) >= 6:
                rows = self.conn.execute(
                    "SELECT * FROM notes WHERE substr(id, 1, ?1) = ?2 LIMIT 2",
                    (len(note_id), note_id),
                ).fetchall()
                if len(rows) > 1:
                    raise DatabaseError("note id 前缀不唯一，请给更长的 id")
                row = rows[0] if rows else None
            if row is None:
                raise DatabaseError("note not found")
            return self._note_from_row(row)

    def create_note(self, input_: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            now_ts = now()
            note_id = str(uuid4())
            body = str(input_.get("body_md") or "")
            title = normalize_title(input_.get("title"), body)
            kind = normalize_kind(input_.get("kind"))
            source = input_.get("source") or "local"
            tags = clean_tags(input_.get("tags"))
            diary_date = input_.get("diary_date")
            self.conn.execute(
                """
                INSERT INTO notes
                    (id, title, body_md, kind, diary_date, tags_json, source, pinned, archived, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    note_id,
                    title,
                    body,
                    kind,
                    diary_date,
                    json.dumps(tags, ensure_ascii=False),
                    source,
                    now_ts,
                    now_ts,
                ),
            )
            self._persist()
            return self.get_note(note_id)

    def update_note(self, note_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self.get_note(note_id)
            title = patch["title"] if patch.get("title") else existing["title"]
            body = patch.get("body_md") if patch.get("body_md") is not None else existing["body_md"]
            kind = (
                normalize_kind(patch.get("kind"))
                if patch.get("kind") is not None
                else existing["kind"]
            )
            if "diary_date" in patch:
                diary_date = patch["diary_date"] or None
            else:
                diary_date = existing["diary_date"]
            tags = patch.get("tags") if patch.get("tags") is not None else existing["tags"]
            self.conn.execute(
                """
                UPDATE notes
                SET title = ?, body_md = ?, kind = ?, diary_date = ?, tags_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    body,
                    kind,
                    diary_date,
                    json.dumps(clean_tags(tags), ensure_ascii=False),
                    now(),
                    note_id,
                ),
            )
            self._persist()
            return self.get_note(note_id)

    def archive_note(self, note_id: str, archived: bool = True) -> dict[str, Any]:
        with self._lock:
            self.conn.execute(
                "UPDATE notes SET archived = ?, updated_at = ? WHERE id = ?",
                (1 if archived else 0, now(), note_id),
            )
            self._persist()
            return self.get_note(note_id)

    def delete_note(self, note_id: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            self._persist()

    def pin_note(self, note_id: str, pinned: bool = True) -> dict[str, Any]:
        with self._lock:
            self.conn.execute(
                "UPDATE notes SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, now(), note_id),
            )
            self._persist()
            return self.get_note(note_id)

    def save_tile_rect(self, note_id: str, rect: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.conn.execute(
                "UPDATE notes SET tile_rect_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(rect, ensure_ascii=False), now(), note_id),
            )
            self._persist()
            return self.get_note(note_id)

    # ---------- AI 写入（append_today / create） ----------

    def write_diary_from_ai(self, input_: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            content_md = str(input_.get("content_md") or "").strip()
            if not content_md:
                raise DatabaseError("content_md is required")
            diary_date = input_.get("diary_date") or local_today()
            mode = input_.get("mode") or "append_today"
            if mode not in ("append_today", "create"):
                raise DatabaseError("mode 必须是 append_today 或 create")
            if mode == "append_today":
                existing = self._find_diary_by_date(diary_date)
                if existing is not None:
                    separator = "" if not existing["body_md"].strip() else "\n\n"
                    updated = self.update_note(
                        existing["id"],
                        {
                            "title": input_.get("title") or existing["title"],
                            "body_md": f"{existing['body_md']}{separator}{content_md}",
                            "kind": "diary",
                            "diary_date": diary_date,
                            # 追加时合并标签：AI 每次带 AstrBot/日记 不该冲掉用户手写的标签
                            "tags": clean_tags(
                                list(existing.get("tags") or [])
                                + clean_tags(input_.get("tags"))
                            ),
                        },
                    )
                    self._record_write_event(updated["id"], "astrbot", input_)
                    return updated
            note = self.create_note(
                {
                    "title": input_.get("title"),
                    "body_md": content_md,
                    "kind": "diary",
                    "diary_date": diary_date,
                    "tags": input_.get("tags") or [],
                    "source": "astrbot",
                }
            )
            self._record_write_event(note["id"], "astrbot", input_)
            return note

    # ---------- 导入 / 导出 ----------

    def import_markdown_files(self, paths: list[str]) -> list[dict[str, Any]]:
        imported: list[dict[str, Any]] = []
        for path in paths:
            file_path = Path(path)
            content = file_path.read_text(encoding="utf-8", errors="replace")
            fallback = file_path.stem or "导入便签"
            parsed = parse_markdown(content, fallback)
            imported.append(
                self.create_note(
                    {
                        "title": parsed["title"],
                        "body_md": parsed["body"],
                        "kind": "diary" if parsed["diary_date"] else "note",
                        "diary_date": parsed["diary_date"],
                        "tags": parsed["tags"],
                        "source": "import",
                    }
                )
            )
        return imported

    def export_note_markdown(self, note_id: str, path: str) -> None:
        note = self.get_note(note_id)
        Path(path).write_text(markdown_for_note(note), encoding="utf-8")

    def export_all_markdown(self, directory: str) -> int:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        notes = self.list_notes(False)
        for note in notes:
            filename = f"{date_prefix(note)}-{safe_filename(note['title'])}.md"
            (target / filename).write_text(markdown_for_note(note), encoding="utf-8")
        return len(notes)

    # ---------- 统计 ----------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            notes = self.list_notes(False)
            archived = self.conn.execute(
                "SELECT COUNT(*) FROM notes WHERE archived = 1"
            ).fetchone()[0]
            total_words = 0
            for note in notes:
                total_words += word_count(note["body_md"])
            return {
                "notes": len(notes),
                "diaries": sum(1 for n in notes if n["kind"] == "diary"),
                "notes_kind": sum(1 for n in notes if n["kind"] == "note"),
                "archived": archived,
                "words": total_words,
                "pinned": sum(1 for n in notes if n["pinned"]),
            }

    # ---------- 内部 ----------

    def _find_diary_by_date(self, diary_date: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT * FROM notes
            WHERE archived = 0 AND kind = 'diary' AND diary_date = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (diary_date,),
        ).fetchone()
        return self._note_from_row(row) if row is not None else None

    def _record_write_event(self, note_id: str, source: str, payload: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO write_events (id, note_id, source, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(uuid4()), note_id, source, json.dumps(payload, ensure_ascii=False), now()),
        )
        self._persist()

    def _note_from_row(self, row: sqlite3.Row, light: bool = False) -> dict[str, Any]:
        tags = json.loads(row["tags_json"] or "[]")
        tile_rect = None
        if row["tile_rect_json"]:
            try:
                tile_rect = json.loads(row["tile_rect_json"])
            except json.JSONDecodeError:
                tile_rect = None
        data = {
            "id": row["id"],
            "title": row["title"],
            "kind": row["kind"],
            "diary_date": row["diary_date"],
            "tags": tags,
            "source": row["source"],
            "pinned": bool(row["pinned"]),
            "tile_rect": tile_rect,
            "archived": bool(row["archived"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        # 轻量列表不带正文；需要正文的地方不传 light，行为和以前一致。
        if not light:
            data["body_md"] = row["body_md"]
        return data

    def _get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def _set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO settings (key, value_json) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """,
            (key, json.dumps(value)),
        )
        self._persist()

    def _get_setting_json(self, key: str) -> Any:
        row = self.conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def _set_setting_json(self, key: str, value: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO settings (key, value_json) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """,
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self._persist()


# ---------- Markdown 解析 / 生成（移植自 db.rs） ----------

def parse_markdown(content: str, fallback_title: str) -> dict[str, Any]:
    body = content.lstrip("\ufeff")
    title: Optional[str] = None
    diary_date: Optional[str] = None
    tags: list[str] = []

    if body.startswith("---\n") or body.startswith("---\r\n"):
        normalized = body.replace("\r\n", "\n")
        end = normalized[4:].find("\n---")
        if end != -1:
            front_matter = normalized[4 : 4 + end]
            for line in front_matter.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    value = value.strip().strip('"').strip("'")
                    key = key.strip()
                    if key == "title":
                        title = value
                    elif key in ("date", "diary_date"):
                        diary_date = value
                    elif key == "tags":
                        tags = parse_tags(value)
            body = normalized[4 + end + len("\n---") :].lstrip("\n")

    if title is None:
        title = first_markdown_heading(body)

    return {
        "title": title or fallback_title,
        "body": body,
        "diary_date": diary_date,
        "tags": clean_tags(tags),
    }


def parse_tags(value: str) -> list[str]:
    return [
        item.strip().strip('"').strip("'")
        for item in value.strip().strip("[]").split(",")
        if item.strip()
    ]


def markdown_for_note(note: dict[str, Any]) -> str:
    tags = json.dumps(note.get("tags") or [], ensure_ascii=False)
    return (
        f"---\ntitle: {yaml_string(note['title'])}\n"
        f"date: {note.get('diary_date') or ''}\n"
        f"tags: {tags}\n"
        f"source: {yaml_string(note.get('source') or '')}\n"
        f"---\n\n{note['body_md']}"
    )


def normalize_title(title: Any, body: str) -> str:
    if isinstance(title, str) and title.strip():
        return title.strip()
    return first_markdown_heading(body) or "未命名便签"


def first_markdown_heading(body: str) -> Optional[str]:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            value = stripped[2:].strip()
            if value:
                return value
    return None


TAG_SPLIT = re.compile(r"[,，、;；]+")


def clean_tags(tags: list[Any] | str | None) -> list[str]:
    """清理标签：去 #、去重，并兼容中英文逗号 / 顿号 / 分号分隔。

    LLM 和用户经常写「日记，日常」，只按英文逗号切会把整串当成一个标签。
    """
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    result: list[str] = []
    for tag in tags:
        for piece in TAG_SPLIT.split(str(tag)):
            piece = piece.strip().lstrip("#").strip()
            if piece and piece not in result:
                result.append(piece)
    return result


def normalize_kind(value: Any) -> str:
    """把 note / diary（含中文写法）归一，非法值直接报错。"""
    kind = str(value or "note").strip().lower()
    if kind in ("diary", "日记"):
        return "diary"
    if kind in ("note", "便签", "笔记"):
        return "note"
    raise DatabaseError("kind 必须是 note 或 diary")


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def safe_filename(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "-", value)
    name = name[:72]
    return name.strip() or "未命名"


def date_prefix(note: dict[str, Any]) -> str:
    if note.get("diary_date"):
        return note["diary_date"]
    return (note.get("created_at") or "")[:10]


def word_count(value: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", value))
    words = len(re.findall(r"[A-Za-z0-9_]+", re.sub(r"[\u4e00-\u9fff]", " ", value)))
    return cjk + words
