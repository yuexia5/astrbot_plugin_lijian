"""璃笺插件冒烟测试：db + bridge server 端到端。"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiohttp

from db import Database, parse_markdown, markdown_for_note, clean_tags, word_count
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
        check("token stable", db.ensure_ai_token("x") == token)

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
                check("archived hidden after delete", len(data["notes"]) == 0)

            # 校验失败用例
            async with session.post(
                f"{base}/api/v1/diary", json={"content_md": ""}, headers=headers
            ) as resp:
                check("empty content 400", resp.status == 400)

        bridge.stop()
        check("bridge stopped", not bridge.running)
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
    print("== 端口冲突 ==")
    test_bridge_port_conflict()
    print(f"\n全部通过（{PASS} 项检查）")
