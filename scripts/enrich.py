"""内容化脚本：为 skill 生成推荐理由 + 场景标签（+ 顺带补中文描述）。

用法（在仓库根目录）::

    python3 -m scripts.enrich                          # 为全部缺内容的 skill 生成
    python3 -m scripts.enrich --skill brainstorming      # 只处理指定 skill（可多次）
    python3 -m scripts.enrich --dry-run                 # 预览：哪些缺内容，不调用 API
    python3 -m scripts.enrich --force                   # 高峰时段强制运行（默认拒绝）

为每个 skill 一次 LLM 调用产出三样，写入 skill-meta.yaml：
- ``recommendation``：推荐理由（1-2 句中文，为什么值得装）
- ``tags``：场景标签（3-6 个中文标签，适合什么场景）
- ``description_zh``：中文描述（仅当缺失时补；已有人工/机器翻译的保留）

幂等：recommendation 和 tags 都已存在的 skill 跳过；已有的 description_zh 不被覆盖。
时段护栏与 translate.py 相同（DeepSeek 峰谷定价：工作日 09-12/14-18 全价拒绝，
其余含周末 5 折）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .lib.index import load_meta, repo_root, save_meta
from .lib.skillfile import read_description
from .translate import (
    DEFAULT_BASE_URL,
    DEFAULT_INTERVAL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    is_peak_hour,
    resolve_api_key,
)

MAX_TOKENS = 800  # 一次产出三样，输出空间比纯翻译大

PROMPT_TEMPLATE = """你是 AI Skill 精选源的编辑。根据下面这个 skill 的信息，产出中文内容化素材。

skill 名称：{name}
英文描述：{description}

请以 JSON 对象输出（不要输出其他任何文字），包含三个字段：
1. "description_zh": 英文描述的中文直译（忠实原意，保留技术术语；{need_desc}）
2. "recommendation": 推荐理由，1-2 句中文，说明"为什么值得装/适合什么情况"，要具体有信息量
3. "tags": 场景标签，3-6 个中文短标签数组（如 ["编码","代码审查","工作流"]），覆盖主要使用场景

