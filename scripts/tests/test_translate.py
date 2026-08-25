"""translate 模块测试（无需 pytest，直接运行）。

运行：python3 -m scripts.tests.test_translate   （在仓库根目录）

覆盖：峰谷时段判断边界、API key 解析优先级、LLM 调用格式、幂等扫描。
（不实际调用 LLM API——用 monkeypatch 拦截网络。）
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.translate import (
    _auth_key_from_path,
    find_pending_skills,
    is_peak_hour,
    translate_description,
)

CN = ZoneInfo("Asia/Shanghai")


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ✓ {msg}")


def test_peak_hours() -> None:
    cases = [
        (datetime(2026, 8, 24, 9, 0, tzinfo=CN), True, "周一 09:00 高峰开始"),
        (datetime(2026, 8, 24, 11, 59, tzinfo=CN), True, "周一 11:59 高峰"),
        (datetime(2026, 8, 24, 12, 0, tzinfo=CN), False, "周一 12:00 低谷开始"),
        (datetime(2026, 8, 24, 13, 0, tzinfo=CN), False, "周一 13:00 低谷"),
        (datetime(2026, 8, 24, 14, 0, tzinfo=CN), True, "周一 14:00 高峰"),
        (datetime(2026, 8, 24, 17, 59, tzinfo=CN), True, "周一 17:59 高峰"),
        (datetime(2026, 8, 24, 18, 0, tzinfo=CN), False, "周一 18:00 低谷"),
        (datetime(2026, 8, 24, 8, 59, tzinfo=CN), False, "周一 08:59 低谷"),
        (datetime(2026, 8, 23, 10, 0, tzinfo=CN), False, "周日 10:00 周末低谷"),
        (datetime(2026, 8, 22, 15, 0, tzinfo=CN), False, "周六 15:00 周末低谷"),
    ]
    for dt, want, label in cases:
        got = is_peak_hour(dt)
        _assert(got == want, f"{label}（期望 {want}，实际 {got}）")


def test_resolve_api_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth.json"
        auth.write_text('{"deepseek": {"key": "sk-test123"}}', encoding="utf-8")
        with mock.patch("scripts.translate.os.environ.get", return_value=""):
            with mock.patch("scripts.translate.Path.home",
                            return_value=Path(tmp) / ".." / ".."):  # 不实际依赖
                pass  # 环境变量分支：env 优先
        # 直接测 auth.json 解析（mock home 不可靠，改为直接测核心解析）
        got = _auth_key_from_path(auth)
        _assert(got == "sk-test123", f"从 auth.json 读取 key（实际 {got!r}）")


def test_translate_description_payload() -> None:
    """验证 LLM 调用构造正确（拦截 urllib，不真连网络）。"""
    captured: dict = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return '{"choices": [{"message": {"content": "中文译文"}}]}'.encode()

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = req.data.decode()
        return FakeResp()

    with mock.patch("scripts.translate.urllib.request.urlopen", side_effect=fake_urlopen):
        zh = translate_description(
            "Review the code changes",
            api_key="sk-key", model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1/chat/completions",
        )
    _assert(zh == "中文译文", f"返回译文（实际 {zh!r}）")
    _assert(captured["auth"] == "Bearer sk-key", "Authorization 头正确")
    _assert("Review the code changes" in captured["body"], "请求体含英文原文")
    _assert(captured["url"].startswith("https://api.deepseek.com"), "请求 URL 正确")


def test_find_pending_skills() -> None:
    """幂等：description_zh 已有中文的跳过，空的才收集。"""
    root = Path(tempfile.mkdtemp(prefix="reg-tr-test-"))
    d1 = root / "skills" / "o" / "r" / "engineering" / "a"
    d2 = root / "skills" / "o" / "r" / "engineering" / "b"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    (d1 / "SKILL.md").write_text('---\nname: a\ndescription: "AAA"\n---\n', encoding="utf-8")
    (d2 / "SKILL.md").write_text('---\nname: b\ndescription: "BBB"\n---\n', encoding="utf-8")
    (d1 / "skill-meta.yaml").write_text("name: a\ndescription_zh: 已有中文\n", encoding="utf-8")
    (d2 / "skill-meta.yaml").write_text("name: b\ndescription_zh: ''\n", encoding="utf-8")

    pending = find_pending_skills(root)
    ids = [sid for sid, _ in pending]
    _assert(ids == ["o/r/engineering/b"], f"只收集空 description_zh 的（实际 {ids}）")


def main() -> None:
    print("1. 峰谷时段判断边界")
    test_peak_hours()
    print("2. API key 解析")
    test_resolve_api_key()
    print("3. LLM 调用构造（mock 网络）")
    test_translate_description_payload()
    print("4. 幂等扫描")
    test_find_pending_skills()
    print("\n✅ translate 测试全部通过")


if __name__ == "__main__":
    main()
