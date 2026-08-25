"""web 模块测试（pytest 兼容 + 可直接运行）。

运行：python3 -m pytest scripts/tests/test_web.py -v
  或 python3 -m scripts.tests.test_web（在仓库根目录）

覆盖：站点生成、数据完整性、JSON 有效性、risk 颜色映射注入。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from scripts.web import RISK_COLOR, RISK_LABEL, build_site


def _make_skill_dir(root: Path, sid: str, *, risk: str = "clean") -> None:
    d = root / "skills" / Path(*sid.split("/"))
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {sid.split("/")[-1]}\ndescription: "desc"\n---\n', encoding="utf-8"
    )
    (d / "skill-meta.yaml").write_text(
        f'name: {sid.split("/")[-1]}\n'
        "source:\n  owner: o\n  repo: r\n"
        "upstream_sha: abc1234\nversion: 1.0.0\nlast_synced_at: '2026-08-25'\n"
        f"risk: {risk}\ndescription_zh: 中文\nrecommendation: 推荐\n"
        "tags: [标签1, 标签2]\n",
        encoding="utf-8",
    )


def _make_index(root: Path, sids: list[str]) -> None:
    lines = ["version: 1", "skills:"]
    for sid in sids:
        parts = sid.split("/")
        lines.append(f"- id: {sid}")
        lines.append(f"  name: {parts[-1]}")
        lines.append("  version: 1.0.0")
        lines.append("  risk: clean")
        lines.append(f"  category: {parts[2] if len(parts) > 3 else ''}")
    (root / "index.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_build_site_generates_files() -> None:
    root = Path(tempfile.mkdtemp(prefix="reg-web-"))
    _make_skill_dir(root, "o/r/eng/skill-a")
    _make_index(root, ["o/r/eng/skill-a"])

    stats = build_site(root)
    assert stats["total"] == 1
    assert (root / "docs" / "index.html").exists()
    assert (root / "docs" / "skills-data.json").exists()


def test_skills_data_complete() -> None:
    root = Path(tempfile.mkdtemp(prefix="reg-web-"))
    _make_skill_dir(root, "o/r/eng/skill-a")
    _make_index(root, ["o/r/eng/skill-a"])

    build_site(root)
    data = json.loads((root / "docs" / "skills-data.json").read_text())
    assert len(data) == 1
    s = data[0]
    for field in ["id", "name", "repo", "category", "version", "risk",
                  "description_zh", "recommendation", "tags"]:
        assert field in s, f"缺字段 {field}"
    assert s["risk"] == "clean"
    assert s["tags"] == ["标签1", "标签2"]


def test_risk_mapping_in_html() -> None:
    root = Path(tempfile.mkdtemp(prefix="reg-web-"))
    _make_skill_dir(root, "o/r/eng/skill-a")
    _make_index(root, ["o/r/eng/skill-a"])

    build_site(root)
    html = (root / "docs" / "index.html").read_text()
    # 颜色映射注入
    for color in RISK_COLOR.values():
        assert color in html, f"缺颜色 {color}"
    # 中文标签注入
    for label in RISK_LABEL.values():
        assert label in html, f"缺标签 {label}"
    # 无残留占位符
    assert "json_risk_color" not in html
    assert "json_risk_label" not in html
    # 事件委托（无 onclick 内联字符串）
    assert 'onclick="' not in html, "不应有 onclick 内联字符串"
    assert "data-tag" in html and "data-id" in html


def main() -> None:
    for name, fn in sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ):
        print(f"  ✓ {name}")
        fn()
    print("\n✅ web 测试全部通过")


if __name__ == "__main__":
    main()