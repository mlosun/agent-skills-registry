# 阶段 1-6 验收清单

> 这是 agent-skills-registry 已完成阶段的验收指引（阶段 0 初始化 → 1 数据层 → 2 入库 → 3 安全扫描 → 4 中文翻译 → 5 上游同步 → 6 内容化/端到端）。
> 每次改完相关脚本后，按此清单自测。

## 0. 前置条件

```bash
cd /Users/mlosun/CNB/agent-skills-registry
python3 --version        # 需 ≥ 3.10
python3 -c "import yaml" # 需已安装 PyYAML
git --version            # import.py 依赖 git clone
```

## 1. 阶段 1 · index/meta 数据层

```bash
# 冒烟测试（index.yaml / skill-meta.yaml 的读写、upsert、remove、防路径穿越）
python3 -m scripts.tests.test_index
```

预期：`✅ 阶段 1 冒烟测试全部通过`

## 2. 阶段 2 · import.py 入库

```bash
# 演练不写盘（验证合集识别 + 跳过 in-progress/deprecated）
python3 -m scripts.import https://github.com/mattpocock/skills --dry-run
# 预期：输出 engineering/misc/productivity 下的 skills，不含 in-progress/deprecated

# 真实入库（已入库会温和跳过）
python3 -m scripts.import https://github.com/mattpocock/skills
# 预期：输出 "已存在（用 --force 覆盖…）" —— 证明幂等

# 覆盖重入
python3 -m scripts.import https://github.com/mattpocock/skills --force
# 预期：输出 "已入库 v1.0.0（risk=clean）"
```

验证产物：

```bash
ls skills/mattpocock/skills/engineering/code-review/
# 预期：SKILL.md + skill-meta.yaml + security-report.md（+ 上游辅助文件）
cat index.yaml   # 预期：version/updated_at + 每 skill 一行 id/name/version/risk/category
```

## 3. 阶段 3 · security_scan.py 安全扫描

```bash
# 引擎测试（恶意检出 + 正常不误报 + 同行组合 + 报告自防护）
python3 -m scripts.tests.test_security_scan
# 预期：`✅ security_scan 测试全部通过`

# 单 skill 扫描（生成/更新 security-report.md）
python3 -m scripts.security_scan skills/mattpocock/skills/engineering/code-review
```

## 4. 阶段 4 · translate.py 中文翻译

前置：需要 DeepSeek API key（自动从 `~/.pi/agent/auth.json` 的 `deepseek.key` 读取，或设环境变量 `DEEPSEEK_API_KEY`）。

```bash
# 引擎测试（时段边界 + key 解析 + LLM 调用构造(mock) + 幂等扫描）
python3 -m scripts.tests.test_translate
# 预期：`✅ translate 测试全部通过`

# 预览：哪些待翻译、各多少字符（不调用 API）
python3 -m scripts.translate --dry-run
# 预期：输出 "待翻译 N 个 skill" 及每个的字符数

# 真实翻译单个（避开高峰时段，或 --force 强制）
python3 -m scripts.translate --skill mattpocock/skills/engineering/code-review
# 预期：输出 "✓ 已翻译: …"
```

时段护栏验证（DeepSeek 峰谷定价）：

```bash
# 工作日高峰时段（09:00-12:00 / 14:00-18:00 北京时间）运行应被拒绝
python3 -m scripts.translate
# 预期：输出 "✗ 当前为 DeepSeek 高峰时段（全价）…" 并退出码 1

# 低谷时段（工作日 12-14 / 18-次日 9，或周末）运行应放行
python3 -m scripts.translate --dry-run
# 预期：输出 "✓ 当前为低谷时段（5 折）"
```

## 5. 阶段 5 · sync.py 上游同步 + GitHub Action

```bash
# sync 引擎测试（patch 版本步进 + 合集发现 + 无更新/更新路径）
python3 -m pytest scripts/tests/test_sync.py -v
# 预期：4 passed（含 version+1、保留 description_zh、重扫）

# 演练：报告哪些仓库有更新，不写盘
python3 -m scripts.sync --dry-run
# 预期：输出每个来源仓库的状态（无更新时："无更新 (SHA xxxx)"）

# 真实同步（有上游更新时 version patch+1）
python3 -m scripts.sync
```

GitHub Action（`check-updates.yml`，每天 03:00 UTC 自动跑）：

```bash
# 验证工作流在远端仓库可用（需已 push）
gh workflow run check-updates.yml --repo mlosun/agent-skills-registry
gh run list --repo mlosun/agent-skills-registry --workflow=check-updates.yml
# 预期：run 显示 success；日志含 "无上游更新，跳过提交" 或 "已提交并推送同步结果"
```

## 6. 阶段 6 · enrich.py 内容化 + 端到端

```bash
# enrich 引擎测试（JSON 解析容错 + 幂等扫描 + LLM 调用构造 mock）
python3 -m pytest scripts/tests/test_enrich.py -v
# 预期：7 passed

# 预览：哪些 skill 缺推荐+标签（不调用 API）
python3 -m scripts.enrich --dry-run --force
# 预期：输出待处理 skill 列表及需要补的内容（高峰时段需 --force 预览）

# 真实生成（低谷时段运行，避免 --force 全价）
python3 -m scripts.enrich

# 端到端验证报告
# 见 docs/END_TO_END.md（入库→扫描→翻译→同步→内容化 五环闭环）
```

## 7. 一致性自检（数据不漂移）

```bash
python3 - <<'PY'
import yaml
from pathlib import Path
idx = yaml.safe_load(open('index.yaml'))
ids = {r['id'] for r in idx['skills']}
disk = {str(m.parent.relative_to('skills')) for m in Path('skills').rglob('skill-meta.yaml')}
print('index 行数:', len(ids), '| 磁盘 meta 数:', len(disk), '| 一一对应:', ids == disk)
mismatch = []
for r in idx['skills']:
    meta = yaml.safe_load((Path('skills') / r['id'] / 'skill-meta.yaml').read_text())
    if meta.get('risk') != r.get('risk') or meta.get('version') != str(r.get('version')):
        mismatch.append(r['id'])
print('risk/version 双份不一致:', len(mismatch))
PY
# 预期：一一对应: True，不一致: 0
```

## 8. 静态检查

```bash
# 语法编译检查
python3 -m py_compile scripts/*.py scripts/lib/*.py
# 预期：无输出、exit 0

# git 工作区应干净
git status --short   # 预期：空输出
```

## 验收通过标准

- [ ] 五套测试（test_index / test_security_scan / test_translate / test_sync / test_enrich）全绿（24 项）
- [ ] `--dry-run` 不写盘、重复导入幂等、sync 无更新不 version+1
- [ ] 恶意样例能命中 R1-R4（high），正常代码不误报（clean）
- [ ] 高峰时段默认拒绝（退出码 1），低谷放行；幂等跳过已翻译
- [ ] sync 更新时 version patch+1、保留 description_zh、重扫报告
- [ ] enrich 幂等（已有推荐+标签跳过），JSON 解析容错
- [ ] `security_scan --rescan-all` 全量重扫并同步 risk 到 meta/index
- [ ] index.yaml 与磁盘目录一一对应，risk/version 双份一致
- [ ] `python3 -m py_compile` 无错、git 工作区干净
- [ ] GitHub Action 可手动触发且 success
- [ ] 端到端报告（docs/END_TO_END.md）与仓库实际状态一致
