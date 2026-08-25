"""SKILL.md 相关工具函数。

集中 SKILL.md 的解析逻辑，供 import / translate 等脚本复用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def read_frontmatter(skill_md: Path) -> dict[str, Any] | None:
    """解析 SKILL.md 的 YAML frontmatter；缺失或无法解析返回 None。

    SKILL.md 规范要求文件以 ``---`` 开头，紧跟 YAML frontmatter。
    返回 dict；无 frontmatter / 不是 dict / YAML 解析失败 / 文件缺失均返回 None。
    """
    if not skill_md.exists():
        return None
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        data = yaml.safe_load(text[3:end]) or {}
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def read_description(skill_md: Path) -> str:
    """从 SKILL.md frontmatter 读取英文 description；无则返回空串。"""
    fm = read_frontmatter(skill_md)
    if fm is None:
        return ""
    desc = fm.get("description")
    return desc.strip() if isinstance(desc, str) else ""
