# HALO Trace Converter

将 `event + payload` 事件流 JSONL 转换为 HALOAgent 可读取的扁平 Span JSONL。

转换器输出 AGENT、LLM、TOOL 三类 Span，并尽可能保留模型消息、工具定义、Tool
输入输出等诊断信息。当前版本同时处理错误状态、事件关联、父子关系、多运行拆分、
时间校验、完整源事件留存和可选属性长度限制。

## 输入格式

输入必须是一行一个 JSON 对象：

```json
{"event":"model_input","timestamp":"2026-07-23T08:00:00Z","payload":{"messages":[]}}
```

每行必须包含：

- `event`：字符串；
- `payload`：JSON 对象；
- `timestamp`：ISO 8601 字符串、Unix 秒或 Unix 毫秒。

时间戳必须有效，并在同一运行/会话内按照事件发生顺序非递减排列。多会话文件允许
不同会话的事件在文件中交错。无效时间戳、缺失时间戳或同一会话内逆序会直接报错。

识别的事件：

```text
agent_start
agent_end
model_input
model_output
tool_call
tool_result
session_lifecycle
subagent_completed
```

`session_lifecycle`、`subagent_completed` 和其他非 Span 事件保存在 AGENT Span 的
`source.events` 中。其他未知事件还会在 stderr 给出警告；使用 `--strict-events`
可以改为遇到未知事件立即失败。

## 快速使用

使用启动脚本：

```bash
sh ./convert.sh INPUT [OUTPUT] [options]
```

如果系统的 Python 命令不是 `python`：

```bash
PYTHON_BIN=python3 sh ./convert.sh INPUT [OUTPUT]
```

转换单个文件：

```bash
python convertToHaloTrace.py mytrace.jsonl converted/traces.jsonl \
  --project-id my-project
```

转换目录下的所有 `.jsonl` 文件：

```bash
python convertToHaloTrace.py input_logs converted_logs \
  --project-id my-project
```

目录模式会递归查找输入目录中的所有 `.jsonl` 文件，并在输出目录中保留相对路径。空文件或只含空白行的文件会显示 `[skip] ... empty JSONL` 并被自动跳过，不会中断其他文件的转换。其他文件仍应是本转换器支持的 `event + payload + timestamp` 事件日志；提示词文件、已经转换过的 HALO JSONL 等不同格式的非空文件应先移出或分类存放，否则转换器会明确报错。

省略输出参数时：

```text
mytrace.jsonl -> mytrace.halo.jsonl
input_logs    -> input_logs-halo-traces
```

完整参数：

```text
--project-id ID
    写入 inference.project_id，默认值为 "converted trace"。

--trace-id ID
    强制单次运行使用指定 trace_id。检测到多个运行时拒绝转换，避免错误合并。
    只能用于单文件输入。

--skip-bad-lines
    跳过无法解析成 JSON 对象的行。它不会跳过字段错误、时间错误或语义校验错误。

--strict-events
    未知事件由“警告后忽略”改为直接失败。

--max-attribute-chars N
    每个字符串 attribute 和 status message 的最大字符数。
    默认值 0 表示不截断、完整保留。
```

直接运行就是不截断的完整内容模式：

```bash
python convertToHaloTrace.py input.jsonl output.jsonl
```

模型和工具的主要内容只在 HALO 标准 attributes 中保存一次：

```text
llm.input_messages
llm.output_messages
llm.tools
llm.system_prompt
input.value
output.value
```

为了保留原事件的顶层信息和未映射扩展字段，转换器还会写入体积较小的上下文：

```text
source.model_input.context
source.model_output.context
source.tool_call.context
source.tool_result.context
source.events
```

`*.context` 不会再复制 messages、system prompt、tools、assistant、Tool 参数或
Tool 输出。它只保存原事件的 `event`、原始 `timestamp`、`session_id`、
`agent_role`、其他顶层字段、字段到 HALO attribute 的映射，以及未被标准字段消费的
payload 扩展字段。没有对应 LLM/Tool Span 的事件则只在 `source.events` 中保存一次。

## 转换语义

### LLM 配对

`model_input + model_output` 生成一个 LLM Span。

如果事件提供以下任一关联字段，转换器会按关联 ID 配对，支持交错事件：

```text
request_id
model_call_id
call_id
message_id
```

关联字段可以位于 payload 顶层，也可以位于 `assistant`、`request`、`response` 或
`metadata` 对象中。

当 `model_output` 没有关联 ID 时，按 FIFO 与最早的等待输入配对。若输出携带了关联
ID，但找不到对应输入，会生成无输入的 LLM Span并给出警告；未匹配的输入会生成
`STATUS_CODE_ERROR` 的 unfinished Span。

如果源日志完全没有关联 ID，转换器无法从任意乱序事件中推断真实请求身份；FIFO
只适用于输出顺序与输入顺序相同的日志。

### Tool 配对和 Span ID

`tool_call + tool_result` 生成一个 TOOL Span。

- 使用 `tool_call_id` 关联 Tool call 和 result；
- 重复 call ID 使用等待队列处理，不再造成重复 Span ID；
- 缺少 call ID 时使用 FIFO 配对，并生成 `generated-<UUID>` 作为 call ID；
- 每个 TOOL Span 使用独立 UUID 作为 `span_id`；
- 原始/生成的 Tool call ID 同时写入：

```text
tool.call_id
tool.call.id
```

### 父子关系

正常关系为：

```text
AGENT
  └─ LLM
      └─ TOOL
```

