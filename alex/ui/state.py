"""窗口 UI 状态的存取（记住上次的调试面板开关等）。"""

from __future__ import annotations

import json

from alex.ui.constants import UI_STATE_PATH


def _load_ui_state() -> dict:
    """TUI 自己的偏好（主题等），与 config.json 分开，避免被 daemon 回写覆盖。"""
    try:
        data = json.loads(UI_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_ui_state(**updates) -> None:
    data = _load_ui_state()
    data.update(updates)
    try:
        UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_STATE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass
