"""璃笺插件冒烟测试：db + bridge server 端到端。"""
import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from db import (
    Database,
    DatabaseError,
    clean_tags,
    local_today,
    markdown_for_note,
    parse_markdown,
    word_count,
)
from server import BridgeServer

PASS = 0


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ok - {name}")


def test_parse_markdown():
    parsed = parse_markdown(
        '---\ntitle: "五月小记"\ndate: 2026-05-22\ntags: [日记, AI]\n---\n\n# 正文标题\n\n内容',
        "fallback",
    )
    check("front matter title", parsed["title"] == "五月小记")
    check("front matter date", parsed["diary_date"] == "2026-05-22")
    check("front matter tags", parsed["tags"] == ["日记", "AI"])
    check("body kept heading", "正文标题" in parsed["body"])
    check("clean_tags dedupe", clean_tags(["  test ", "#test", "test"]) == ["test"])
    check("word_count", word_count("你好 world 123") == 4)


def test_database():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "lijian.sqlite3")
        token = db.ensure_ai_token()
        check("token generated", token.startswith("lijian-"))
        check("config seed overrides", db.ensure_ai_token("x") == "x")
        check("seed persisted", db.settings()["ai_token"] == "x")
        check("empty seed keeps stored", db.ensure_ai_token("") == "x")

        n1 = db.write_diary_from_ai(
            {"content_md": "早上写一点", "title": "今日", "tags": ["ai"], "mode": "create"}
        )
        check("create diary", n1["kind"] == "diary" and n1["source"] == "astrbot")
        n2 = db.write_diary_from_ai({"content_md": "晚上补充", "mode": "append_today"})
        check("append reuses same note", n1["id"] == n2["id"])
        check("append merged body", "早上写一点" in n2["body_md"] and "晚上补充" in n2["body_md"])

        note = db.create_note(
            {"title": "便签一", "body_md": "# 标题\n\n内容", "kind": "note", "tags": ["#t"]}
        )
        check("note title from heading fallback", note["title"] == "便签一")
        check("note tag cleaned", note["tags"] == ["t"])

        updated = db.update_note(note["id"], {"body_md": "新内容", "tags": ["x", "y"]})
        check("update body/tags", updated["body_md"] == "新内容" and updated["tags"] == ["x", "y"])

        archived = db.archive_note(note["id"], True)
        check("archive flag", archived["archived"] is True)
        listed = db.list_notes(False)
        check("archived hidden", all(x["id"] != note["id"] for x in listed))
        listed_all = db.list_notes(True)
        check("archived visible when asked", any(x["id"] == note["id"] for x in listed_all))

        stats = db.stats()
        check("stats counts", stats["notes"] == 1 and stats["archived"] == 1)

        md = markdown_for_note(n2)
        check("markdown front matter", md.startswith("---\ntitle:") and "晚上补充" in md)

        p = Path(tmp) / "hello.md"
        p.write_text('---\ntitle: 导入篇\ndate: 2026-01-01\ntags: [a, b]\n---\n\n正文', encoding="utf-8")
        imported = db.import_markdown_files([str(p)])
        check("import parses", imported[0]["title"] == "导入篇" and imported[0]["kind"] == "diary")

        count = db.export_all_markdown(str(Path(tmp) / "out"))
        check("export all count", count == 2)

        # ---- v1.0.2 回归 ----
        check(
            "local today uses local timezone",
            local_today() == datetime.now().astimezone().date().isoformat(),
        )
        check("get_note accepts 8-char prefix", db.get_note(n2["id"][:8])["id"] == n2["id"])

        merged = db.write_diary_from_ai({"content_md": "追加一段", "tags": ["补充"]})
        check("append merges tags", set(merged["tags"]) == {"ai", "补充"})

        cleared = db.update_note(merged["id"], {"diary_date": None})
        check("diary_date can be cleared", cleared["diary_date"] is None)

        tagged = db.create_note(
            {"title": "标签", "body_md": "x", "kind": "note", "tags": "日记，日常、随手"}
        )
        check("cjk tag separators", tagged["tags"] == ["日记", "日常", "随手"])

        try:
            db.create_note({"title": "坏", "body_md": "x", "kind": "todo"})
            kind_rejected = False
        except DatabaseError:
            kind_rejected = True
        check("invalid kind rejected", kind_rejected)

        try:
            db.write_diary_from_ai({"content_md": "x", "mode": "append"})
            mode_rejected = False
        except DatabaseError:
            mode_rejected = True
        check("invalid mode rejected", mode_rejected)

        sig_before = db.note_signature()
        db.create_note({"title": "签名", "body_md": "x", "kind": "note"})
        check("note signature changes", db.note_signature() != sig_before)

        # 两条同前缀的记录，get_note 必须拒绝而不是随便挑一条
        first_dup = db.create_note({"title": "甲", "body_md": "a", "kind": "note"})
        second_dup = db.create_note({"title": "乙", "body_md": "b", "kind": "note"})
        db.conn.execute("UPDATE notes SET id = ? WHERE id = ?", ("dup-0001", first_dup["id"]))
        db.conn.execute("UPDATE notes SET id = ? WHERE id = ?", ("dup-0002", second_dup["id"]))
        db.conn.commit()
        try:
            db.get_note("dup-00")
            ambiguous = False
        except DatabaseError:
            ambiguous = True
        check("ambiguous prefix rejected", ambiguous)

        db.close()
        check("db close ok", True)


