Observations are written by the promotion sweep (SKMEM-P3-04). Each is a `fact`
memory (`source_tier` self-generated) that passes a code-only citation gate:
every evidence memory exists, its quote is verbatim, and the text quotes a
source. An observation inherits the most restrictive `context_tag` of its
evidence. The step has a per-sweep cap (`max_observations_per_sweep`, default 5)
and a kill switch (`SKMEMORY_OBSERVATIONS=0`); a failure never fails the sweep.
