## Extracted Helpers

- ids: `_coerce_number`, `score` | fact: _coerce_number converts various input types to float and is used wherever score numeric normalization is required.
- ids: `payload`, `tags` | fact: payload is constructed from raw.get('payload') and tags are normalized and deduplicated before being stored in the payload or record tags.

## Data Model Impact

- ids: `LargeRecord`, `record_id` | fact: LargeRecord requires record_id as a string identifier populated from raw.get('record_id') or raw.get('id').
- ids: `tenant`, `payload` | fact: tenant is normalized to a lower-case trimmed string and payload holds batch-specific metadata such as batch and offset.

## Safety Checks

- ids: `record_id`, `transform_batch_001` | fact: transform_batch_001 checks for duplicate record_id using a seen set and skips duplicates to avoid double-processing.
- ids: `record_id`, `transform_batch_040` | fact: transform_batch_040 similarly guards against duplicate record_id by using a seen set during iteration.

## Test Strategy

- ids: `run_all`, `LargeRecord` | fact: run_all invokes transform_batch_001 and its tests should assert that returned LargeRecord instances contain expected payload keys and status values.
- ids: `_coerce_number`, `score` | fact: tests should cover _coerce_number with integer, float, and numeric-string inputs to ensure score normalization is consistent.
