---
name: judge-xiaoyi-results
description: >-
  Judge XiaoYi result artifacts without running XiaoYi or calling an external
  Judge API. Use for FileOrganization_* cases backed by a final outputs directory
  plus metadata.json rubrics, and for prepared numeric WorkspaceBench Task
  directories backed by metadata.json, case_manifest.json, agent.json,
  normalized_runner_log.jsonl, and output/. Supports Judge-only requests,
  re-Judge, scoring, and the post-run batch Judge phase delegated by run-xiaoyi,
  run-xiaoyi-loop, or run-xiaoyi-halo-loop.
---

# Judge XiaoYi Results

Evaluate frozen local evidence only. Never launch XiaoYi, use HDC, clean device
files, continue a dialog, call an external Judge/model API, or request a Judge
API key.

## Select exactly one adapter

Choose `file-organization` when all of these hold:

- the selector matches `^FileOrganization_[0-9]+_[0-9]+$`;
- `metadata.json.absolute_id` matches that selector;
- the evidence is one final `outputs/` tree containing `Desktop/`, `Download/`,
  and `Documents/`.

Choose `workspacebench` when the selector is a non-negative numeric Task ID and
the prepared directory contains `metadata.json`, `case_manifest.json`,
`agent.json`, `normalized_runner_log.jsonl`, and `output/`.

Do not reinterpret a FileOrganization numeric suffix as a WorkspaceBench Task
ID. Reject mixed batches before evaluation. Judge existing artifacts only; a
missing input is a Judge error, not permission to rerun the task.

## File-organization adapter

Treat `metadata.json.rubrics` as the complete final-state contract. Do not read
the original prompt, setup.json, expect.json, source files, JSONL, content.txt,
completed.json, or XiaoYi's claims to change the score.

Accept `<run_dir>/runner_batch.json` containing `version = 1`,
`adapter = "file-organization"`, `runnerFinished = true`, `runDir`, ordered case
IDs, terminal execution outcomes, metadata paths, and final outputs paths. Refuse
to start if the file is missing, any selected case is omitted/duplicated/reordered,
an outcome is not `complete`, `incomplete-after-3-continues`, or
`execution-error`, or any selected case is still running or waiting for a
continue decision. Use:

```text
<judge_batch_dir> = <agent_workspace>/xiaoyi_judge/file-organization/<run_id>
<case_result> = <judge_batch_dir>/<case_id>/judge_result.json
<batch_summary> = <judge_batch_dir>/batch_summary.json
```

Build a queue of Judgeable cases. Record missing metadata, missing outputs, or an
incomplete outputs manifest as case-level Judge input errors without rerunning
XiaoYi. Assign every remaining case to exactly one fresh Judge subagent. Run up
to the available concurrency limit, wait for completions, and fill freed slots
until the queue is empty. Never give one subagent multiple cases.

Give a file-organization Judge subagent only the shared Judge Skill root, case
ID, metadata path, outputs path, and result path. Require it to run the bundled
deterministic evaluator once and modify only its assigned result path:

```powershell
& <python> -B "<skill_root>\scripts\judge_file_organization.py" `
  --metadata "<test_file_base>\<case_id>\metadata.json" `
  --outputs "<case_run_dir>\outputs" `
  --result "<judge_batch_dir>\<case_id>\judge_result.json"
```

The evaluator must:

- require all three output roots and preserve exact case-sensitive names;
- ignore `outputs_manifest.json` as Runner bookkeeping;
- normalize `\` and `/` in rubric paths without allowing path traversal;
- compare direct-child sets exactly, including unexpected entries;
- verify requested file/directory types and MD5 values;
- fail safely with `status = "error"` for an unsupported rubric or incomplete
  outputs snapshot;
- fingerprint `metadata.json` and the three output trees so a stale result is
  never resumed after inputs change.

Judge only the latest clean `outputs/` mirror. Score the artifact exactly as it
exists; do not guess a different remote state from timestamps or Trace. The
Runner owns clean per-round replacement before this batch phase begins.

## WorkspaceBench adapter

Use one isolated Judge subagent per prepared numeric Task. A calling run Skill
may prepare the evidence, but this Skill owns evaluation, result validation, and
batch scoring.

Wait for the complete Runner/prepare batch handoff before spawning any Judge.
Queue every prepared Task needing evaluation, run up to the available subagent
concurrency limit, and fill freed slots until the queue is empty. Record
Runner/prepare failures in the batch summary without asking a Judge subagent to
recreate their evidence.

Give each subagent only its Task ID, prepared directory, result path, and this
contract:

1. Judge exactly one Task and inspect no other Task result.
2. Read `metadata.json`, `case_manifest.json`, `agent.json`, and
   `normalized_runner_log.jsonl`; inspect every file in `output/` and relevant
   source files in `data/`.
3. Use artifact-specific skills for spreadsheets, documents, PDFs,
   presentations, and images when available. Judge actual contents rather than
   filenames or Trace claims alone.
4. Evaluate every rubric independently in metadata order using concrete
   evidence. Insufficient evidence means `passed = false`.
5. Write only the assigned `judge_result.json`. Do not modify prepared evidence.

Write the existing Agent Judge result shape:

```json
{
  "version": 1,
  "taskId": "117",
  "status": "success",
  "judgeType": "codex-subagent",
  "inputFingerprint": {"algorithm": "sha256", "value": "...", "fileCount": 3},
  "rubrics": [
    {
      "index": 0,
      "rubric": "rubric text",
      "passed": true,
      "confidence": 0.95,
      "evidence": "specific artifact evidence"
    }
  ],
  "summary": {"total": 1, "passed": 1, "failed": 0},
  "passed": true,
  "score": 1.0,
  "feedback": "1/1 rubrics passed."
}
```

Copy `inputFingerprint` exactly from `case_manifest.json`. Set score to passed
rubrics divided by total rubrics and top-level `passed = true` only when every
rubric passes. Write `status = "error"` with an error message on unrecoverable
Judge failure.

## Validate and summarize

After every evaluation, reopen `judge_result.json` instead of trusting a process
exit or subagent message. Verify:

- selector and dataset adapter;
- input fingerprint;
- rubric indexes and exact text in metadata order;
- summary arithmetic, score, and top-level passed value.

For a batch, write `batch_summary.json` containing only the selected current
artifacts. Use string IDs so numeric Tasks and FileOrganization cases share the
same report contract. Return one concise row per selector with Judge status,
score, passed rubrics, action (`judged` or `resumed`), and result path.

Successful Judge execution does not mean the task passed. Keep `status` and
`passed` separate in every report.
