"""桌面提示音：AI 停下来、等审批、等回答时响一声。

用 Windows 自带的 winsound 播 WAV：没有第三方依赖，也不接管音频设备。
播放放在后台线程里，重复次数之间留一点空隙，TUI 不会被卡住；
非 Windows 平台静默跳过。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

DEFAULT_SOUND = "assets/sounds/06_chord_soft.wav"
DEFAULT_COOLDOWN = 2.0
EVENTS = ("reply_end", "permission", "ask")

# 每个事件各自记上次响的时间：刚答完一轮又要审批，两次都得提醒
_last_played: dict[str, float] = {}
_lock = threading.Lock()


def settings() -> dict:
    from alex.config import CONFIG

    return CONFIG.get("notify") or {}


def enabled_for(event: str) -> bool:
    """这个事件现在该不该响。"""
    if event not in EVENTS:
        return False
    config = settings()
    if not config.get("enabled", True):
        return False
    return bool(config.get(event, True))


def resolve(target: str | Path | None) -> Path | None:
    """相对路径按仓库根解析；找不到文件就不响。"""
    if not target:
        return None
    path = Path(target).expanduser()
    if not path.is_absolute():
        from alex.config import ROOT

        path = ROOT / path
    return path if path.is_file() else None


def _play(path: Path, repeat: int, gap: float) -> None:
    try:
        import winsound
    except Exception:  # 非 Windows 或 Winsound 不可用
        return
    flags = winsound.SND_FILENAME | winsound.SND_NODEFAULT
    for index in range(max(1, repeat)):
        try:
            winsound.PlaySound(str(path), flags)
        except Exception:
            return
        if index + 1 < max(1, repeat):
            time.sleep(max(0.0, gap))


def chime(event: str, *, repeat: int | None = None) -> bool:
    """按事件响一声；返回是否真的安排了播放（冷却期内返回 False）。"""
    if sys.platform != "win32" or not enabled_for(event):
        return False
    config = settings()
    path = resolve(config.get("sound") or DEFAULT_SOUND)
    if path is None:
        return False
    cooldown = float(config.get("cooldown", DEFAULT_COOLDOWN) or 0.0)
    now = time.monotonic()
    with _lock:
        last = _last_played.get(event)
        if last is not None and cooldown > 0 and now - last < cooldown:
            return False
        _last_played[event] = now
    times = int(repeat if repeat is not None else config.get("repeat", 1) or 1)
    gap = float(config.get("gap", 0.12) or 0.0)
    threading.Thread(
        target=_play,
        args=(path, times, gap),
        name=f"chime-{event}",
        daemon=True,
    ).start()
    return True


def reset() -> None:
    """清掉冷却记录（测试用）。"""
    with _lock:
        _last_played.clear()


def set_enabled(value: bool, *, path: Path | None = None) -> None:
    """写回 config.json 的 notify.enabled，并同步内存里的 CONFIG。

    只改这一个字段：先把磁盘上的版本读出来再改，不拿进程内的 CONFIG 覆盖全量，
    免得把别处（比如 daemon 正在写的 permission_mode）冲掉。
    """
    import json

    from alex.config import CONFIG, CONFIG_PATH

    target = Path(path) if path is not None else CONFIG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    notify = raw.get("notify")
    if not isinstance(notify, dict):
        notify = {}
        raw["notify"] = notify
    notify["enabled"] = bool(value)
    target.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    memory = CONFIG.get("notify")
    if not isinstance(memory, dict):
        memory = {}
        CONFIG["notify"] = memory
    memory["enabled"] = bool(value)


def _main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else "reply_end"
    print(f"{event}: {'scheduled' if chime(event) else 'skipped'}")
    time.sleep(2.0)
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动试听入口
    raise SystemExit(_main(sys.argv))
