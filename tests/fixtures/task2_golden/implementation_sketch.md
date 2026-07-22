## Helper Signatures

- ids: `LargeRecord`, `transform_batch_001` | fact: transform_batch_001 signature is def transform_batch_001(records: Iterable[Mapping[str, Any]]) -> list[LargeRecord] and returns LargeRecord objects.
- ids: `LargeRecord`, `transform_batch_040` | fact: transform_batch_040 signature is def transform_batch_040(records: Iterable[Mapping[str, Any]]) -> list[LargeRecord] and returns LargeRecord objects.

## Pseudocode

- ids: `record_id`, `tenant` | fact: For each raw in records compute record_id = raw.get('record_id') or raw.get('id') and tenant = str(raw.get('tenant') or 'default').strip().lower().
- ids: `_coerce_number`, `score` | fact: Compute score = _coerce_number(raw.get('score', 0)) and then use score to set payload['score_band'] based on threshold.

## Migration Steps

- ids: `payload`, `tags` | fact: Ensure existing payload dict is copied via dict(raw.get('payload') or {}) and then add keys batch and offset; ensure tags are deduplicated and stored as a tuple.
- ids: `run_all`, `transform_batch_001` | fact: Update run_all tests or invocation to confirm it aggregates statuses from transform_batch_001 results.

## Verification

- ids: `run_all`, `transform_batch_040` | fact: Verify that run_all does not accidentally call transform_batch_040 and that separate tests cover transform_batch_040.
- ids: `LargeRecord`, `payload` | fact: Verify produced LargeRecord.payload includes batch and offset keys and expected status values.
