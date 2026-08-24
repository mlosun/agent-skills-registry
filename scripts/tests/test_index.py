"""index 模块冒烟测试（无需 pytest，直接运行验证阶段 1 核心逻辑）。

运行：python -m scripts.tests.test_index   （在仓库根目录）
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


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="registry-test-"))

    print("1. load_index 幂等（无文件时返回空结构且不落盘）")
    idx_data = load_index(root)
    _assert(idx_data["version"] == 1, "schema version == 1")
    _assert(idx_data["skills"] == [], "skills 为空列表")

    print("2. save_meta + 往返读取")
    meta = {
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
    sid = skill_id(meta)
    _assert(
        sid == "mattpocock/skills/engineering/code-review", f"skill_id 推导正确: {sid}"
    )
    save_meta(root, sid, meta)
    loaded = load_meta(root, sid)
    _assert(loaded is not None and loaded["name"] == "code-review", "meta 写读往返一致")

    print("3. row_from_meta + upsert + save_index")
    row = row_from_meta(meta)
    _assert(
        row["id"] == sid and row["risk"] == "clean" and row["version"] == "1.0.0",
        f"index 行精简快照生成正确: {row['id']}",
    )
    is_new = upsert_skill(idx_data, row)
    _assert(is_new is True, "首次 upsert 判定为新增")
    save_index(idx_data, root)

    print("4. reload index + 幂等再次 upsert（判定为更新而非新增）")
    reloaded = load_index(root)
    _assert(len(reloaded["skills"]) == 1, "index 持久化含 1 行")
    same = upsert_skill(reloaded, row)
    _assert(same is False, "重复 upsert 判定为更新")

    print("5. remove_skill")
    removed = remove_skill(reloaded, sid)
    _assert(removed is True and len(reloaded["skills"]) == 0, "remove 生效")

    print("6. 非法的 skill id 被拒绝（防路径穿越）")
    try:
        skill_dir(root, "../../etc")
        _assert(False, "应当抛出 ValueError")
    except ValueError:
        _assert(True, "非法 id 抛 ValueError")

    print("\n✅ 阶段 1 冒烟测试全部通过")


if __name__ == "__main__":
    main()
