# HALO Trace Converter

将 `event + payload` 格式的事件流 JSONL 转换为 HALOAgent 可读取的扁平 Span JSONL。

转换器输出 AGENT、LLM、TOOL 三类 Span，保留模型消息、工具定义、工具输入输出和未映射的源事件字段，并负责运行拆分、事件配对、错误状态推断、父子关系构建和输出校验。Agent 内部采用真实 HALOAgent 运行日志使用的扁平结构：同一 Agent 的 LLM 和 TOOL 都直接挂在该 AGENT Span 下。

## 环境要求

- Python 3.10 或更高版本
- 输入文件编码为 UTF-8
- 每行一个 JSON 对象

## 输入格式

最小事件示例：

```json
{"event":"model_input","timestamp":"2026-07-23T08:00:00Z","payload":{"messages":[]}}
```

每行必须包含：

- `event`：事件类型字符串
- `payload`：JSON 对象
- `timestamp`：ISO 8601、Unix 秒或 Unix 毫秒时间戳

支持的主要事件：

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

同一个运行或会话内的事件必须按时间非递减排列。多会话文件允许不同会话的事件交错。

`session_lifecycle`、`subagent_completed` 等非 Span 事件会保存在 AGENT Span 的 `source.events` 中。未知事件默认发出警告；使用 `--strict-events` 可改为直接失败。

## 快速使用

在本仓库根目录批量转换 `traces2`：

```powershell
python .\better_harness_v3\scripts\halo-trace-converter\convertToHaloTrace.py .\traces2 .\traces2-halo
```

转换单个文件：

```bash
python convertToHaloTrace.py input.jsonl output.jsonl
```

转换目录中的全部 `.jsonl`：

```bash
python convertToHaloTrace.py input_logs output_logs
```

目录模式会递归查找输入目录中的 `.jsonl`，并在输出目录中保留相对路径。空 JSONL 会被显示为 `[skip] ... empty JSONL`，不会中断其他文件的转换。成功并入主 Trace 的独立子 Agent 日志会显示为 `[merge]`，不再生成重复的独立输出文件。

Linux 或 macOS 也可以使用包装脚本：

```bash
sh ./convert.sh INPUT [OUTPUT] [options]
```

如果 Python 命令是 `python3`：

```bash
PYTHON_BIN=python3 sh ./convert.sh INPUT [OUTPUT]
```

省略输出路径时：

```text
input.jsonl -> input.halo.jsonl
input_logs  -> input_logs-halo-traces
```

## 命令行参数

```text
--project-id ID
    写入 inference.project_id，默认值为 "converted trace"。

--trace-id ID
    强制使用指定 trace_id。仅允许单文件、单运行输入。

--skip-bad-lines
    跳过无法解析成 JSON 对象的行。
    不会跳过字段、时间或语义校验错误。

--strict-events
    遇到不支持的事件类型时直接失败，而不是警告后忽略。

--max-attribute-chars N
    限制单个字符串 attribute 和 status message 的字符数。
    默认值 0 表示不截断。
```

## 转换语义

### 运行拆分

一个输入文件可以包含多个顺序执行的 agent run。以下情况会开始新的 run：

- 出现新的 `agent_start`
- 前一个 `agent_end` 之后又出现事件
- `run_id` 发生变化

每个 run 生成一个独立 Trace 和一个 AGENT 根 Span。Trace ID 依次取：

1. payload 或事件顶层的 `run_id`
2. `session_id`
3. 自动生成的 UUID

没有显式运行边界的旧日志按 `session_id` 分组。

### LLM 配对

`model_input + model_output` 生成一个 LLM Span。

转换器优先使用以下关联字段配对交错事件：

```text
request_id
model_call_id
call_id
message_id
```

关联字段可以位于 payload 顶层，也可以位于 `assistant`、`request`、`response` 或 `metadata` 中。

没有关联 ID 时按 FIFO 配对。未匹配的输入会生成 `STATUS_CODE_ERROR` 的 unfinished LLM Span。

### Tool 配对

`tool_call + tool_result` 生成一个 TOOL Span。

- 使用 `tool_call_id` 配对 call 和 result
- 缺少 call ID 时按 FIFO 配对并生成 `generated-<UUID>`
- 每个 TOOL Span 使用独立 UUID 作为 `span_id`
- Tool call ID 同时写入 `tool.call_id` 和 `tool.call.id`

### 父子关系

通常的层级为：

```text
AGENT
  ├─ LLM
  ├─ TOOL
  ├─ LLM
  └─ TOOL
```