转换器优先从 `model_output.tool_calls_decided`、`tool_calls` 或
`assistant.content` 中提取 Tool call ID，并将对应 Tool 挂到决定该调用的 LLM。
没有显式 Tool ID 时，Tool 会挂到当前事件段最近的 LLM；无法确定 LLM 时才回退到
AGENT 根节点。

LLM 的结束时间会扩展到其 Tool 子节点结束时间，保证父 Span 时间范围覆盖子节点。

### 错误状态

Tool 和 LLM 共用错误识别逻辑，会递归检查常见字段，包括：

```text
error / error_message / errCode / errMsg / exception
is_error / failed / cancelled / timeout
success == false
ok == false
exitCode != 0
returncode != 0
HTTP status >= 400
status/state/finish_reason/stop_reason 为失败状态
```

没有明确成功信号时，非空 `stderr` 也视为失败。若结果明确包含
`success=true` 或 `ok=true`，且没有其他失败信号，仅有 stderr 不会自动判错，以
避免把警告输出误判为失败。

任一子 Span 失败时，AGENT 根 Span 也标记为 `STATUS_CODE_ERROR`。

### 多运行输入

一个输入文件可以包含多个顺序执行的 Agent 运行。转换器会根据以下边界拆分：

- 新的 `agent_start`；
- 前一次 `agent_end` 后出现新事件；
- `run_id` 发生变化。

带有 `agent_start/run_id` 的运行保持为一个 Trace，即使
`subagent_completed` 携带不同的子会话 ID。没有显式运行边界的旧日志则按
`session_id` 分组；因此不同会话可以交错，每个会话只生成一个根 Span。

每个运行生成独立 AGENT 根 Span 和 Trace。Trace ID 依次取：

1. payload 或事件顶层的 `run_id`；
2. `session_id`；
3. 自动生成的 UUID。

## 输出格式

输出仍是一行一个 JSON Span，并包含 HALO 使用的标准顶层字段：

```text
trace_id
span_id
parent_span_id
trace_state
name
kind
start_time
end_time
status
resource
scope
attributes
```

每个 Span 至少包含：

```text
inference.export.schema_version
inference.project_id
inference.observation_kind
openinference.span.kind
```

此外，每个 AGENT Span 带有 `source.event_count`。转换器会保留每个源字段的去向：
主要内容进入 HALO 标准 attributes，未映射内容进入相应的 `*.context`，其他事件进入
`source.events`。

转换器写文件前还会检查：

- 顶层字段和内部结构；
- status code；
- 时间戳合法且 `start_time <= end_time`；
- 同一 Trace 内 Span ID 唯一；
- parent Span 存在于同一 Trace；
- 父子关系无环，且父 Span 时间范围覆盖子节点；
- 每个 Trace 恰好有一个根 Span；
- Observation kind 属于 AGENT、LLM、TOOL。

这些校验用于防止转换器自行产生结构损坏的 Trace，但不能证明原始事件内容真实或
完整。

## 使用 HALOAgent 验证

从本仓库根目录运行：

```bash
python -c "import json,sys; from pathlib import Path; sys.path.insert(0,'haloagent'); from engine.traces.models.canonical_span import SpanRecord; p=Path('converted/traces.jsonl'); rows=[json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]; [SpanRecord.model_validate(x) for x in rows]; print('validated',len(rows))"
```

`SpanRecord` 会验证 HALO 的数据模型。正式使用时还应通过
`TraceIndexBuilder`/`TraceStore` 建索引并读取一次，以验证实际数据路径。

## 完整保留、隐私和长度限制

这个转换器专用于当前 `event+payload` 日志，默认不脱敏、不截断。system prompt、
模型消息、Tool 参数/结果、stdout 和 stderr 都会写入对应的 HALO 标准 attribute，
但不会在 `source.*` 中重复保存一份大体积副本。

如需人为限制单个字符串 attribute，可显式指定：

```bash
python convertToHaloTrace.py input.jsonl output.jsonl \
  --max-attribute-chars 65536
```

启用限制后不能再保证逐字段完整恢复。将完整日志交给外部模型前，应在转换流程之外
按实际数据规范执行隐私检查。

## 已知限制

- 没有关联 ID 的乱序 LLM 请求只能按 FIFO 配对；
- 无法从事件流中不存在的信息恢复准确因果关系；
- `stderr` 是否代表失败取决于生产者约定，转换器采用“明确成功优先”的保守规则；
- `--skip-bad-lines` 可能让运行事件不完整，使用后应检查 `skipped` 数量。

## 代码结构

`convertToHaloTrace.py` 是稳定主入口。直接调用它即可完成参数解析、读取、完整转换、
输出校验和写入：

```bash
python convertToHaloTrace.py INPUT [OUTPUT] [options]
```

当前目录排版：

```text
halo-trace-converter/
├─ convert.sh                 Shell 启动脚本
├─ convertToHaloTrace.py      唯一 Python 主入口
├─ README.md
├─ converter_core/            转换实现
├─ task/                      待转换输入
└─ task-halo-traces/          转换输出及 HALO 索引
```

`converter_core/` 内部模块：

```text
models.py       共享类型和 ConversionOptions
content.py      时间、JSON 解析和可选长度限制
status.py       Tool/LLM 错误识别及关联 ID 提取
builders.py     AGENT/LLM/TOOL Span 构造
validation.py   输入、Span 和 Trace 图校验
conversion.py   事件配对、多运行拆分和转换编排
io.py           JSONL 读写及输出路径
cli.py          命令行参数和批量入口
```

原有 Python 调用方式也由主入口继续导出，例如：

```python
from convertToHaloTrace import ConversionOptions, convert_events, convert_file
```
