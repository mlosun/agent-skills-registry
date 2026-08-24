"""registry 索引与元数据的唯一读写入口。

统一封装两类 YAML：

- ``index.yaml`` —— 仓库级装订页。每 skill 一行精简快照（id/name/version/risk/category），
  只服务"全局扫描与发现"（CLI 的 check/update/list 一次拉取即可比版本）。
- ``skill-meta.yaml`` —— skill 级详情。与源码同目录共置，变化只影响单个 skill。

所有脚本（import/sync/translate/security_scan）对这两个文件的读写都必须走本模块，
保证单点维护 schema 与持久化逻辑。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ---- 文件名 / 版本常量 ----
INDEX_FILENAME = "index.yaml"
META_FILENAME = "skill-meta.yaml"
INDEX_SCHEMA_VERSION = 1

# 合法 skill 名 / owner / repo / category 的字符集（写入路径前防御）
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------- 路径定位 ----------------


def repo_root() -> Path:
    """定位 registry 仓库根目录。

    本文件位于 ``<root>/scripts/lib/index.py``，向上两级即仓库根。
    """
    return Path(__file__).resolve().parents[2]


def index_path(root: Path | None = None) -> Path:
    """返回 ``index.yaml`` 的绝对路径。"""
    return (root or repo_root()) / INDEX_FILENAME


def skill_dir(root: Path, skill_id: str) -> Path:
    """由 skill id（``owner/repo/[category/]skill``）定位 ``skills/`` 下的目录。

    对 id 每一段做字符白名单校验，防止路径穿越。
    """
    segments = [seg for seg in skill_id.split("/") if seg]
    if (
        not segments
        or any(seg in (".", "..") for seg in segments)
        or any(not _SEGMENT_RE.match(seg) for seg in segments)
    ):
        raise ValueError(f"非法 skill id: {skill_id!r}")
    return root / "skills" / Path(*segments)


def meta_path(root: Path, skill_id: str) -> Path:
    """返回某 skill 的 ``skill-meta.yaml`` 绝对路径。"""
    return skill_dir(root, skill_id) / META_FILENAME


# ---------------- YAML 基础读写 ----------------


def load_yaml(path: Path) -> dict[str, Any] | None:
    """读取 YAML 文件，返回 dict；文件不存在或为空返回 None。"""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else None


def dump_yaml(path: Path, data: dict[str, Any], *, comment: str | None = None) -> None:
    """写出 YAML 文件。

    用 ``sort_keys=False`` 保持字段插入顺序（产出稳定、git diff 干净），
    可选在文件头写一行注释。自动创建父目录。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if comment:
        for line in comment.strip().splitlines():
            lines.append(f"# {line}")
        lines.append("")  # 注释块与正文之间空一行
    lines.append(
        yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False, default_flow_style=False
        ).rstrip("\n")
    )
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------- index.yaml（仓库级装订页） ----------------


def empty_index() -> dict[str, Any]:
    """构造一个空的 index 结构（含 schema 版本头）。"""
    return {
        "version": INDEX_SCHEMA_VERSION,
        "updated_at": utc_now_iso(),
        "skills": [],
    }


def load_index(root: Path | None = None) -> dict[str, Any]:
    """读取 index.yaml；不存在或格式异常时返回空结构（幂等，不抛错）。"""
    data = load_yaml(index_path(root))
    if data is None or "skills" not in data or not isinstance(data.get("skills"), list):
        return empty_index()
    # 兼容：schema 版本缺失时补默认值
    data.setdefault("version", INDEX_SCHEMA_VERSION)
    data.setdefault("updated_at", utc_now_iso())
    return data


def save_index(index: dict[str, Any], root: Path | None = None) -> Path:
    """写出 index.yaml，刷新 updated_at 并排序 skills（按 id 稳定排序）。"""
    index["version"] = INDEX_SCHEMA_VERSION
    index["updated_at"] = utc_now_iso()
    index["skills"] = sorted(index.get("skills", []), key=lambda r: r.get("id", ""))
    path = index_path(root)
    dump_yaml(
        path,
        index,
        comment=f"registry 装订页 · schema v{INDEX_SCHEMA_VERSION} · 由 scripts 自动维护，勿手改",
    )
    return path


def upsert_skill(index: dict[str, Any], row: dict[str, Any]) -> bool:
    """按 ``id`` 插入或更新一行 skill 快照。

    返回 True 表示新增，False 表示已存在并更新。
    """
    target = row.get("id")
    if not target:
        raise ValueError("skill row 缺少 id 字段")
    for i, existing in enumerate(index.get("skills", [])):
        if existing.get("id") == target:
            index["skills"][i] = row
            return False
    index.setdefault("skills", []).append(row)
    return True


def remove_skill(index: dict[str, Any], skill_id: str) -> bool:
    """按 id 删除一行 skill 快照，返回是否删除成功。"""
    before = len(index.get("skills", []))
    index["skills"] = [r for r in index.get("skills", []) if r.get("id") != skill_id]
    return len(index["skills"]) != before


# ---------------- skill-meta.yaml（skill 级详情） ----------------


def skill_id(meta: dict[str, Any]) -> str:
    """由 skill-meta 推导唯一 id = ``owner/repo/[category/]name``。"""
    src = meta["source"]
    parts = [src["owner"], src["repo"]]
    if src.get("category"):
        parts.append(src["category"])
    parts.append(meta["name"])
    return "/".join(parts)


def row_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """从 skill-meta 生成 index.yaml 里的一行精简快照。

    只取全局扫描需要的字段：id / name / version / risk / category。
    """
    src = meta.get("source", {})
    return {
        "id": skill_id(meta),
        "name": meta["name"],
        "version": meta.get("version", "0.0.0"),
        "risk": meta.get("risk", "clean"),
        "category": src.get("category", ""),
    }


def load_meta(root: Path, skill_id: str) -> dict[str, Any] | None:
    """读取某 skill 的 skill-meta.yaml。"""
    return load_yaml(meta_path(root, skill_id))


def save_meta(root: Path, skill_id: str, meta: dict[str, Any]) -> Path:
    """写出某 skill 的 skill-meta.yaml。"""
    path = meta_path(root, skill_id)
    dump_yaml(path, meta, comment="skill 级元数据 · 由 scripts 自动维护，请谨慎手改")
    return path
