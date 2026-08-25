"""sync 模块测试（pytest 兼容 + 可直接运行）。

运行：python3 -m pytest scripts/tests/test_sync.py -v
  或 python3 -m scripts.tests.test_sync（在仓库根目录）

覆盖：patch 版本步进、无更新路径、更新路径（version+1/保留 description_zh/重扫）、
新增路径。用临时 git 仓库模拟上游，避免真实网络。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from scripts.sync import _bump_patch, _discover, sync_repo


def _git(dir_: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(dir_), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo(path: Path, files: dict[str, str], commit_msg: str = "init") -> str:
    """创建带内容的 git 仓库，返回 HEAD SHA。"""
    path.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    _git(path, "init", "-b", "main")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", commit_msg)
    return _git(path, "rev-parse", "HEAD")


def _mk_skill(name: str, desc: str = "desc") -> str:
    return f'---\nname: {name}\ndescription: "{desc}"\n---\nbody\n'


def test_bump_patch() -> None:
    assert _bump_patch("1.0.0") == "1.0.1", "patch 步进"
    assert _bump_patch("1.0.9") == "1.0.10", "跨 9"
    assert _bump_patch("2.5.0") == "2.5.1", "多版本号"
    assert _bump_patch("bad") == "1.0.1", "异常格式回退"


def test_discover_collection() -> None:
    root = Path(tempfile.mkdtemp(prefix="reg-sync-disc-"))
    _make_repo(
        root,
        {
            "skills/engineering/code-review/SKILL.md": _mk_skill("code-review"),
            "skills/productivity/grill-me/SKILL.md": _mk_skill("grill-me"),
            "README.md": "root",
        },
    )
    found = _discover(root)
    ids = sorted((f"{'/'.join(s['category_parts'])}/{s['name']}" for s in found))
    assert ids == ["engineering/code-review", "productivity/grill-me"], ids


def test_sync_no_update() -> None:
    """SHA 相同 → 无更新，version 不变。"""
    root = Path(tempfile.mkdtemp(prefix="reg-sync-"))
    upstream = root / "upstream"
    sha = _make_repo(
        upstream,
        {
            "skills/engineering/code-review/SKILL.md": _mk_skill("code-review"),
        },
    )

    # 构造 registry：index 记录同 SHA
    idx = root / "index.yaml"
    idx.write_text(
        "version: 1\nskills:\n"
        "- id: mattpocock/skills/engineering/code-review\n  name: code-review\n"
        "  version: 1.0.3\n  risk: clean\n  category: engineering\n",
        encoding="utf-8",
    )
    meta_dir = root / "skills" / "mattpocock" / "skills" / "engineering" / "code-review"
    meta_dir.mkdir(parents=True)
    (meta_dir / "SKILL.md").write_text(_mk_skill("code-review"), encoding="utf-8")
    (meta_dir / "skill-meta.yaml").write_text(
        f"name: code-review\nupstream_sha: {sha}\nversion: 1.0.3\nrisk: clean\n"
        f"description_zh: 已有中文\n",
        encoding="utf-8",
    )

    # mock 克隆：让 sync 从本地 upstream 复制而非真网络
    from unittest import mock

    def fake_clone(owner, repo, dest):
        shutil_cp = __import__("shutil").copytree
        shutil_cp(upstream, dest)
        return sha, "main"

    with mock.patch("scripts.sync._clone_repo", side_effect=fake_clone):
        stats = sync_repo(root, "mattpocock", "skills", dry_run=False)

    assert len(stats["unchanged"]) == 1, f"应无更新（实际 {stats}）"
    assert stats["updated"] == [], "无更新时不应 version+1"
    # version 不变
    import yaml

    meta = yaml.safe_load((meta_dir / "skill-meta.yaml").read_text())
    assert meta["version"] == "1.0.3", "version 不应变"


def test_sync_update_existing() -> None:
    """SHA 不同 → 更新：version patch+1、保留 description_zh、重扫。"""
    import yaml
    from unittest import mock

    root = Path(tempfile.mkdtemp(prefix="reg-sync-"))
    upstream = root / "upstream"
    sha_old = _make_repo(
        upstream,
        {
            "skills/engineering/code-review/SKILL.md": _mk_skill(
                "code-review", "old desc"
            ),
        },
        "old",
    )

    # 先建一个"新版本"的旧 SKILL.md（模拟旧内容）
    upstream_old = root / "upstream_old"
    _make_repo(
        upstream_old,
        {
            "skills/engineering/code-review/SKILL.md": _mk_skill(
                "code-review", "old desc"
            ),
        },
        "old",
    )

    # registry 初始记录（old sha）
    meta_dir = root / "skills" / "mattpocock" / "skills" / "engineering" / "code-review"
    meta_dir.mkdir(parents=True)
    (meta_dir / "SKILL.md").write_text(
        _mk_skill("code-review", "old desc"), encoding="utf-8"
    )
    (meta_dir / "skill-meta.yaml").write_text(
        f"name: code-review\nupstream_sha: {sha_old}\nversion: 1.0.0\nrisk: clean\n"
        f"description_zh: 我的人工翻译\n",
        encoding="utf-8",
    )
    (root / "index.yaml").write_text(
        "version: 1\nskills:\n"
        "- id: mattpocock/skills/engineering/code-review\n  name: code-review\n"
        "  version: 1.0.0\n  risk: clean\n  category: engineering\n",
        encoding="utf-8",
    )

    # 上游有新提交 → 新 SHA（在 upstream 上加一个新文件 commit）
    (upstream / "skills" / "engineering" / "code-review" / "extra.md").write_text(
        "new\n", encoding="utf-8"
    )
    _git(upstream, "add", "-A")
    _git(
        upstream, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "update"
    )
    sha_new = _git(upstream, "rev-parse", "HEAD")
    assert sha_new != sha_old

    def fake_clone(owner, repo, dest):
        __import__("shutil").copytree(upstream, dest)
        return sha_new, "main"

    with mock.patch("scripts.sync._clone_repo", side_effect=fake_clone):
        stats = sync_repo(root, "mattpocock", "skills", dry_run=False)

    assert len(stats["updated"]) == 1, f"应有 1 个更新（实际 {stats}）"
    meta = yaml.safe_load((meta_dir / "skill-meta.yaml").read_text())
    assert meta["version"] == "1.0.1", f"version 应 patch+1（实际 {meta['version']}）"
    assert meta["upstream_sha"] == sha_new, "upstream_sha 更新"
    assert meta["description_zh"] == "我的人工翻译", "description_zh 保留"
    assert (meta_dir / "extra.md").exists(), "新代码已复制"
    assert (meta_dir / "security-report.md").exists(), "重扫报告生成"
    # index 行也更新
    idx = yaml.safe_load((root / "index.yaml").read_text())
    assert idx["skills"][0]["version"] == "1.0.1", "index version 同步更新"


def main() -> None:
    for name, fn in sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ):
        print(f"  ✓ {name}")
        fn()
    print("\n✅ sync 测试全部通过")


if __name__ == "__main__":
    main()
