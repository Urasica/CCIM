## Pattern

- ids: `LargeRecord`, `transform_batch_001` | fact: LargeRecord is a dataclass with fields record_id, tenant, score, tags, payload and transform_batch_001 returns a list[LargeRecord].
- ids: `_coerce_number`, `score` | fact: _coerce_number returns a float and is invoked to produce the numeric score value from raw.get('score', 0).

## Repeated Fields

- ids: `record_id`, `tenant` | fact: record_id is derived from raw.get('record_id') or raw.get('id') with a batch-specific fallback and tenant is normalized via str(...).strip().lower().
- ids: `tags`, `payload` | fact: tags are collected from raw.get('tags') into a deduplicated list then converted to a tuple and payload is created from dict(raw.get('payload') or {}) and mutated with batch and offset.

## Control Flow

- ids: `transform_batch_001`, `record_id` | fact: transform_batch_001 iterates enumerate(records), computes record_id for each raw item and skips records already seen using a seen set.
- ids: `transform_batch_040`, `score` | fact: transform_batch_040 computes score as _coerce_number(raw.get('score', 0)) plus 5.

## Compression Relevance

- ids: `run_all`, `transform_batch_001` | fact: run_all calls transform_batch_001 and inspects item.payload.get('status') when aggregating results.
- ids: `LargeRecord`, `payload` | fact: LargeRecord includes a payload Mapping which carries keys such as batch and offset added by the transform function.
