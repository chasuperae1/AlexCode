"""项目路径与配置读写。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"

# 内置只读技能：始终启用、设置页不可关闭，源文件标记为只读并拦截写入
LOCKED_SKILLS = frozenset({"alexcode"})


def _read_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# 原地更新，保证其他模块 import 到的引用在 /reload 后仍然有效
CONFIG = _read_config()


def reload_config() -> None:
    """重新读取 config.json。"""
    CONFIG.clear()
    CONFIG.update(_read_config())


def save_config() -> None:
    """把内存中的配置写回 config.json。"""
    CONFIG_PATH.write_text(
        json.dumps(CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def project_path(key: str) -> Path:
    """把配置里的相对路径解析成项目内的绝对路径。"""
    return ROOT / CONFIG[key]


def workspace_path(*, create: bool = False) -> Path:
    """当前工作区；未设置时默认使用项目根目录下的 agent_workspace。"""
    raw = CONFIG.get("workdir") or "agent_workspace"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def locked_skill_dirs() -> list[Path]:
    """内置只读技能的目录（读/写守卫用）。"""
    return [project_path("skills_dir") / name for name in LOCKED_SKILLS]


def lock_skill_files() -> None:
    """把内置技能的源文件标记为只读。

    Windows 上是只读属性，类 Unix 上是去掉写权限位；已锁定时不重复设置。
    """
    for folder in locked_skill_dirs():
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            try:
                os.chmod(path, stat.S_IREAD)
            except OSError:
                pass


def load_rules() -> str:
    """读取规则：rules/*.md（排除已关闭的）+ 当前目录 AGENTS.md。"""
    parts: list[str] = []
    excluded = set(CONFIG.get("excluded_rules", []))
    rules_dir = project_path("rules_dir")
    if rules_dir.is_dir():
        for path in sorted(rules_dir.glob("*.md")):
            if path.name in excluded:
                continue
            parts.append(path.read_text(encoding="utf-8").strip())
    for name in CONFIG["extra_rules"]:
        path = Path.cwd() / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)
