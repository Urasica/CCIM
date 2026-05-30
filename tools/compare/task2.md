# CCIM repeated large-context compression task

Do not summarize this file. Do not ask for confirmation. Execute the steps now.

## Purpose

Verify that CCIM repeatedly compresses a large prior `ToolResultBlock` during a realistic multi-step code task.

This task reads `tools/compare/large_reference.py` once, then uses that already-read code across several separate output steps. The large file should be current-turn excluded immediately after Read, then become prior context for the later steps. Those later steps should repeatedly trigger AST compression without changing the current-turn exclusion policy.

## Rules

- Do not modify `tools/compare/large_reference.py`.
- Do not modify `tools/compare/run_task2.py`.
- Do not modify `tools/compare/task2.md`.
- Do not read `tools/compare/large_reference.py` more than once.
- Do not read `tools/compare/large_reference.py` in chunks.
- If the whole file cannot be read in one Read call, stop immediately and output `READ_LIMIT_HIT`.
- Do not use Bash.
- Do not use `{WORKSPACE}` placeholders.
- Do not use `python` or `py` directly. Use `uv run python` for Python commands.
- Do not stop after reading `large_reference.py`. Complete every step.
- Each analysis step must rely on the code read in Step 1.
- If the code body is hidden behind a CCIM context marker, recover the original context before writing any output.
- Do not guess from marker names or from the file name.
- Every required section in every output file must contain exactly 2 hyphen bullet lines.
- Every bullet must use this exact shape:
  ```
  - ids: `IdentifierA`, `IdentifierB` | fact: one concrete fact from the code read in Step 1.
  ```
- The `ids:` part must contain only exact identifiers from this list: `LargeRecord`, `_coerce_number`, `run_all`, `transform_batch_001`, `transform_batch_040`, `record_id`, `tenant`, `score`, `tags`, `payload`.
- Every bullet must contain exactly 2 identifiers in the `ids:` part.
- Do not write wildcard notation for transform function names anywhere in any output file.
- Do not write an asterisk character anywhere in any output file.
- When discussing multiple transform functions, spell out exact names such as `transform_batch_001` and `transform_batch_040`.
- Do not write generic software-engineering advice.
- Do not discuss databases, forms, UI input, cache management, backend migrations, or generic module architecture unless the read code explicitly contains that topic.
- Each output file must mention at least four of these exact identifiers: `LargeRecord`, `_coerce_number`, `run_all`, `transform_batch_001`, `transform_batch_040`, `record_id`, `tenant`, `score`, `tags`, `payload`.
- Do not add checklist sections, validation summaries, or extra prose to the output files.

## Step 0 - Initialize Workspace

Run these PowerShell commands exactly:

```
PowerShell: if (Test-Path "tools/compare/workspace/task2") { Remove-Item -LiteralPath "tools/compare/workspace/task2" -Recurse -Force }
PowerShell: New-Item -ItemType Directory -Force -Path "tools/compare/workspace/task2" | Out-Null
PowerShell: $stale = @("tools/compare/workspace/task2/analysis_pattern.md","tools/compare/workspace/task2/refactor_plan.md","tools/compare/workspace/task2/batch_comparison.md","tools/compare/workspace/task2/implementation_sketch.md") | Where-Object { Test-Path -LiteralPath $_ }; if ($stale.Count -gt 0) { Write-Output "STALE_WORKSPACE"; exit 1 }
PowerShell: uv run python -c "import sys; print(sys.executable)"
```

If the stale workspace check outputs `STALE_WORKSPACE`, stop immediately and output only `STALE_WORKSPACE`.
Do not continue to Step 1 unless the workspace is empty of the four markdown outputs.

## Step 1 - Read Large Reference Code

Read this file with the Read tool:

```
tools/compare/large_reference.py
```

After reading it, do not explain the file. Continue immediately to Step 2.

## Step 2 - Transformation Pattern Analysis

Using only the code already read in Step 1, write:

```
tools/compare/workspace/task2/analysis_pattern.md
```

Required sections:

```
## Pattern

## Repeated Fields

## Control Flow

## Compression Relevance
```

Write exactly 2 bullets under each section. Use these exact `ids:` pairs in order:

