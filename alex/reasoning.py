"""推理强度：按 provider 生成档位，并解析成模型请求参数。

沿用 opencode 的 variant 思路：每个模型一组具名档位，配置可覆盖默认档位。
档位最终落到 AgentScope 模型的 ``Parameters`` 字段上：

- deepseek：``thinking_enable`` + ``reasoning_effort``（high / max）
- openai：``reasoning_effort``（none / minimal / low / medium / high / xhigh）
- custom（OpenAI 兼容网关）：默认 low / medium / high，可在 profile 里覆盖
"""

from __future__ import annotations

from typing import Any

DEEPSEEK_LEVELS: dict[str, dict[str, Any]] = {
    "off": {"thinking_enable": False},
    "high": {"thinking_enable": True, "reasoning_effort": "high"},
    "max": {"thinking_enable": True, "reasoning_effort": "max"},
}

OPENAI_LEVELS: dict[str, dict[str, Any]] = {
    "none": {"thinking_enable": False},
    "minimal": {"reasoning_effort": "minimal"},
    "low": {"reasoning_effort": "low"},
    "medium": {"reasoning_effort": "medium"},
    "high": {"reasoning_effort": "high"},
    "xhigh": {"reasoning_effort": "xhigh"},
}

CUSTOM_LEVELS: dict[str, dict[str, Any]] = {
    key: OPENAI_LEVELS[key] for key in ("low", "medium", "high")
}

PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "deepseek": DEEPSEEK_LEVELS,
    "openai": OPENAI_LEVELS,
    "custom": CUSTOM_LEVELS,
    "openai-compatible": CUSTOM_LEVELS,
}

DEFAULT_LEVEL: dict[str, str] = {
    "deepseek": "high",
    "openai": "medium",
    "custom": "medium",
    "openai-compatible": "medium",
}


def provider_of(cfg: dict[str, Any]) -> str:
    """档位按 canonical 归属；公司网关可用它声明“其实是 OpenAI”。"""
    return str(
        cfg.get("canonical") or cfg.get("provider", "deepseek")
    ).lower()


def _setting(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """读取 reasoning 配置；False / 缺失表示该模型不支持档位。"""
    raw = cfg.get("reasoning")
    if raw is True:
        return {}
    if isinstance(raw, dict):
        return raw
    return None


def levels_of(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """该模型可用的档位；配置里写了 levels 就覆盖预设。"""
    setting = _setting(cfg)
    if setting is None:
        return {}
    preset = PRESETS.get(provider_of(cfg), {})
    custom = setting.get("levels")
    if isinstance(custom, list):
        return {
            str(name): dict(preset[str(name)])
            for name in custom
            if str(name) in preset
        }
    if isinstance(custom, dict):
        return {str(name): dict(body or {}) for name, body in custom.items()}
    return dict(preset)


def default_level(cfg: dict[str, Any]) -> str | None:
    levels = levels_of(cfg)
    if not levels:
        return None
    wanted = (_setting(cfg) or {}).get("default") or DEFAULT_LEVEL.get(
        provider_of(cfg)
    )
    if wanted in levels:
        return str(wanted)
    return next(iter(levels))


def current_level(cfg: dict[str, Any]) -> str | None:
    """配置里的当前档位，无效时退回默认档位。"""
    levels = levels_of(cfg)
    if not levels:
        return None
    saved = (_setting(cfg) or {}).get("current")
    if saved in levels:
        return str(saved)
    return default_level(cfg)


def resolve_level(
    cfg: dict[str, Any], preferred: str | None = None
) -> str | None:
    levels = levels_of(cfg)
    if not levels:
        return None
    if preferred in levels:
        return str(preferred)
    return current_level(cfg)


def request_parameters(cfg: dict[str, Any]) -> dict[str, Any]:
    """当前档位对应的模型参数，交给 ``Parameters(...)``。"""
    level = current_level(cfg)
    if level is None:
        return {}
    return dict(levels_of(cfg)[level])
