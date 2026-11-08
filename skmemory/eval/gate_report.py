"""Markdown renderer for the eval regression gate (SKMEM-P1-06B).

Turns a gate run (``collect_metrics()`` output, the committed baseline and the
regression list from ``compare()``) into a markdown report: a per-class table,
an aggregate table, and the verdict. The text starts with a hidden marker so CI
can find its own PR comment and update it in place instead of stacking a new one
per push.

Pure functions, no I/O beyond :func:`write_report`; fully deterministic (rows
are sorted), so the same run always renders the same bytes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Hidden HTML comment. The workflow greps for this exact string to find the
# existing comment; keep it in sync with .github/workflows/regression-gate.yml.
MARKER = "<!-- skmemory-regression-gate -->"

_PER_CLASS_PREFIXES = ("class_bench.candidate.", "fusion_bench.interleave_rerank.")


def _split_class(name: str) -> tuple[str, str, str] | None:
    """Return (class, metric, source) for a per-class metric key, else None."""
    for prefix in _PER_CLASS_PREFIXES:
        if name.startswith(prefix):
            rest = name[len(prefix) :]
            if rest.startswith("q."):  # per-query floor, counted not tabled
                return None
            cls, sep, metric = rest.partition(".")
            if not sep:  # fusion mrr / recall@5 aggregate
                return None
            source = "class_bench" if prefix.startswith("class_bench") else "fusion_bench"
            return cls, metric, source
    return None


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


def _delta(cur: float | None, base: float | None) -> str:
    if cur is None or base is None:
        return "-"
    d = cur - base
    return "0" if abs(d) < 1e-9 else f"{d:+.3f}"


def _status(cur: float | None, base: float | None, eps: float) -> str:
    if cur is None:
        return "MISSING"
    if base is not None and cur < base - eps:
        return "REGRESSED"
    return "ok"


def render_markdown(
    current: Mapping,
    baseline: Mapping,
    regressions: list[str],
    *,
    epsilon: float = 1e-9,
) -> str:
    """Render the gate result as markdown, beginning with :data:`MARKER`."""
    cur = current.get("metrics", {})
    base = baseline.get("metrics", {})

    class_rows: list[tuple[str, str, str, str]] = []
    agg_rows: list[tuple[str, str]] = []
    per_query = 0
    for name in sorted(set(cur) | set(base)):
        c, b = cur.get(name), base.get(name)
        vals = f"{_fmt(b)} | {_fmt(c)} | {_delta(c, b)}"
        st = _status(c, b, epsilon)
        parts = _split_class(name)
        if parts:
            cls, metric, source = parts
            class_rows.append((cls, f"{source}.{metric}", vals, st))
        elif ".q." in name:
            per_query += 1
        else:
            agg_rows.append((name, f"{vals} | {st}"))
    class_rows.sort(key=lambda r: (r[0], r[1]))

    verdict = "FAIL" if regressions else "PASS"
    lines = [
        MARKER,
        f"### skmemory eval regression gate: {verdict}",
        "",
        f"leak_count: **{current.get('leak_count', 0)}** (must be 0). "
        f"Per-query floors checked: {per_query}. Regressions: {len(regressions)}.",
        "",
        "#### Per query class",
        "",
        "| class | metric | baseline | current | delta | status |",
        "|---|---|---|---|---|---|",
    ]
    lines += [f"| {cls} | {metric} | {vals} | {st} |" for cls, metric, vals, st in class_rows]
    lines += [
        "",
        "#### Aggregate",
        "",
        "| metric | baseline | current | delta | status |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {name} | {vals} |" for name, vals in agg_rows]
    if regressions:
        lines += ["", "#### Regressions", ""]
        lines += [f"- {r}" for r in regressions]
    return "\n".join(lines) + "\n"


def write_report(text: str, path: str | os.PathLike | None) -> None:
    """Write *text* to *path* and append it to $GITHUB_STEP_SUMMARY when set."""
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
