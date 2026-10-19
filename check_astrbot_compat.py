#!/usr/bin/env python3
"""在当前安装的 AstrBot 上实测本插件的接入点是否仍然兼容。

和 test_main.py 不一样：那里用 mock 模拟 AstrBot，验证的是插件自己的逻辑；
这里的每一条检查都跑在真实框架代码上，验证的是「插件和这个版本的 AstrBot 还对得上」。

用法（必须用 AstrBot 自己的解释器）：

    /home/baiwei/AstrBot/venv/bin/python check_astrbot_compat.py

全部通过退出码 0，任何一条不过退出码 1。升级 AstrBot 之后先跑这个，比等书架打不开再查省事。
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_NAME = "astrbot_plugin_lijian"


@contextlib.contextmanager
def isolated_cwd():
    """在临时目录里跑。

    AstrBot 的配置和数据路径是按 cwd 推断的，直接在插件目录里 import 它，
    会在仓库中建出 data/cmd_config.json、data/t2i_templates/ 这些运行时产物。
    """
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="lijian-compat-") as tmp:
        os.chdir(tmp)
        try:
            yield
        finally:
            os.chdir(previous)


def ensure_astrbot_importable() -> None:
    """AstrBot 常常是源码目录而不是装进 venv 的包，这里把根目录补进 sys.path。

    优先级：已经在路径里 → 环境变量 ASTRBOT_ROOT → 逐层往上找含 astrbot/__init__.py 的目录。
    """
    try:
        import astrbot  # noqa: F401

        return
    except ImportError:
        pass

    candidates: list[Path] = []
    env_root = os.environ.get("ASTRBOT_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.extend(PLUGIN_DIR.parents)

    for root in candidates:
        if (root / "astrbot" / "__init__.py").is_file():
            sys.path.insert(0, str(root))
            return

KNOWN_ROUTES = [
    ("GET", f"/{PLUGIN_NAME}/notes"),
    ("GET", f"/{PLUGIN_NAME}/notes/<note_id>"),
    ("POST", f"/{PLUGIN_NAME}/notes"),
    ("PATCH", f"/{PLUGIN_NAME}/notes/<note_id>"),
    ("DELETE", f"/{PLUGIN_NAME}/notes/<note_id>"),
    ("POST", f"/{PLUGIN_NAME}/notes/<note_id>/archive"),
    ("POST", f"/{PLUGIN_NAME}/notes/<note_id>/restore"),
    ("POST", f"/{PLUGIN_NAME}/notes/<note_id>/pin"),
    ("POST", f"/{PLUGIN_NAME}/notes/<note_id>/unpin"),
    ("GET", f"/{PLUGIN_NAME}/notes/<note_id>/markdown"),
    ("POST", f"/{PLUGIN_NAME}/diary"),
    ("GET", f"/{PLUGIN_NAME}/export/all"),
    ("GET", f"/{PLUGIN_NAME}/export/json"),
    ("POST", f"/{PLUGIN_NAME}/import/json"),
    ("POST", f"/{PLUGIN_NAME}/import"),
    ("GET", f"/{PLUGIN_NAME}/random"),
    ("GET", f"/{PLUGIN_NAME}/weekly"),
    ("GET", f"/{PLUGIN_NAME}/signature"),
    ("POST", f"/{PLUGIN_NAME}/media"),
    ("GET", f"/{PLUGIN_NAME}/media/<name>"),
    ("GET", f"/{PLUGIN_NAME}/stats"),
    ("GET", f"/{PLUGIN_NAME}/status"),
]

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def fatal(name: str, detail: str) -> None:
    check(False, name, detail)
    summary()
    sys.exit(1)


def summary() -> None:
    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    print()
    print(f"结果：{passed}/{total} 通过")
    failed = [name for ok, name, _ in results if not ok]
    if failed:
        print("未通过：" + "、".join(failed))


def main() -> int:
    # ---------- 1. 框架版本 ----------
    ensure_astrbot_importable()
    try:
        import astrbot
        from astrbot.core.config.default import DEFAULT_VALUE_MAP
    except ImportError as exc:
        fatal(
            "导入 astrbot",
            f"{exc}。用 AstrBot 的 venv 跑，或设 ASTRBOT_ROOT 指向 AstrBot 源码目录",
        )

    version = getattr(astrbot, "__version__", "未知")
    print(f"AstrBot 版本：{version}")
    print()

    # ---------- 2. 插件依赖的公开符号 ----------
    try:
        from astrbot.api.star import Context, Star, register  # noqa: F401
    except ImportError as exc:
        fatal("astrbot.api.star", str(exc))

    try:
        from astrbot.api.event import AstrMessageEvent, filter
        check(True, "astrbot.api.event")
        check(hasattr(filter, "command"), "filter.command")
        check(hasattr(filter, "llm_tool"), "filter.llm_tool")
    except ImportError as exc:
        fatal("astrbot.api.event", str(exc))

    try:
        import astrbot.api.web as web_api
        for symbol in ("json_response", "error_response", "request", "PluginUploadFile"):
            check(hasattr(web_api, symbol), f"web.{symbol}")
    except ImportError as exc:
        fatal("astrbot.api.web", str(exc))

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        check(callable(get_astrbot_plugin_data_path), "get_astrbot_plugin_data_path")
    except ImportError as exc:
        fatal("get_astrbot_plugin_data_path", str(exc))

    sig = inspect.signature(Context.register_web_api)
    params = list(sig.parameters)
    check(
        params[:5] == ["self", "route", "view_handler", "methods", "desc"],
        "register_web_api 签名",
        str(sig),
    )

    # ---------- 3. 加载插件模块 ----------
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    spec = importlib.util.spec_from_file_location("lijian_compat_probe", PLUGIN_DIR / "main.py")
    if spec is None or spec.loader is None:
        fatal("加载 main.py", "无法构建模块 spec")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        fatal("加载 main.py", f"{type(exc).__name__}: {exc}")
    check(True, "加载 main.py")

    plugin_cls = getattr(module, "LijianPlugin", None)
    if plugin_cls is None:
        fatal("找到 LijianPlugin", "main.py 里没有这个类")

    # ---------- 4. 路由能被框架匹配 ----------
    try:
        from astrbot.dashboard.api.plugins import _match_registered_web_api
    except ImportError:
        _match_registered_web_api = None  # 框架内部实现变动，降级自查

    class _RouteSink:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def register_web_api(self, route, handler, methods, desc):
            self.calls.append((route, handler, methods, desc))

    sink = _RouteSink()
    instance = object.__new__(plugin_cls)
    instance.context = sink
    try:
        instance._register_web_apis()
    except Exception as exc:  # noqa: BLE001
        fatal("注册 Web API", f"{type(exc).__name__}: {exc}")

    check(len(sink.calls) == len(KNOWN_ROUTES), "路由数量", f"{len(sink.calls)} 条")

    def _fallback_match(registered, subpath: str, method: str):
        """框架私有函数改名时的兜底：按 <name> 占位符自行匹配。"""
        request_path = f"/{subpath.lstrip('/')}"
        for route, handler, methods, _ in registered:
            if method.upper() not in [m.upper() for m in methods]:
                continue
            pattern = re.escape(route.strip())
            pattern = re.sub(r"\\<(?P<n>[A-Za-z_][A-Za-z0-9_]*)\\>", r"(?P<\g<n>>[^/]+)", pattern)
            matched = re.fullmatch(pattern, request_path)
            if matched:
                return handler, matched.groupdict()
        return None

    matcher = _match_registered_web_api or _fallback_match
    if _match_registered_web_api is None:
        check(False, "框架路由匹配函数", "私有实现已变动，已改用兜底匹配，请人工核对")

    for method, route in KNOWN_ROUTES:
        probe = re.sub(r"<[^>]+>", "abc12345", route).lstrip("/")
        try:
            found = matcher(sink.calls, probe, method)
        except Exception as exc:  # noqa: BLE001
            check(False, f"{method} {route}", f"匹配异常 {type(exc).__name__}: {exc}")
            continue
        if not found:
            check(False, f"{method} {route}", "框架匹配不到这条路由")
            continue
        handler, path_values = found
        try:
            inspect.signature(handler).bind(**path_values)
        except TypeError as exc:
            check(False, f"{method} {route}", f"handler 签名不接受 {path_values}：{exc}")
            continue
        check(True, f"{method} {route}", getattr(handler, "__name__", "?"))

    # ---------- 5. Pages 目录约定 ----------
    try:
        from astrbot.dashboard.services.plugin_page_service import (
            PLUGIN_PAGE_ENTRY_FILE_NAME,
            PLUGIN_PAGE_ROOT_DIR_NAMES,
        )

        page_dirs = [PLUGIN_DIR / name for name in PLUGIN_PAGE_ROOT_DIR_NAMES]
        found_root = next((p for p in page_dirs if p.is_dir()), None)
        check(found_root is not None, "Pages 根目录", str(PLUGIN_PAGE_ROOT_DIR_NAMES))
        if found_root is not None:
            pages = sorted(p.name for p in found_root.iterdir() if p.is_dir())
            check(bool(pages), "发现页面", "、".join(pages))
            for page in pages:
                entry = found_root / page / PLUGIN_PAGE_ENTRY_FILE_NAME
                check(entry.is_file(), f"页面入口 {page}", PLUGIN_PAGE_ENTRY_FILE_NAME)
    except ImportError as exc:
        check(False, "Pages 约定", f"取不到常量：{exc}")

    # ---------- 6. i18n ----------
    i18n_dir = PLUGIN_DIR / ".astrbot-plugin" / "i18n"
    locales = sorted(p.stem for p in i18n_dir.glob("*.json")) if i18n_dir.is_dir() else []
    check(bool(locales), "i18n 文件", "、".join(locales) or "无")
    for locale in locales:
        try:
            data = json.loads((i18n_dir / f"{locale}.json").read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            check(False, f"i18n {locale}", f"解析失败：{exc}")
            continue
        pages_block = data.get("pages") if isinstance(data, dict) else None
        check(
            isinstance(pages_block, dict) and "bookshelf" in pages_block,
            f"i18n {locale} pages.bookshelf",
            "缺这一项时页面标题会退化成 bookshelf",
        )

    # ---------- 7. 配置 schema ----------
    try:
        from astrbot.core.config.astrbot_config import AstrBotConfig

        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        unknown = sorted({meta.get("type") for meta in schema.values() if isinstance(meta, dict)} - set(DEFAULT_VALUE_MAP))
        check(not unknown, "schema 类型受支持", "、".join(unknown) or "全部在 DEFAULT_VALUE_MAP 内")
        config = AstrBotConfig(schema=schema)
        missing = [key for key in schema if key not in config]
        check(not missing, "schema 可加载", "、".join(missing) or f"{len(schema)} 项全部就位")
    except Exception as exc:  # noqa: BLE001
        check(False, "schema 可加载", f"{type(exc).__name__}: {exc}")

    # ---------- 8. metadata 一致性 ----------
    metadata_path = PLUGIN_DIR / "metadata.yaml"
    if metadata_path.is_file():
        text = metadata_path.read_text(encoding="utf-8")
        match = re.search(r"^version:\s*(.+)$", text, re.MULTILINE)
        declared = match.group(1).strip().lstrip("v") if match else None
        runtime = getattr(module, "PLUGIN_VERSION", None)
        check(
            declared == runtime,
            "版本号一致",
            f"metadata={declared} main={runtime}",
        )
    else:
        check(False, "metadata.yaml", "文件不存在")

    summary()
    return 0 if all(ok for ok, _, _ in results) else 1


if __name__ == "__main__":
    with isolated_cwd():
        sys.exit(main())
