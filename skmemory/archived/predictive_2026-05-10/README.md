# archived: predictive recall (2026-05-10)

Built around Jonathan Clements' AMK framework. Tracked co-occurrence,
time-of-day, tag affinity, and recency-weighted frequency to pre-load
memories likely to be relevant for the current context.

## Why archived

- **Never wired in production.** Audit on 2026-05-10 found zero imports
  outside its own test file. `predictive.py` sat next to the live store for
  ~8 weeks (last touched 2026-03-18) without ever being invoked.
- **SKWhisper supersedes it.** SKWhisper v0.4 does semantic-recency
  surfacing via embeddings, regenerates `whisper.md` every 30 min, and is
  already in the session-start path. Its signal beats co-occurrence
  heuristics on the same data, and we already maintain it.
- **Spec was incomplete.** Predicted IDs were never wired into
  `skmemory context` or `skmemory ritual` — the modules that were supposed
  to consume them. A consumer-less producer is dead code.

## Restoring

Move `predictive.py` back to `skmemory/predictive.py` and the test to
`tests/test_predictive.py`. Then wire a real consumer (e.g. context loader
biases) in the same PR. Don't restore without a consumer — that's how it
ended up here.

## Related

- SKWhisper: `~/clawd/projects/skwhisper/` (live recall-surface curator)
- AMK origin: Jonathan Clements' Agent Memory Kernel framework
