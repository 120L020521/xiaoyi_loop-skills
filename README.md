# Halo Trace Converter

将 `event + payload` 格式的 agent 运行日志转换为 HALOAgent 可读取的 `traces.jsonl`。

这个工具适用于一类事件流 JSONL 日志：模型输入、模型输出、工具调用、工具结果分别记录成独立事件。脚本会把这些事件合并成 HALO 兼容的 AGENT、LLM、TOOL span，同时尽量完整保留原始信息。


## 功能

- 将事件流 JSONL 日志转换为 HALOAgent 兼容的 span JSONL。
- 支持单文件转换。
- 支持目录批量转换，会递归读取目录下所有 `.jsonl` 文件。
- 尽量完整保留原始日志信息。
- 保留模型消息、工具调用、工具结果、raw details、工具定义、system prompt 等信息。
- 输出 AGENT、LLM、TOOL 三类 span。
- 只依赖 Python 标准库。

## 输入格式

输入文件必须是 JSONL：一行一个 JSON 对象。

每一行需要类似这样：

```json
{"event": "model_input", "payload": {}, "timestamp": "..."}
```

支持的事件类型：

```text
agent_start
model_input
model_output
tool_call
tool_result
```

转换关系：

```text
model_input + model_output -> LLM span
tool_call + tool_result    -> TOOL span
所有子 span                 -> 一个 AGENT 根 span
```

## 快速开始

下载本仓库后，运行：

```bash
python convertToHaloTrace.py INPUT [OUTPUT] --project-id my-project
```

其中：

```text
INPUT  可以是单个 .jsonl 文件，也可以是一个目录
OUTPUT 可选，可以是目标 .jsonl 文件，也可以是输出目录
```

如果不手动指定 `OUTPUT`：

```text
单文件输入: mytrace.jsonl -> mytrace.halo.jsonl
目录输入:   input_logs    -> input_logs-halo-traces
```

## 转换单个文件

```bash
python convertToHaloTrace.py mytrace.jsonl converted/traces.jsonl --project-id mytrace-demo
```

也可以省略输出路径：

```bash
python convertToHaloTrace.py mytrace.jsonl --project-id mytrace-demo
```

这会自动生成：

```text
mytrace.halo.jsonl
```

示例输出：

```text
files=1 converted=7 skipped=0 output=converted/traces.jsonl
```

## 批量转换目录

```bash
python convertToHaloTrace.py input_logs converted_logs --project-id mytrace-demo
```

也可以省略输出目录：

```bash
python convertToHaloTrace.py input_logs --project-id mytrace-demo
```

这会自动生成：

```text
input_logs-halo-traces
```

脚本会递归查找 `input_logs` 目录下的所有 `.jsonl` 文件，并在输出目录中保留相对路径。

例如输入：

```text
input_logs/run1.jsonl
input_logs/nested/run2.jsonl
```

会输出为：

```text
converted_logs/run1.jsonl
converted_logs/nested/run2.jsonl
```

## 参数说明

```text
--project-id       写入 inference.project_id 的项目名，默认是 "converted trace"
--trace-id         强制指定 trace_id，只能用于单文件转换
--skip-bad-lines   遇到非法 JSON 行时跳过，而不是直接报错
```

指定固定 trace_id 的例子：

```bash
python convertToHaloTrace.py mytrace.jsonl converted/traces.jsonl \
  --project-id demo \
  --trace-id fixed-trace-id
```

注意：目录批量转换时不允许使用 `--trace-id`，因为多个文件共用同一个 trace_id 会让 HALO 难以区分不同运行。

## 输出格式

输出文件也是 JSONL：一行一个 HALO 兼容 span。

每个 span 都包含这些顶层字段：

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

常见 attributes：

```text
inference.export.schema_version
inference.project_id
inference.observation_kind
openinference.span.kind
llm.input_messages
llm.output_messages
llm.tools
llm.system_prompt
tool.name
tool.call.id
input.value
output.value
tool.is_error
```

## 清洗逻辑

这个脚本的目标不是删减信息，而是把多层 JSON 字符串还原成结构化 JSON。

原始日志里经常会出现这种内容：

```text
"{\"data\":{\"raw\":{\"output\":\"{\\\"exitCode\\\":256,\\\"stderr\\\":\\\"...\\\"}\"}}}"
```

这其实是 JSON 字符串里又套了一层 JSON 字符串，所以会出现很多反斜杠。

脚本会把它递归解析成更清晰的结构：

```json
{
  "data": {
    "raw": {
      "output": {
        "exitCode": 256,
        "stderr": "..."
      }
    }
  }
}
```

这样做不会丢失语义信息，只是把“字符串里的 JSON”还原成“真正的 JSON 对象”。

脚本会尽量保留：

```text
完整 messages
完整 assistant
完整 tools
完整 system_prompt
完整 tool_call args
完整 tool_result payload
完整 details
完整 raw
stdout
stderr
exitCode
success
error message
timestamp
usage
provider/model
```

## 验证 HALOAgent 是否可读

如果本地有 `haloagent` 项目，可以用 HALO 的 `SpanRecord` 模型验证输出：

```bash
python -c "import json, sys; from pathlib import Path; sys.path.insert(0, 'haloagent'); from engine.traces.models.canonical_span import SpanRecord; p=Path('converted/traces.jsonl'); rows=[json.loads(l) for l in p.read_text(encoding='utf-8').splitlines() if l.strip()]; [SpanRecord.model_validate(r) for r in rows]; print('validated', len(rows))"
```

示例输出：

```text
validated 7
```

## 用 HALO CLI 分析转换结果

转换完成后，可以把输出文件传给 HALO：

```bash
uv run halo converted/traces.jsonl \
  -p "Diagnose this agent trace. Focus on model decisions, tool calls, tool failures, repeated mistakes, and improvement opportunities. Cite concrete span evidence." \
  --model "$MODEL" \
  --max-turns 20 \
  --max-depth 1
```

运行 HALO CLI 前，需要确认模型相关环境变量已经设置好，例如：

```bash
export OPENAI_API_KEY="..."
export OPENAI_BASE_URL="..."
export MODEL="..."
```

## 注意事项

- 本工具只适用于 `event + payload` 事件流日志，不适用于任意 JSONL。
- 目录批量转换时，如果输出目录在输入目录内部，脚本会跳过输出目录下的文件，避免重复转换自己的输出。
- 脚本只使用 Python 标准库。
- 输出文件通过 HALOAgent span schema 校验，但为了完整保留源日志，可能会包含一些额外 attributes。
