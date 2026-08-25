# agent-skills-registry

精选 AI Agent Skills 源仓库（纯镜像模式）。

从 GitHub 搜集 skills 入库，统一维护版本与安全标签，供 `agent-skills-manager` (asm CLI) 作为唯一安装源。

> 项目定位与技术方案见 `agent-skills-studio/docs/`。本仓库文档导航见 [docs/README.md](docs/README.md)。

## 目录结构

```
├── index.yaml       ← 仓库级装订页（每 skill 一行：id/name/version/risk/category）
├── rules.yaml       ← 安全扫描规则库（R1-R5，增删规则不改代码）
├── skills/          ← 实际的 skill 文件（镜像，按 来源仓库/分类/skill 分层）
│   └── <owner>/<repo>/[<category>/]<skill>/
│       ├── SKILL.md          skill 主文件
│       ├── ...               上游辅助文件（原样保留）
│       ├── skill-meta.yaml   skill 级元数据（version/upstream_sha/risk/description_zh/recommendation/tags）
│       └── security-report.md 安全扫描报告（入库时生成）
├── scripts/         ← 维护脚本
│   ├── import.py            入库新 skill（GitHub URL → 镜像 + 元数据 + 扫描）
│   ├── security_scan.py     安全扫描引擎（rules.yaml 驱动，支持 --rescan-all）
│   ├── translate.py         中文描述翻译（DeepSeek 非推理模型，带峰谷时段护栏）
│   ├── enrich.py            内容化（推荐理由 + 场景标签，JSON 结构化）
│   ├── sync.py              上游同步更新（version patch+1 + 重扫）
│   └── web.py               生成 GitHub Pages 站点
├── web/             ← 站点产物（web.py 生成，GitHub Pages 部署，勿手改）
└── docs/            ← 中文文档（架构/贡献/维护/决策/验收，见 docs/README.md）
```

## 使用

```bash
# 入库（合集仓库自动枚举所有 SKILL.md，默认跳过 in-progress/deprecated）
python3 -m scripts.import https://github.com/mattpocock/skills

# 安全扫描单个 skill
python3 -m scripts.security_scan skills/<owner>/<repo>/<category>/<skill>

# 演练不写盘 / 覆盖重入 / 连半成品一起
python3 -m scripts.import <repo> --dry-run
python3 -m scripts.import <repo> --force
python3 -m scripts.import <repo> --include-drafts
```

## 设计要点

- **纯镜像**：`skills/` 里直接存 skill 源码，安装不依赖上游可用性。
- **防重名分层**：`skills/<owner>/<repo>/[<category>/]<skill>/`，保留上游分类与 README。
- **双元数据**：`skill-meta.yaml`（skill 级详情）+ `index.yaml`（仓库级轻量装订页，CLI 一次拉取即可全局扫描）。
- **全 YAML 统一**：SKILL.md frontmatter / skill-meta / index 全用 PyYAML，一套解析器贯穿。
- **安全把关**：入库自动跑静态扫描（R1-R5），结果写 risk 标签 + security-report.md；high 不阻止入库，由人工后续处理。
