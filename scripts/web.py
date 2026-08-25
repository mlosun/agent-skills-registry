"""静态站点生成器：把 registry 的全部 skill 生成一个可浏览的 GitHub Pages 网站。

用法（在仓库根目录）::

    python3 -m scripts.web                    # 生成 docs/index.html + docs/skills-data.json
    python3 -m scripts.web --no-open          # 生成但不尝试打开

输出到 ``docs/``（GitHub Pages 从 main 分支的 docs/ 部署）：
- ``docs/index.html``       单页应用（深色现代风：卡片网格 + 搜索 + 筛选 + 标签云 + 详情弹层）
- ``docs/skills-data.json`` 全部 skill 的结构化数据（页面运行时 fetch）

数据来源：index.yaml（id/name/version/risk/category）+ 各 skill-meta.yaml（描述/推荐/标签/来源/SHA）。
零第三方依赖，仅标准库。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .lib.index import load_index, repo_root

# 输出目录（相对仓库根）
OUTPUT_DIR = "docs"

RISK_LABEL = {"clean": "安全", "medium": "中风险", "high": "高风险"}
RISK_COLOR = {"clean": "#3fb950", "medium": "#d29922", "high": "#f85149"}

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Skills 精选源</title>
<style>
  :root {{
    --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3;
    --muted: #8b949e; --accent: #58a6ff; --hover: #1f6feb;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    min-height: 100vh;
  }}
  header {{
    padding: 32px 24px 20px; border-bottom: 1px solid var(--border); background: var(--panel);
  }}
  header h1 {{ font-size: 24px; }}
  header p {{ color: var(--muted); margin-top: 6px; font-size: 14px; }}
  .stats {{ display: flex; gap: 20px; margin-top: 14px; flex-wrap: wrap; }}
  .stat {{ color: var(--muted); font-size: 13px; }}
  .stat b {{ color: var(--text); }}
  .controls {{
    padding: 16px 24px; display: flex; gap: 12px; flex-wrap: wrap; align-items: center;
    border-bottom: 1px solid var(--border); background: var(--bg);
    position: sticky; top: 0; z-index: 10;
  }}
  input, select {{
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    padding: 8px 12px; border-radius: 6px; font-size: 14px; outline: none;
  }}
  input {{ flex: 1; min-width: 200px; }}
  input:focus, select:focus {{ border-color: var(--accent); }}
  .filters {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .chip {{
    background: var(--panel); border: 1px solid var(--border); color: var(--muted);
    padding: 5px 12px; border-radius: 20px; font-size: 12px; cursor: pointer; user-select: none;
  }}
  .chip.active {{ background: var(--hover); border-color: var(--accent); color: #fff; }}
  .tags-cloud {{ padding: 8px 24px 14px; display: flex; gap: 6px; flex-wrap: wrap; }}
  .tag-chip {{
    background: var(--panel); border: 1px solid var(--border); color: var(--muted);
    padding: 3px 10px; border-radius: 12px; font-size: 11px; cursor: pointer;
  }}
  .tag-chip.active {{ background: var(--hover); color: #fff; border-color: var(--accent); }}
  main {{ padding: 20px 24px 60px; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px;
  }}
  .card {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; cursor: pointer; transition: border-color .15s, transform .15s;
    display: flex; flex-direction: column; gap: 10px;
  }}
  .card:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
  .card-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
  .card-name {{ font-size: 16px; font-weight: 600; word-break: break-all; }}
  .risk {{
    font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; white-space: nowrap;
  }}
  .card-meta {{ color: var(--muted); font-size: 12px; }}
  .card-desc {{ color: var(--text); font-size: 13px; line-height: 1.5;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
  .card-tags {{ display: flex; gap: 4px; flex-wrap: wrap; }}
  .card-tag {{
    background: rgba(88,166,255,.12); color: var(--accent); font-size: 11px;
    padding: 2px 8px; border-radius: 8px;
  }}
  .empty {{ color: var(--muted); text-align: center; padding: 40px; }}
  /* 详情弹层 */
  .overlay {{
    position: fixed; inset: 0; background: rgba(0,0,0,.6); display: none; align-items: center;
    justify-content: center; z-index: 100; padding: 20px;
  }}
  .overlay.open {{ display: flex; }}
  .modal {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    max-width: 720px; width: 100%; max-height: 85vh; overflow-y: auto; padding: 24px;
  }}
  .modal h2 {{ font-size: 20px; margin-bottom: 4px; }}
  .modal .source {{ color: var(--muted); font-size: 13px; margin-bottom: 16px; }}
  .modal h3 {{ font-size: 14px; color: var(--muted); margin: 16px 0 6px; }}
  .modal p {{ font-size: 14px; line-height: 1.7; }}
  .modal .rec {{ background: rgba(88,166,255,.08); border-left: 3px solid var(--accent);
    padding: 10px 14px; border-radius: 0 6px 6px 0; }}
  .modal .meta-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr)); gap: 10px; margin-top: 16px; }}
  .meta-item {{ background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; }}
  .meta-item .k {{ color: var(--muted); font-size: 11px; }}
  .meta-item .v {{ font-size: 13px; margin-top: 2px; word-break: break-all; }}
  .close {{ float: right; background: none; border: none; color: var(--muted); font-size: 22px; cursor: pointer; }}
  .close:hover {{ color: var(--text); }}
</style>
</head>
<body>
<header>
  <h1>✨ AI Skills 精选源</h1>
  <p>从 GitHub 精选、安全扫描、中文解读的 Agent Skills 集合</p>
  <div class="stats" id="stats"></div>
</header>

<div class="controls">
  <input id="search" type="text" placeholder="搜索 skill 名称 / 描述 / 推荐理由...">
  <select id="repo-filter"><option value="">全部来源</option></select>
  <select id="risk-filter"><option value="">全部风险</option><option value="clean">安全</option><option value="medium">中风险</option><option value="high">高风险</option></select>
</div>
<div class="tags-cloud" id="tags-cloud"></div>

<main>
  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" style="display:none">没有匹配的 skill</div>
</main>

<div class="overlay" id="overlay">
  <div class="modal" id="modal"></div>
</div>

<script>
const DATA_URL = 'skills-data.json';
let skills = [], activeTag = '', activeRepo = '', activeRisk = '';

async function init() {{
  const res = await fetch(DATA_URL);
  skills = await res.json();
  render();
}}

function esc(s) {{
  return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function riskColor(r) {{ return {json_risk_color}; }}
function riskLabel(r) {{ return {json_risk_label}; }}

function render() {{
  // stats
  const repos = new Set(skills.map(s => s.repo));
  const clean = skills.filter(s => s.risk === 'clean').length;
  document.getElementById('stats').innerHTML =
    `<span class="stat"><b>${{skills.length}}</b> 个 skill</span>
     <span class="stat"><b>${{repos.size}}</b> 个来源仓库</span>
     <span class="stat"><b>${{clean}}</b> 个安全</span>`;

  // repo filter
  const rf = document.getElementById('repo-filter');
  const prev = rf.value;
  rf.innerHTML = '<option value="">全部来源</option>' + [...repos].sort().map(r =>
    `<option value="${{esc(r)}}">${{esc(r)}}</option>`).join('');
  rf.value = prev;

  // tag cloud (top 30)
  const counts = {{}};
  skills.forEach(s => (s.tags || []).forEach(t => counts[t] = (counts[t] || 0) + 1));
  const top = Object.entries(counts).sort((a,b) => b[1]-a[1]).slice(0,30);
  document.getElementById('tags-cloud').innerHTML =
    '<span class="chip' + (activeTag ? '' : ' active') + '" data-tag="">全部</span>' +
    top.map(([t,c]) => `<span class="chip${{activeTag===t?' active':''}}" data-tag="${{esc(t)}}">${{esc(t)}} (${{c}})</span>`).join('');

  // grid
  const q = document.getElementById('search').value.trim().toLowerCase();
  const list = skills.filter(s => {{
    if (activeRepo && s.repo !== activeRepo) return false;
    if (activeRisk && s.risk !== activeRisk) return false;
    if (activeTag && !(s.tags || []).includes(activeTag)) return false;
    if (q) {{
      const hay = (s.name + ' ' + (s.description_zh||'') + ' ' + (s.recommendation||'')).toLowerCase();
      if (!hay.includes(q)) return false;
    }}
    return true;
  }});
  document.getElementById('empty').style.display = list.length ? 'none' : 'block';
  document.getElementById('grid').innerHTML = list.map(s => `
    <div class="card" data-id="${{esc(s.id)}}">
      <div class="card-top">
        <div class="card-name">${{esc(s.name)}}</div>
        <span class="risk" style="background:${{riskColor(s.risk)}}22;color:${{riskColor(s.risk)}}">${{riskLabel(s.risk)}}</span>
      </div>
      <div class="card-meta">${{esc(s.repo)}}${{s.category ? ' · ' + esc(s.category) : ''}}</div>
      <div class="card-desc">${{esc(s.recommendation || s.description_zh || '')}}</div>
      <div class="card-tags">${{(s.tags || []).slice(0,4).map(t => `<span class="card-tag">${{esc(t)}}</span>`).join('')}}</div>
    </div>`).join('');
}}

function setTag(t) {{ activeTag = t; render(); }}

function openModal(id) {{
  const s = skills.find(x => x.id === id);
  if (!s) return;
  const meta = [
    ['来源', s.repo], ['分类', s.category || '-'], ['版本', 'v' + s.version],
    ['风险', riskLabel(s.risk)], ['上游 SHA', (s.upstream_sha||'').slice(0,8) || '-'],
    ['最近同步', s.last_synced_at || '-'],
  ];
  document.getElementById('modal').innerHTML = `
    <button class="close" data-close>&times;</button>
    <h2>${{esc(s.name)}}</h2>
    <div class="source">${{esc(s.id)}}</div>
    <span class="risk" style="background:${{riskColor(s.risk)}}22;color:${{riskColor(s.risk)}}">${{riskLabel(s.risk)}}</span>
    ${{s.recommendation ? `<h3>推荐理由</h3><div class="rec">${{esc(s.recommendation)}}</div>` : ''}}
    ${{s.description_zh ? `<h3>描述</h3><p>${{esc(s.description_zh)}}</p>` : ''}}
    ${{(s.tags||[]).length ? `<h3>标签</h3><div class="card-tags">${{s.tags.map(t => `<span class="card-tag">${{esc(t)}}</span>`).join('')}}</div>` : ''}}
    <div class="meta-grid">${{meta.map(([k,v]) => `<div class="meta-item"><div class="k">${{k}}</div><div class="v">${{esc(v)}}</div></div>`).join('')}}</div>`;
  document.getElementById('overlay').classList.add('open');
}}

function closeModal() {{ document.getElementById('overlay').classList.remove('open'); }}

// 事件委托：点击标签 / 卡片 / 关闭按钮
let lastClick = 0;
document.addEventListener('click', e => {{
  const now = Date.now();
  if (now - lastClick < 300) return; // 防抖：避免标签订到卡片上的重复触发
  const tagEl = e.target.closest('[data-tag]');
  if (tagEl) {{ lastClick = now; setTag(tagEl.getAttribute('data-tag') || ''); return; }}
  const card = e.target.closest('[data-id]');
  if (card) {{ lastClick = now; openModal(card.getAttribute('data-id')); return; }}
  if (e.target.closest('[data-close]') || e.target.id === 'overlay') {{ lastClick = now; closeModal(); }}
}});
document.getElementById('search').addEventListener('input', render);
document.getElementById('repo-filter').addEventListener('change', e => {{ activeRepo = e.target.value; render(); }});
document.getElementById('risk-filter').addEventListener('change', e => {{ activeRisk = e.target.value; render(); }});
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

init();
</script>
</body>
</html>
"""


