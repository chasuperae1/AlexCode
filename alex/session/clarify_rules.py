"""Goal 澄清的本地启发式判定：纯规则、零 token、可单测。

只决定“要不要向模型要澄清问题”，不负责出题；出题仍由一次结构化调用完成。
规则保持保守：宁可少问，也不要对已经说清楚的目标反复盘问。
"""

from __future__ import annotations

import re
import unicodedata

# 模糊措辞：出现这些词，说明判定标准没交代清楚
VAGUE = (
    "适合我",
    "适合我的",
    "好一点",
    "好点",
    "随便",
    "都行",
    "都可以",
    "看着办",
    "帮我看看",
    "差不多",
    "尽量",
    "之类的",
    "帮我挑",
    "帮我选",
)

# 至少要说清“做什么”的对象域；命中任一即认为对象明确
DOMAIN_WORDS = (
    "工作",
    "岗位",
    "职位",
    "简历",
    "面试",
    "文件",
    "代码",
    "脚本",
    "程序",
    "网页",
    "页面",
    "接口",
    "bug",
    "配置",
    "报告",
    "论文",
    "表格",
    "文档",
    "计划",
    "方案",
    "视频",
    "图片",
    "邮件",
    "说明",
    "调研",
    "分析",
    "整理",
    "翻译",
)

FILE_RE = re.compile(r"\w+\.(py|js|ts|tsx|json|md|txt|yaml|yml|toml|ini|html|css|bat|sh)\b", re.I)

MIN_CHARS = 12


def normalize(text: str) -> str:
    """全角转半角、压缩空白，避免全角字符把长度与词表匹配带偏。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(value.split())


def needs_clarification(text: str, min_chars: int = MIN_CHARS) -> bool:
    """目标是否缺关键信息，需要出选择题。"""
    value = normalize(text)
    if not value:
        return False
    if len(value) < min_chars:
        return True
    lowered = value.lower()
    if any(word in lowered for word in VAGUE):
        return True
    if FILE_RE.search(value):
        return False
    if any(word in lowered for word in DOMAIN_WORDS):
        return False
    return True