同一个 Agent 执行产生的全部 LLM 和 TOOL Span 都直接以该 AGENT
Span 为父节点。因此主 Agent 的 LLM/TOOL 共享 `agent.main.span_id`
作为 `parent_span_id`；子 Agent 的 LLM/TOOL 共享对应子 AGENT 的
`span_id`。LLM 与 TOOL 的调用关系通过时间顺序、`tool.call.id`、
模型输出中的 tool call 和 TOOL 输入输出还原，不使用 Span 嵌套表达。

父节点契约如下：

```text
agent.main.parent_span_id = ""

主 LLM.parent_span_id  = agent.main.span_id
主 TOOL.parent_span_id = agent.main.span_id

agent.coder.parent_span_id = function.run_subagent.span_id

子 LLM.parent_span_id  = agent.coder.span_id
子 TOOL.parent_span_id = agent.coder.span_id
```

因此判断一个工具由哪个 Agent 调用时，只需读取该 TOOL 的直接父 Span：

```text
TOOL.parent_span_id == agent.main.span_id
    → 主 Agent 调用

TOOL.parent_span_id == agent.coder.span_id
    → coder 子 Agent 调用
```

`function.run_subagent` 本身是主 Agent 调用的 TOOL；被委托的
`agent.coder` 才是它下面的子执行。

父 Span 的时间范围会扩展到覆盖全部子 Span。

### 子 Agent 日志

单文件模式始终只转换该文件中的证据。`run_subagent` 或
`call_subagent` 与其他工具一样生成标准 TOOL Span；没有独立子日志时，
子 Agent 的返回消息仍保存在该 TOOL Span 的 `output.value` 中。

目录模式会在全部输入文件中按以下关系查找独立子日志：

```text
主日志 tool_result.details.child_session_id
    =
子日志 session_id
```

只有同时满足以下条件时才会合并：

- 主日志存在 `run_subagent` 或 `call_subagent` TOOL 调用；
- `tool_call_id` 能定位对应的 TOOL Span；
- `child_session_id` 能定位包含 subagent 模型或工具事件的详细日志。

匹配后只追加 HALO 已有的标准 Span 类型：

```text
AGENT main
  ├─ LLM
  ├─ TOOL run_subagent
  │   └─ AGENT <agent_profile>
  │       ├─ LLM
  │       └─ TOOL
  ├─ LLM
  └─ TOOL
```

主 Agent 已有 Span 保持不变。子 Agent Span 使用与主 Agent 相同的
`trace_id`，其根 AGENT 的 `parent_span_id` 指向 `run_subagent` TOOL。
转换器不会输出 `SUBAGENT` observation kind，也不会添加
`subagent.*` 自定义 attributes。

成功合并的独立子日志不会再次生成单独输出文件，避免 HALO 读取输出目录时
把同一次子执行识别成另一个 Trace。未匹配的子日志仍按普通输入独立转换。

没有独立子日志时，转换结果只包含 `function.run_subagent` TOOL，不会
凭空生成 `agent.coder` 或子 Agent 内部 LLM/TOOL。HALOAgent 仍可从
该 TOOL 的 `input.value` 和 `output.value` 看到委托 profile、prompt、
`child_session_id`、`terminal_status` 和最终返回内容，但不能还原子
Agent 内部的具体工具调用。

## 状态判定

### TOOL 和 LLM

转换器递归检查常见失败字段：

```text
error / error_message / errCode / errMsg / exception
is_error / failed / cancelled / timeout
success == false
ok == false
exitCode != 0
returncode != 0
HTTP status >= 400
失败状态的 status/state/finish_reason/stop_reason
```

stderr 规则：

- 存在独立失败信号时，仍然判定为 ERROR。
- 只有非空 `stderr`、没有成功或退出码信号时，判定为 ERROR。
- 结构化 `success=true`、`ok=true` 或 `exitCode=0` 可以证明 stderr 本身不代表失败。
- 对 shell 包装器的已知格式，转换器会解析 `details.raw.data.output` 中的 JSON 字符串。
- 当 `is_error=false`、内层 `exitCode=0`、stdout JSON 中 `ok=true`，且外层错误内容与 stderr 完全相同时，这些外层包装器假失败字段会被忽略。
- 非零退出码、独立 `failed=true`、不同的错误消息等不会被成功信号覆盖。

这条窄范围规则用于处理把进度日志写入 stderr 的命令，同时避免掩盖真实失败。

