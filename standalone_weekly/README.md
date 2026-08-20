# 独立周报 Runner

本目录包含 `run_weekly.py` 使用的可读 Runner 源码和默认配置，不依赖
`skills/`。分发时需要同时复制仓库根目录的 `run_weekly.py` 和本目录。

推荐布局：

```text
<runner_root>/
├── run_weekly.py
└── standalone_weekly/
    ├── assets/weekly_config.json
    └── scripts/
        ├── run_weekly.py
        └── runtime/*.py
```

业务数据可以放在其他位置：

```powershell
python -B "<runner_root>\run_weekly.py" c1 c2 `
  --project-root "D:\weekly-data" `
  --note-root "E:\xiaoyi-data\note" `
  --agent-workspace "F:\weekly-results"
```

- `--project-root`：包含 `task/` 和 `deliverables_final/`。
- `--note-root`：指向 `note/` 本身。
- `--agent-workspace`：批次产物输出位置。
- `--mock-runner-script`：可直接指定 `run_data_mock.py`，优先于
  `--note-root`。

需要从 Skill 的最新版源码刷新本目录时运行：

```powershell
python -B .\scripts\build_standalone_weekly.py
```

该同步脚本只读取 Skill，不会修改 Skill，也不会生成 Base64 内容。
