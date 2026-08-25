"""enrich 模块测试（pytest 兼容 + 可直接运行）。

运行：python3 -m pytest scripts/tests/test_enrich.py -v
  或 python3 -m scripts.tests.test_enrich（在仓库根目录）

覆盖：JSON 解析容错、幂等扫描、LLM 调用构造（mock 网络，不真连）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from scripts.enrich import (
    _parse_result,
    enrich_one_call,
    find_pending_skills,
)


def _mk_meta(path: Path, *, has_rec: bool = False, has_tags: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["name: x"]
    if has_rec:
        lines.append("recommendation: 已有推荐")
    if has_tags:
        lines.append("tags: [编码]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_result_normal() -> None:
    r = _parse_result(
        '{"description_zh":"测试","recommendation":"值得装","tags":["编码","审查"]}',
        "x",
    )
    assert r["recommendation"] == "值得装"
    assert r["tags"] == ["编码", "审查"]


def test_parse_result_codeblock() -> None:
    r = _parse_result(
        '```json\n{"description_zh":"a","recommendation":"b","tags":["x","y"]}\n```',
        "x",
    )
    assert r["recommendation"] == "b"
    assert r["tags"] == ["x", "y"]


def test_parse_result_extra_text() -> None:
    r = _parse_result(
        '好的，这是结果：{"recommendation":"推荐","tags":["z"],"description_zh":""}',
        "x",
    )
    assert r["recommendation"] == "推荐"


def test_parse_result_tags_as_string() -> None:
    r = _parse_result(
        '{"description_zh":"","recommendation":"r","tags":"编码,审查,测试"}', "x"
    )
    assert r["tags"] == ["编码", "审查", "测试"]


def test_parse_result_missing_recommendation() -> None:
    try:
        _parse_result('{"description_zh":"","tags":["x"]}', "x")
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass


def test_enrich_one_call_payload() -> None:
    """验证 LLM 调用构造（mock 网络，不真连）。"""
    captured: dict = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            # 用 json.dumps 构造含中文的响应体，避免 bytes 字面量非 ASCII
            inner = '{"description_zh":"","recommendation":"\u63a8\u8350","tags":["a"]}'
            return json.dumps(
                {
                    "choices": [{"message": {"content": inner}}],
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = req.data.decode()
        captured["format"] = jsoncheck(req.data)
        return FakeResp()

    import json

    def jsoncheck(data):
        return json.loads(data).get("response_format", {}).get("type")

    with mock.patch("scripts.enrich.urllib.request.urlopen", side_effect=fake_urlopen):
        r = enrich_one_call(
            "x",
            "desc",
            api_key="sk-key",
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1/chat/completions",
            need_desc=True,
        )
    assert r["recommendation"] == "推荐"
    assert captured["auth"] == "Bearer sk-key"
    assert captured["format"] == "json_object", "应请求结构化输出"


def test_find_pending_skills() -> None:
    """幂等：已有推荐+标签的跳过，缺的收集。"""
    root = Path(tempfile.mkdtemp(prefix="reg-enrich-"))
    d1 = root / "skills" / "o" / "r" / "a"
    d2 = root / "skills" / "o" / "r" / "b"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    (d1 / "SKILL.md").write_text(
        '---\nname: a\ndescription: "D"\n---\n', encoding="utf-8"
    )
    (d2 / "SKILL.md").write_text(
        '---\nname: b\ndescription: "D"\n---\n', encoding="utf-8"
    )
    _mk_meta(d1 / "skill-meta.yaml", has_rec=True, has_tags=True)  # 完整 → 跳过
    _mk_meta(d2 / "skill-meta.yaml")  # 缺 → 收集

    pending = find_pending_skills(root)
    ids = [sid for sid, _ in pending]
    assert ids == ["o/r/b"], f"只收集缺内容的（实际 {ids}）"


def main() -> None:
    for name, fn in sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ):
        print(f"  ✓ {name}")
        fn()
    print("\n✅ enrich 测试全部通过")


if __name__ == "__main__":
    main()
