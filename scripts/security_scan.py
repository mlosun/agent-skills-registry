"""安全扫描引擎：对 skill 目录做静态启发式扫描，产出 risk 标签与报告。

用法（在仓库根目录）::

    python -m scripts.security_scan skills/<owner>/<repo>/<category>/<skill>
    python -m scripts.security_scan <skill_dir> --no-report   # 不写报告，仅打印

流程：遍历目录 → 按文件类型套用 rules.yaml 的规则 → 聚合结论
（任一 block 命中 → high；仅 warn → medium；无命中 → clean）。

阶段3 决策：high 不阻止入库（由后续人工流程处理），本脚本只负责
「打标签 + 出报告」；import.py 调用本模块并把 risk 写进 skill-meta.yaml。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .lib.index import repo_root

# ---- 文件类型判定 ----
_SCRIPT_EXTS = {".sh", ".bash", ".zsh", ".py", ".js", ".ts", ".rb", ".pl", ".php", ".go", ".rs"}
_DOC_EXTS = {".md", ".markdown", ".txt", ".rst"}
_CONFIG_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
_SPECIAL_CONFIG = {"dockerfile", "makefile", "requirements.txt", "package.json", "pyproject.toml"}

# 跳过目录：测试夹具/示例/隐藏目录/依赖安装目录（§2.5.2 排除项，白名单可配置）
_SKIP_DIRS = {".git", ".github", "node_modules", "venv", ".venv", "__pycache__",
              "test", "tests", "fixtures", "fixture", "examples", "example", "samples"}
_SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
              ".woff", ".woff2", ".ttf", ".lock", ".pyc"}

# 每规则每文件最多记录/显示前 N 处命中，防报告爆炸
_MAX_HITS_PER_RULE_PER_FILE = 5


@dataclass
class Finding:
    """一条命中记录。"""

    rule_id: str
    rule_name: str
    severity: str
    file: str  # 相对 skill 目录
    line: int
    match: str
    suggestion: str


@dataclass
class ScanResult:
    """一个 skill 目录的扫描结果。"""

    risk: str  # clean / medium / high
    findings: list[Finding] = field(default_factory=list)
    scanned_files: int = 0
    skipped_files: int = 0

    def summary(self) -> dict[str, Any]:
        dist: dict[str, int] = {}
        for f in self.findings:
            dist[f.rule_id] = dist.get(f.rule_id, 0) + 1
        return {
            "total_hits": len(self.findings),
            "by_rule": dist,
            "scanned_files": self.scanned_files,
            "skipped_files": self.skipped_files,
            "conclusion": self.risk,
        }


# ---------------- 规则加载 ----------------

def load_rules(path: Path | None = None) -> list[dict[str, Any]]:
    """从 rules.yaml 加载规则列表。path 缺省用仓库根 rules.yaml。"""
    p = path or (repo_root() / "rules.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data.get("rules", [])


def _compile_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """把规则里的正则 matcher 预编译。literal / pair 原样保留。"""
    compiled = dict(rule)
    compiled["matchers"] = []
    for m in rule.get("matchers", []):
        m2 = dict(m)
        if m.get("type") == "regex":
            m2["_rx"] = re.compile(m["pattern"], re.IGNORECASE)
        if m.get("type") == "pair":
            m2["_rx_a"] = re.compile(m["a"], re.IGNORECASE)
            m2["_rx_b"] = re.compile(m["b"], re.IGNORECASE)
        compiled["matchers"].append(m2)
    return compiled


# ---------------- 文件类型 ----------------

def _classify(path: Path) -> set[str]:
    """判定文件适用的类型集合。"""
    name = path.name.lower()
    suffix = path.suffix.lower()
    types: set[str] = set()
    if name in _SPECIAL_CONFIG:
        types.add("config")
    if suffix in _SCRIPT_EXTS:
        types.add("script")
    if suffix in _DOC_EXTS:
        types.add("doc")
    if suffix in _CONFIG_EXTS:
        types.add("config")
    if not suffix:
        types.add("executable")
    return types


# ---------------- 扫描 ----------------

def scan_skill(skill_dir: Path, rules: list[dict[str, Any]] | None = None) -> ScanResult:
    """扫描一个 skill 目录，返回聚合结果。"""
    rules = rules if rules is not None else load_rules()
    compiled = [_compile_rule(r) for r in rules]
    findings: list[Finding] = []
    scanned = skipped = 0

    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir).as_posix()
        parts = path.relative_to(skill_dir).parts
        if any(p in _SKIP_DIRS for p in parts[:-1]) or path.suffix.lower() in _SKIP_EXTS:
            skipped += 1
            continue
        if path.name in ("skill-meta.yaml", "security-report.md"):
            continue  # 元数据/报告是脚本自己生成的，跳过避免自我污染
        scanned += 1
        types = _classify(path)

        for rule in compiled:
            ftypes = rule.get("file_types", [])
            if "all" not in ftypes and not (types & set(ftypes)):
                continue
            hits = _scan_file(path, rel, rule, types)
            findings.extend(hits[: _MAX_HITS_PER_RULE_PER_FILE])

    risk = _aggregate(findings)
    return ScanResult(risk=risk, findings=findings, scanned_files=scanned, skipped_files=skipped)


def _scan_file(path: Path, rel: str, rule: dict[str, Any], types: set[str]) -> list[Finding]:
    """对单个文件套一条规则，返回命中（最多每 matcher 前 N 处）。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    hits: list[Finding] = []
    for matcher in rule.get("matchers", []):
        mtype = matcher.get("type", "literal")
        if mtype == "regex":
            rx = matcher["_rx"]
            count = 0
            for i, line in enumerate(lines, start=1):
                m = rx.search(line)
                if m:
                    snippet = m.group(0).strip() or line.strip()
                    hits.append(Finding(rule["id"], rule["name"], rule["severity"],
                                        rel, i, snippet, rule.get("suggestion", "")))
                    count += 1
                    if count >= _MAX_HITS_PER_RULE_PER_FILE:
                        break
        elif mtype == "pair":
            # 组合检测。scope: same_line（a、b 在同一行出现）或 same_file（同文件内都出现，默认）。
            scope = matcher.get("scope", "same_file")
            if scope == "same_line":
                for i, line in enumerate(lines, start=1):
                    if matcher["_rx_a"].search(line) and matcher["_rx_b"].search(line):
                        hits.append(Finding(
                            rule["id"], rule["name"], rule["severity"],
                            rel, i, "组合(同行): a 与 b 同现",
                            rule.get("suggestion", ""),
                        ))
                        break
            else:
                text = "\n".join(lines)
                ma = matcher["_rx_a"].search(text)
                mb = matcher["_rx_b"].search(text)
                if ma and mb:
                    line_no = text.count("\n", 0, ma.start()) + 1
                    hits.append(Finding(rule["id"], rule["name"], rule["severity"],
                                        rel, line_no, f"组合: {ma.group(0)!r} + {mb.group(0)!r}",
                                        rule.get("suggestion", "")))
        else:  # literal
            needle = matcher.get("value", "")
            count = 0
            for i, line in enumerate(lines, start=1):
                if needle in line:
                    hits.append(Finding(rule["id"], rule["name"], rule["severity"],
                                        rel, i, needle, rule.get("suggestion", "")))
                    count += 1
                    if count >= _MAX_HITS_PER_RULE_PER_FILE:
                        break
    return hits