async def test_bridge():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "lijian.sqlite3")
        token = db.ensure_ai_token()
        bridge = BridgeServer(db, host="127.0.0.1", port=0)  # 0 -> 随机端口
        started = bridge.start()
        check("bridge started", started)
        assert bridge._site is not None
        port = bridge._site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        async with aiohttp.ClientSession() as session:
            # health 无需令牌
            async with session.get(f"{base}/api/v1/health") as resp:
                data = await resp.json(content_type=None)
                check("health ok", resp.status == 200 and data["ok"])

            # 未授权 401
            async with session.get(f"{base}/api/v1/notes") as resp:
                check("unauthorized 401", resp.status == 401)

            headers = {"Authorization": f"Bearer {token}"}

            # 写日记（append 两次 -> 同一篇）
            async with session.post(
                f"{base}/api/v1/diary",
                json={"content_md": "第一次", "title": "桥接", "tags": ["bridge"]},
                headers=headers,
            ) as resp:
                first = await resp.json(content_type=None)
                check("diary write", resp.status == 200 and first["ok"])
            async with session.post(
                f"{base}/api/v1/diary",
                json={"content_md": "第二次", "mode": "append_today"},
                headers=headers,
            ) as resp:
                second = await resp.json(content_type=None)
                check("diary append same id", second["note_id"] == first["note_id"])

            async with session.get(f"{base}/api/v1/notes", headers=headers) as resp:
                data = await resp.json(content_type=None)
                check("notes list", resp.status == 200 and len(data["notes"]) == 1)
                check("no body by default", "body_md" not in data["notes"][0])

            async with session.get(
                f"{base}/api/v1/notes?include_body=true", headers=headers
            ) as resp:
                data = await resp.json(content_type=None)
                check("notes with body", "body_md" in data["notes"][0])

            async with session.post(
                f"{base}/api/v1/notes",
                json={"title": "桥接便签", "body_md": "# 新建\n\n内容", "kind": "note", "tags": ["t"]},
                headers=headers,
            ) as resp:
                created = await resp.json(content_type=None)
                check("notes create", resp.status == 200 and created["ok"] and created["action"] == "created")
                check("notes create note", created["note"]["title"] == "桥接便签" and created["note"]["kind"] == "note")
                created_id = created["note"]["id"]

            async with session.get(f"{base}/api/v1/notes/{created_id}", headers=headers) as resp:
                created_read = await resp.json(content_type=None)
                check("created note readable", created_read["note"]["body_md"] == "# 新建\n\n内容")

            async with session.post(
                f"{base}/api/v1/notes", json={}, headers=headers
            ) as resp:
                check("notes create empty 400", resp.status == 400)

            note_id = first["note_id"]
            async with session.get(f"{base}/api/v1/notes/{note_id}", headers=headers) as resp:
                note = await resp.json(content_type=None)
                check("note read body", note["note"]["body_md"] == "第一次\n\n第二次")

            async with session.patch(
                f"{base}/api/v1/notes/{note_id}", json={"title": "改标题"}, headers=headers
            ) as resp:
                patched = await resp.json(content_type=None)
                check("note patch", patched["ok"] and patched["title"] == "改标题")

            async with session.delete(f"{base}/api/v1/notes/{note_id}", headers=headers) as resp:
                deleted = await resp.json(content_type=None)
                check("note archive via delete", deleted["ok"] and deleted["action"] == "archived")

            async with session.get(f"{base}/api/v1/notes", headers=headers) as resp:
                data = await resp.json(content_type=None)
                ids = [n["id"] for n in data["notes"]]
                check("archived hidden after delete", note_id not in ids and created_id in ids)

            async with session.delete(f"{base}/api/v1/notes/{created_id}", headers=headers) as resp:
                deleted2 = await resp.json(content_type=None)
                check("created note archived", deleted2["ok"] and deleted2["action"] == "archived")

            # 校验失败用例
            async with session.post(
                f"{base}/api/v1/diary", json={"content_md": ""}, headers=headers
            ) as resp:
                check("empty content 400", resp.status == 400)

            # SSE：未授权 401
            async with session.get(f"{base}/api/v1/events") as resp:
                check("sse unauthorized 401", resp.status == 401)

            # SSE：连接后写一条，应在数秒内收到 notes_changed
            async with session.get(f"{base}/api/v1/events", headers=headers) as resp:
                check("sse connected", resp.status == 200 and resp.headers.get("Content-Type", "").startswith("text/event-stream"))
                await asyncio.sleep(0.5)
                async with session.post(
                    f"{base}/api/v1/notes",
                    json={"title": "SSE 便签", "body_md": "触发事件", "kind": "note"},
                    headers=headers,
                ) as resp2:
                    await resp2.json(content_type=None)
                chunk = b""
                try:
                    while b"notes_changed" not in chunk:
                        data = await asyncio.wait_for(resp.content.read(4096), timeout=6)
                        if not data:
                            break
                        chunk += data
                    check("sse notes_changed", b"notes_changed" in chunk)
                except asyncio.TimeoutError:
                    check("sse notes_changed", b"notes_changed" in chunk)

        bridge.stop()
        check("bridge stopped", not bridge.running)
        # v1.0.2：同一个实例重启时不能沿用上一次的就绪标记
        check("bridge restarts cleanly", bridge.start())
        bridge.stop()
        check("bridge stopped again", not bridge.running)
        db.close()