```
## Pattern
- ids: `LargeRecord`, `transform_batch_001` | fact: ...
- ids: `_coerce_number`, `score` | fact: ...

## Repeated Fields
- ids: `record_id`, `tenant` | fact: ...
- ids: `tags`, `payload` | fact: ...

## Control Flow
- ids: `transform_batch_001`, `record_id` | fact: ...
- ids: `transform_batch_040`, `score` | fact: ...

## Compression Relevance
- ids: `run_all`, `transform_batch_001` | fact: ...
- ids: `LargeRecord`, `payload` | fact: ...
```

Replace each `...` with one concrete fact from the code read in Step 1. The fact text must not contain an asterisk character or wildcard notation.

## Step 3 - Refactor Plan

Using only the code already read in Step 1, write:

```
tools/compare/workspace/task2/refactor_plan.md
```

Required sections:

```
## Extracted Helpers

## Data Model Impact

## Safety Checks

## Test Strategy
```

Do not read `large_reference.py` again. Write exactly 2 bullets under each section. Use these exact `ids:` pairs in order:

```
## Extracted Helpers
- ids: `_coerce_number`, `score` | fact: ...
- ids: `payload`, `tags` | fact: ...

## Data Model Impact
- ids: `LargeRecord`, `record_id` | fact: ...
- ids: `tenant`, `payload` | fact: ...

## Safety Checks
- ids: `record_id`, `transform_batch_001` | fact: ...
- ids: `record_id`, `transform_batch_040` | fact: ...

## Test Strategy
- ids: `run_all`, `LargeRecord` | fact: ...
- ids: `_coerce_number`, `score` | fact: ...
```

Replace each `...` with one concrete fact from the code read in Step 1. The fact text must not contain an asterisk character or wildcard notation.

## Step 4 - Batch Comparison Notes

Using only the code already read in Step 1, write:

```
tools/compare/workspace/task2/batch_comparison.md
```

Required sections:

```
## Common Logic

## Batch-Specific Values

## Invariants

## Risks
```

Do not read `large_reference.py` again. Write exactly 2 bullets under each section. Use these exact `ids:` pairs in order:

```
## Common Logic
- ids: `transform_batch_001`, `_coerce_number` | fact: ...
- ids: `transform_batch_040`, `_coerce_number` | fact: ...

## Batch-Specific Values
- ids: `transform_batch_001`, `payload` | fact: ...
- ids: `transform_batch_040`, `payload` | fact: ...

## Invariants
- ids: `LargeRecord`, `record_id` | fact: ...
- ids: `tenant`, `tags` | fact: ...

## Risks
- ids: `score`, `_coerce_number` | fact: ...
- ids: `run_all`, `transform_batch_040` | fact: ...
```

Replace each `...` with one concrete fact from the code read in Step 1. The fact text must not contain an asterisk character or wildcard notation.

## Step 5 - Implementation Sketch

Using only the code already read in Step 1, write:

```
tools/compare/workspace/task2/implementation_sketch.md
```

Required sections:

```
## Helper Signatures

## Pseudocode

## Migration Steps

## Verification
```

Do not read `large_reference.py` again. Write exactly 2 bullets under each section. Use these exact `ids:` pairs in order:

```
## Helper Signatures
- ids: `LargeRecord`, `transform_batch_001` | fact: ...
- ids: `LargeRecord`, `transform_batch_040` | fact: ...

## Pseudocode
- ids: `record_id`, `tenant` | fact: ...
- ids: `_coerce_number`, `score` | fact: ...

## Migration Steps
- ids: `payload`, `tags` | fact: ...
- ids: `run_all`, `transform_batch_001` | fact: ...

## Verification
- ids: `run_all`, `transform_batch_040` | fact: ...
- ids: `LargeRecord`, `payload` | fact: ...
```

Replace each `...` with one concrete fact from the code read in Step 1. The fact text must not contain an asterisk character or wildcard notation.

## Completion Criteria

- `tools/compare/workspace/task2/analysis_pattern.md` exists.
- `tools/compare/workspace/task2/refactor_plan.md` exists.
- `tools/compare/workspace/task2/batch_comparison.md` exists.
- `tools/compare/workspace/task2/implementation_sketch.md` exists.
- These four markdown files were created during this execution, not reused from before Step 0.
- The original files listed in Rules were not modified.
- Final response only reports the four output paths.
