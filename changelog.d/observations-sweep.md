Observations are written by the promotion sweep (SKMEM-P3-04). Each is a `fact`
memory (`source_tier` self-generated) that passes a code-only citation gate:
every evidence memory exists, its quote is verbatim, and the text quotes a
source. An observation inherits the most restrictive `context_tag` of its
evidence. The step has a per-sweep cap (`max_observations_per_sweep`, default 5)
and a kill switch (`SKMEMORY_OBSERVATIONS=0`); a failure never fails the sweep.
Each sweep refreshes open observations in place: evidence is re-checked, proof_count
is recounted (never truncated), the context tag is recomputed, and a file is rewritten
only when a value changed. Below two valid evidence items an observation is closed
(`valid_to`), never deleted.
