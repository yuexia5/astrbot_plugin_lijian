"""璃笺 AstrBot 插件：无头环境下的日记与便签书库。

把桌面版璃笺（Tauri）的存储与写入接口移植进 AstrBot：
- 指令：/日记状态 /写日记 /日记列表 /读日记 /删日记（英文名作别名保留）
- LLM 工具：write_lijian_diary / list_lijian_notes / search_lijian_notes / read_lijian_note /
  update_lijian_note / delete_lijian_note / lijian_weekly
- 自己写：每天到点把当天攒的聊天交给模型，用当前人格的口吻写成一篇日记
- 本地写入接口：监听 127.0.0.1:27176，与桌面版璃笺 API 兼容
- WebUI 页面：书架 (pages/bookshelf)，含那年今天、连续天数
- 自动备份：每天把整库压成 .json.gz，放在数据目录的 backups/ 下，只留最近 N 份
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
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
# 查询词列表最多回这么多条，超过就告诉前端一共多少。
SEARCH_LIMIT = 200
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

# 自动备份：落在数据目录的 backups/ 下，只保留最近 N 份。
BACKUP_DIR_NAME = "backups"
BACKUP_KEEP_DEFAULT = 30
BACKUP_HOUR_DEFAULT = 3

# 写入接口固定监听本机回环。不提供开关：开了也没人会去关，暴露到局域网反而危险。
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 27176

# 自动写日记：每天到点把还没写进日记的聊天交给模型，写成一篇。
#
# 一天最多一次，这是它主动花钱的全部。但「省钱」不等于「一句话都不写」：
# 一句话也是话，安静的一天也是日子。真正要挡的是天天写空话，不是少聊了几句。
AUTO_WRITE_JOB_NAME = "璃笺：把今天写成日记"
AUTO_MIN_MATERIAL = 1            # 有一条就够，一句话也值得记
AUTO_MIN_MATERIAL_CHARS = 15     # 「嗯」「在」这种凑数的还是不算
AUTO_MATERIAL_MAX_CHARS = 8000   # 一次喂给模型的素材上限，约 4k token
AUTO_RICH_MATERIAL_CHARS = 300   # 素材超过这个量，才要求写长一点
AUTO_IDLE_GAP_DAYS = 2           # 冷场时也写，但隔两天才补一篇，别天天念经

# 让模型写日记时用的口径。重点是别让它编：没聊过的事一律不许出现。
# 这段话一天只用一次，写长点不值几个钱，但它决定了日记像人还是像周报。
AUTO_WRITE_SYSTEM = (
    "你要写一篇日记。写的人是前面那个人格，不是助手，不是旁白，"
    "所以用第一人称「我」，用那个人的语气、口头习惯和看事情的角度。\n"
    "这篇日记是写给以后会忘掉今天的自己看的。要具体：当时在做什么、"
    "听到哪句话、心里冒出来的第一个念头是什么。\n"
    "几条硬规矩：\n"
    "- 只写素材里真出现过的对话和事。素材里没有的人、地点、天气、情节，一律不许补。\n"
    "- 不许编对方的原话。要引用就引素材里现成的那一句。\n"
    "- 不总结，不升华，不写「今天真是充实的一天」这种收尾。写到哪算哪。\n"
    "- 不写「作为一个 AI」「希望对你有帮助」这种助手腔，也不要说教。\n"
    "- 情绪靠动作和反应写出来，别直接贴「我很开心」这种标签。\n"
    "- 句子可以短，可以断，可以留一句没头没尾的话。人写日记不讲究结构。\n"
    "- 直接给正文。不要代码块，不要「这是日记」的开场，不要「亲爱的日记」。"
)

# 备份文件是 gzip 过的 JSON，恢复时按魔数判断，不靠扩展名。
MAX_BACKUP_BYTES = 64 * 1024 * 1024
GZIP_MAGIC = b"\x1f\x8b"

# 新建日记时套的模板。{date} 会替换成本地日期。
DEFAULT_DIARY_TEMPLATE = (
    "**天气**：\n"
    "**心情**：\n\n"
    "## 今天做了什么\n\n- \n\n"
    "## 三件小事\n\n1. \n2. \n3. \n"
)
PLUGIN_VERSION = "1.5.3"


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
        self._backup_task: asyncio.Task | None = None
        self._data_dir = self._resolve_data_dir()
        try:
            self._open_database()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"璃笺数据库初始化失败: {exc}")
        if self._db is not None:
            self._start_bridge()
            self._start_backup_service()
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
        # 库固定放在插件数据目录，不给配置项——挪库是极少数人的需求，
        # 却让每个人都要判断「这个空框要不要填」。
        db_path = self._data_dir / "lijian.sqlite3"
        self._db = Database(db_path)
        # 令牌空着进来：库里已有就沿用（外部脚本的令牌不会因为升级失效），
        # 没有就生成一个新的。书架页面能看到。
        self._db.ensure_ai_token("")
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
        # 地址和端口写死在本机回环上：日记数据不该有「暴露到局域网」这个选项。
        host, port = BRIDGE_HOST, BRIDGE_PORT
        self._bridge = BridgeServer(self._db, host=host, port=port)
        if not self._bridge.start():
            logger.warning(
                f"璃笺本地接口启动失败（端口 {host}:{port} 可能被占用），"
                "写入仍可通过指令 / LLM 工具 / 书架页面进行。"
            )

    # ---------- 自动写日记 ----------

    def _auto_write_enabled(self) -> bool:
        try:
            return bool(self.config.get("auto_write_enabled", True))
        except Exception:  # noqa: BLE001
            return True

    def _auto_write_at(self) -> tuple[int, int]:
        """「几点写」解析成 (时, 分)。值是 "21:00" / "20:20" 这种，也认老的整数 21。

        键名故意留着 auto_write_hour 没改。改名的话，老用户存的值会被新键的默认值
        顶掉，他自己设的那个点就悄悄滚回 23 点了——这种丢配置比键名别扭难受得多。
        """
        raw = self.config.get("auto_write_hour", "23:00")
        text = "" if raw is None else str(raw).strip()
        match = re.match(r"^(\d{1,2})\s*[:：]\s*(\d{1,2})$", text)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
        else:
            try:
                hour, minute = int(text), 0
            except (TypeError, ValueError):
                hour, minute = 23, 0
        return max(0, min(hour, 23)), max(0, min(minute, 59))

    @staticmethod
    def _hhmm(hour: int, minute: int) -> str:
        return f"{hour:02d}:{minute:02d}"

    async def initialize(self) -> None:
        """AstrBot 加载完插件后调这里，定时任务要在事件循环里注册。"""
        await self._register_auto_write_job()

    async def _register_auto_write_job(self) -> None:
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            logger.info("璃笺自动写日记：当前 AstrBot 没有定时任务管理，跳过排程")
            return
        # 热重载会重新加载插件，上一次注册的任务得先摘掉，否则攒一串。
        try:
            for job in await manager.list_jobs("basic"):
                if str(getattr(job, "name", "")) == AUTO_WRITE_JOB_NAME:
                    await manager.delete_job(job.job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺清理旧定时任务失败: {exc}")
        if not self._auto_write_enabled():
            logger.info("璃笺自动写日记未启用（auto_write_enabled=false）")
            return
        hour, minute = self._auto_write_at()
        try:
            await manager.add_basic_job(
                name=AUTO_WRITE_JOB_NAME,
                cron_expression=f"{minute} {hour} * * *",
                handler=self._auto_write_tick,
                description="璃笺：把今天聊过的写成一篇日记",
                persistent=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺自动写日记排程失败: {exc}")
            return
        logger.info(f"璃笺自动写日记已排程：每天 {self._hhmm(hour, minute)}")

    @filter.on_llm_request()
    async def collect_material_on_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """聊天时顺手把对方的话记进素材池。"""
        if not self._auto_write_enabled() or self._db is None:
            return
        if not event.is_private_chat():
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        try:
            await asyncio.to_thread(
                self._db.material_append, event.unified_msg_origin, "user", text
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺记录聊天素材失败: {exc}")

    @filter.on_llm_response()
    async def collect_material_on_response(
        self, event: AstrMessageEvent, response: Any
    ):
        """把自己说的话也记进去，写日记时才知道当时怎么应的。"""
        if not self._auto_write_enabled() or self._db is None:
            return
        if not event.is_private_chat():
            return
        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            return
        try:
            await asyncio.to_thread(
                self._db.material_append, event.unified_msg_origin, "assistant", text
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺记录聊天素材失败: {exc}")

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """模型爱把正文裹在 ```markdown 里，日记本上不该有这层壳。"""
        body = str(text or "").strip()
        if not body.startswith("```"):
            return body
        lines = body.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @staticmethod
    def _split_ai_tags(text: str) -> tuple[str, list[str]]:
        """把正文末尾那行「标签：a、b」拆出来，正文里不留这一行。

        只从最后几行里找，找不到就原样返回——标签是锦上添花，
        不该因为模型少写一行就把整篇日记判成不合格。
        """
        body = str(text or "").strip()
        if not body:
            return "", []
        lines = body.splitlines()
        start = max(len(lines) - 4, 0)
        for idx in range(len(lines) - 1, start - 1, -1):
            matched = re.match(
                r"^\s*[-*]?\s*\**\s*(?:标签|tags?)\s*\**\s*[：:]\s*(.*?)\s*$",
                lines[idx],
                re.IGNORECASE,
            )
            if not matched:
                continue
            tags = []
            for tag in clean_tags(matched.group(1)):
                piece = tag.strip().strip("。.、，,")
                if piece and piece not in tags:
                    tags.append(piece[:10])
                if len(tags) >= 3:
                    break
            rest = "\n".join(lines[:idx] + lines[idx + 1 :]).rstrip()
            return rest, tags
        return body, []

    def _auto_write_done_today(self) -> bool:
        try:
            last = self._db.state_get("auto_write", {}) or {}
        except Exception:  # noqa: BLE001
            return False
        return isinstance(last, dict) and str(last.get("date", "")) == local_today()

    async def _persona_prompt(self, umo: str = "") -> str:
        """取这台 AstrBot 里给这个会话设的人格卡。

        日记得用她自己的口吻写，不能是千篇一律的「AI 助手」腔。
        取不到就空着，退回通用口径，不能因为没人格就不写。
        """
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        try:
            persona = await manager.get_default_persona_v3(umo or None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺读取人格失败，用默认口吻写: {exc}")
            return ""
        if not isinstance(persona, dict):
            return ""
        return str(persona.get("prompt") or "").strip()

    @staticmethod
    def _provider_name(prov) -> str:
        cfg = getattr(prov, "provider_config", None) or {}
        if isinstance(cfg, dict):
            return str(cfg.get("id") or cfg.get("type") or "")
        return type(prov).__name__

    def _diary_provider(self, umo: str = ""):
        """写日记用哪个模型：配置里选了就用选的，没选就跟着会话的主模型走。

        「主 LLM Provider」不是全局那一格。AstrBot 允许给不同会话绑不同的配置文件
        （这台机器上就有一份叫「粟茗」的，里面设了 deepseek），解析的时候要看 umo。
        不带 umo 只会读到默认那份，默认那份空着时还会退到提供商列表第一个，
        那个位置常常躺着一个早就下线的模型。

        所以这里把素材里那个会话的 umo 带进去，读到的才是主人真正在用的那个。
        """
        want = str(self.config.get("auto_write_provider_id", "") or "").strip()
        if want:
            try:
                for prov in self.context.get_all_providers():
                    if self._provider_name(prov) == want:
                        return prov
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"璃笺查找指定模型失败: {exc}")
            logger.warning(f"璃笺：配置里指定的模型 {want} 找不到，改用主 LLM Provider")
        for arg in ([umo, None] if umo else [None]):
            try:
                prov = self.context.get_using_provider(arg)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"璃笺取主 LLM Provider 失败: {exc}")
                continue
            if prov is not None:
                return prov
        return None

    def _days_since_last_diary(self) -> int:
        """距上一篇日记几天。一篇都没写过（或者读不出来）返回 -1。"""
        try:
            last = self._db.last_diary_date()
        except Exception:  # noqa: BLE001
            return -1
        if not last:
            return -1
        try:
            day = datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            return -1
        return (datetime.now().date() - day).days

    async def _auto_write_tick(self) -> None:
        """到点了：把还没写进日记的聊天交给模型，写成一篇。"""
        await self._compose_diary(manual=False)

    async def _compose_diary(self, manual: bool = False) -> dict:
        """写一篇日记：取素材、调模型、落库、推进游标。

        manual=True 是主人在聊天里主动要的（发「写日记」不带内容）。
        这条路不该被「今天已经写过了」「今天没怎么聊」挡下来——他开口要了，
        就该给。挡住的只有定时任务那条路。

        返回 {"ok": bool, "message": str, "note": dict|None, "chars": int}。
        """
        if self._db is None:
            return {"ok": False, "message": "璃笺数据库不可用。"}
        # 一天只写一篇。定时任务万一被重载多排了一次，也不能写两篇、扣两次额度。
        if not manual and self._auto_write_done_today():
            logger.info("璃笺自动写日记：今天已经写过了，跳过")
            return {"ok": False, "message": "今天已经写过了"}
        try:
            cursor = await asyncio.to_thread(self._db.material_cursor)
            digest, count, last_id = await asyncio.to_thread(
                self._db.material_digest, cursor, AUTO_MATERIAL_MAX_CHARS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺读取聊天素材失败: {exc}")
            return {"ok": False, "message": f"读不到聊天素材：{exc}"}

        # 有话说就写，一句也算。没话也不等于没得写——「他今天没来」本身就是日记。
        # 但天天写这个就成了念经，所以冷场要隔几天才补一篇。
        has_material = (
            count >= AUTO_MIN_MATERIAL and len(digest) >= AUTO_MIN_MATERIAL_CHARS
        )
        idle_days = 0
        if not has_material:
            idle_days = await asyncio.to_thread(self._days_since_last_diary)
            if not manual:
                if idle_days < AUTO_IDLE_GAP_DAYS:
                    logger.info(
                        f"璃笺自动写日记：今天没怎么聊（{count} 条 / {len(digest)} 字），"
                        f"上一篇才 {idle_days} 天前，这次不写（不调用模型）"
                    )
                    return {"ok": False, "message": "今天没怎么聊，先不写"}
                if idle_days < 0:
                    # 刚装好、一篇都还没写过。这时候写「好久没消息」是凭空造的，
                    # 等真有人聊过、开过头，冷场才有意义。
                    logger.info("璃笺自动写日记：一篇日记都还没有，今晚不凭空开篇")
                    return {"ok": False, "message": "还没有旧日记，先不凭空开篇"}

        # 用哪个会话的 umo 取人格：素材里最后说话的那个；没素材就用默认人格。
        session = ""
        try:
            pool = await asyncio.to_thread(self._db.material_pool, cursor)
            if pool:
                session = str(pool[-1].get("s") or "")
        except Exception:  # noqa: BLE001
            pass
        persona = await self._persona_prompt(session)
        system_prompt = f"{persona}\n\n{AUTO_WRITE_SYSTEM}" if persona else AUTO_WRITE_SYSTEM

        today = local_today()
        # 标签让模型自己给：它知道今天这篇写了什么，写死一个「日记」等于没打。
        tag_hint = (
            "\n- 正文最后另起一行写「标签：a、b、c」，两到三个词，"
            "写这篇里真的出现过的东西或心情，具体一点（糖、加班、雨夜），"
            "别写「日记」「日常」这种空泛词"
        )
        if has_material:
            if len(digest) >= AUTO_RICH_MATERIAL_CHARS:
                length_hint = "三百到六百字"
            else:
                length_hint = "一百五十到三百字，今天话不多，别硬撑，写真正记住的就行"
            prompt = (
                f"今天是 {today}。以下是今天我和对方聊过的话，按时间顺序：\n\n"
                f"{digest}\n\n"
                "把今天写成一篇日记。\n"
                "- 挑真正记住的那几个片段写，别逐句复述对话\n"
                "- 可以写当时没说出口的那半句\n"
                "- 对方说过的话，挑一句值得记的引上去，别整段搬\n"
                f"- {length_hint}，可以用一两个小标题，别堆列表和表格"
                f"{tag_hint}"
            )
        else:
            # 手动要一篇但今天什么都没聊时，idle_days 可能还是 -1（一篇旧日记都没有），
            # 照抄进提示词会变成「已经 -1 天没有收到消息」，模型只会照着这句写。
            quiet_days = max(idle_days, 1)
            prompt = (
                f"今天是 {today}。到今天为止，已经 {quiet_days} 天没有收到对方的任何消息了。\n\n"
                "写一篇今天的日记。\n"
                "- 就写这件事：这些天很安静，没有人在\n"
                "- 可以写怎么打发时间、有没有反复看手机、有没有假装不在意\n"
                "- 不要编对方说过的话，也不要编这些天发生了什么——什么消息都没有\n"
                "- 八十到一百五十字，短一点，别铺陈，别抒情到天上去"
                f"{tag_hint}"
            )

        provider = self._diary_provider(session)
        if provider is None:
            logger.warning("璃笺自动写日记：没有可用的模型提供商，跳过")
            return {"ok": False, "message": "没有可用的模型提供商"}
        provider_name = self._provider_name(provider)
        try:
            response = await provider.text_chat(
                prompt=prompt, system_prompt=system_prompt
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"璃笺自动写日记调用模型失败（{provider_name}）: {exc}"
                "；素材游标没动，在璃笺配置里换个模型重跑还能补上"
            )
            return {"ok": False, "message": f"模型调用失败（{provider_name}）：{exc}"}
        body = self._strip_code_fence(getattr(response, "completion_text", ""))
        body, ai_tags = self._split_ai_tags(body)
        if not body:
            logger.warning("璃笺自动写日记：模型返回了空正文，游标不动，下次再试")
            return {"ok": False, "message": "模型返回了空正文，素材没动，可以再试一次"}
        if not ai_tags:
            ai_tags = ["日记"]
        try:
            note = await asyncio.to_thread(
                self._db.write_diary_from_ai,
                {
                    "content_md": body,
                    "title": None,
                    "diary_date": None,
                    "tags": ai_tags,
                    "mode": "append_today",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺自动写日记写入失败: {exc}")
            return {"ok": False, "message": f"写入书架失败：{exc}"}
        # 写成功了才推进游标：这一次失败不该把这些话算成已经写过。
        # 推进而不是清空，所以今天 23 点之后聊的那几句会接着进明天那一篇。
        try:
            await asyncio.to_thread(self._db.material_set_cursor, last_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺推进素材游标失败: {exc}")
        try:
            self._db.state_set(
                "auto_write",
                {
                    "date": local_today(),
                    "note_id": note["id"],
                    "chars": len(body),
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺记录自动写日记状态失败: {exc}")
        logger.info(
            f"璃笺自动写日记完成：{note['title']}"
            f"（用了 {count} 条素材、{len(digest)} 字，写成 {len(body)} 字）"
        )
        return {"ok": True, "message": "写好了", "note": note, "chars": len(body)}

    # ---------- 自动备份 ----------

    def _backup_enabled(self) -> bool:
        try:
            return bool(self.config.get("backup_enabled", True))
        except Exception:  # noqa: BLE001
            return True

    def _backup_dir(self) -> Path:
        return self._data_dir / BACKUP_DIR_NAME

    def _backup_keep(self) -> int:
        return BACKUP_KEEP_DEFAULT

    def _backup_hour(self) -> int:
        return BACKUP_HOUR_DEFAULT

    def _backup_files(self) -> list[Path]:
        directory = self._backup_dir()
        try:
            return sorted(
                directory.glob("lijian-*.json.gz"),
                key=lambda path: path.name,
                reverse=True,
            )
        except OSError:
            return []

    def _prune_backups(self) -> int:
        """只留最近 keep 份。多删了没法找回，所以按文件名排序，不碰不认识的。"""
        keep = self._backup_keep()
        removed = 0
        for path in self._backup_files()[keep:]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def _run_backup(self) -> dict[str, Any]:
        """立刻写一份全量备份。压缩后落盘，写完立即收紧权限。"""
        db = self._require_db()
        directory = self._backup_dir()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        payload = db.export_json()
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = directory / f"lijian-{stamp}.json.gz"
        # 同一秒里连按两次「立即备份」不能互相覆盖，加个序号。
        counter = 1
        while path.exists():
            counter += 1
            path = directory / f"lijian-{stamp}-{counter}.json.gz"
        tmp = path.with_suffix(".tmp")
        try:
            with gzip.open(tmp, "wb") as handle:
                handle.write(raw)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        pruned = self._prune_backups()
        info = {
            "file": path.name,
            "count": payload.get("count", 0),
            "bytes": path.stat().st_size if path.exists() else 0,
            "pruned": pruned,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            db.state_set("last_backup", {"date": local_today(), **info})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺备份状态写入失败: {exc}")
        return info

    def _backup_due(self) -> bool:
        if not self._backup_enabled() or self._db is None:
            return False
        last = self._db.state_get("last_backup", {}) or {}
        last_date = str(last.get("date", "")) if isinstance(last, dict) else ""
        if last_date == local_today():
            return False
        return datetime.now().hour >= self._backup_hour()

    async def _backup_loop(self) -> None:
        """半小时看一次：今天还没备份、且已过设定的点钟，就补一份。"""
        while True:
            try:
                if self._backup_due():
                    info = await asyncio.to_thread(self._run_backup)
                    logger.info(
                        f"璃笺自动备份完成: {info['file']}（{info['count']} 篇，"
                        f"{format_size(info['bytes'])}）"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"璃笺自动备份失败: {exc}")
            await asyncio.sleep(1800)

    def _start_backup_service(self) -> None:
        if not self._backup_enabled():
            logger.info("璃笺自动备份未启用（backup_enabled=false）")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # 没有事件循环的场合（脚本里直接 new 插件）就退化成同步补一次。
            try:
                if self._backup_due():
                    self._run_backup()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"璃笺自动备份失败: {exc}")
            return
        self._backup_task = loop.create_task(self._backup_loop())

    def _stop_backup_service(self) -> None:
        if self._backup_task is not None:
            self._backup_task.cancel()
            self._backup_task = None

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
        register(f"/{PLUGIN_NAME}/on-this-day", self.api_on_this_day, ["GET"], "书架：那年今天")
        register(f"/{PLUGIN_NAME}/backups", self.api_list_backups, ["GET"], "书架：备份列表")
        register(f"/{PLUGIN_NAME}/backup", self.api_backup_now, ["POST"], "书架：立即备份")
        register(f"/{PLUGIN_NAME}/import/backup", self.api_import_backup, ["POST"], "书架：从备份文件恢复")
        register(f"/{PLUGIN_NAME}/weekly", self.api_weekly, ["GET"], "书架：周报素材")
        register(f"/{PLUGIN_NAME}/signature", self.api_signature, ["GET"], "书架：内容签名（轮询用）")
        register(f"/{PLUGIN_NAME}/media", self.api_upload_media, ["POST"], "书架：上传图片")
        register(f"/{PLUGIN_NAME}/media/<name>", self.api_get_media, ["GET"], "书架：读取图片")
        register(f"/{PLUGIN_NAME}/import", self.api_import, ["POST"], "书架：导入 Markdown")
        register(f"/{PLUGIN_NAME}/stats", self.api_stats, ["GET"], "书架：统计")
        register(f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "书架：运行状态")

    async def terminate(self):
        self._stop_backup_service()
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

    @filter.command("日记状态", alias={"lijian_status"})
    async def lijian_status(self, event: AstrMessageEvent):
        """看看日记本现在怎么样。"""
        if self._db is None:
            yield event.plain_result("日记本打不开了，检查一下 AstrBot 的数据目录能不能写。")
            return
        try:
            stats = self._db.stats()
        except Exception as exc:  # noqa: BLE001
            stats = {"notes": 0, "words": 0, "archived": 0}
            logger.warning(f"璃笺统计失败: {exc}")
        try:
            material_count = self._db.material_pending()
        except Exception:  # noqa: BLE001
            material_count = 0

        lines = [
            f"璃笺状态：正常 · 一共 {stats.get('notes', 0)} 篇，"
            f"{stats.get('words', 0)} 字，归档 {stats.get('archived', 0)} 篇",
            "",
        ]
        # 自动写日记：说人话，告诉她今晚会不会写、现在还差什么
        if not self._auto_write_enabled():
            lines.append("自己写日记：关了")
        elif self._auto_write_done_today():
            last = self._db.state_get("auto_write", {}) or {}
            lines.append(
                f"自己写日记：今天已经写好了"
                f"（{last.get('at', '')}，{last.get('chars', 0)} 字）"
            )
        else:
            hour, minute = self._auto_write_at()
            at = self._hhmm(hour, minute)
            if material_count > 0:
                lines.append(
                    f"自己写日记：定在 {at}，还有 {material_count} 条聊天没写进去"
                )
            else:
                gap = self._days_since_last_diary()
                if gap < 0:
                    lines.append(f"自己写日记：定在 {at}，还没写过日记，先不动笔")
                elif gap >= AUTO_IDLE_GAP_DAYS:
                    lines.append(f"自己写日记：定在 {at}，今天没聊，会写一篇安静的")
                else:
                    lines.append(f"自己写日记：定在 {at}，今天没聊，先不写")
        if self._backup_enabled():
            lines.append(f"备份：每天一份，留最近 {BACKUP_KEEP_DEFAULT} 份")
        else:
            lines.append("备份：关")

        # 出问题了才说，正常的时候不占地方
        if not self._bridge_running():
            lines.append("")
            lines.append("本地写入接口没起来，可能是端口被上一个实例占着——不影响写日记，重启一次 AstrBot 就好。")

        lines.append("")
        lines.append("看书架：AstrBot 网页 → 插件 → 璃笺 → 页面")
        yield event.plain_result("\n".join(lines))

    @filter.command("写日记", alias={"lijian_write"})
    async def lijian_write(self, event: AstrMessageEvent, content: str = ""):
        """让璃笺现在写一篇今天的日记。

        不带内容：自己取聊天素材，让模型按人格写一篇（跟到点自动写是同一条路）。
        带上文字：把你给的这段原样存成一篇。
        """
        text = (content or "").strip()
        if text:
            # 指令参数是按空格切开的，长一点的话只会拿到第一个词。
            # 既然都写了内容，多半是整段话，改从原始消息里把指令后面全截下来。
            raw = str(event.get_message_str() or "").strip()
            for cmd_name in ("写日记", "lijian_write"):
                pos = raw.find(cmd_name)
                if pos >= 0:
                    tail = raw[pos + len(cmd_name) :].strip()
                    if tail:
                        text = tail
                        break
        if not text:
            result = await self._compose_diary(manual=True)
            if not result.get("ok"):
                yield event.plain_result(
                    f"这篇没写成：{result.get('message') or '未知原因'}"
                )
                return
            note = result.get("note") or {}
            yield event.plain_result(
                f"写好了：《{note.get('title') or '日记'}》，{result.get('chars') or 0} 字"
            )
            return
        if self._db is None:
            yield event.plain_result("璃笺数据库不可用。")
            return
        try:
            note = self._db.write_diary_from_ai(
                {
                    "content_md": text,
                    "title": None,
                    "tags": ["日记"],
                    "mode": self.config.get("default_mode", "append_today"),
                }
            )
        except Exception as exc:
            yield event.plain_result(f"璃笺写入失败：{exc}")
            return
        yield event.plain_result(
            f"已写入璃笺：{note['title']}（{note.get('diary_date') or '无日期'}）"
        )

    @filter.command("日记列表", alias={"lijian_list"})
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

    @filter.command("读日记", alias={"lijian_read"})
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

    @filter.command("删日记", alias={"lijian_delete"})
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
        """翻自己的日记本，看最近写了哪几篇。返回标题、日期、摘要、标签，不含全文。

        什么时候必须调它：对方问「你写日记了吗」「今天写了什么」「你都有什么记录」
        「你还记着些什么」这类问题的时候，先调这个看一眼再回答。
        你自己的日记是自动写的，你不调工具就看不见，凭印象答「我没写过」是错的——
        日记本里其实一直有东西。

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

    @filter.llm_tool(name="search_lijian_notes")
    async def search_lijian_notes(
        self,
        event: AstrMessageEvent,
        keyword: str,
        since: str = "",
        until: str = "",
        limit: int = 8,
    ):
        """在自己写过的日记和便签里翻找。想回忆某件事、某个人、某段时间发生过什么时用它。

        对方问「你还记得吗」「我们之前是不是说过」「上次那个事」的时候，
        先调这个搜一遍再回答，别直接说想不起来——你每晚都写过日记，里面大概率有。

        只拿到摘要和命中处的一小段原文；想读全文再用 read_lijian_note 按 id 取。

        Args:
            keyword(string): 要找的词，中文两个字也命中。留空表示不限关键词。
            since(string): 起始日期，格式 YYYY-MM-DD，可为空表示不限。
            until(string): 截止日期，格式 YYYY-MM-DD，可为空表示不限。
            limit(int): 最多返回几条，默认 8，上限 50。
        """
        if self._db is None:
            return self._tool_error("璃笺检索失败", RuntimeError("数据库不可用"))
        try:
            limit = max(1, min(int(limit or 8), 50))
        except (TypeError, ValueError):
            limit = 8

        def _norm_date(value: str) -> str:
            text = str(value or "").strip()[:10].replace("/", "-")
            if not text:
                return ""
            try:
                datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return ""
            return text

        since_day = _norm_date(since)
        until_day = _norm_date(until)
        try:
            if str(keyword or "").strip():
                notes = await asyncio.to_thread(
                    self._db.search_notes, str(keyword).strip(), False, 500
                )
            else:
                notes = await asyncio.to_thread(self._db.list_notes, False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺检索失败: {exc}")
            return self._tool_error("璃笺检索失败", exc)

        hits: list[dict[str, Any]] = []
        needle = str(keyword or "").strip().lower()
        for note in notes:
            stamp = str(note.get("diary_date") or note.get("updated_at") or "")[:10]
            if since_day and stamp and stamp < since_day:
                continue
            if until_day and stamp and stamp > until_day:
                continue
            hits.append(note)
            if len(hits) >= limit:
                break

        def _snippet(body: str) -> str:
            text = " ".join(str(body or "").split())
            if not needle:
                return text[:80] + ("…" if len(text) > 80 else "")
            pos = text.lower().find(needle)
            if pos < 0:
                return text[:80] + ("…" if len(text) > 80 else "")
            start = max(0, pos - 30)
            end = min(len(text), pos + len(needle) + 50)
            piece = text[start:end]
            return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")

        return json.dumps(
            {
                "ok": True,
                "count": len(hits),
                "range": {"since": since_day or "不限", "until": until_day or "不限"},
                "notes": [
                    {
                        "id": note["id"],
                        "title": note["title"],
                        "kind": note["kind"],
                        "diary_date": note.get("diary_date"),
                        "tags": note["tags"],
                        "snippet": _snippet(note.get("body_md", "")),
                    }
                    for note in hits
                ],
            },
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
        matched = None
        if q:
            notes = self._db.search_notes(q, include_archived)
            # 命中数超过一页时，告诉前端一共多少，别让人以为只剩这些。
            if len(notes) >= SEARCH_LIMIT:
                matched = self._db.search_count(q, include_archived)
        else:
            notes = self._db.list_notes(include_archived, light=light)
        if kind in ("diary", "note"):
            notes = [note for note in notes if note["kind"] == kind]
        payload = {"notes": [self._summary(note, include_body) for note in notes]}
        if q:
            payload["matched"] = matched if matched is not None else len(notes)
        return json_response(payload)

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

    async def api_on_this_day(self):
        """往年的今天。只回摘要，正文等点开再取。"""
        if self._db is None:
            return error_response("数据库不可用")
        day = (request.query.get("date") or local_today()).strip()
        if not DATE_RE.match(day):
            return error_response("日期格式应为 YYYY-MM-DD")
        try:
            notes = self._db.on_this_day(day, limit=10)
        except Exception as exc:
            return error_response(str(exc))
        items = []
        for note in notes:
            summary = self._summary(note)
            summary["years_ago"] = note.get("years_ago")
            summary["found_date"] = note.get("found_date")
            summary["excerpt"] = excerpt_text(note.get("body_md") or "", 80)
            items.append(summary)
        return json_response({"date": day, "count": len(items), "items": items})

    async def api_list_backups(self):
        if self._db is None:
            return error_response("数据库不可用")
        items = []
        for path in self._backup_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                }
            )
        return json_response(
            {
                "dir": str(self._backup_dir()),
                "keep": self._backup_keep(),
                "enabled": self._backup_enabled(),
                "last": self._db.state_get("last_backup", {}),
                "items": items,
            }
        )

    async def api_backup_now(self):
        if self._db is None:
            return error_response("数据库不可用")
        try:
            info = await asyncio.to_thread(self._run_backup)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"璃笺手动备份失败: {exc}")
            return error_response(f"备份失败：{exc}")
        return json_response({"ok": True, "backup": info})

    async def api_import_backup(self):
        """从 .json 或 .json.gz 备份恢复。

        前端不用解压，也不受 JSON 请求体大小限制；临时文件放大盘上，
        这台机器的 /tmp 只有 10M，备份一旦超过就直接写不进去。
        """
        if self._db is None:
            return error_response("数据库不可用")
        files = await request.files()
        upload = files.get("file") if files else None
        if upload is None:
            return error_response("缺少上传文件（字段名 file）")
        tmp = self._data_dir / f".import-{uuid4().hex}.tmp"
        try:
            from astrbot.api.web import PluginUploadFile

            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp.touch(mode=0o600, exist_ok=True)
            secure_temp_file(tmp)
            if isinstance(upload, PluginUploadFile):
                await upload.save(tmp)
            else:
                tmp.write_bytes(upload.file.read())
            secure_temp_file(tmp)
            size = tmp.stat().st_size
            if size > MAX_BACKUP_BYTES:
                return error_response(
                    f"备份文件太大（{size // 1024 // 1024} MB），上限 "
                    f"{MAX_BACKUP_BYTES // 1024 // 1024} MB"
                )
            raw = tmp.read_bytes()
            if raw[:2] == GZIP_MAGIC:
                try:
                    raw = gzip.decompress(raw)
                except OSError as exc:
                    return error_response(f"压缩包解不开：{exc}")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return error_response("不是合法的 JSON 备份")
            result = self._db.import_json(payload)
        except Exception as exc:  # noqa: BLE001
            return error_response(str(exc))
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return json_response(
            {
                **result,
                "message": f"新建 {result['created']}，覆盖 {result['updated']}，跳过 {result['skipped']}",
            }
        )

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
                "backup": {
                    "enabled": self._backup_enabled(),
                    "dir": str(self._backup_dir()),
                    "keep": self._backup_keep(),
                    "files": len(self._backup_files()),
                    "last": self._db.state_get("last_backup", {}),
                },
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


def excerpt_text(markdown: str, limit: int = 80) -> str:
    """列表和横幅上用的纯文本摘要：去掉标题、图片、链接和标记。"""
    text = str(markdown or "")
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # 标题行整行去掉：横幅上已经有标题了，再来一遍只是重复。
    text = re.sub(r"^\s{0,3}#{1,6}\s*.*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"[*_>`~]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def format_size(num: int) -> str:
    """小文件也别显示成 0 KB，看着像备份是空的。"""
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / (1024 * 1024):.2f} MB"


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