JSON 示例：
{{"description_zh": "...", "recommendation": "...", "tags": ["...", "..."]}}
"""


# ---------------- LLM 调用 ----------------


def enrich_one_call(
    name: str,
    description: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    need_desc: bool,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """调用 LLM，返回 {description_zh, recommendation, tags}。"""
    if not description.strip():
        raise ValueError("SKILL.md 缺少英文 description，无法内容化")
    need_desc_text = "翻译它" if need_desc else "保留为空字符串"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": PROMPT_TEMPLATE.format(
                    name=name, description=description, need_desc=need_desc_text
                ),
            },
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.5,
        "stream": False,
        "response_format": {"type": "json_object"},  # DeepSeek 支持结构化输出
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
    return _parse_result(text, name)


def _parse_result(text: str, name: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON；容错处理（可能带代码块/多余文字）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试提取第一个 { ... }
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError(f"LLM 未返回 JSON: {text[:200]!r}") from None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"LLM 返回的 JSON 无法解析: {text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"LLM 返回非对象: {str(data)[:200]!r}")
    recommendation = str(data.get("recommendation", "")).strip()
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = [str(t).strip() for t in tags if str(t).strip()]
    if not recommendation:
        raise RuntimeError(f"LLM 未生成 recommendation（{name}）")
    return {
        "description_zh": str(data.get("description_zh", "")).strip(),
        "recommendation": recommendation,
        "tags": tags,
    }


# ---------------- 主流程 ----------------


def find_pending_skills(root: Path) -> list[tuple[str, Path]]:
    """扫描所有 skill-meta.yaml，返回需要内容化的 (skill_id, meta_path)。

    需要 = recommendation 或 tags 缺失（description_zh 只在缺失时才补，不算 pending 条件）。
    """
    pending: list[tuple[str, Path]] = []
    for meta_file in sorted((root / "skills").rglob("skill-meta.yaml")):
        sid = meta_file.parent.relative_to(root / "skills").as_posix()
        meta = load_meta(root, sid)
        if meta is None:
            continue
        if meta.get("recommendation", "").strip() and meta.get("tags"):
            continue  # 已有完整内容
        pending.append((sid, meta_file))
    return pending


def enrich_skill(
    root: Path,
    sid: str,
    *,
    api_key: str,
    model: str,
    base_url: str,
    dry_run: bool,
) -> tuple[str, str]:
    """内容化单个 skill。返回 (sid, 结果描述)。"""
    skill_dir = root / "skills" / Path(*sid.split("/"))
    meta = load_meta(root, sid) or {}
    name = meta.get("name", sid.split("/")[-1])
    description = read_description(skill_dir / "SKILL.md")

    need_desc = not meta.get("description_zh", "").strip()
    has_content = meta.get("recommendation", "").strip() and meta.get("tags")

    if dry_run:
        what = []
        if need_desc:
            what.append("补中文描述")
        if not has_content:
            what.append("生成推荐+标签")
        return sid, f"✓ 待处理（{' + '.join(what)}）"

    result = enrich_one_call(
        name,
        description,
        api_key=api_key,
        model=model,
        base_url=base_url,
        need_desc=need_desc,
    )
    meta["recommendation"] = result["recommendation"]
    meta["tags"] = result["tags"]
    if need_desc and result["description_zh"]:
        meta["description_zh"] = result["description_zh"]
    save_meta(root, sid, meta)
    return sid, f"✓ 已生成推荐 + {len(result['tags'])} 标签" + (
        " + 中文" if need_desc else ""
    )


# ---------------- CLI ----------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="enrich", description="为 skill 生成推荐理由 + 场景标签"
    )
    parser.add_argument(
        "--skill", action="append", help="只处理指定 skill id（可多次）"
    )
    parser.add_argument("--dry-run", "-n", action="store_true", help="预览，不调用 API")
    parser.add_argument("--force", action="store_true", help="高峰时段强制运行")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"模型（默认 {DEFAULT_MODEL}）"
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API 地址")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"两次请求间隔秒数（默认 {DEFAULT_INTERVAL}）",
    )
    args = parser.parse_args(argv)

    if is_peak_hour():
        if args.force:
            print("⚠ 当前为高峰时段（全价），--force 强制继续")
        else:
            print(
                "✗ 当前为 DeepSeek 高峰时段（全价）。允许运行时段："
                "工作日 12:00-14:00 / 18:00-次日 09:00，及周末全天。"
                "可用 --force 强制（不推荐）。"
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

    pending = find_pending_skills(root)
    if args.skill:
        wanted = set(args.skill)
        pending = [(sid, mp) for sid, mp in pending if sid in wanted]
        missing = wanted - {sid for sid, _ in pending}
        for sid in sorted(missing):
            print(f"  · {sid}  不在待处理列表（可能已有内容）")

    if not pending:
        print("（没有需要内容化的 skill，全部已有推荐+标签）")
        return 0
    print(f"待处理 {len(pending)} 个 skill：")

    ok = fail = 0
    for i, (sid, _mp) in enumerate(pending, start=1):
        try:
            _, msg = enrich_skill(
                root,
                sid,
                api_key=api_key,
                model=args.model,
                base_url=args.base_url,
                dry_run=args.dry_run,
            )
            print(f"  [{i}/{len(pending)}] {sid}  {msg}")
            ok += 1
        except (RuntimeError, ValueError) as exc:
            print(f"  [{i}/{len(pending)}] {sid}  ✗ {exc}")
            fail += 1
        if i < len(pending):
            time.sleep(args.interval)

    print(
        f"\n完成：成功 {ok}，失败 {fail}"
        + ("（演练模式，未写盘）" if args.dry_run else "")
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
