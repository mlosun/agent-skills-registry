# agent-skills-registry

精选 AI Agent Skills 源仓库（纯镜像模式）。

从 GitHub 搜集 skills 入库，统一维护版本与安全标签，供 `agent-skills-manager` (asm CLI) 作为唯一安装源。

> 项目定位与技术方案见 `agent-skills-studio/docs/`。

## 目录结构

```
├── index.yaml     ← 总索引（所有 skill 元数据 + 版本 + 风险标签）
├── skills/        ← 实际的 skill 文件（镜像）
└── scripts/       ← 维护脚本
    ├── import.py         入库新 skill
    ├── security_scan.py  入库安全扫描
    ├── translate.py      中文描述翻译
    └── sync.py           同步上游更新
```

（结构随 `index.yaml` 设计演进而调整，见设计讨论。）
