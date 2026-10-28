"""模拟 AstrBot 运行时，验证 main.py 的插件类：注册、指令、工具、Web API、桥接。"""
import asyncio
import gzip
import json
from datetime import datetime, timedelta
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
        self.message_str = text
        self.unified_msg_origin = f"test:FriendMessage:{session}"
        self._private = private
        self._session = session

    def plain_result(self, message):
        return message

    def is_private_chat(self):
        return self._private

    def get_session_id(self):
        return self._session


class _Filter:
    def command(self, name, alias=None, sub_command=None, **kwargs):
        def decorate(func):
            func.__command__ = name
            func.__alias__ = set(alias or set())
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

    def on_llm_response(self, priority=0):
        def decorate(func):
            func.__hook__ = "on_llm_response"
            return func

        return decorate


filter_mod.command = _Filter().command
filter_mod.llm_tool = _Filter().llm_tool
filter_mod.on_llm_request = _Filter().on_llm_request
filter_mod.on_llm_response = _Filter().on_llm_response
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
        self.cron_manager = FakeCronManager()
        self.persona_manager = None
        self.provider = None

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, methods, desc))

    def get_using_provider(self, umo=None):
        return self.provider


class FakeCronJob:
    def __init__(self, job_id, name):
        self.job_id = job_id
        self.name = name


class FakeCronManager:
    """够用的定时任务桩：记下排了什么，能列出、能删。"""

    def __init__(self):
        self.jobs = []
        self.handlers = {}
        self._seq = 0

    async def list_jobs(self, job_type=None):
        return list(self.jobs)

    async def add_basic_job(
        self,
        *,
        name,
        cron_expression,
        handler,
        description=None,
        persistent=False,
        **kwargs,
    ):
        self._seq += 1
        job = FakeCronJob(f"job-{self._seq}", name)
        self.jobs.append(job)
        self.handlers[job.job_id] = (cron_expression, handler)
        return job

    async def delete_job(self, job_id):
        self.jobs = [job for job in self.jobs if job.job_id != job_id]
        self.handlers.pop(job_id, None)


class FakePersonaManager:
    def __init__(self, prompt=""):
        self.prompt = prompt

    async def get_default_persona_v3(self, umo=None):
        return {"name": "测试人格", "prompt": self.prompt}


