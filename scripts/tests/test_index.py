"""index 模块测试（pytest 兼容 + 可直接运行验证阶段 1 核心逻辑）。

运行：python3 -m pytest scripts/tests/test_index.py -v
  或 python3 -m scripts.tests.test_index（在仓库根目录）
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from scripts.lib.index import (
    load_index,
    load_meta,
    remove_skill,
    row_from_meta,
    save_index,
    save_meta,
    skill_dir,
    skill_id,
    upsert_skill,
)


def _sample_meta() -> dict:
    return {
        "name": "code-review",
        "source": {
            "owner": "mattpocock",
            "repo": "skills",
            "category": "engineering",
            "path_in_repo": "skills/engineering/code-review",
        },
        "upstream_branch": "main",
        "upstream_sha": "abc1234",
        "version": "1.0.0",
        "last_synced_at": "2026-08-24",
        "risk": "clean",
        "description_zh": "按两轴审查代码改动：标准 + 规格",
    }


def test_load_index_idempotent() -> None:
    root = Path(tempfile.mkdtemp(prefix="registry-test-"))
    idx_data = load_index(root)
    assert idx_data["version"] == 1, "schema version == 1"
    assert idx_data["skills"] == [], "skills 为空列表"


def test_meta_roundtrip() -> None:
    root = Path(tempfile.mkdtemp(prefix="registry-test-"))
    meta = _sample_meta()
    sid = skill_id(meta)
    assert sid == "mattpocock/skills/engineering/code-review", f"skill_id 推导: {sid}"
    save_meta(root, sid, meta)
    loaded = load_meta(root, sid)
    assert loaded is not None and loaded["name"] == "code-review", "meta 写读往返一致"


def test_upsert_and_index_save() -> None:
    root = Path(tempfile.mkdtemp(prefix="registry-test-"))
    meta = _sample_meta()
    sid = skill_id(meta)
    idx_data = load_index(root)
    row = row_from_meta(meta)
    assert row["id"] == sid and row["risk"] == "clean" and row["version"] == "1.0.0"
    assert upsert_skill(idx_data, row) is True, "首次 upsert 判定为新增"
    save_index(idx_data, root)
    reloaded = load_index(root)
    assert len(reloaded["skills"]) == 1, "index 持久化含 1 行"
    assert upsert_skill(reloaded, row) is False, "重复 upsert 判定为更新"


def test_remove_skill() -> None:
    root = Path(tempfile.mkdtemp(prefix="registry-test-"))
    meta = _sample_meta()
    sid = skill_id(meta)
    idx_data = load_index(root)
    upsert_skill(idx_data, row_from_meta(meta))
    assert remove_skill(idx_data, sid) is True, "remove 生效"
    assert len(idx_data["skills"]) == 0


def test_invalid_skill_id_rejected() -> None:
    root = Path(tempfile.mkdtemp(prefix="registry-test-"))
    try:
        skill_dir(root, "../../etc")
        raise AssertionError("应当抛出 ValueError")
    except ValueError:
        pass  # 预期行为：非法 id 被拒绝


def main() -> None:
    """直接运行入口（python -m scripts.tests.test_index）。"""
    for name, fn in sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ):
        print(f"  ✓ {name}")
        fn()
    print("\n✅ 阶段 1 冒烟测试全部通过")


if __name__ == "__main__":
    main()