def _aggregate(findings: list[Finding]) -> str:
    """聚合结论：任一 block → high；仅 warn → medium；无 → clean。"""
    if any(f.severity == "block" for f in findings):
        return "high"
    if findings:
        return "medium"
    return "clean"


# ---------------- 报告 ----------------

def render_report(skill_name: str, result: ScanResult) -> str:
    """渲染 markdown 扫描报告文本。"""
    s = result.summary()
    lines = [
        "# 安全扫描报告",
        "",
        f"- skill: `{skill_name}`",
        f"- 扫描时间: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}",
        f"- 扫描文件数: {s['scanned_files']}（跳过 {s['skipped_files']}）",
        f"- **结论: `{s['conclusion']}`**",
        "",
        "## 命中明细",
        "",
    ]
    if result.findings:
        for f in result.findings:
            lines.append(
                f"- `{f.file}`:{f.line} `[{f.rule_id}][{f.severity}]` "
                f"命中: {f.match} 建议: {f.suggestion}"
            )
    else:
        lines.append("无命中。")
    lines += ["", "## 汇总", "", f"- 命中总数: {s['total_hits']}"]
    if s["by_rule"]:
        for rid, n in sorted(s["by_rule"].items()):
            lines.append(f"- {rid}: {n}")
    lines += ["", "> 由 security_scan.py 自动生成，仅供参考，不构成安全保证。"]
    return "\n".join(lines)


def write_report(skill_dir: Path, result: ScanResult) -> Path:
    """把报告写到 skill 目录下的 security-report.md。"""
    report = skill_dir / "security-report.md"
    report.write_text(render_report(skill_dir.name, result), encoding="utf-8")
    return report


# ---------------- CLI ----------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="security_scan", description="扫描 skill 目录安全性")
    parser.add_argument("skill_dir", help="要扫描的 skill 目录")
    parser.add_argument("--no-report", action="store_true", help="不写 security-report.md")
    parser.add_argument("--rules", help="自定义 rules.yaml 路径")
    args = parser.parse_args(argv)

    skill_dir = Path(args.skill_dir).resolve()
    if not skill_dir.is_dir():
        print(f"✗ 目录不存在: {skill_dir}", file=sys.stderr)
        return 1

    rules = load_rules(Path(args.rules) if args.rules else None)
    result = scan_skill(skill_dir, rules)
    s = result.summary()

    print(f"扫描: {skill_dir.name}")
    print(f"  文件: 扫描 {s['scanned_files']}，跳过 {s['skipped_files']}")
    if result.findings:
        for f in result.findings:
            print(f"  [{f.rule_id}][{f.severity}] {f.file}:{f.line} 命中: {f.match}")
    print(f"  结论: {s['conclusion']}")

    if not args.no_report:
        report_path = write_report(skill_dir, result)
        print(f"  报告: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
