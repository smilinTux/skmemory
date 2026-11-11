`skmemory pg migrate` and `pg roles` no longer open an agent memory store, so they work on a fresh node before any agent is registered (the first step of `skos install skbrain`).