async def test_security_hardening():
    """库文件权限、安全响应头、SSE 反代透传、内部异常脱敏。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "sec.sqlite3")
        token = db.ensure_ai_token("k" * 32)
        db.create_note({"body_md": "私密内容", "title": "私密"})

        # 日记是明文 sqlite，主库和 WAL/SHM 都不该给同机其他用户留读权限
        modes = {
            p.name: (p.stat().st_mode & 0o777)
            for p in Path(tmp).iterdir()
            if p.is_file()
        }
        check("db/wal/shm 0600", bool(modes) and all(m == 0o600 for m in modes.values()))

        bridge = BridgeServer(db, host="127.0.0.1", port=0)
        check("hardening bridge started", bridge.start())
        port = bridge._site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        auth = {"Authorization": f"Bearer {token}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/api/v1/health") as resp:
                check(
                    "security headers",
                    resp.headers.get("X-Content-Type-Options") == "nosniff"
                    and resp.headers.get("X-Frame-Options") == "DENY"
                    and resp.headers.get("Referrer-Policy") == "no-referrer",
                )

            # query 令牌在本机直连时仍然可用（EventSource 带不了 header）
            async with session.get(f"{base}/api/v1/events?token={token}") as resp:
                check("sse query token local ok", resp.status == 200)

            # 有转发头就说明前面有反代，此时 query 令牌一律不认，
            # 否则令牌会一路写进反代日志
            for proxy_header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
                async with session.get(
                    f"{base}/api/v1/events?token={token}",
                    headers={proxy_header: "8.8.8.8"},
                ) as resp:
                    check(
                        f"sse query token behind {proxy_header} rejected",
                        resp.status == 401,
                    )

            # 内部异常不能被原样回给调用方
            def boom(_note_id):
                raise RuntimeError("unable to open database file: /home/secret/lijian.sqlite3")

            original = db.get_note
            db.get_note = boom
            try:
                async with session.get(
                    f"{base}/api/v1/notes/abc12345", headers=auth
                ) as resp:
                    body = await resp.json(content_type=None)
                    check(
                        "internal error redacted",
                        resp.status == 500
                        and "secret" not in str(body)
                        and body.get("error") == "internal error",
                    )
            finally:
                db.get_note = original

        bridge.stop()
        db.close()


def test_bridge_port_conflict():
    import socket

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "a.sqlite3")
        db.ensure_ai_token()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        try:
            bridge = BridgeServer(db, host="127.0.0.1", port=port)
            check("port conflict start fails", not bridge.start())
            bridge.stop()
        finally:
            sock.close()
        db.close()


def test_parse_markdown_heading_fallback():
    parsed = parse_markdown("# 标题行\n\n正文", "未命名")
    check("heading fallback", parsed["title"] == "标题行")
    parsed2 = parse_markdown("随便写写", "未命名")
    check("empty fallback", parsed2["title"] == "未命名")


if __name__ == "__main__":
    print("== markdown 解析 ==")
    test_parse_markdown()
    test_parse_markdown_heading_fallback()
    print("== 数据库 ==")
    test_database()
    print("== 桥接接口 ==")
    asyncio.run(test_bridge())
    print("== 安全加固 ==")
    asyncio.run(test_security_hardening())
    print("== 端口冲突 ==")
    test_bridge_port_conflict()
    print(f"\n全部通过（{PASS} 项检查）")
