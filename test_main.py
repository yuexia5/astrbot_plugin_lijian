"""模拟 AstrBot 运行时，验证 main.py 的插件类：注册、指令、工具、Web API、桥接。"""
import asyncio
import json
import logging
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ---------- astrbot mock ----------
logging.basicConfig(level=logging.WARNING)

astrbot = type(sys)("astrbot")
sys.modules["astrbot"] = astrbot

from types import ModuleType


class AstrBotConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


api = type(sys)("astrbot.api")
api.AstrBotConfig = AstrBotConfig
api.logger = logging.getLogger("mock-astrbot")
astrbot.api = api

event_mod = ModuleType("astrbot.api.event")
filter_mod = ModuleType("astrbot.api.event.filter")


class AstrMessageEvent:
    def __init__(self, text=""):
        self.text = text

    def plain_result(self, message):
        return message


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


filter_mod.command = _Filter().command
filter_mod.llm_tool = _Filter().llm_tool
event_mod.filter = filter_mod
event_mod.AstrMessageEvent = AstrMessageEvent
api.event = event_mod
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.event.filter"] = filter_mod

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


def check(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
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
    check("web apis registered", len(ctx.registered) == 13)
    routes = {route for route, _, _ in ctx.registered}
    check("route notes list", f"/astrbot_plugin_lijian/notes" in routes)
    check("route archive POST", f"/astrbot_plugin_lijian/notes/<note_id>/archive" in routes)
    check("route export all", f"/astrbot_plugin_lijian/export/all" in routes)

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

    plugin.terminate_sync = None
    asyncio.run(plugin.terminate())
    check("bridge stopped after terminate", not plugin._bridge or not plugin._bridge.running)
    check("db closed", plugin._db is None)

print(f"\n全部通过（{PASS} 项检查）")