def build_site(root: Path) -> dict[str, Any]:
    """读取全部 skill 数据，生成站点文件。返回统计。"""
    index = load_index(root)
    skills: list[dict[str, Any]] = []
    for row in sorted(index.get("skills", []), key=lambda r: r.get("id", "")):
        meta_path = root / "skills" / row["id"] / "skill-meta.yaml"
        meta = (
            yaml.safe_load(meta_path.read_text(encoding="utf-8"))
            if meta_path.exists()
            else {}
        )
        parts = row["id"].split("/")
        skills.append(
            {
                "id": row["id"],
                "name": row.get("name", parts[-1]),
                "repo": f"{parts[0]}/{parts[1]}",
                "category": row.get("category") or None,
                "version": row.get("version"),
                "risk": row.get("risk", "clean"),
                "upstream_sha": meta.get("upstream_sha", ""),
                "last_synced_at": meta.get("last_synced_at", ""),
                "description_zh": meta.get("description_zh", ""),
                "recommendation": meta.get("recommendation", ""),
                "tags": meta.get("tags", []),
            }
        )

    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(
        INDEX_HTML.format(
            json_risk_color=json.dumps(RISK_COLOR, ensure_ascii=False),
            json_risk_label=json.dumps(RISK_LABEL, ensure_ascii=False),
        ),
        encoding="utf-8",
    )
    (out_dir / "skills-data.json").write_text(
        json.dumps(skills, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return {"total": len(skills), "output": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="web", description="生成 GitHub Pages 静态站点"
    )
    parser.add_argument("--no-open", action="store_true", help="生成后不打开浏览器")
    args = parser.parse_args(argv)

    root = repo_root()
    stats = build_site(root)
    print(f"已生成 {stats['total']} 个 skill 的站点到 {stats['output']}/")
    print("  - index.html（单页应用）\n  - skills-data.json")
    if not args.no_open:
        import webbrowser

        webbrowser.open((root / OUTPUT_DIR / "index.html").as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
