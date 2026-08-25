# agent-skills-registry 架构演进定稿

- **日期**：2026-08-24
- **状态**：已批准（用户逐点确认）
- **上游规格**：`agent-skills-studio/docs/02-产品与技术.md` §2（纯镜像 registry 设计）

> 本文是对上游规格 §2.2 / §2.3 的两处主动演进，落地于 `agent-skills-registry` 仓库搭建过程。

---

## 背景

`02-产品与技术.md` §2 定义了 registry 为「纯镜像 + 单一 `index.yaml` 总索引」。在动手前对两处设计做了演进探讨，核心动因是**真实输入**（`mattpocock/skills` 合集仓库）暴露了原设计未覆盖的问题：

- skills 会非常多、且跨仓库**大量重名**（`code-review`/`handoff`/`teach` 等通用名几乎每个合集都有）；
- 合集仓库**自带多层结构**（`engineering/`、`productivity/`…），单 skill 仓库与合集仓库形态差异大；
- 单一巨型 `index.yaml` 在几十上百（乃至 200+）skills 时会产生整文件重写、并发冲突、可读性差等问题。

---

## 决策 1 · `skills/` 目录按来源仓库分层

**结构**：`skills/<owner>/<repo>/[<category>/]<skill-name>/`

```
skills/mattpocock/skills/engineering/code-review/
├── SKILL.md
└── skill-meta.yaml
skills/mattpocock/skills/productivity/grill-me/
├── SKILL.md
└── skill-meta.yaml
```

规则：

- **顶层 = 来源仓库**（`<owner>/<repo>`），从根本上避免跨仓库重名。
- **后续层级 = 上游原样保留**，包括分类目录（`engineering/`、`productivity/`…）及其中的 **README.md**（分类说明文档一并镜像）。
- 单 skill 仓库则落在 `skills/<owner>/<repo>/<skill-name>/`（无 category 层）。
- **唯一标识**（`id`）= `owner/repo/[category/]skill-name`。

代价（镜像 + 溯源模式下可接受）：路径变长；同一逻辑 skill 从多来源来会存多份，由索引层面标注而非目录去重。

**对 `import.py` 的连带要求**：必须能识别**合集仓库 ≠ 单 skill 仓库**——合集仓库要遍历内部所有 SKILL.md 逐个入库，而非只找一个。

---

## 决策 2 · 分布式元数据 + 轻量索引

两级 YAML 分工：

| 文件 | 层级 | 角色 | 维护侧 |
|------|------|------|--------|
| `skill-meta.yaml` | skill 级 | 单 skill 详情（version / upstream_sha / risk / description_zh / source） | import/sync 写，CLI 按需读 |
| `index.yaml` | 仓库级 | 装订页（schema version + updated_at + 每 skill 一行精简快照） | 由各 skill 的 meta 汇总生成，供全局扫描 |

### skill-meta.yaml（每个 skill 目录内一份）

```yaml
name: code-review
source:
  owner: mattpocock
  repo: skills
  category: engineering
  path_in_repo: skills/engineering/code-review
upstream_branch: main
upstream_sha: abc1234
version: "1.0.0"
last_synced_at: "2026-08-24"
risk: clean        # clean / medium / high
description_zh: "按两轴审查代码改动：标准 + 规格…"
```

> `description_en` **不重复维护**——从 `SKILL.md` frontmatter 读取（源已自带）。

### index.yaml（仓库级装订页）

```yaml
version: 1
updated_at: "2026-08-24T09:00:00Z"
skills:
  - id: mattpocock/skills/engineering/code-review
    name: code-review
    version: "1.0.0"
    risk: clean
    category: engineering
  - id: mattpocock/skills/productivity/grill-me
    name: grill-me
    version: "1.0.0"
    risk: clean
    category: productivity
```

**分界**：`skill-meta.yaml` 管「单点精确读」（变了只动一个文件，写侧主力）；`index.yaml` 管「全局聚合扫描」——CLI 的 `check/update/list` 用**一个文件** O(1) 次请求扫出全部 skill 的 version/risk，不必为每次全量扫描做 O(N) 次文件请求。

**index.yaml 永不做详情**：详情一律在 `skill-meta.yaml`；单 skill 更新只按 id 改写 index 里那一行，天然错开并发冲突。`version: 1` 为 schema 版本号，供 CLI 做格式兼容判断（与上文 index 的 `updated_at` 同属「仓库级」信息，meta 无法承担）。

---

## 决策 3 · 全链路统一 YAML

格式归一理由：

1. 本项目 YAML 已无处不在——`SKILL.md` frontmatter、CLI 本地 `~/.asm/skills.yaml`、`index.yaml`；**JSON 反而是异类**。
2. 全链路只用 `PyYAML`（`yaml.safe_load` 亦能直接解析 JSON 内容），一个解析器、一套心智。
3. YAML 可注释、可读性好，适配 registry「人工审核的精选索引」性质（§2.6 周维护有人工确认/改描述）。
4. 原规格与 CLI 已锁定 `PyYAML`，零新增依赖。

> JSON 的胜出场景仅当 meta 交由强类型静态语言 / 前端 JS 消费且需严格无歧义解析——backend 为 Python，不适用。

---

## 对阶段 1-2 的实现约束

- **阶段 1**：封装 index / meta 的 YAML 读写模块（唯一入口），约定 `skill-meta.yaml` + `index.yaml` 的 schema；提供「按 id 更新 index 单行」的能力。
- **阶段 2（import.py）**：输入 GitHub URL → 识别合集/单 skill → 克隆并校验 SKILL.md → 复制到分层路径 → 生成 `skill-meta.yaml` → 更新 `index.yaml` 对应行 → （预留安全扫描挂钩）。依赖仅 `PyYAML` + `git`。
- **阶段 3-5**：`security_scan.py`（§2.5 规则）挂 import 之后；`translate.py` 只补 `description_zh`；`sync.py` 对比 `upstream_sha` 并 version+1。

---

## 决策 4 · 同步/翻译分工（2026-08-25 确认）

**问题**：GitHub Action 定时跑 sync，但 Action 环境无本地 DeepSeek key，翻译是否需要/能否自动化？

**结论**：当前保持「自动同步 + 手动翻译」分工。

| 任务 | 工具 | 需要 API key？ | 运行方式 |
|------|------|--------------|---------|
| 同步上游（拉新代码 / version+1 / 重扫） | `sync.py` | ❌ 不需要（纯 git+文件+静态扫描） | ✅ GitHub Action 每天 03:00 UTC 自动跑 |
| 翻译描述（英→中） | `translate.py` | ✅ DeepSeek key | ⏰ 本地手动、低谷时段运行 |

**理由**：
1. `sync.py` 无任何 LLM/API 依赖，Action 环境（无 key）可正常运行——已实测通过。
2. `translate.py` 依赖 key + 受 DeepSeek 峰谷定价限制（工作日北京时间 09-12/14-18 全价，其余含周末 5 折）；Action cron 为 UTC 时间，自动翻译易撞高峰（贵）。
3. 权衡：自动同步保证内容不过时，手动翻译控制成本。

**未来改进（已计划，待项目稳定后）**：
- 在 GitHub Settings → Secrets 配置 `DEEPSEEK_API_KEY`。
- Action 中用 `${{ secrets.DEEPSEEK_API_KEY }}` 注入 translate 步骤。
- 处理 cron UTC → 北京时间高峰规避（或接受全价自动翻译）。
- 届时改为「全自动同步 + 全自动翻译」。
