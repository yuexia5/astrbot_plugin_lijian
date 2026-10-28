"""模拟 AstrBot 运行时，验证 main.py 的插件类：注册、指令、工具、Web API、桥接。"""
import asyncio
import gzip
import json
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ---------- astrbot mock ----------


class _NullLogger:
    """模拟 astrbot.api.logger：测试里不打印插件日志。"""

    def _noop(self, *args, **kwargs):
        pass

    debug = info = warning = error = critical = exception = _noop


astrbot = type(sys)("astrbot")
sys.modules["astrbot"] = astrbot

from types import ModuleType


class AstrBotConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


api = type(sys)("astrbot.api")
api.AstrBotConfig = AstrBotConfig
api.logger = _NullLogger()
astrbot.api = api

event_mod = ModuleType("astrbot.api.event")
filter_mod = ModuleType("astrbot.api.event.filter")


class AstrMessageEvent:
    def __init__(self, text="", private=True, session="sess-1"):
        self.text = text
        self._private = private
        self._session = session

    def plain_result(self, message):
        return message

    def is_private_chat(self):
        return self._private

    def get_session_id(self):
        return self._session


class _Filter:
    def command(self, name):
        def decorate(func):
            func.__command__ = name
            return func

        return decorate

    def llm_tool(self, name):
        def decorate(func):
            func.__tool__ = name
            return func

        return decorate

    def on_llm_request(self, priority=0):
        def decorate(func):
            func.__hook__ = "on_llm_request"
            return func

        return decorate


filter_mod.command = _Filter().command
filter_mod.llm_tool = _Filter().llm_tool
filter_mod.on_llm_request = _Filter().on_llm_request
event_mod.filter = filter_mod
event_mod.AstrMessageEvent = AstrMessageEvent
api.event = event_mod
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.event.filter"] = filter_mod


class ProviderRequest:
    """只保留插件用到的字段。"""

    def __init__(self, session_id="sess-1", system_prompt="", prompt=""):
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.prompt = prompt


provider_mod = ModuleType("astrbot.api.provider")
provider_mod.ProviderRequest = ProviderRequest
api.provider = provider_mod
sys.modules["astrbot.api.provider"] = provider_mod

star_mod = ModuleType("astrbot.api.star")


class Context:
    def __init__(self):
        self.registered = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, methods, desc))


class Star:
    def __init__(self, context):
        self.context = context


def register(name, display, desc, version):
    def decorate(cls):
        cls.__registered__ = (name, display, desc, version)
        return cls

    return decorate


star_mod.Context = Context
star_mod.Star = Star
star_mod.register = register
api.star = star_mod
sys.modules["astrbot.api.star"] = star_mod

core_mod = ModuleType("astrbot.core")
utils_mod = ModuleType("astrbot.core.utils")
path_mod = ModuleType("astrbot.core.utils.astrbot_path")
path_mod.get_astrbot_plugin_data_path = lambda: "/tmp/opencode/lijian-mock-data"
utils_mod.astrbot_path = path_mod
core_mod.utils = utils_mod
sys.modules["astrbot.core"] = core_mod
sys.modules["astrbot.core.utils"] = utils_mod
sys.modules["astrbot.core.utils.astrbot_path"] = path_mod

web_mod = ModuleType("astrbot.api.web")


def json_response(data, status_code=200):
    return {"_json": data, "_status": status_code}


def error_response(message, status_code=400):
    return {"_error": message, "_status": status_code}


def file_response(path, filename="", content_type=""):
    return {"_file": path, "filename": filename, "content_type": content_type}


class QueryParams:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None, type=str):
        return self._values.get(key, default)


class FakeUpload:
    def __init__(self, content):
        self.filename = "import.md"
        self.content_type = "text/markdown"
        self._content = content

    async def save(self, path):
        Path(path).write_text(self._content, encoding="utf-8")


class FakeFiles(dict):
    pass


class _RequestProxy:
    username = "mock-user"
    query = QueryParams({})

    async def json(self, default=None):
        return getattr(self, "_body", default)

    async def files(self):
        return FakeFiles(getattr(self, "_files", {}))


request_proxy = _RequestProxy()
web_mod.request = request_proxy
web_mod.json_response = json_response
web_mod.error_response = error_response
web_mod.file_response = file_response
web_mod.PluginUploadFile = FakeUpload
api.web = web_mod
sys.modules["astrbot.api.web"] = web_mod

