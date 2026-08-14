"""Judge Agent — orchestrates rubric scoring of runner-agent outputs.

Responsibilities:
  1. Read metadata.json and output files from a task directory.
  2. Run the native Workspace-Bench judge (LLM-as-a-judge) with the LangChain
     bridge and strict output collection installed.
  3. Return per-rubric pass/fail results + summary.

Does NOT:
  - Invoke the runner agent (that's runner_agent's job).
  - Prepare workspaces.
  - Contain the LLM/adapters/parsing logic — those live in:
      judge_model.py        LangChain judge LLM + chat completion
      langchain_bridge.py   agent_eval chat-completion monkey-patch
      output_collector.py   agent_eval strict output monkey-patch
"""

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from standalone_judge.config import (
    get_judge_base_url,
    get_judge_key,
    get_judge_model,
)
from standalone_judge.judge_core.langchain_bridge import (
    disable_langchain_judge,
    enable_langchain_judge,
)
from standalone_judge.judge_core.output_collector import (
    disable_strict_outputs,
    enable_strict_outputs,
)
from standalone_judge.judge_core.wb_eval import agent_eval, require_agent_eval

Json = Any
logger = logging.getLogger(__name__)

__all__ = ["run_judge"]


def run_judge(
    *,
    task_dir: Path,
    metadata: dict | None = None,
    output_files: list[str] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    trace_mode: str = "compact",
    use_langchain_judge: bool = True,
) -> dict[str, Json]:
    """Run native workspace-bench judge on a task directory.

    Expects task_dir to contain:
      - metadata.json
      - agent.json (evaluation_sys format with trace)
      - output/ subdirectory with result files
    """
    require_agent_eval()

    api_key = api_key or get_judge_key()
    base_url = base_url or get_judge_base_url()
    model = model or get_judge_model()
    artifact_model_name = re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_")
    artifact_model_name = artifact_model_name or "unknown"

    if metadata is None:
        meta_path = task_dir / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"metadata.json not found in {task_dir}")
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))

    rubrics = metadata.get("rubrics", [])

    if output_files is None:
        output_dir = task_dir / "output"
        if output_dir.exists():
            output_files = [str(f) for f in output_dir.iterdir() if f.is_file()]
        else:
            output_files = []

    agent_json_path = task_dir / "agent.json"
    if not agent_json_path.exists():
        agent_json = {
            "trace": {
                "prompt": {"user": ""},
                "executionTrace": [],
                "outputs": {
                    "returnedPaths": [os.path.relpath(f, task_dir) for f in output_files],
                    "outputManifest": [
                        {
                            "sourcePath": os.path.basename(f),
                            "outputPath": os.path.basename(f),
                            "sizeBytes": os.path.getsize(f),
                        }
                        for f in output_files
                        if os.path.isfile(f)
                    ],
                },
            }
        }
        agent_json_path.write_text(
            json.dumps(agent_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    output_dir = task_dir / "output"
    output_dir.mkdir(exist_ok=True)
    for src in output_files:
        if os.path.isfile(src):
            dst = output_dir / os.path.basename(src)
            if not dst.exists():
                shutil.copy2(src, dst)

    if trace_mode not in {"compact", "full"}:
        raise ValueError(f"Unsupported trace mode: {trace_mode!r}")
    if use_langchain_judge:
        enable_langchain_judge()
    enable_strict_outputs()
    previous_trace_mode = os.environ.get("STANDALONE_JUDGE_TRACE_MODE")
    os.environ["STANDALONE_JUDGE_TRACE_MODE"] = trace_mode

    eval_yaml = task_dir / ".standalone_judge_eval_config.yaml"
    eval_yaml.write_text(
        f'model_name: "{artifact_model_name}"\n'
        f'baseUrl: "{base_url}"\n'
        f'model: "{model}"\n'
        f'apiKey: "{api_key}"\n',
        encoding="utf-8",
    )

    try:
        result = agent_eval.evaluate_task(
            task_dir=str(task_dir),
            eval_yaml_path=str(eval_yaml),
            overwrite=True,
            max_retries=3,
            max_output_files=50,
            max_str_len=12000,
        )
    finally:
        disable_strict_outputs()
        if use_langchain_judge:
            disable_langchain_judge()
        if previous_trace_mode is None:
            os.environ.pop("STANDALONE_JUDGE_TRACE_MODE", None)
        else:
            os.environ["STANDALONE_JUDGE_TRACE_MODE"] = previous_trace_mode
        eval_yaml.unlink(missing_ok=True)

    if not (isinstance(result, dict) and result.get("success") is True):
        err = result.get("error") if isinstance(result, dict) else str(result)
        for native_path in task_dir.glob("rubrics_judge--*.json"):
            native_path.unlink(missing_ok=True)
        return {
            "rubrics": [
                {"rubric": r, "passed": False, "evidence": f"Judge evaluation failed: {err}"}
                for r in rubrics
            ],
            "summary": {"total": len(rubrics), "passed": 0, "failed": len(rubrics)},
            "passed": False,
            "score": 0.0,
            "feedback": f"Native judge failed: {err}",
            "_judge_error": err,
        }

    model_name = result.get("evalModel") or model or "unknown"
    rubrics_path = task_dir / f"rubrics_judge--{model_name}.json"
    if rubrics_path.exists():
        try:
            rubrics_result = json.loads(
                rubrics_path.read_text(encoding="utf-8")
            )
        finally:
            rubrics_path.unlink(missing_ok=True)
        if isinstance(rubrics_result, dict) and "rubrics" in rubrics_result:
            passed = sum(1 for r in rubrics_result["rubrics"] if r.get("passed"))
            total = len(rubrics_result["rubrics"])
            score = passed / total if total > 0 else 0.0
            rubrics_result["passed"] = score >= 1.0
            rubrics_result["score"] = score
            rubrics_result["feedback"] = f"Score: {score:.2f}. {passed}/{total} rubrics passed."
            rubrics_result["judgeModel"] = model
            rubrics_result["artifactModelName"] = artifact_model_name
            return rubrics_result

    rubrics_path.unlink(missing_ok=True)
    return {
        "rubrics": [
            {
                "rubric": r,
                "passed": False,
                "explanation": "Native judge did not produce rubric output",
            }
            for r in rubrics
        ],
        "summary": {"total": len(rubrics), "passed": 0, "failed": len(rubrics)},
        "passed": False,
        "score": 0.0,
        "feedback": "Native judge did not produce rubric output",
    }
