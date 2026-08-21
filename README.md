# XiaoYi 周报 Runner

按人员×周次自动执行小艺周报生成任务的命令行工具。

## 前置条件

- Python 3.10+
- HDC 命令行工具（`hdc` 命令可用）
- 已连接 HarmonyOS 设备

## 目录结构

```
weekly-runner/
├── run_weekly.py                  # 命令行入口
├── standalone_weekly/             # Runner 运行时源码
│   ├── assets/
│   │   └── weekly_config.json     # 默认配置
│   ├── scripts/
│   │   ├── run_weekly.py          # 内部 launcher
│   │   └── runtime/               # 运行时模块
│   └── README.md
├── task/                          # 人员任务数据（必须）
│   ├── 周泽宇/
│   │   ├── zhouzeyu-firstweek/
│   │   │   └── metadata.json
│   │   └── zhouzeyu-secondweek/
│   │       └── metadata.json
│   ├── 苏晚/
│   ├── 唐可/
│   ├── 陈景明/
│   └── 方一诺/
├── deliverables_final/            # 交付数据（必须）
│   └── 周报生成-原始story.xlsx
├── note/                          # mock 数据脚本目录
│   └── data_yangshi/
│       └── jiaoben/
│           └── run_data_mock.py   # 数据清空+推送入口
└── workspace1/                    # 批次产物输出（可选）
    └── weekly-batches-MMDD-vN/
```

## 首次使用须知

克隆仓库后，`note/data_yangshi/jiaoben/` 下有两个脚本包含写死的绝对路径，需要替换为你的本地路径。

### 需要修改的文件

**1. `note/data_yangshi/jiaoben/change_file.py`**

第 9 行 `DEFAULT_PATH`：
```python
# 改前
DEFAULT_PATH = r'D:\Code\Personal\note\data_yangshi\new\周泽宇\第一周'

# 改后（按你的实际路径填写）
DEFAULT_PATH = r'<你的路径>\note\data_yangshi\new\周泽宇\第一周'
```

**2. `note/data_yangshi/jiaoben/make_data.py`**

共 7 处需要修改：

| 行号 | 内容 |
|------|------|
| 18 | `DEFAULT_PATH = r'D:\codes\weekly-runner\note\data_yangshi\new\周泽宇\第一周'` |
| 765 | `open("D:/codes/weekly-runner/note/data_yangshi/jiaoben/mock_responses/git_name_mock.json", ...)` |
| 770 | `open("D:/codes/weekly-runner/note/data_yangshi/jiaoben/mock_responses/stat_mock.json", ...)` |
| 772 | `open("D:/codes/weekly-runner/note/data_yangshi/jiaoben/mock_rules.json", ...)` |
| 775 | `hdc file send D:\\codes\\weekly-runner\\note\\data_yangshi\\jiaoben\\mock_rules.json ...` |
| 779 | `list_entries('D:/codes/weekly-runner/note/data_yangshi/jiaoben/mock_responses')` |
| 790 | help 文本中的示例路径 |

将所有 `D:\codes\weekly-runner` 替换为你的实际项目路径即可。也可以将这些硬编码路径改为基于 `Path(__file__).resolve().parent` 的相对路径，这样换机器就不需要再改了。

### 不需要修改的文件

`run_data_mock.py` 已使用 `Path(__file__).resolve().parent` 计算路径，无需修改。

## 快速开始

```bash
# 查看可用 target
python run_weekly.py --list-targets

# 运行单个 target
python run_weekly.py c1

# 运行多个 target
python run_weekly.py c1 c2 f1

# 运行所有 5 人×2 周 = 10 个 target
python run_weekly.py z1 z2 s1 s2 t1 t2 c1 c2 f1 f2
```

## Target 与人员映射

| Target | 人员 | 周次 |
|--------|------|------|
| z1 | 周泽宇 | 第一周 |
| z2 | 周泽宇 | 第二周 |
| s1 | 苏晚 | 第一周 |
| s2 | 苏晚 | 第二周 |
| t1 | 唐可 | 第一周 |
| t2 | 唐可 | 第二周 |
| c1 | 陈景明 | 第一周 |
| c2 | 陈景明 | 第二周 |
| f1 | 方一诺 | 第一周 |
| f2 | 方一诺 | 第二周 |

