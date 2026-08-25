"""上游同步脚本：把 registry 中的 skill 与上游仓库保持同步。

用法（在仓库根目录）::

    python3 -m scripts.sync                          # 同步所有来源仓库
    python3 -m scripts.sync --repo mattpocock/skills  # 只同步指定仓库（可多次）
    python3 -m scripts.sync --dry-run                 # 演练：只报告哪些有更新，不写盘

流程（按来源仓库为单位，一次浅克隆对比整个仓库）：
1. 从 index 收集该仓库下所有 skill + 记录的 upstream_sha
2. 浅克隆上游 → 取 HEAD SHA
3. 对比：
   - SHA 相同 → 该仓库所有 skill 无更新，跳过
   - SHA 不同 → 枚举上游所有 SKILL.md：
       * 已有 skill：覆盖新代码 + version patch+1（1.0.0→1.0.1）+ 重新安全扫描
       * 新增 skill：入库 version=1.0.0（与 import.py 一致）
       * 上游删除的 skill：保留本地 + 提示 asm remove（§3.4 约定）
4. 汇总报告（更新/新增/上游删除/无变化）

说明：sync 只处理"内容更新"。description_zh（人工/LLM 产物）在更新时**保留**，
不被上游覆盖；版本号是 registry 内部计数，patch 步进。
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

from .lib.index import (
    load_index,
    load_meta,
    repo_root,
    row_from_meta,
    save_index,
    save_meta,
    upsert_skill,
)
from .lib.skillfile import read_frontmatter
from .security_scan import scan_skill, write_report

# ---- 常量 ----
FIRST_VERSION = "1.0.0"
_REPO_RE = re.compile(
    r"^(?:https?://github\.com/)?([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def parse_repo(arg: str) -> tuple[str, str]:
    """把 URL 或 owner/repo 解析成 (owner, repo)。"""
    m = _REPO_RE.match(arg.strip())
    if not m:
        raise ValueError(
            f"无法解析的仓库：{arg!r}（期望 https://github.com/owner/repo 或 owner/repo）"
        )
    return m.group(1), m.group(2)


def _clone_repo(owner: str, repo: str, dest: Path) -> tuple[str, str]:
    """浅克隆到 dest，返回 (HEAD_sha, branch)。"""
    url = f"https://github.com/{owner}/{repo}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    branch = (
        subprocess.run(
            ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        or "main"
    )
    return sha, branch


def _discover(clone: Path) -> list[dict[str, Any]]:
    """枚举克隆树里所有 SKILL.md（同步场景不跳过草稿分类——上游分类变化也要感知）。

    返回每个 skill：{src_dir, rel_dir, name, category_parts, container}。
    """
    found: list[dict[str, Any]] = []
    for sk in sorted(clone.rglob("SKILL.md")):
        rel_dir = sk.parent.relative_to(clone)
        parts = rel_dir.parts
        if any(p.startswith(".") for p in parts):
            continue
        container = bool(parts and parts[0] == "skills")
        rest = list(parts[1:] if container else parts)
        name = rest[-1]
        category_parts = rest[:-1]
        found.append(
            {
                "src_dir": sk.parent,
                "rel_dir": rel_dir.as_posix(),
                "name": name,
                "category_parts": category_parts,
                "container": container,
            }
        )
    return found


def _bump_patch(version: str) -> str:
    """版本 patch 步进：1.0.0 → 1.0.1；异常格式回退 1.0.1。"""
    parts = version.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
        return f"{major}.{minor}.{patch + 1}"
    except (ValueError, IndexError):
        return "1.0.1"


def _copy_category_readme(
    root: Path, owner: str, repo: str, clone: Path,
    category_parts: list[str], container: bool,
) -> None:
    """来源分类目录的 README.md 保留到 registry 对应分类（缺失才复制）。"""
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


def sync_repo(root: Path, owner: str, repo: str, *, dry_run: bool) -> dict[str, Any]:
    """同步一个来源仓库，返回统计 {updated, added, removed, unchanged, failed}。"""
    stats = {
        "updated": [], "added": [], "removed": [], "unchanged": [], "failed": [],
    }

    index = load_index(root)
    # 收集该仓库下的 skill（owner/repo 前缀匹配）
    repo_skills = [r for r in index.get("skills", []) if r["id"].startswith(f"{owner}/{repo}/")]
    if not repo_skills:
        stats["failed"].append((f"{owner}/{repo}", "index 中无此仓库的 skill，先用 import"))
        return stats
    recorded_sha = _read_recorded_sha(root, repo_skills[0]["id"])

    with tempfile.TemporaryDirectory(prefix="reg-sync-") as tmp:
        clone = Path(tmp) / repo
        try:
            head_sha, branch = _clone_repo(owner, repo, clone)
        except subprocess.CalledProcessError as exc:
            err = exc.stderr if exc.stderr is not None else ""
            if err.strip():
                tail = err.strip().splitlines()[-1]
            else:
                tail = str(exc)
            stats["failed"].append((f"{owner}/{repo}", f"克隆失败: {tail}"))
            return stats

        if recorded_sha and head_sha == recorded_sha:
            stats["unchanged"].append((f"{owner}/{repo}", f"无更新 (SHA {head_sha[:8]})"))
            return stats

        # 上游有更新：枚举所有 SKILL.md，逐 skill 处理
        upstream_skills = _discover(clone)
        upstream_ids = set()
        for sk in upstream_skills:
            cp = sk["category_parts"]
            sid = "/".join([owner, repo] + cp + [sk["name"]])
            upstream_ids.add(sid)
            exists = any(r.get("id") == sid for r in repo_skills)
            try:
                if exists:
                    _sync_existing(root, clone, owner, repo, head_sha, branch, sk,
                                   dry_run=dry_run, stats=stats, index=index)
                else:
                    _sync_new(root, clone, owner, repo, head_sha, branch, sk,
                              dry_run=dry_run, stats=stats, index=index)
            except Exception as exc:  # noqa: BLE001 —— 单 skill 失败不影响其余
                stats["failed"].append((sid, f"{type(exc).__name__}: {exc}"))

        # 上游已删除/改名的 skill：本地保留，但提示
        local_ids = {r["id"] for r in repo_skills}
        for sid in sorted(local_ids - upstream_ids):
            stats["removed"].append((sid, "上游已删除/不存在，本地保留"))

        # 分类 README 保留（非演练）
        if not dry_run:
            seen_cats = set()
            for sk in upstream_skills:
                key = "/".join(sk["category_parts"])
                if key in seen_cats:
                    continue
                seen_cats.add(key)
                _copy_category_readme(root, owner, repo, clone,
                                      sk["category_parts"], sk["container"])

    return stats


def _read_recorded_sha(root: Path, sid: str) -> str:
    """从 skill-meta.yaml 读取已记录的 upstream_sha（index 行不含 SHA）。"""
    meta = load_meta(root, sid)
    return (meta or {}).get("upstream_sha", "")


def _sync_existing(
    root: Path, clone: Path, owner: str, repo: str, head_sha: str, branch: str,
    sk: dict[str, Any], *, dry_run: bool, stats: dict[str, Any], index: dict,
) -> None:
    """更新已有 skill：覆盖新代码 + version patch+1 + 重扫；保留 description_zh。"""
    cp = sk["category_parts"]
    sid = "/".join([owner, repo] + cp + [sk["name"]])

    skill_md = sk["src_dir"] / "SKILL.md"
    if read_frontmatter(skill_md) is None:
        stats["failed"].append((sid, "SKILL.md 缺少可解析的 frontmatter，跳过更新"))
        return

    old_meta = load_meta(root, sid) or {}
    old_version = str(old_meta.get("version", FIRST_VERSION))
    old_desc_zh = old_meta.get("description_zh", "")
    new_version = _bump_patch(old_version)

    if dry_run:
        stats["updated"].append(
            (sid, f"v{old_version} → v{new_version}（SHA {head_sha[:8]}）")
        )
        return

    dest_dir = root / "skills" / owner / repo / Path(*cp) / sk["name"]
    if dest_dir.exists():
        try:
            shutil.rmtree(dest_dir)
        except OSError as exc:
            raise RuntimeError(f"无法清空旧目录 {dest_dir}: {exc}") from exc
    shutil.copytree(sk["src_dir"], dest_dir)

    # 重新安全扫描（上游更新可能引入新内容）
    scan_result = scan_skill(dest_dir)
    risk = scan_result.risk
    write_report(dest_dir, scan_result)

    meta = {
        "name": sk["name"],
        "source": {
            "owner": owner,
            "repo": repo,
            "category": "/".join(cp) if cp else None,
            "path_in_repo": sk["rel_dir"],
        },
        "upstream_branch": branch,
        "upstream_sha": head_sha,
        "version": new_version,
        "last_synced_at": str(date.today()),
        "risk": risk,
        "description_zh": old_desc_zh,  # 翻译是人工/LLM 产物，保留不被上游覆盖
    }
    save_meta(root, sid, meta)
    upsert_skill(index, row_from_meta(meta))
    save_index(index, root)
    stats["updated"].append(
        (sid, f"v{old_version} → v{new_version}（risk={risk}）")
    )


def _sync_new(
    root: Path, clone: Path, owner: str, repo: str, head_sha: str, branch: str,
    sk: dict[str, Any], *, dry_run: bool, stats: dict[str, Any], index: dict,
) -> None:
    """上游新增的 skill：入库 version=1.0.0（与 import.py 相同语义）。"""
    cp = sk["category_parts"]
    sid = "/".join([owner, repo] + cp + [sk["name"]])

    skill_md = sk["src_dir"] / "SKILL.md"
    if read_frontmatter(skill_md) is None:
        stats["failed"].append((sid, "SKILL.md 缺少可解析的 frontmatter，跳过"))
        return

    if dry_run:
        stats["added"].append((sid, "新增（入库 v1.0.0）"))
        return

    dest_dir = root / "skills" / owner / repo / Path(*cp) / sk["name"]
    shutil.copytree(sk["src_dir"], dest_dir)
    scan_result = scan_skill(dest_dir)
    risk = scan_result.risk
    write_report(dest_dir, scan_result)

    meta = {
        "name": sk["name"],
        "source": {
            "owner": owner,
            "repo": repo,
            "category": "/".join(cp) if cp else None,
            "path_in_repo": sk["rel_dir"],
        },
        "upstream_branch": branch,
        "upstream_sha": head_sha,
        "version": FIRST_VERSION,
        "last_synced_at": str(date.today()),
        "risk": risk,
        "description_zh": "",
    }
    save_meta(root, sid, meta)
    upsert_skill(index, row_from_meta(meta))
    save_index(index, root)
    stats["added"].append((sid, f"已入库 v1.0.0（risk={risk}）"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync", description="把 registry 中的 skill 与上游仓库保持同步"
    )
    parser.add_argument(
        "--repo",
        action="append",
        help="只同步指定仓库（owner/repo 或 URL，可多次）；缺省同步全部来源",
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="演练，不写盘")
    args = parser.parse_args(argv)

    root = repo_root()
    index = load_index(root)

    # 收集来源仓库（去重，按 owner/repo）
    repos: list[tuple[str, str]] = []
    if args.repo:
        for raw in args.repo:
            try:
                repos.append(parse_repo(raw))
            except ValueError as exc:
                print(f"✗ {exc}")
    else:
        seen = set()
        for r in index.get("skills", []):
            parts = r["id"].split("/")
            key = (parts[0], parts[1])
            if key not in seen:
                seen.add(key)
                repos.append(key)

    if not repos:
        print("（index 中没有来源仓库，请先用 import 入库）")
        return 1

    ok = fail = 0
    for owner, repo in repos:
        print(f"\n==> 同步 {owner}/{repo}")
        stats = sync_repo(root, owner, repo, dry_run=args.dry_run)
        for sid, msg in stats["updated"]:
            print(f"  ↻ {sid}  {msg}")
        for sid, msg in stats["added"]:
            print(f"  + {sid}  {msg}")
        for sid, msg in stats["removed"]:
            print(f"  - {sid}  {msg}")
        for sid, msg in stats["unchanged"]:
            print(f"  · {sid}  {msg}")
        for sid, msg in stats["failed"]:
            print(f"  ✗ {sid}  {msg}")
        if stats["failed"]:
            fail += len(stats["failed"])
        else:
            ok += 1

    print(f"\n完成：{ok} 个仓库成功，{fail} 个有失败" + ("（演练模式，未写盘）" if args.dry_run else ""))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())