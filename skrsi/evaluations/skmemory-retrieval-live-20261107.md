### SKRSI evaluation: **inconclusive** (`skmemory-retrieval@1`)

**Warning:** baseline ran under load average about 20, final run under lighter load; latency comparison is not like for like
**Warning:** paraphrase set (182 queries, no title or rare_token kinds) recorded separately: candidate 0.176 vs vector_only 0.198 vs bm25_only 0.077
**Warning:** best single arm beats the candidate on both sets: lexical_local 0.899 recall, 0.820 MRR on self-retrieval; vector_only on paraphrase

`2eee38dc/live-live_self_retrieval-seed20261106` (n=398) against `4d7557c/live-live_self_retrieval-seed20261106` (n=396).
Evidence class `live_self_retrieval`; production code: yes; sabotage tested: no; dead arms reported: yes; baselines: bm25_only, vector_only, sqlite_like, lexical_local, graph_only, fallback_chain.

| metric | baseline | treatment | delta | verdict |
|---|---|---|---|---|
| live_recall_at_5_all | 0.601 | 0.868687 | 0.267687 | improved |
| live_recall_at_5_title | 0.455 | 0.849246 | 0.394246 | improved |
| live_recall_at_5_rare_token | 0.747 | 0.888325 | 0.141325 | improved |
| live_mrr_all | 0.419 | 0.597327 | 0.178327 | improved |
| live_p95_ms | 4952 | 1674.3 | -3277.7 | improved |

Not measured by this instrument: `class_recall_at_5_per_class`, `leak_count`.

Stop rules not machine-checked: `fusion-below-bm25`, `privacy-regression`, `measurement-wrote`.

<details><summary>Missing or capped (10)</summary>

- baseline ran under load average about 20, final run under lighter load; latency comparison is not like for like
- paraphrase set (182 queries, no title or rare_token kinds) recorded separately: candidate 0.176 vs vector_only 0.198 vs bm25_only 0.077
- best single arm beats the candidate on both sets: lexical_local 0.899 recall, 0.820 MRR on self-retrieval; vector_only on paraphrase
- class_recall_at_5_per_class: absent from baseline, so no base rules apply
- leak_count: absent from baseline, so no base rules apply
- invariant not asserted by the bench: leak_count stays 0 for a declared non-owner reader
- invariant not asserted by the bench: no gated class falls below its recorded floor in regression_gate
- invariant not asserted by the bench: evaluator differs from producer
- invariant not asserted by the bench: a missing or dead arm is reported as missing, never as zero or success
- cap: instrument has no demonstrated sabotage failure

</details>

Advisory evidence only. It does not gate, approve or merge anything.