## 配置目录

### 必须目录

运行前需确保以下目录存在且包含正确数据：

| 目录 | 说明 |
|------|------|
| `task/` | 按人员组织的任务目录，每人一个子目录，内含 `metadata.json` |
| `deliverables_final/` | 原始交付数据（如 `周报生成-原始story.xlsx`） |
| `note/data_yangshi/jiaoben/` | mock 数据脚本，含 `run_data_mock.py` |

### 目录查找顺序

`run_weekly.py` 会按以下优先级查找 `note/data_yangshi/jiaoben/run_data_mock.py`：

1. `--mock-runner-script` 直接指定的路径
2. `--note-root` 指向的 `data_yangshi/jiaoben/run_data_mock.py`
3. 默认：`<project-root>/note/data_yangshi/jiaoben/run_data_mock.py`

### 自定义目录布局

如果数据不在仓库默认位置，可通过参数指定：

```bash
# 数据在 D:\weekly-data，note 在 E:\xiaoyi-data\note
python run_weekly.py c1 c2 \
  --project-root "D:\weekly-data" \
  --note-root "E:\xiaoyi-data\note"
```

- `--project-root`：包含 `task/` 和 `deliverables_final/` 的根目录
- `--note-root`：指向 `note/` 目录本身
- `--agent-workspace`：批次产物输出根目录（默认为命令执行时的当前目录）
- `--mock-runner-script`：直接指定 `run_data_mock.py`，优先级最高

## 命令行参数

```
usage: run_weekly.py [-h] [--project-root DIR] [--note-root DIR]
                     [--mock-runner-script FILE] [--agent-workspace DIR]
                     [--config FILE] [--device ID] [--date YYYYMMDD]
                     [--dry-run] [--rerun] [--stop-on-error] [--verbose]
                     [--list-targets]
                     [targets ...]
```

| 参数 | 说明 |
|------|------|
| `targets` | 一个或多个 note target（如 c1 c2 f1），支持空格、逗号、中文逗号分隔 |
| `--project-root DIR` | 包含 `task/` 和 `deliverables_final/` 的数据根目录 |
| `--note-root DIR` | note 目录路径 |
| `--mock-runner-script FILE` | 直接指定 `run_data_mock.py`，优先级高于 `--note-root` |
| `--agent-workspace DIR` | 批次产物输出根目录 |
| `--config FILE` | 自定义周报 Runner JSON 配置 |
| `--device ID` | HDC 目标设备 ID |
| `--date YYYYMMDD` | 运行日期 |
| `--dry-run` | 只预演，不操作 HDC |
| `--rerun` | 覆盖已完成的任务 |
| `--stop-on-error` | 遇到错误时停止 |
| `-v, --verbose` | 输出详细日志 |
| `--list-targets` | 列出 target 映射后退出 |

## 批次产物输出

每次运行会在 `--agent-workspace`（或当前目录）下自动创建版本化批次目录：

```
workspace1/
└── weekly-batches-0821-v1/   # 日期-版本号
    ├── <task-id>/
    │   └── xiaoyi_file_runs/
    │       └── outputs/      # 生成的 docx/html 文件
    └── ...
```

版本号自动递增（`v1`, `v2`, ...），不会覆盖已有批次。

## 配置文件

可通过 `--config` 指定 JSON 配置文件覆盖默认值：

```json
{
  "month": "2026-07",
  "calendar_start": "2026-07-01",
  "calendar_end": "2026-07-31",
  "xiaoyi_timeout": 1800,
  "helper_timeout": 300,
  "poll_seconds": 3,
  "task_interval": 3,
  "person_interval": 5,
  "artifact_wait_timeout_seconds": 45,
  "artifact_poll_seconds": 2,
  "artifact_stable_checks": 2,
  "prompt_suffix": ""
}
```

## 产物判定

- docx + html 均存在 = 成功
- 仅 html = 失败（docx 未生成）
- 超时 = 重新执行 Runner