### AGENT 根 Span

根 Span 优先使用 `agent_end` 的明确结果，而不是简单继承任意子 Span 的失败：

```text
agent_end.status = completed/succeeded/success/ok
    → AGENT 为 STATUS_CODE_OK

agent_end.status = failed/error/cancelled 等失败状态
    → AGENT 为 STATUS_CODE_ERROR

存在 agent_start，但缺少 agent_end
    → AGENT 为 STATUS_CODE_ERROR
    → status.message = "agent_end event is missing"
```

`agent_end` 不单独生成 Span。原始事件完整保存在根 AGENT 的
`source.events` 中，同时用于决定根 AGENT 的 `status` 和 `end_time`。
时间戳会统一转换成 UTC、纳秒格式并以 `Z` 结尾。

如果日志完全没有 agent 生命周期事件，或者 `agent_end` 没有可识别的终止状态，才回退到子 Span 聚合规则：

```text
任一子 Span 为 ERROR → AGENT 为 ERROR
否则 → AGENT 为 OK
```

已完成 run 中出现过的工具错误仍保留为 TOOL ERROR，但不会把最终完成的 AGENT 根 Span 强制改成 ERROR。转换器不添加 `recovered_error_count` 或其他恢复状态字段；错误是否被后续调用恢复，应由 HALOAgent 根据 Span 时间线和工具证据进行诊断。

## 输出格式

每行是一个 JSON Span，包含：

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

主要内容写入 HALO 标准 attributes：

```text
llm.input_messages
llm.output_messages
llm.tools
llm.system_prompt
input.value
output.value
```

源事件上下文保存在：

```text
source.model_input.context
source.model_output.context
source.tool_call.context
source.tool_result.context
source.events
```

`*.context` 不重复保存已经进入标准 attribute 的大体积内容。

## 输出校验

写入文件前会检查：

- 必需顶层字段和 attribute
- status code
- 时间戳合法且 `start_time <= end_time`
- 同一 Trace 内 `span_id` 唯一
- parent Span 存在于同一 Trace
- 父子关系无环
- 父 Span 时间范围覆盖子 Span
- 每个 Trace 恰好一个根 Span
- Observation kind 属于 AGENT、LLM、TOOL

这些检查保证转换后的 Trace 结构合法，但不能补回源日志中缺失的事件。

## 运行测试

在转换器目录运行：

```powershell
python -m unittest discover -s tests -p "test_converter.py" -v
```

## 完整性、隐私和截断

默认不脱敏、不截断。system prompt、模型消息、Tool 参数、Tool 结果、stdout 和 stderr 都可能进入输出。

如需限制单个字符串 attribute：

```bash
python convertToHaloTrace.py input.jsonl output.jsonl \
  --max-attribute-chars 65536
```

启用截断后不能保证逐字段完整恢复。将日志交给外部系统或模型前，应按实际数据规范执行隐私检查。

## 已知限制

- 没有关联 ID 的乱序 LLM 请求只能按 FIFO 配对。
- 无法恢复源事件流中不存在的因果信息。
- 扁平结构不使用 Span 嵌套表达某个 TOOL 对应哪次 LLM 输出；该关系需要通过 `tool.call.id`、模型消息和时间线恢复。
- 没有独立子 Agent 日志时，只能诊断 `run_subagent` 的输入、最终结果和工具状态，不能诊断子 Agent 内部执行步骤。
- 当前通用状态解析会识别 `failed=true`、`ok=false`、`is_error=true`、超时、错误字段和非零退出码；如果子 Agent 结果只有 `terminal_status=failed` 而没有其他失败信号，该值会保留在 `output.value` 中，但不会单独把 TOOL 提升为 `STATUS_CODE_ERROR`。
- `stderr` 的语义取决于工具生产者；转换器只能依据当前已知包装格式进行保守判断。
- `--skip-bad-lines` 可能造成运行事件不完整，使用后应检查 skipped 数量。
- 每次重新转换都会生成新的随机 `span_id`。

## 代码结构

```text
halo-trace-converter/
├─ convert.sh
├─ convertToHaloTrace.py
├─ README.md
├─ converter_core/
│  ├─ models.py
│  ├─ content.py
│  ├─ status.py
│  ├─ builders.py
│  ├─ validation.py
│  ├─ conversion.py
│  ├─ io.py
│  └─ cli.py
└─ tests/
   └─ test_converter.py
```

Python 调用方式：

```python
from convertToHaloTrace import ConversionOptions, convert_events, convert_file
```
