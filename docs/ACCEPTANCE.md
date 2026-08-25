# 阶段 1-3 验收清单

> 这是 agent-skills-registry 已完成阶段的验收指引（阶段 0 初始化 → 1 数据层 → 2 入库 → 3 安全扫描）。
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

## 4. 一致性自检（数据不漂移）

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

## 5. 静态检查

```bash
# 语法编译检查
python3 -m py_compile scripts/import.py scripts/security_scan.py scripts/lib/index.py
# 预期：无输出、exit 0

# git 工作区应干净
git status --short   # 预期：空输出
```

## 验收通过标准

- [ ] 两套测试（test_index / test_security_scan）全绿
- [ ] `--dry-run` 不写盘、重复导入幂等
- [ ] 恶意样例能命中 R1-R4（high），正常代码不误报（clean）
- [ ] index.yaml 与磁盘目录一一对应，risk/version 双份一致
- [ ] `python3 -m py_compile` 无错、git 工作区干净
