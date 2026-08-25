"""中文翻译脚本：把 skill 的英文 description 翻译成中文，写入 skill-meta.yaml。

用法（在仓库根目录）::

    python3 -m scripts.translate                          # 翻译所有 description_zh 为空的 skill
    python3 -m scripts.translate --skill code-review       # 只翻译指定 skill（可多次）
    python3 -m scripts.translate --dry-run                 # 预览：哪些需要翻译，不调用 API
    python3 -m scripts.translate --force                   # 高峰时段强制运行（默认拒绝）
    python3 -m scripts.translate --interval 2.0            # 两次请求间隔秒数（默认 1.0）

LLM 接入（零第三方依赖，urllib 直连 DeepSeek，OpenAI 兼容协议）：
- Key 来源优先级：环境变量 ``DEEPSEEK_API_KEY`` → ``~/.pi/agent/auth.json`` 的 deepseek.key
- API: ``https://api.deepseek.com/v1/chat/completions``（可 --base-url 覆盖）
- 模型: deepseek-v4-flash（便宜够用，可 --model 覆盖）

时段护栏（DeepSeek 峰谷定价，成本控制）：
- 高峰（全价）: 工作日北京时间 09:00-12:00 与 14:00-18:00
- 低谷（5 折）: 工作日高峰之外 + 周末全天
- 默认在高峰时段拒绝运行，--force 强制（接受全价）

幂等：只翻译 ``description_zh`` 为空的 skill；已有中文的跳过，重复跑不重复消耗。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .lib.index import load_meta, repo_root, save_meta
from .lib.skillfile import read_description

# ---- 常量 ----
# 用 deepseek-chat（非推理模型）：翻译任务快、便宜、无 reasoning_content 空输出坑。
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_INTERVAL = 1.0
DEFAULT_TIMEOUT = 60.0
MAX_TOKENS = 400  # 非推理模型下足够中文译文空间

# 北京时间（DeepSeek 峰谷定价以北京时间为准）
CN_TZ = ZoneInfo("Asia/Shanghai")

# 高峰窗口：工作日 09:00-12:00 与 14:00-18:00（半开区间 [start, end)）
_PEAK_WINDOWS = ((9, 12), (14, 18))

PROMPT_TEMPLATE = """你是 AI Skill 描述的翻译员。把下面的英文 skill 描述翻译成简体中文。

要求：
1. 直译为主，忠实原意，不添加、不删减信息
2. 保留 skill 名、技术术语、命令、文件名等不翻译
3. 语气中性、简洁，适合作为工具元数据描述
4. 只输出译文本身，不要解释、不要加引号或 Markdown

英文原文：
{description}
"""


# ---------------- 时段护栏 ----------------

def is_peak_hour(now: datetime | None = None) -> bool:
    """判断当前北京时间的 `now`（缺省取当前时刻）是否处于高峰时段。

    规则：工作日 09:00-12:00 或 14:00-18:00 为高峰；周末全天低谷。
    """
    now = now or datetime.now(CN_TZ)
    # isoweekday(): 1-5 为周一至周五，6-7 为周末
    if now.isoweekday() >= 6:
        return False
    hour = now.hour
    return any(start <= hour < end for start, end in _PEAK_WINDOWS)


def _describe_window() -> str:
    """人类可读的允许运行时段描述。"""
    return "工作日 12:00-14:00 / 18:00-次日 09:00，及周末全天"


# ---------------- LLM 接入 ----------------

def _auth_key_from_path(auth_path: Path) -> str:
    """从给定 auth.json 路径读取 deepseek.key；不存在/损坏返回空串。"""
    try:
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    dsk = auth.get("deepseek", {}) if isinstance(auth, dict) else {}
    key = dsk.get("key", "") if isinstance(dsk, dict) else ""
    return key.strip()


def resolve_api_key() -> str:
    """获取 DeepSeek API key：环境变量 DEEPSEEK_API_KEY → ~/.pi/agent/auth.json。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    auth_path = Path.home() / ".pi" / "agent" / "auth.json"
    key = _auth_key_from_path(auth_path) if auth_path.exists() else ""
    if not key:
        raise RuntimeError(
            "未找到 DeepSeek API key：请设置环境变量 DEEPSEEK_API_KEY，"
            f"或确保 {auth_path} 包含 deepseek.key"
        )
    return key