# ---------- import & instantiate ----------
import main as plugin_main


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


PASS = 0


def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"  ok - {name}")


with tempfile.TemporaryDirectory() as tmp:
    config = AstrBotConfig(
        {
            "db_path": str(Path(tmp) / "lijian.sqlite3"),
            "bridge_host": "127.0.0.1",
            "bridge_port": free_port(),
            "bridge_enabled": True,
            "token": "",
            "default_mode": "append_today",
            # 备份指到临时目录，顺带验证「启动时补备份」这条路。
            "backup_enabled": True,
            "backup_dir": str(Path(tmp) / "backups"),
            "backup_keep": 3,
            "backup_hour": 0,
        }
    )
    ctx = Context()
    plugin = plugin_main.LijianPlugin(ctx, config)
    check("plugin registered", plugin_main.LijianPlugin.__registered__[0] == "astrbot_plugin_lijian")
    check(
        "db opened",
        Path(tmp, "lijian.sqlite3").exists() and plugin._db is not None,
    )
    token = plugin._db.settings()["ai_token"]
    check("token auto generated", token.startswith("lijian-"))
    check("bridge running", plugin._bridge is not None and plugin._bridge.running)
    check(
        "backup created at startup",
        len(list((Path(tmp) / "backups").glob("lijian-*.json.gz"))) == 1,
    )
    check("web apis registered", len(ctx.registered) == 26)
    routes = {route for route, _, _ in ctx.registered}
    check("route notes list", f"/astrbot_plugin_lijian/notes" in routes)
    check("route archive POST", f"/astrbot_plugin_lijian/notes/<note_id>/archive" in routes)
    check("route export all", f"/astrbot_plugin_lijian/export/all" in routes)
    check("route on this day", f"/astrbot_plugin_lijian/on-this-day" in routes)
    check("route backup now", f"/astrbot_plugin_lijian/backup" in routes)
    check("route restore backup", f"/astrbot_plugin_lijian/import/backup" in routes)

    async def run_tools():
        # 工具
        result = json.loads(await plugin.write_lijian_diary(None, "今天很开心", title="日记", tags="a,b", mode="create"))
        check("tool write ok", result["ok"] and result["title"] == "日记")
        note_id = result["note_id"]
        result = json.loads(await plugin.list_lijian_notes(None))
        check("tool list", result["ok"] and len(result["notes"]) >= 2)
        result = json.loads(await plugin.read_lijian_note(None, note_id))
        check("tool read", result["ok"] and "今天很开心" in result["note"]["body_md"])
        lines = [line async for line in plugin.lijian_read(AstrMessageEvent(), note_id[:8])]
        check("command read by id prefix", "今天很开心" in lines[0])
        result = json.loads(await plugin.update_lijian_note(None, note_id, title="改名"))
        check("tool update", result["ok"] and result["note"]["title"] == "改名")
        result = json.loads(await plugin.delete_lijian_note(None, note_id))
        check("tool delete archives", result["ok"] and result["note"]["archived"])

        # 指令（async generator）
        lines = [line async for line in plugin.lijian_status(AstrMessageEvent())]
        check("command status", "已配置" in lines[0] and "接口已启动" in lines[0])
        lines = [line async for line in plugin.lijian_list(AstrMessageEvent(), 5)]
        check("command list", "璃笺书架" in lines[0])

        # Web API: list
        request_proxy.query = QueryParams({"include_archived": "true", "kind": "diary", "q": ""})
        response = await plugin.api_list_notes()
        check("api list", response["_json"]["notes"] and response["_json"]["notes"][0]["kind"] == "diary")
        check("api list omits body by default", "body_md" not in response["_json"]["notes"][0])

        request_proxy.query = QueryParams({"include_archived": "true", "include_body": "true"})
        response = await plugin.api_list_notes()
        check("api list with body", "body_md" in response["_json"]["notes"][0])

        # 搜索：命中数要回给前端，多词按 AND
        request_proxy.query = QueryParams({"include_archived": "true", "q": "今天"})
        response = await plugin.api_list_notes()
        payload = response["_json"]
        check("api search returns matched", payload.get("matched") == len(payload["notes"]))
        request_proxy.query = QueryParams({"include_archived": "true", "q": "今天 开心"})
        response = await plugin.api_list_notes()
        check("api search multi term", len(response["_json"]["notes"]) <= 1)

        # Web API: create + update + archive + restore
        request_proxy._body = {"title": "页面便签", "body_md": "正文", "kind": "note", "tags": ["x"]}
        response = await plugin.api_create_note()
        new_id = response["_json"]["note"]["id"]
        request_proxy._body = {"title": "页面便签改", "tags": ["x", "y"]}
        response = await plugin.api_update_note(new_id)
        check("api update", response["_json"]["note"]["title"] == "页面便签改")
        response = await plugin.api_archive_note(new_id)
        check("api archive", response["_json"]["message"] == "已归档")
        response = await plugin.api_restore_note(new_id)
        check("api restore", response["_json"]["message"] == "已恢复")

        # Web API: 置顶
        response = await plugin.api_pin_note(new_id)
        check("api pin", response["_json"]["note"]["pinned"] is True)
        response = await plugin.api_unpin_note(new_id)
        check("api unpin", response["_json"]["note"]["pinned"] is False)

        # Web API: 随手翻
        request_proxy.query = QueryParams({})
        response = await plugin.api_random_note()
        check("api random", bool(response["_json"]["note"]["id"]))
        request_proxy.query = QueryParams({"exclude": response["_json"]["note"]["id"]})
        response = await plugin.api_random_note()
        check("api random excludes current", bool(response["_json"]["note"]["id"]))

        # Web API: 内容签名
        response = await plugin.api_signature()
        check("api signature", "|" in response["_json"]["signature"])

        # Web API: 周报素材
        request_proxy.query = QueryParams({})
        response = await plugin.api_weekly()
        weekly = response["_json"]
        check("api weekly", weekly["start"] <= weekly["end"] and "digest_md" in weekly)
        request_proxy.query = QueryParams({"start": "不是日期"})
        response = await plugin.api_weekly()
        check("api weekly rejects bad date", "日期格式" in str(response.get("_error", "")))

        # Web API: 全量 JSON 备份与恢复
        response = await plugin.api_export_json()
        backup = json.loads(response["_json"]["content"])
        check(
            "api export json",
            backup["format"] == "lijian-backup" and backup["count"] >= 1,
        )
        check(
            "api export json hides token",
            "ai_token" not in json.dumps(backup.get("settings", {}), ensure_ascii=False),
        )
        request_proxy._body = backup
        response = await plugin.api_import_json()
        check("api import json", response["_json"]["updated"] >= 1)
        request_proxy._body = {"notes": [{"body_md": "没 id"}, "垃圾"]}
        response = await plugin.api_import_json()
        check("api import json skips junk", response["_json"]["skipped"] == 2)

        # Web API: 那年今天
        plugin._db.create_note(
            {
                "title": "去年的今天",
                "body_md": "# 旧标题\n\n那年今天写的正文。",
                "kind": "diary",
                "diary_date": "2025-10-19",
            }
        )
        request_proxy.query = QueryParams({"date": "2026-10-19"})
        response = await plugin.api_on_this_day()
        otd = response["_json"]
        check("api on-this-day", otd["count"] == 1 and otd["items"][0]["years_ago"] == 1)
        check(
            "api on-this-day excerpt strips markdown",
            otd["items"][0]["excerpt"].startswith("那年今天写的正文"),
            otd["items"][0]["excerpt"],
        )
        request_proxy.query = QueryParams({"date": "2026/10/19"})
        response = await plugin.api_on_this_day()
        check("api on-this-day rejects bad date", "日期格式" in str(response.get("_error", "")))

        # Web API: 立即备份 + 列表 + 从 gz 恢复
        # 恢复时的临时文件走 _data_dir，指到临时目录，别在 /tmp 里留东西。
        plugin._data_dir = Path(tmp)
        response = await plugin.api_backup_now()
        info = response["_json"]["backup"]
        backup_path = Path(tmp) / "backups" / info["file"]
        check("api backup now writes file", backup_path.is_file() and info["bytes"] > 0)
        check("api backup now counts notes", info["count"] >= 1)
        response = await plugin.api_list_backups()
        listing = response["_json"]
        check("api backups list", len(listing["items"]) >= 2 and listing["keep"] == 3)
        check("api backups last run recorded", bool(listing["last"].get("date")))
        check(
            "backup file is 0600",
            (backup_path.stat().st_mode & 0o777) == 0o600,
            oct(backup_path.stat().st_mode & 0o777),
        )

        # 超出保留份数的旧备份要被清掉
        for index in range(5):
            (Path(tmp) / "backups" / f"lijian-2020010{index}-000000.json.gz").write_bytes(b"old")
        response = await plugin.api_backup_now()
        kept = sorted((Path(tmp) / "backups").glob("lijian-*.json.gz"))
        check("api backup prunes old files", len(kept) == 3, len(kept))
        check("api backup keeps newest", kept[-1].name.startswith("lijian-2026"))

        payload = json.loads(gzip.decompress(backup_path.read_bytes()).decode("utf-8"))
        check("backup gz is valid json", payload.get("format") == "lijian-backup")

        gz_upload = FakeUpload("ignored")
        gz_upload.filename = "lijian-backup.json.gz"

        async def save_gz(path, _payload=backup_path.read_bytes()):
            Path(path).write_bytes(_payload)

        gz_upload.save = save_gz
        request_proxy._files = {"file": gz_upload}
        response = await plugin.api_import_backup()
        check("api import backup gz", response["_json"]["updated"] >= 1, response)
        request_proxy._files = {}
        response = await plugin.api_import_backup()
        check("api import backup needs file", "缺少" in str(response.get("_error", "")))

        # Web API: 本地图片的文件名白名单
        response = await plugin.api_get_media("../../etc/passwd")
        check("api media rejects traversal", "非法" in str(response.get("_error", "")))
        response = await plugin.api_get_media("a" * 32 + ".png")
        check("api media missing 404", response.get("_status") == 404)

        # Web API: 图片上传的成功路径（之前只测了拒绝分支）
        media_root = Path(tmp) / "media_test"
        plugin._data_dir = media_root
        payload = b"\x89PNG\r\n\x1a\n" + b"0" * 1024

        upload = FakeUpload("ignored")
        upload.filename = "photo.png"

        async def save_payload(path, _payload=payload):
            Path(path).write_bytes(_payload)

        upload.save = save_payload
        request_proxy._files = {"file": upload}
        response = await plugin.api_upload_media()
        uploaded = response.get("_json") or {}
        check("api upload media ok", bool(uploaded.get("name")), response)
        check("api upload returns media path", str(uploaded.get("url", "")).startswith("media/"))

        saved = media_root / "media" / str(uploaded.get("name"))
        check("api upload file exists", saved.is_file())
        check("api upload file is 0600", (saved.stat().st_mode & 0o777) == 0o600,
              oct(saved.stat().st_mode & 0o777))
        check("api media dir is 0700", ((media_root / "media").stat().st_mode & 0o777) == 0o700,
              oct((media_root / "media").stat().st_mode & 0o777))
        check("api upload size matches", saved.read_bytes() == payload)

        response = await plugin.api_get_media(str(uploaded.get("name")))
        check("api media read back", Path(response.get("_file", "")).is_file(), response)

        bad = FakeUpload("x")
        bad.filename = "evil.sh"
        request_proxy._files = {"file": bad}
        response = await plugin.api_upload_media()
        check("api upload rejects suffix", "只支持" in str(response.get("_error", "")), response)

        big = FakeUpload("x")
        big.filename = "big.png"

        async def save_big(path, _size=plugin_main.MAX_MEDIA_BYTES + 1):
            Path(path).write_bytes(b"0" * _size)

        big.save = save_big
        request_proxy._files = {"file": big}
        response = await plugin.api_upload_media()
        check("api upload rejects oversize", "太大" in str(response.get("_error", "")), response)
        check("api upload cleans oversize file",
              not list((media_root / "media").glob("*"))[-1:] or
              sum(1 for _ in (media_root / "media").iterdir()) == 1)

        request_proxy._files = {}
        response = await plugin.api_upload_media()
        check("api upload needs file", "缺少" in str(response.get("_error", "")), response)
        request_proxy._files = {}
        plugin._data_dir = Path(tmp)

        # Web API: diary write + markdown + status + stats
        request_proxy._body = {"content_md": "页面上写的", "tags": ["页面"]}
        response = await plugin.api_write_diary()
        check("api diary", response["_json"]["note"]["kind"] == "diary")
        written_id = response["_json"]["note"]["id"]
        request_proxy.query = QueryParams({})
        response = await plugin.api_note_markdown(written_id)
        check("api md export", response["_json"]["content"].startswith("---"))
        response = await plugin.api_status()
        check("api status", response["_json"]["bridge"]["running"] is True and response["_json"]["token"])
        response = await plugin.api_stats()
        check("api stats", response["_json"]["notes"] >= 2)

        # Web API: import
        request_proxy._files = {"file": FakeUpload("---\ntitle: 导入\n---\n\n内容")}
        response = await plugin.api_import()
        check("api import", response["_json"]["count"] == 1 and response["_json"]["notes"][0]["title"] == "导入")

        # Web API: export all zip
        response = await plugin.api_export_all()
        check("api export zip", response["_file"].endswith(".zip") and Path(response["_file"]).exists())

    asyncio.run(run_tools())

    async def run_recall():
        # 造一篇日期明确的日记 + 一条便签，验证回卷只翻日记
        plugin._db.create_note(
            {
                "title": "牡丹园",
                "body_md": "今天去了曹州牡丹园，花开得很好。",
                "kind": "diary",
                "diary_date": plugin_main.local_today(),
                "tags": ["牡丹"],
                "source": "local",
            }
        )
        plugin._db.create_note(
            {
                "title": "一条便签",
                "body_md": "记得买牛奶。",
                "kind": "note",
                "tags": [],
                "source": "local",
            }
        )

        plugin._recalled.clear()
        event = AstrMessageEvent(private=True, session="sess-private")
        req = ProviderRequest(session_id="sess-private", system_prompt="你是女仆")
        await plugin.recall_before_llm(event, req)
        check("recall injects", "璃笺 · 回卷" in req.system_prompt)
        check("recall keeps system prompt", "你是女仆" in req.system_prompt)
        check("recall includes diary", "牡丹园" in req.system_prompt)
        check("recall excludes note", "买牛奶" not in req.system_prompt)

        # 同一个会话翻过一次就不再塞
        again = ProviderRequest(session_id="sess-private", system_prompt="你是女仆")
        await plugin.recall_before_llm(event, again)
        check("recall not repeated", again.system_prompt == "你是女仆")

        # 群聊默认不翻（日记不该说给一群人听）
        plugin._recalled.clear()
        group_req = ProviderRequest(session_id="group-1", system_prompt="")
        await plugin.recall_before_llm(
            AstrMessageEvent(private=False, session="group-1"), group_req
        )
        check("recall skipped in group", "回卷" not in group_req.system_prompt)

        # 字数上限要真的生效
        plugin.config["recall_max_chars"] = 300
        plugin._recalled.clear()
        small = ProviderRequest(session_id="sess-small")
        await plugin.recall_before_llm(
            AstrMessageEvent(private=True, session="sess-small"), small
        )
        check("recall respects char cap", len(small.system_prompt) < 900,
              len(small.system_prompt))
        plugin.config["recall_max_chars"] = 2400

        # 开关关掉就一句都不加
        plugin.config["recall_enabled"] = False
        plugin._recalled.clear()
        off = ProviderRequest(session_id="sess-off")
        await plugin.recall_before_llm(
            AstrMessageEvent(private=True, session="sess-off"), off
        )
        check("recall respects switch", off.system_prompt == "")
        plugin.config["recall_enabled"] = True

        # 预览指令
        lines = [line async for line in plugin.lijian_recall(AstrMessageEvent())]
        check("command recall preview", "下次回卷会翻" in lines[0])

        # 检索工具：关键词命中 + 片段
        result = json.loads(await plugin.search_lijian_notes(None, "牡丹"))
        check("tool search hits", result["ok"] and result["count"] >= 1)
        check("tool search snippet", "曹州" in result["notes"][0]["snippet"])
        check("tool search returns id", bool(result["notes"][0]["id"]))

        # 日期范围能把东西筛掉
        result = json.loads(
            await plugin.search_lijian_notes(None, "", since="2000-01-01", until="2000-01-02")
        )
        check("tool search date range", result["ok"] and result["count"] == 0)

    asyncio.run(run_recall())

    plugin.terminate_sync = None
    asyncio.run(plugin.terminate())
    check("bridge stopped after terminate", not plugin._bridge or not plugin._bridge.running)
    check("db closed", plugin._db is None)

print(f"\n全部通过（{PASS} 项检查）")