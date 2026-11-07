### SKRSI evaluation: **inconclusive** (`skmemory-retrieval@1`)

**Warning:** baseline ran under load average about 20, this run under 3 to 7; latency comparison is not like for like
**Warning:** paraphrase set (182 queries) on 765528b to d609dd0: fused 0.187 to 0.192 vs vector_only 0.198, inside the 0.02 noise floor; recorded separately
**Warning:** small agent jarvis on this head: fused 0.890 vs best single arm 0.892, 0 semantic timeouts, p95 667 ms
**Warning:** noise floors are estimated from cross-run spread, not a controlled repeat

`2eee38dc/live-live_self_retrieval-seed20261106` (n=398) against `6366da8/live-live_self_retrieval-seed20261106` (n=395).
Evidence class `live_self_retrieval`; production code: yes; sabotage tested: no; dead arms reported: yes; baselines: bm25_only, vector_only, sqlite_like, lexical_local, graph_only, fallback_chain.

| metric | baseline | treatment | delta | verdict |
|---|---|---|---|---|
| live_recall_at_5_all | 0.601 | 0.853165 | 0.252165 | improved |
| live_recall_at_5_title | 0.455 | 0.864322 | 0.409322 | improved |
| live_recall_at_5_rare_token | 0.747 | 0.841837 | 0.0948367 | improved |
| live_mrr_all | 0.419 | 0.764319 | 0.345319 | improved |
| live_p95_ms | 4952 | 1313.2 | -3638.8 | improved |

Not measured by this instrument: `class_recall_at_5_per_class`, `leak_count`.

Stop rules not machine-checked: `fusion-below-bm25`, `privacy-regression`, `measurement-wrote`.

<details><summary>Missing or capped (11)</summary>

- baseline ran under load average about 20, this run under 3 to 7; latency comparison is not like for like
- paraphrase set (182 queries) on 765528b to d609dd0: fused 0.187 to 0.192 vs vector_only 0.198, inside the 0.02 noise floor; recorded separately
- small agent jarvis on this head: fused 0.890 vs best single arm 0.892, 0 semantic timeouts, p95 667 ms
- noise floors are estimated from cross-run spread, not a controlled repeat
- class_recall_at_5_per_class: absent from baseline, so no base rules apply
- leak_count: absent from baseline, so no base rules apply
- invariant not asserted by the bench: leak_count stays 0 for a declared non-owner reader
- invariant not asserted by the bench: no gated class falls below its recorded floor in regression_gate
- invariant not asserted by the bench: evaluator differs from producer
- invariant not asserted by the bench: a missing or dead arm is reported as missing, never as zero or success
- cap: instrument has no demonstrated sabotage failure

</details>

Advisory evidence only. It does not gate, approve or merge anything.