class FakeProvider:
    """假的模型提供商：记住被调了几次、传了什么，返回预设正文。"""

    def __init__(self, text="今天过得还行。"):
        self.text = text
        self.calls = []

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})

        class _Resp:
            pass

        resp = _Resp()
        resp.completion_text = self.text
        return resp


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
# 数据目录现在写死在插件数据目录下（没有 db_path 配置项了），
# 用可变容器让每个用例能把根目录指到自己的临时目录里。
MOCK_DATA_ROOT = {"path": "/tmp/opencode/lijian-mock-data"}
path_mod.get_astrbot_plugin_data_path = lambda: MOCK_DATA_ROOT["path"]
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
    # 数据目录、端口、备份份数都不再是配置项，改由模块常量决定；
    # 测试里把它们指到临时目录 / 空闲端口，别撞上正在跑的 AstrBot。
    MOCK_DATA_ROOT["path"] = tmp
    plugin_main.BRIDGE_PORT = free_port()
    plugin_main.BACKUP_KEEP_DEFAULT = 3
    plugin_main.BACKUP_HOUR_DEFAULT = 0
    # 配置项都有默认值，这里只动跟测试有关的两个
    config = AstrBotConfig(
        {
            "default_mode": "append_today",
            "backup_enabled": True,
        }
    )
    data_root = Path(tmp) / plugin_main.PLUGIN_NAME
    ctx = Context()
    plugin = plugin_main.LijianPlugin(ctx, config)
    check("plugin registered", plugin_main.LijianPlugin.__registered__[0] == "astrbot_plugin_lijian")

    # 指令名要说人话，英文留作别名，老用法不断
    command_names = set()
    command_aliases = set()
    for member in vars(plugin_main.LijianPlugin).values():
        if hasattr(member, "__command__"):
            command_names.add(member.__command__)
        command_aliases |= getattr(member, "__alias__", set())
    check(
        "commands are chinese",
        {"日记状态", "写日记", "日记列表", "读日记", "删日记"} <= command_names,
        command_names,
    )
    check(
        "commands keep english alias",
        {"lijian_status", "lijian_write", "lijian_delete"} <= command_aliases,
        command_aliases,
    )
    check(
        "db opened",
        (data_root / "lijian.sqlite3").exists() and plugin._db is not None,
    )
    token = plugin._db.settings()["ai_token"]
    check("token auto generated", token.startswith("lijian-"))
    check("bridge running", plugin._bridge is not None and plugin._bridge.running)
    check(
        "backup created at startup",
        len(list((data_root / "backups").glob("lijian-*.json.gz"))) == 1,
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
        check(
            "command status is plain spoken",
            "璃笺状态：正常" in lines[0] and "自己写日记" in lines[0],
            lines[0][:80],
        )
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
        plugin._data_dir = data_root
        response = await plugin.api_backup_now()
        info = response["_json"]["backup"]
        backup_path = data_root / "backups" / info["file"]
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
            (data_root / "backups" / f"lijian-2020010{index}-000000.json.gz").write_bytes(b"old")
        response = await plugin.api_backup_now()
        kept = sorted((data_root / "backups").glob("lijian-*.json.gz"))
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
        plugin._data_dir = data_root

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


    async def run_auto_write():
        # 定时任务：注册一次，重载不叠加
        await plugin.initialize()
        check("auto write job registered", len(ctx.cron_manager.jobs) == 1)
        job = ctx.cron_manager.jobs[0]
        expression, handler = ctx.cron_manager.handlers[job.job_id]
        check("auto write cron expression", expression == "0 23 * * *", expression)
        check("auto write handler bound", handler == plugin._auto_write_tick)
        await plugin.initialize()
        check("auto write job not duplicated", len(ctx.cron_manager.jobs) == 1)

        # 「几点写」支持到分钟：填 20:20，排出来就是 20 点 20 分
        plugin.config["auto_write_hour"] = "20:20"
        await plugin.initialize()
        expression, _ = ctx.cron_manager.handlers[ctx.cron_manager.jobs[0].job_id]
        check("auto write cron with minute", expression == "20 20 * * *", expression)
        check("auto write parse hhmm", plugin._auto_write_at() == (20, 20))
        # 老配置里存的是整数，别因为换了格式就把人家设的点弄丢
        plugin.config["auto_write_hour"] = 21
        check("auto write parse legacy int", plugin._auto_write_at() == (21, 0))
        plugin.config["auto_write_hour"] = "7:5"
        check("auto write parse short hhmm", plugin._auto_write_at() == (7, 5))
        plugin.config["auto_write_hour"] = "乱填"
        check("auto write bad value falls back", plugin._auto_write_at() == (23, 0))
        plugin.config["auto_write_hour"] = "23:00"
        await plugin.initialize()

        # 素材采集：双方的话都要留下，群里的话一句不留
        plugin._db.material_clear()
        private_event = AstrMessageEvent(text="今天累不累", private=True, session="sess-p")

        def _reply(text):
            return type("R", (), {"completion_text": text})()

        await plugin.collect_material_on_request(private_event, ProviderRequest())
        await plugin.collect_material_on_response(private_event, _reply("还行，你呢"))
        pool = plugin._db.material_pool()
        check("material collected", len(pool) == 2, len(pool))
        check("material roles", [item["r"] for item in pool] == ["user", "assistant"])
        await plugin.collect_material_on_response(private_event, _reply("还行，你呢"))
        check("material dedupes repeat", len(plugin._db.material_pool()) == 2)
        group_event = AstrMessageEvent(text="群里说一句", private=False, session="g1")
        await plugin.collect_material_on_request(group_event, ProviderRequest())
        check("material skipped in group", len(plugin._db.material_pool()) == 2)

        # 今天刚写过、现在也没话：一个 token 都不该花
        provider = FakeProvider("不该被调用")
        ctx.provider = provider
        plugin._db.material_clear()
        plugin._db.state_set("auto_write", {})
        plugin._db.write_diary_from_ai(
            {
                "content_md": "刚才已经写过一篇了。",
                "title": "刚写过",
                "diary_date": None,
                "tags": [],
                "mode": "create",
            }
        )
        await plugin._auto_write_tick()
        check("auto write skips when quiet", provider.calls == [])
        check("auto write skip is cheap", plugin._db.material_pending() == 0)

        # 素材够了才写，并且要用设好的人格口吻
        for index in range(8):
            plugin._db.material_append(
                "test:FriendMessage:sess-p",
                "user" if index % 2 == 0 else "assistant",
                f"第{index}条聊天内容，说了一些今天发生的事，凑够字数用。",
            )
        ctx.persona_manager = FakePersonaManager("你是白薇，主人的女仆，说话简短。")
        provider = FakeProvider("```markdown\n## 今天\n\n聊了很久，也累了。\n```")
        ctx.provider = provider
        await plugin._auto_write_tick()
        check("auto write calls model once", len(provider.calls) == 1, len(provider.calls))
        check(
            "auto write uses persona",
            "白薇" in str(provider.calls[0]["system_prompt"]),
            provider.calls[0]["system_prompt"][:60],
        )
        check(
            "auto write feeds material",
            "第0条聊天内容" in str(provider.calls[0]["prompt"]),
        )
        check("auto write consumes material", plugin._db.material_pending() == 0)
        check("auto write keeps rows for audit", len(plugin._db.material_pool()) == 8)
        # 游标之后的才算没写过：补一句新的，只该数这一条
        plugin._db.material_append(
            "test:FriendMessage:sess-p", "user", "写完之后又聊了一句。"
        )
        check("cursor only counts newer", plugin._db.material_pending() == 1)
        check("auto write marks today", plugin._auto_write_done_today())
        wrote = plugin._db.search_notes("聊了很久")
        check("auto write saved note", len(wrote) >= 1)
        check("auto write strips fence", "```" not in wrote[0]["body_md"])

        # 同一天再触发也不许再写一遍——这是额度
        provider = FakeProvider("第二次不该发生")
        ctx.provider = provider
        plugin._db.material_append(
            "test:FriendMessage:sess-p", "user", "又聊了两句，但今天已经写过了。"
        )
        await plugin._auto_write_tick()
        check("auto write once per day", provider.calls == [])

        # 但主人自己开口要，就不该被「今天已经写过了」挡下来
        provider = FakeProvider("**手动要的那篇**\n\n他让我现在写一篇。\n\n标签：手动、戳一下")
        ctx.provider = provider
        result = await plugin._compose_diary(manual=True)
        check(
            "manual write ignores once-per-day",
            len(provider.calls) == 1,
            len(provider.calls),
        )
        check("manual write ok", result.get("ok") is True, str(result)[:80])
        check("manual write has chars", (result.get("chars") or 0) > 0)
        check("manual write has note", bool(result.get("note")))

        # 命令在不带内容时不能报「必要参数缺失」，得能自己写
        import inspect as _inspect

        params = _inspect.signature(plugin.lijian_write).parameters
        check(
            "write command content optional",
            params["content"].default == "",
            repr(params["content"].default),
        )

        # 一句话也值得写：门槛不该卡在条数上
        plugin._db.material_clear()
        plugin._db.state_set("auto_write", {})
        plugin._db.material_append(
            "test:FriendMessage:sess-p", "user", "今天下雨，忘了带伞，回来鞋全湿了。"
        )
        provider = FakeProvider("就这一句，也记一下。")
        ctx.provider = provider
        await plugin._auto_write_tick()
        check(
            "auto write accepts single line",
            len(provider.calls) == 1,
            len(provider.calls),
        )

        # 冷场也能写：好几天没聊，写一篇安静的，但不许编
        plugin._db.material_clear()
        plugin._db.state_set("auto_write", {})
        old_day = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        # 把前面几篇的日期整体挪到三天前，造出「冷场三天」这个场景
        plugin._db.conn.execute(
            "UPDATE notes SET diary_date = ? WHERE kind = 'diary'", (old_day,)
        )
        plugin._db.conn.commit()
        provider = FakeProvider("这几天没人说话，屋里安静得能听见钟走。")
        ctx.provider = provider
        await plugin._auto_write_tick()
        check(
            "auto write fills quiet days",
            len(provider.calls) == 1,
            len(provider.calls),
        )
        check(
            "quiet prompt forbids inventing",
            "没有收到对方" in str(provider.calls[0]["prompt"]),
        )

        # 一篇都还没写过：不凭空开篇，「好久没消息」得先有消息可对比
        plugin._db.material_clear()
        plugin._db.state_set("auto_write", {})
        plugin._db.conn.execute("DELETE FROM notes")
        plugin._db.conn.commit()
        provider = FakeProvider("不该被调用")
        ctx.provider = provider
        await plugin._auto_write_tick()
        check("auto write does not invent first entry", provider.calls == [])

        # 关掉开关就不排程
        plugin.config["auto_write_enabled"] = False
        await plugin.initialize()
        check("auto write job removed when off", ctx.cron_manager.jobs == [])
        plugin.config["auto_write_enabled"] = True

        # 代码块剥离的单测
        check("strip fence plain", plugin._strip_code_fence("正文") == "正文")
        check(
            "strip fence marked",
            plugin._strip_code_fence("```markdown\n正文\n```") == "正文",
        )

        # 读日记/删日记不带 id 时给的小清单，不能抛异常
        hint = plugin._recent_notes_hint()
        check("recent hint is text", isinstance(hint, str) and hint != "")
        check("recent hint short id", "—" in hint or "还没有条目" in hint)

        # 标签行拆分：三种写法都要认，且正文里不能留下那一行
        body, tags = plugin._split_ai_tags("**标题**\n\n正文一段。\n\n标签：糖、撒娇、晚安")
        check("split tags plain", (body, tags) == ("**标题**\n\n正文一段。", ["糖", "撒娇", "晚安"]))
        body, tags = plugin._split_ai_tags("正文。\n\n**标签**：加班，雨夜")
        check("split tags bold", tags == ["加班", "雨夜"] and body == "正文。")
        body, tags = plugin._split_ai_tags("正文。\n\n标签: 糖、撒娇")
        check("split tags half colon", tags == ["糖", "撒娇"])
        check("split tags none", plugin._split_ai_tags("正文，没有标签行。") == ("正文，没有标签行。", []))
        body, tags = plugin._split_ai_tags("正文。\n\n标签：")
        check("split tags empty drops line", body == "正文。" and tags == [])
        check(
            "split tags keeps inline word",
            plugin._split_ai_tags("正文里说到标签：这个词")[1] == [],
        )

    asyncio.run(run_auto_write())

    plugin.terminate_sync = None
    asyncio.run(plugin.terminate())
    check("bridge stopped after terminate", not plugin._bridge or not plugin._bridge.running)
    check("db closed", plugin._db is None)

print(f"\n全部通过（{PASS} 项检查）")