def translate_description(
    description: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """调用 LLM 把英文 description 翻译成中文。

    返回译文（已 strip）；API 失败抛 RuntimeError。
    """
    if not description.strip():
        raise ValueError("description 为空，无需翻译")
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": PROMPT_TEMPLATE.format(description=description)},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.3,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"无法连接 API: {exc}") from exc

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"API 响应格式异常: {str(body)[:200]}") from None
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise RuntimeError(
            "API 返回空译文（若使用推理模型，可能因 max_tokens 被推理内容占满；"
            "建议换用非推理模型如 deepseek-chat）"
        )
    return text


# ---------------- 翻译主流程 ----------------

def find_pending_skills(root: Path) -> list[tuple[str, Path]]:
    """扫描所有 skill-meta.yaml，返回需要翻译的 (skill_id, meta_path) 列表。

    需要翻译 = description_zh 为空/缺失 且 存在 SKILL.md 有英文 description。
    """
    pending: list[tuple[str, Path]] = []
    for meta_file in sorted((root / "skills").rglob("skill-meta.yaml")):
        sid = meta_file.parent.relative_to(root / "skills").as_posix()
        meta = load_meta(root, sid)
        if meta is None:
            continue
        # 已有中文描述则跳过（幂等）
        if meta.get("description_zh", "").strip():
            continue
        skill_md = meta_file.parent / "SKILL.md"
        if not skill_md.exists():
            continue
        pending.append((sid, meta_file))
    return pending


def translate_skill(
    root: Path,
    sid: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    dry_run: bool,
) -> tuple[str, str]:
    """翻译单个 skill 的 description 并写回 skill-meta.yaml。

    返回 (sid, 结果描述)，结果描述以「✓」或「✗」开头。
    """
    skill_dir = root / "skills" / Path(*sid.split("/"))
    description = read_description(skill_dir / "SKILL.md")
    if not description.strip():
        return sid, "✗ SKILL.md 缺少英文 description，跳过"
    if dry_run:
        return sid, f"✓ 待翻译（{len(description)} 字符）"
    zh = translate_description(description, api_key=api_key, model=model, base_url=base_url)
    meta = load_meta(root, sid) or {}
    meta["description_zh"] = zh
    save_meta(root, sid, meta)
    return sid, f"✓ 已翻译: {zh[:40]}{'…' if len(zh) > 40 else ''}"


# ---------------- CLI ----------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="translate", description="把 skill 的英文 description 翻译成中文"
    )
    parser.add_argument(
        "--skill",
        action="append",
        help="只翻译指定 skill id（可多次传入）；缺省翻译全部待翻译的",
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="预览，不调用 API")
    parser.add_argument(
        "--force",
        action="store_true",
        help="高峰时段强制运行（接受全价，不推荐）",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API 地址")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"两次请求间隔秒数（默认 {DEFAULT_INTERVAL}，防限流）",
    )
    args = parser.parse_args(argv)

    # 时段护栏
    if is_peak_hour():
        if args.force:
            print("⚠ 当前为高峰时段（全价），--force 强制继续")
        else:
            print(
                f"✗ 当前为 DeepSeek 高峰时段（全价）。允许运行时段：{_describe_window()}。"
                f"可用 --force 强制（不推荐）。"
            )
            return 1
    else:
        print("✓ 当前为低谷时段（5 折）。")

    root = repo_root()
    try:
        api_key = resolve_api_key()
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1

    # 收集待翻译 skill
    pending = find_pending_skills(root)
    if args.skill:
        wanted = set(args.skill)
        pending = [(sid, mp) for sid, mp in pending if sid in wanted]
        missing = wanted - {sid for sid, _ in pending}
        for sid in sorted(missing):
            print(f"  · {sid}  未在待翻译列表（可能已有中文或无 SKILL.md）")

    if not pending:
        print("（没有需要翻译的 skill，全部已有中文描述）")
        return 0
    print(f"待翻译 {len(pending)} 个 skill：")

    ok = fail = 0
    for i, (sid, _meta_file) in enumerate(pending, start=1):
        try:
            _, msg = translate_skill(
                root, sid, api_key=api_key, model=args.model,
                base_url=args.base_url, dry_run=args.dry_run,
            )
            print(f"  [{i}/{len(pending)}] {sid}  {msg}")
            ok += 1
        except (RuntimeError, ValueError) as exc:
            print(f"  [{i}/{len(pending)}] {sid}  ✗ {exc}")
            fail += 1
        if i < len(pending):
            time.sleep(args.interval)

    print(f"\n完成：成功 {ok}，失败 {fail}" + ("（演练模式，未写盘）" if args.dry_run else ""))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())