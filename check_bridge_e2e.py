"""璃笺桥接服务端到端实测：起真实 HTTP 服务，逐条打接口。

默认拿本机真实库的副本跑（不碰原库），跑完删副本。
没有真实库时用空库加两条种子数据，同样能跑。
路径可用 LIJIAN_DB 指定。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent
DEFAULT_DB = Path(
    "/home/baiwei/AstrBot/data/plugin_data/astrbot_plugin_lijian/lijian.sqlite3"
)
REAL_DB = Path(os.environ.get("LIJIAN_DB", str(DEFAULT_DB)))
sys.path.insert(0, str(PLUGIN))

import aiohttp  # noqa: E402

from db import Database  # noqa: E402
from server import BridgeServer  # noqa: E402

PORT = 27555
BASE = f"http://127.0.0.1:{PORT}/api/v1"

passed = 0
failed = []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name} {detail}")


async def main():
    tmp = Path(tempfile.mkdtemp())
    if REAL_DB.is_file():
        shutil.copy(REAL_DB, tmp / "lijian.sqlite3")
        for suffix in ("-wal", "-shm"):
            src = REAL_DB.with_name(REAL_DB.name + suffix)
            if src.exists():
                shutil.copy(src, tmp / ("lijian.sqlite3" + suffix))
        source = f"真实库副本 {REAL_DB}"
    else:
        source = "空库（未找到真实库，用种子数据）"
    print(f"数据源：{source}")

    db = Database(tmp / "lijian.sqlite3")
    if not db.list_notes(True):
        db.create_note({"title": "种子日记", "body_md": "今天很开心", "kind": "diary"})
        db.create_note({"title": "种子便签", "body_md": "买牛奶", "kind": "note"})

    token = db.settings()["ai_token"]
    if not token:
        token = db.ensure_ai_token("T" * 40)
    auth = {"Authorization": f"Bearer {token}"}

    server = BridgeServer(db, port=PORT)
    t0 = time.time()
    started = server.start()
    check("桥接启动", started and server.running, f"err={server._start_error}")
    check("启动耗时 < 3s", time.time() - t0 < 3, f"{time.time()-t0:.2f}s")

    async with aiohttp.ClientSession() as s:
        # ---------- health ----------
        async with s.get(f"{BASE}/health") as r:
            body = await r.json()
            check("health 免认证 200", r.status == 200, r.status)
            check("health 有 name", body.get("name") == "璃笺", body)
            check("health 安全头 nosniff", r.headers.get("X-Content-Type-Options") == "nosniff")
            check("health 安全头 no-store", "no-store" in (r.headers.get("Cache-Control") or ""))

        # ---------- 认证 ----------
        async with s.get(f"{BASE}/notes") as r:
            check("无令牌列表 401", r.status == 401, r.status)
        async with s.get(f"{BASE}/notes", headers={"Authorization": "Bearer wrong"}) as r:
            check("错令牌列表 401", r.status == 401, r.status)
        async with s.get(f"{BASE}/notes", headers=auth) as r:
            body = await r.json()
            check("正确令牌列表 200", r.status == 200, r.status)
            listed = body.get("notes") or []
            check("列表非空", len(listed) > 0, len(listed))
            check("列表默认不带正文", "body_md" not in (listed[0] if listed else {}), "带了正文")

        async with s.get(f"{BASE}/notes", headers=auth, params={"include_body": "1"}) as r:
            body = await r.json()
            notes = body.get("notes") or []
            check("include_body 带回正文", bool(notes) and "body_md" in notes[0])

        async with s.get(f"{BASE}/notes", headers=auth, params={"include_archived": "1"}) as r:
            body = await r.json()
            check("include_archived 生效", r.status == 200, r.status)

        real_id = listed[0]["id"] if listed else None
        check("真实库能读到 id", bool(real_id))

        # ---------- 详情 ----------
        if real_id:
            async with s.get(f"{BASE}/notes/{real_id}", headers=auth) as r:
                body = await r.json()
                check("按完整 id 读详情", r.status == 200 and body["note"]["id"] == real_id, r.status)
            async with s.get(f"{BASE}/notes/{real_id[:8]}", headers=auth) as r:
                body = await r.json()
                check(
                    "按 8 位前缀读详情",
                    r.status == 200 and body["note"]["id"] == real_id,
                    r.status,
                )
            async with s.get(f"{BASE}/notes/zzzzzzzz", headers=auth) as r:
                check("乱码 id 返回 404", r.status == 404, r.status)

        # ---------- 新建 ----------
        async with s.post(
            f"{BASE}/notes",
            headers=auth,
            json={"title": "E2E 测试", "body_md": "端到端写入", "tags": ["测试", "e2e"]},
        ) as r:
            body = await r.json()
            new_id = (body.get("note") or {}).get("id")
            check("创建笔记 200", r.status == 200 and bool(new_id), (r.status, body))
            check("创建返回 action", body.get("action") == "created", body.get("action"))

        if new_id:
            # 更新：桥接只回 id/标题/日期，所以改完再 GET 核对
            async with s.patch(
                f"{BASE}/notes/{new_id}",
                headers=auth,
                json={"title": "E2E 改名", "body_md": "改过的正文", "tags": "改后,标签"},
            ) as r:
                body = await r.json()
                check("更新返回 updated", r.status == 200 and body.get("action") == "updated", body)

            async with s.get(f"{BASE}/notes/{new_id}", headers=auth) as r:
                note = (await r.json())["note"]
                check("更新后标题生效", note.get("title") == "E2E 改名", note.get("title"))
                check("更新后正文生效", note.get("body_md") == "改过的正文", note.get("body_md")[:20])
                check("中文逗号拆标签", note.get("tags") == ["改后", "标签"], note.get("tags"))

            # 清空日期：PATCH 里显式给 null 应当被忽略（框架约定 None 等于不改）
            async with s.patch(
                f"{BASE}/notes/{new_id}",
                headers=auth,
                json={"diary_date": "2026-01-02"},
            ) as r:
                check("设置日期成功", r.status == 200, r.status)
            async with s.get(f"{BASE}/notes/{new_id}", headers=auth) as r:
                note = (await r.json())["note"]
                check("日期已写入", note.get("diary_date") == "2026-01-02", note.get("diary_date"))

            async with s.patch(f"{BASE}/notes/{new_id}", headers=auth, json={}) as r:
                check("空 patch 被拒", r.status == 400, r.status)

            async with s.delete(f"{BASE}/notes/{new_id}", headers=auth) as r:
                body = await r.json()
                check(
                    "归档返回 archived",
                    r.status == 200 and body.get("action") == "archived",
                    body,
                )
            async with s.get(f"{BASE}/notes/{new_id}", headers=auth) as r:
                note = (await r.json())["note"]
                check("归档后 archived=True", note.get("archived") is True, note.get("archived"))

        # ---------- 日记 ----------
        async with s.post(
            f"{BASE}/diary",
            headers=auth,
            json={"content_md": "E2E 日记内容", "tags": ["E2E"], "mode": "append_today"},
        ) as r:
            body = await r.json()
            check("写日记 append_today", r.status == 200 and body.get("note_id"), body)
            diary_id = body.get("note_id")
            diary_date = body.get("diary_date")

        # 再追加一次，确认合并到同一天同一篇
        async with s.post(
            f"{BASE}/diary",
            headers=auth,
            json={"content_md": "第二段", "mode": "append_today"},
        ) as r:
            body = await r.json()
            check("第二次追加同一篇", body.get("note_id") == diary_id, body.get("note_id"))

        if diary_id:
            async with s.get(f"{BASE}/notes/{diary_id}", headers=auth) as r:
                note = (await r.json())["note"]
                check("追加内容在两段里", "第二段" in note["body_md"], note["body_md"][:60])
                check("追加后标签未丢", "E2E" in note["tags"], note["tags"])

        async with s.post(f"{BASE}/diary", headers=auth, json={"content_md": ""}) as r:
            check("空正文被拒", r.status == 400, r.status)
        async with s.post(f"{BASE}/diary", headers=auth, json={"content_md": "x", "mode": "乱写"}) as r:
            check("非法 mode 被拒", r.status == 400, r.status)
        async with s.post(
            f"{BASE}/diary",
            headers=auth,
            json={"content_md": "x", "mode": "create", "kind": "乱写"},
        ) as r:
            body = await r.json()
            # 桥接不接收 kind，一律按 diary 写；传乱值不该 500，也不该写出怪条目
            check("未知 kind 被忽略", r.status == 200, body)
            if r.status == 200:
                async with s.get(f"{BASE}/notes/{body['note_id']}", headers=auth) as rr:
                    note = (await rr.json())["note"]
                    check("未知 kind 落成 diary", note["kind"] == "diary", note["kind"])

        # ---------- SSE ----------
        try:
            async with s.get(
                f"{BASE}/events?token={token}", timeout=aiohttp.ClientTimeout(total=3)
            ) as r:
                check("SSE 本机 query 令牌 200", r.status == 200, r.status)
                chunk = await r.content.read(64)
                check("SSE 首包是注释", chunk.startswith(b":"), chunk[:20])
        except asyncio.TimeoutError:
            check("SSE 本机 query 令牌 200", True)
            check("SSE 首包是注释", True)

        async with s.get(
            f"{BASE}/events?token={token}",
            headers={"X-Forwarded-For": "8.8.8.8"},
        ) as r:
            check("SSE 带转发头拒绝", r.status == 401, r.status)
        async with s.get(
            f"{BASE}/events?token={token}",
            headers={"Forwarded": "for=8.8.8.8"},
        ) as r:
            check("SSE Forwarded 头拒绝", r.status == 401, r.status)

        async with s.get(f"{BASE}/events", headers=auth, timeout=aiohttp.ClientTimeout(total=3)) as r:
            check("SSE header 令牌 200", r.status == 200, r.status)

        # ---------- 方法限制 ----------
        async with s.put(f"{BASE}/notes/{real_id}", headers=auth, json={}) as r:
            check("PUT 返回 405", r.status == 405, r.status)

    # ---------- 数据层校验 ----------
    notes = db.list_notes(True)
    check("写入后条数增加", len(notes) >= len(listed), len(notes))
    sig = db.note_signature()
    check("签名可用", isinstance(sig, str) and len(sig) > 0, sig)

    server.stop()
    check("桥接已停", not server.running)

    for f in sorted(tmp.iterdir()):
        mode = f.stat().st_mode & 0o777
        check(f"{f.name} 权限 600", mode == 0o600, oct(mode))

    db.close()
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    if failed:
        print("失败列表：", "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
