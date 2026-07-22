## Common Logic

- ids: `transform_batch_001`, `_coerce_number` | fact: transform_batch_001 uses _coerce_number to derive a numeric score from raw.get('score').
- ids: `transform_batch_040`, `_coerce_number` | fact: transform_batch_040 also uses _coerce_number to normalize the score before applying batch-specific adjustments.

## Batch-Specific Values

- ids: `transform_batch_001`, `payload` | fact: transform_batch_001 sets payload['batch']=1 and payload['offset']=offset for each processed record.
- ids: `transform_batch_040`, `payload` | fact: transform_batch_040 sets payload['batch']=40 and payload['offset']=offset for each processed record.

## Invariants

- ids: `LargeRecord`, `record_id` | fact: Every LargeRecord produced includes a non-empty record_id field derived from the source raw data.
- ids: `tenant`, `tags` | fact: tenant is normalized to a lower-case trimmed string and tags are converted to a tuple of unique tag strings.

## Risks

- ids: `score`, `_coerce_number` | fact: If _coerce_number raises ValueError for unexpected inputs, score parsing across batches can fail.
- ids: `run_all`, `transform_batch_040` | fact: run_all calls only transform_batch_001 and does_not_call transform_batch_040 which could leave batch_040 unexercised by run_all.
