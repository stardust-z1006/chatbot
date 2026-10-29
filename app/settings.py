# -*- coding: utf-8 -*-
"""配置加载。

分工很明确：
  - config.yaml  → 行为（人设、开关、阈值），带注释，给人看
  - data/override.yaml → 后台面板改过的值，优先级更高，删掉即恢复
  - .env         → 密钥（不进版本库）
  - 环境变量     → 临时覆盖（优先级最高）

老项目把两百多行大写常量写在 config.py 里、密钥硬编码进去，改一个开关要
翻文件；这里换成 yaml + .env 两层。

为什么不直接把面板改的值写回 config.yaml：那份文件里全是解释「为什么这么定」
的注释，PyYAML 一回写就全没了。覆盖层只存被改过的键，config.yaml 一个字不动，
「恢复默认」就是从这个文件里删掉一个键，可逆。
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

_MISSING = object()
OVERRIDE_HEADER = (
    "# 后台面板改过的参数。优先级高于 config.yaml，删掉本文件即全部恢复默认。\n"
    "# 手改也行，格式就是普通的嵌套 yaml。\n"
)


def _read_env_file(path: Path) -> None:
    """极简 .env 解析，省掉一个 python-dotenv 依赖。"""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


class Section(dict):
    """允许 cfg.llm.model 这种点号访问的 dict 包装。"""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        if isinstance(value, dict) and not isinstance(value, Section):
            value = Section(value)
            self[name] = value
        return value


class Settings:
    def __init__(
        self,
        data: Dict[str, Any],
        root: Path,
        base: Optional[Dict[str, Any]] = None,
        override_path: Optional[Path] = None,
    ):
        self._data = data
        # base 是 config.yaml 的原样，用来算「默认值」和「恢复默认」
        self._base = copy.deepcopy(base) if base is not None else copy.deepcopy(data)
        self.root = root
        self.override_path = override_path
        self.overrides: Dict[str, Any] = {}
        # 最近一次落盘失败的原因。空字符串 = 一切正常。
        # 面板靠它区分「真的改了」和「只改了内存」，别再拿 print 糊过去。
        self.save_error = ""

    # -- 读取 ---------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def default(self, path: str) -> Any:
        """config.yaml 里的原值（不含覆盖）。面板上显示「默认 xx」用。"""
        node: Any = self._base
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return copy.deepcopy(node)

    def section(self, name: str) -> Section:
        value = self._data.get(name) or {}
        return Section(value) if isinstance(value, dict) else Section()

    def rel(self, rel_path: str) -> Path:
        path = Path(rel_path)
        return path if path.is_absolute() else (self.root / path)

    # -- 写入（后台面板用）--------------------------------------------
    def set_override(self, path: str, value: Any) -> None:
        """改一个参数：内存里立刻生效，同时落盘到 override.yaml。"""
        _set_path(self._data, path, value)
        self.overrides[path] = value
        self._save_overrides()

    def clear_override(self, path: str) -> None:
        """恢复这一个参数的默认值。"""
        base_value = _get_path(self._base, path, _MISSING)
        if base_value is _MISSING:
            _del_path(self._data, path)
        else:
            _set_path(self._data, path, copy.deepcopy(base_value))
        self.overrides.pop(path, None)
        self._save_overrides()

    def clear_all_overrides(self) -> None:
        for path in list(self.overrides):
            self.clear_override(path)

    def is_overridden(self, path: str) -> bool:
        return path in self.overrides

    def _save_overrides(self) -> None:
        if self.override_path is None:
            return
        if not self.overrides:
            # 全恢复默认了就把文件删掉，别留一个空壳让人猜
            try:
                self.override_path.unlink()
            except OSError:
                pass
            self.save_error = ""
            return
        tree: Dict[str, Any] = {}
        for path, value in self.overrides.items():
            _set_path(tree, path, value)
        try:
            import yaml

            text = OVERRIDE_HEADER + yaml.safe_dump(
                tree, allow_unicode=True, sort_keys=True, default_flow_style=False
            )
            self.override_path.parent.mkdir(parents=True, exist_ok=True)
            self.override_path.write_text(text, encoding="utf-8")
            self.save_error = ""
        except Exception as exc:  # noqa: BLE001
            # 内存里已经改了，磁盘上没改 —— 重启就会回到旧值。
            # 这种事必须让调用方（面板）知道，否则用户以为改成功了。
            self.save_error = f"{type(exc).__name__}: {exc}"
            logger.error("覆盖配置写不进去（%s）：%s", self.override_path, exc)

    # -- 常用路径 -----------------------------------------------------
    @property
    def data_dir(self) -> Path:
        path = self.rel(self.get("store.dir", "data"))
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def prompt_dir(self) -> Path:
        path = self.rel("prompts")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.sqlite3"

    # -- 密钥 ---------------------------------------------------------
    @property
    def api_key(self) -> str:
        return (
            os.environ.get("DEEPSEEK_API_KEY")
            or self.get("llm.api_key", "")
            or ""
        ).strip()

    @property
    def base_url(self) -> str:
        return (
            os.environ.get("DEEPSEEK_BASE_URL")
            or self.get("llm.base_url", "https://api.deepseek.com")
        ).rstrip("/")


# ------------------------------------------------------------------ 路径工具
def _set_path(tree: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _get_path(tree: Dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _del_path(tree: Dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = tree
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _flatten(tree: Dict[str, Any], prefix: str = "", out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """嵌套 dict → {"a.b.c": 值}，给面板对照用。"""
    out = {} if out is None else out
    for key, value in (tree or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            _flatten(value, path, out)
        else:
            out[path] = value
    return out


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {path.name} 解析失败，忽略：{exc}")
        return {}


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> None:
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def load(root: Optional[Path] = None) -> Settings:
    root = root or ROOT
    _read_env_file(root / ".env")

    raw = _read_yaml(root / "config.yaml")
    base = copy.deepcopy(raw)   # yaml 写错了也让它带默认值起来

    store_rel = str(_get_path(raw, "store.dir", "data") or "data")
    store_dir = Path(store_rel)
    if not store_dir.is_absolute():
        store_dir = root / store_dir
    override_path = store_dir / "override.yaml"

    overrides = _read_yaml(override_path)
    _merge(raw, overrides)

    cfg = Settings(raw, root, base=base, override_path=override_path)
    cfg.overrides = _flatten(overrides)
    return cfg
