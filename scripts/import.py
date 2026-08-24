"""入库脚本：从 GitHub 把 skill 镜像进 registry。

用法（在仓库根目录）::

    python -m scripts.import https://github.com/mattpocock/skills
    python -m scripts.import owner/repo another/owner/repo
    python -m scripts.import owner/repo --include-drafts   # 连半成品/废弃一起入
    python -m scripts.import owner/repo --force            # 已存在则覆盖重入
    python -m scripts.import owner/repo --dry-run          # 演练，不写盘

流程：解析 URL → 浅克隆并记录 upstream_sha → 枚举仓库内所有 SKILL.md
（合集/单 skill 同一套逻辑）→ 逐个校验/复制/写元数据/更新 index。

决策（阶段2 已确认）：合集检测用「枚举所有 SKILL.md」；默认跳过
in-progress/deprecated 等半成品分类；已存在默认温和跳过（--force 覆盖）；
首次入库 version 恒为 1.0.0。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from scripts.lib.index import (
    load_index,
    repo_root,
    row_from_meta,
    save_index,
    save_meta,
    upsert_skill,
)

# ---- 常量 ----
# 半成品/废弃分类：默认跳过，--include-drafts 时入库
DRAFT_DIRS = {"in-progress", "deprecated", "wip", "experimental", "draft", "_draft", "archive"}
FIRST_VERSION = "1.0.0"

# 匹配 owner/repo，兼容前导 https://github.com/ 与尾部 .git
_REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def parse_repo(arg: str) -> tuple[str, str]:
    """把 URL 或 ``owner/repo`` 解析成 (owner, repo)。"""
    m = _REPO_RE.match(arg.strip())
    if not m:
        raise ValueError(f"无法解析的仓库：{arg!r}（期望 https://github.com/owner/repo 或 owner/repo）")
    return m.group(1), m.group(2)


def _clone_repo(owner: str, repo: str, dest: Path) -> tuple[str, str]:
    """浅克隆到 dest，返回 (upstream_sha, upstream_branch)。"""
    url = f"https://github.com/{owner}/{repo}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True, capture_output=True, text=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() or "main"
    return sha, branch


def _read_frontmatter(skill_md: Path) -> dict[str, Any] | None:
    """解析 SKILL.md 的 YAML frontmatter；缺失或无法解析返回 None。"""
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


def _discover(clone: Path, include_drafts: bool) -> list[dict[str, Any]]:
    """在克隆树里枚举所有 SKILL.md，推导 id / 分类 / 源路径。

    返回每个 skill 的 dict：{src_dir, id_prefix, rel_dir, name, category_parts, container}。
    """
    found: list[dict[str, Any]] = []
    for sk in sorted(clone.rglob("SKILL.md")):
        rel_dir = sk.parent.relative_to(clone)
        parts = rel_dir.parts
        if any(p.startswith(".") for p in parts):
            continue  # 跳过隐藏目录（.github、.git 等）
        container = bool(parts and parts[0] == "skills")
        rest = list(parts[1:] if container else parts)
        name = rest[-1]
        category_parts = rest[:-1]
        if not include_drafts and any(c in DRAFT_DIRS for c in category_parts):
            continue  # 默认跳过半成品/废弃分类
        found.append({
            "src_dir": sk.parent,
            "rel_dir": rel_dir.as_posix(),
            "name": name,
            "category_parts": category_parts,
            "container": container,
        })
    return found


def _copy_category_readme(root: Path, owner: str, repo: str, clone: Path,
                          category_parts: list[str], container: bool) -> None:
    """若来源分类目录下存在 README.md，原样保留到 registry 对应分类。"""
    if not category_parts:
        return
    base = Path("skills") if container else Path(".")
    src_reader = clone / base / Path(*category_parts) / "README.md"
    if not src_reader.exists():
        return
    dest_dir = root / "skills" / owner / repo / Path(*category_parts)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "README.md"
    if not dest.exists():
        shutil.copy2(src_reader, dest)


def import_repo(root: Path, owner: str, repo: str, *,
                include_drafts: bool, force: bool, dry_run: bool) -> dict[str, Any]:
    """导入一个仓库，返回统计 {added, skipped, failed}。"""
    stats = {"added": [], "skipped": [], "failed": []}

    with tempfile.TemporaryDirectory(prefix="reg-import-") as tmp:
        clone = Path(tmp) / repo
        try:
            sha, branch = _clone_repo(owner, repo, clone)
        except subprocess.CalledProcessError as exc:
            err = exc.stderr if exc.stderr is not None else ""
            if err.strip():
                tail = err.strip().splitlines()[-1]
            else:
                tail = str(exc)
            stats["failed"].append((f"{owner}/{repo}", f"克隆失败: {tail}"))
            return stats

        skills = _discover(clone, include_drafts)
        if not skills:
            stats["skipped"].append((f"{owner}/{repo}", "未发现 SKILL.md"))
            return stats

        for sk in skills:
            cp = sk["category_parts"]
            sid_path = "/".join([owner, repo] + cp + [sk["name"]])
            try:
                _import_one(root, clone, owner, repo, sha, branch, sk,
                            force=force, dry_run=dry_run, stats=stats)
            except Exception as exc:  # noqa: BLE001 —— 单 skill 失败不影响其余
                stats["failed"].append((sid_path, f"{type(exc).__name__}: {exc}"))

        # 分类 README 保留（非演练时）
        if not dry_run:
            for sk in skills:
                _copy_category_readme(root, owner, repo, clone,
                                      sk["category_parts"], sk["container"])

    return stats


def _import_one(root: Path, clone: Path, owner: str, repo: str, sha: str, branch: str,
                sk: dict[str, Any], *, force: bool, dry_run: bool,
                stats: dict[str, Any]) -> None:
    """导入单个 skill：校验→复制→写 meta→更新 index。"""
    cp = sk["category_parts"]
    sid = "/".join([owner, repo] + cp + [sk["name"]])

    # 重复处理：默认温和跳过，--force 覆盖
    index = load_index(root)
    exists = any(r.get("id") == sid for r in index.get("skills", []))
    if exists and not force:
        stats["skipped"].append((sid, "已存在（用 --force 覆盖，或 sync 更新）"))
        return

    # 校验 SKILL.md frontmatter（必须可解析）
    skill_md = sk["src_dir"] / "SKILL.md"
    frontmatter = _read_frontmatter(skill_md)
    if frontmatter is None:
        stats["failed"].append((sid, "SKILL.md 缺少可解析的 frontmatter"))
        return

    if dry_run:
        stats["added"].append((sid, "dry-run：将复制并写入元数据"))
        return

    # 已存在且 --force：先清旧再抄新
    dest_dir = root / "skills" / owner / repo / Path(*cp) / sk["name"]
    if dest_dir.exists():
        try:
            shutil.rmtree(dest_dir)
        except OSError as exc:
            raise RuntimeError(f"无法清空旧目录 {dest_dir}: {exc}") from exc
    shutil.copytree(sk["src_dir"], dest_dir)

    meta = {
        "name": sk["name"],
        "source": {
            "owner": owner,
            "repo": repo,
            "category": "/".join(cp) if cp else None,
            "path_in_repo": sk["rel_dir"],
        },
        "upstream_branch": branch,
        "upstream_sha": sha,
        "version": FIRST_VERSION,
        "last_synced_at": str(date.today()),
        "risk": "clean",  # 阶段3 安全扫描接入后据此重写
        "description_zh": "",
    }
    save_meta(root, sid, meta)
    upsert_skill(index, row_from_meta(meta))
    save_index(index, root)
    stats["added"].append((sid, "已入库 v1.0.0"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="import", description="把 GitHub skills 入库 registry")
    parser.add_argument("repos", nargs="+", help="GitHub URL 或 owner/repo")
    parser.add_argument("--include-drafts", action="store_true",
                        help="连 in-progress/deprecated 等半成品分类一起入库")
    parser.add_argument("--force", action="store_true", help="同 id 已存在则覆盖重入")
    parser.add_argument("--dry-run", "-n", action="store_true", help="演练，不写盘")
    args = parser.parse_args(argv)

    root = repo_root()
    for raw in args.repos:
        try:
            owner, repo = parse_repo(raw)
        except ValueError as exc:
            print(f"✗ {exc}")
            continue
        print(f"\n==> 导入 {owner}/{repo}（include_drafts={args.include_drafts}, "
              f"force={args.force}, dry_run={args.dry_run}）")
        stats = import_repo(root, owner, repo,
                            include_drafts=args.include_drafts,
                            force=args.force, dry_run=args.dry_run)
        for sid, msg in stats["added"]:
            print(f"  + {sid}  {msg}")
        for sid, msg in stats["skipped"]:
            print(f"  · {sid}  {msg}")
        for sid, msg in stats["failed"]:
            print(f"  ✗ {sid}  {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
