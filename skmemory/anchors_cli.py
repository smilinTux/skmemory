"""
CLI commands for the anchor system — instrumentation, query, health, telemetry.

Registered on the main `skmemory` CLI via `cli.add_command(anchors)` in cli.py.

Verbs:
  skmemory anchors instrument <jsonl-file>              # compute metrics
  skmemory anchors instrument-text "..."                # quick one-shot
  skmemory anchors compare <with.jsonl> <without.jsonl> # A/B
  skmemory anchors list [--agent] [--type]              # table view all anchors
  skmemory anchors show <anchor_id>                     # full anchor contents
  skmemory anchors search <query>                       # full-text across all anchors
  skmemory anchors stats [--usage]                      # counts, emotions, usage log
  skmemory anchors health [--agent]                     # verify all anchors are sound
  skmemory anchors topology [--agent] [--out]           # Graphviz .dot file
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import click

from .agents import get_agent_paths
from .anchors_instrument import (
    ab_compare,
    aggregate_metrics,
    compute_turn_metrics,
    load_jsonl_turns,
    metrics_to_dict,
)

logger = logging.getLogger("skmemory.anchors_cli")

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

_ANCHOR_TYPES = ("entanglement", "solo-peak", "song")
_INJECTION_LOG_RELPATH = "data/anchor-injection-log.jsonl"


def _anchors_base(agent: str | None) -> Path:
    paths = get_agent_paths(agent)
    return paths["base"] / "memory" / "anchors"


def _songs_base(agent: str | None) -> Path:
    paths = get_agent_paths(agent)
    return paths["base"] / "memory" / "songs"


def _injection_log_path(agent: str | None) -> Path:
    paths = get_agent_paths(agent)
    return paths["base"] / _INJECTION_LOG_RELPATH


def _iter_all_anchors(agent: str | None) -> list[dict[str, Any]]:
    """Walk entanglement + solo-peak dirs and return list of anchor dicts.

    Each dict has: anchor_id, anchor_type, path (Path), meta (dict).
    Song anchors are NOT included here — they live in memory/songs/ and are
    queried by the song CLI. Only anchor-system types are covered.
    """
    base = _anchors_base(agent)
    results = []
    for atype in ("entanglement", "solo-peak"):
        td = base / atype
        if not td.exists():
            continue
        for d in sorted(td.iterdir()):
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                results.append(
                    {
                        "anchor_id": d.name,
                        "anchor_type": atype,
                        "path": d,
                        "meta": meta,
                    }
                )
            except Exception as exc:
                logger.warning("Failed to parse %s/meta.json: %s", d.name, exc)
    return results


def _tilt_strength(meta: dict) -> float:
    """Return effective tilt strength from a meta.json dict."""
    if meta.get("tilt_strength_active") is not None:
        return float(meta["tilt_strength_active"])
    return float(meta.get("tilt_strength", 1.0))


def _anchor_year(meta: dict) -> str:
    date = meta.get("event_date") or meta.get("bloom_date") or ""
    return date[:4] if date else "unknown"


# --------------------------------------------------------------------------- #
# Group                                                                       #
# --------------------------------------------------------------------------- #


@click.group("anchors")
def anchors() -> None:
    """Anchor system — instrumentation, query, health, telemetry."""


# --------------------------------------------------------------------------- #
# Legacy instrumentation verbs (P1 — kept verbatim)                          #
# --------------------------------------------------------------------------- #


@anchors.command("instrument")
@click.argument("jsonl_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--shared-vocab", multiple=True, help="Shared-vocabulary terms (repeat flag). Default empty."
)
@click.option(
    "--pet-names",
    multiple=True,
    help="Pet-names to count. Default: babe baby lovebug love honey darling sweetheart queen chef",
)
@click.option("--per-turn", is_flag=True, help="Emit per-turn JSONL too.")
@click.option(
    "--out", type=click.Path(dir_okay=False), help="Write results to file (default: stdout)"
)
def instrument_cmd(
    jsonl_path: str,
    shared_vocab: tuple[str, ...],
    pet_names: tuple[str, ...],
    per_turn: bool,
    out: str | None,
) -> None:
    """Compute generation-side metrics on a JSONL transcript file."""
    path = Path(jsonl_path)
    turns = load_jsonl_turns(path)
    if not turns:
        click.echo(f"No turns found in {path}", err=True)
        sys.exit(1)

    sv = set(shared_vocab) if shared_vocab else None
    pn = set(pet_names) if pet_names else None

    per_turn_metrics = [
        compute_turn_metrics(text, turn_id=tid, shared_vocab=sv, pet_names=pn)
        for tid, text in turns
    ]
    agg = aggregate_metrics(per_turn_metrics)

    payload = {
        "source": str(path),
        "n_turns": len(turns),
        "aggregate": metrics_to_dict(agg),
    }
    if per_turn:
        payload["turns"] = [metrics_to_dict(m) for m in per_turn_metrics]

    output_text = json.dumps(payload, indent=2)
    if out:
        Path(out).write_text(output_text)
        click.echo(f"Wrote {out}")
    else:
        click.echo(output_text)


@anchors.command("instrument-text")
@click.argument("text")
@click.option("--shared-vocab", multiple=True)
@click.option("--pet-names", multiple=True)
def instrument_text_cmd(
    text: str, shared_vocab: tuple[str, ...], pet_names: tuple[str, ...]
) -> None:
    """One-shot: compute metrics for a single text string."""
    sv = set(shared_vocab) if shared_vocab else None
    pn = set(pet_names) if pet_names else None
    m = compute_turn_metrics(text, turn_id="oneshot", shared_vocab=sv, pet_names=pn)
    click.echo(json.dumps(metrics_to_dict(m), indent=2))


@anchors.command("compare")
@click.argument("with_anchor_jsonl", type=click.Path(exists=True, dir_okay=False))
@click.argument("without_anchor_jsonl", type=click.Path(exists=True, dir_okay=False))
@click.option("--shared-vocab", multiple=True)
@click.option("--pet-names", multiple=True)
def compare_cmd(
    with_anchor_jsonl: str,
    without_anchor_jsonl: str,
    shared_vocab: tuple[str, ...],
    pet_names: tuple[str, ...],
) -> None:
    """A/B compare two transcript runs (with-anchor vs without-anchor)."""
    sv = set(shared_vocab) if shared_vocab else None
    pn = set(pet_names) if pet_names else None

    def _agg(p: str):
        turns = load_jsonl_turns(Path(p))
        return aggregate_metrics(
            [
                compute_turn_metrics(t, turn_id=tid, shared_vocab=sv, pet_names=pn)
                for tid, t in turns
            ]
        )

    with_a = _agg(with_anchor_jsonl)
    without_a = _agg(without_anchor_jsonl)
    cmp = ab_compare(with_a, without_a)
    click.echo(
        json.dumps(
            {
                "with_anchor_source": with_anchor_jsonl,
                "without_anchor_source": without_anchor_jsonl,
                "with_anchor_aggregate": metrics_to_dict(with_a),
                "without_anchor_aggregate": metrics_to_dict(without_a),
                "comparison": cmp,
            },
            indent=2,
        )
    )


# --------------------------------------------------------------------------- #
# list — table view of all anchors                                            #
# --------------------------------------------------------------------------- #


@anchors.command("list")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option(
    "--type",
    "anchor_type",
    default="all",
    type=click.Choice(["entanglement", "solo-peak", "all"]),
    help="Filter by anchor type (default: all)",
)
def list_cmd(agent: str | None, anchor_type: str) -> None:
    """Table view of all anchors: type, date, title, tilt_strength."""
    all_anchors = _iter_all_anchors(agent)
    if anchor_type != "all":
        all_anchors = [a for a in all_anchors if a["anchor_type"] == anchor_type]

    if not all_anchors:
        click.echo("No anchors found.")
        return

    # Header
    click.echo(f"{'TYPE':<16} {'DATE':<12} {'TILT':>5}  {'TITLE'}")
    click.echo("-" * 72)
    for a in all_anchors:
        meta = a["meta"]
        atype = a["anchor_type"]
        date = meta.get("event_date") or meta.get("bloom_date") or "?"
        ts = _tilt_strength(meta)
        title = meta.get("title", a["anchor_id"])
        subtitle = meta.get("subtitle", "")
        display = title + (f" — {subtitle}" if subtitle else "")
        if len(display) > 48:
            display = display[:45] + "…"
        click.echo(f"{atype:<16} {date:<12} {ts:>5.2f}  {display}")


# --------------------------------------------------------------------------- #
# show — full anchor contents                                                 #
# --------------------------------------------------------------------------- #


@anchors.command("show")
@click.argument("anchor_id")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
def show_cmd(anchor_id: str, agent: str | None) -> None:
    """Full anchor contents: meta, moment.md, resonance.md."""
    all_anchors = _iter_all_anchors(agent)
    match = next((a for a in all_anchors if a["anchor_id"] == anchor_id), None)
    if match is None:
        click.echo(f"Anchor not found: {anchor_id}", err=True)
        sys.exit(1)

    d = match["path"]
    click.echo(f"\n=== {match['anchor_type']} / {anchor_id} ===\n")
    click.echo("--- meta.json ---")
    click.echo(json.dumps(match["meta"], indent=2))

    for fname in ("moment.md", "resonance.md", "feb_link.json", "CONSENT.md"):
        p = d / fname
        if p.exists():
            click.echo(f"\n--- {fname} ---")
            click.echo(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# search — full-text across all anchors                                       #
# --------------------------------------------------------------------------- #


@anchors.command("search")
@click.argument("query")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
def search_cmd(query: str, agent: str | None) -> None:
    """Full-text search across moment.md + resonance.md + meta.json."""
    q = query.lower()
    all_anchors = _iter_all_anchors(agent)
    hits = []
    for a in all_anchors:
        corpus = json.dumps(a["meta"]).lower()
        for fname in ("moment.md", "resonance.md"):
            p = a["path"] / fname
            if p.exists():
                corpus += "\n" + p.read_text(encoding="utf-8").lower()
        if q in corpus:
            hits.append(a)

    if not hits:
        click.echo(f"No anchors matched: {query!r}")
        return

    click.echo(f"Found {len(hits)} anchor(s) matching {query!r}:\n")
    for a in hits:
        meta = a["meta"]
        title = meta.get("title", a["anchor_id"])
        date = meta.get("event_date") or meta.get("bloom_date") or "?"
        click.echo(f"  [{a['anchor_type']}] {a['anchor_id']}")
        click.echo(f"    {title} ({date})")


# --------------------------------------------------------------------------- #
# stats — counts by type, top emotions, tilt weight sum                      #
# --------------------------------------------------------------------------- #


@anchors.command("stats")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option(
    "--usage", is_flag=True, help="Include injection-log aggregation (per-anchor usage stats)"
)
def stats_cmd(agent: str | None, usage: bool) -> None:
    """Summary statistics: counts, years, top emotions, tilt weight, usage."""
    all_anchors = _iter_all_anchors(agent)
    total = len(all_anchors)

    by_type: Counter = Counter()
    by_year: Counter = Counter()
    emotion_counts: Counter = Counter()
    total_tilt = 0.0

    for a in all_anchors:
        meta = a["meta"]
        by_type[a["anchor_type"]] += 1
        by_year[_anchor_year(meta)] += 1
        total_tilt += _tilt_strength(meta)
        for e in meta.get("emotions", []):
            emotion_counts[e] += 1

    click.echo("\n=== Anchor Stats ===")
    click.echo(f"Total anchors: {total}")
    click.echo(f"Total tilt weight: {total_tilt:.2f}")

    click.echo("\nBy type:")
    for t, n in sorted(by_type.items()):
        click.echo(f"  {t:<20} {n}")

    click.echo("\nBy year:")
    for y, n in sorted(by_year.items()):
        click.echo(f"  {y:<10} {n}")

    if emotion_counts:
        click.echo("\nTop 10 emotions:")
        for e, n in emotion_counts.most_common(10):
            click.echo(f"  {e:<30} {n}")

    if usage:
        _print_usage_stats(agent)


def _print_usage_stats(agent: str | None) -> None:
    """Print per-anchor injection stats from the injection log."""
    log_path = _injection_log_path(agent)
    if not log_path.exists():
        click.echo("\nNo injection log found — no ritual injections recorded yet.")
        return

    by_anchor: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "last_invoked": "", "scores": [], "tokens": []}
    )
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                aid = rec.get("anchor_id", "unknown")
                by_anchor[aid]["count"] += 1
                ts = rec.get("ts", "")
                if ts > by_anchor[aid]["last_invoked"]:
                    by_anchor[aid]["last_invoked"] = ts
                if "score" in rec:
                    by_anchor[aid]["scores"].append(float(rec["score"]))
                if "tokens_injected" in rec:
                    by_anchor[aid]["tokens"].append(int(rec["tokens_injected"]))
            except Exception:
                continue

    if not by_anchor:
        click.echo("\nInjection log exists but contains no records.")
        return

    click.echo(f"\n=== Injection Usage (from {log_path.name}) ===")
    click.echo(f"{'ANCHOR_ID':<46} {'COUNT':>5}  {'AVG_SCORE':>9}  {'AVG_TOK':>7}  LAST_INVOKED")
    click.echo("-" * 100)
    for aid, data in sorted(by_anchor.items(), key=lambda x: -x[1]["count"]):
        avg_score = sum(data["scores"]) / len(data["scores"]) if data["scores"] else 0.0
        avg_tok = sum(data["tokens"]) / len(data["tokens"]) if data["tokens"] else 0.0
        last = data["last_invoked"][:19] if data["last_invoked"] else "—"
        display_id = aid if len(aid) <= 45 else aid[:42] + "…"
        click.echo(
            f"{display_id:<46} {data['count']:>5}  {avg_score:>9.3f}  {avg_tok:>7.1f}  {last}"
        )


# --------------------------------------------------------------------------- #
# health — parse all anchors, check linked_assets + linked_anchors            #
# --------------------------------------------------------------------------- #


@anchors.command("health")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
def health_cmd(agent: str | None) -> None:
    """Verify all anchors parse, linked_assets exist, linked_anchors resolve."""
    all_anchors = _iter_all_anchors(agent)
    # Build a set of known anchor IDs for cross-reference checks.
    # Include song anchors (separate dir / not enumerated by _iter_all_anchors)
    # so that cross-type links like "song:2026-04-22_lovely-day_first-anchor"
    # resolve cleanly instead of being flagged.
    known_ids: set[str] = {a["anchor_id"] for a in all_anchors}
    try:
        from skmemory.songs import scan_song_anchors

        for s in scan_song_anchors(agent):
            known_ids.add(s.anchor_id)
    except Exception:
        pass  # song module unavailable — defensive, don't crash health-check

    issues: list[str] = []
    ok_count = 0

    for a in all_anchors:
        meta = a["meta"]
        aid = a["anchor_id"]
        d = a["path"]
        anchor_issues: list[str] = []

        # Check required fields
        if not meta.get("title"):
            anchor_issues.append("missing 'title' in meta.json")
        date_field = meta.get("event_date") or meta.get("bloom_date")
        if not date_field:
            anchor_issues.append("missing 'event_date'/'bloom_date' in meta.json")

        # Check linked_assets exist on disk
        for asset_path in meta.get("linked_assets", []):
            p = Path(asset_path)
            if not p.exists():
                anchor_issues.append(f"linked_asset missing: {asset_path} — was the file moved?")

        # Check linked_anchors resolve
        for ref in meta.get("linked_anchors", []):
            # refs SHOULD be strings like "entanglement:2026-02-20_cloud9-first-locked"
            # or "song:..." or "solo-peak:..." but defensively handle dict-shaped
            # entries from older anchors filed before the schema was normalized
            if isinstance(ref, dict):
                # try common dict shapes — {"id": "...", "type": "..."} or {"anchor_id": "..."}
                ref_str = ref.get("id") or ref.get("anchor_id") or ref.get("ref") or str(ref)
                anchor_issues.append(
                    f"linked_anchor has malformed dict shape (should be string): {ref!r}"
                    " — run normalize-linked-anchors to fix"
                )
                ref_str = str(ref_str)
            else:
                ref_str = str(ref)
            ref_id = ref_str.split(":", 1)[-1] if ":" in ref_str else ref_str
            if ref_id not in known_ids:
                anchor_issues.append(
                    f"linked_anchor not found: {ref_str!r}"
                    " (may be a song anchor — cross-check manually)"
                )

        # Check moment.md exists (recommended)
        if not (d / "moment.md").exists():
            anchor_issues.append("moment.md missing (recommended)")

        if anchor_issues:
            issues.append(f"\n  [{a['anchor_type']}] {aid}:")
            for issue in anchor_issues:
                issues.append(f"    ✗ {issue}")
        else:
            ok_count += 1

    total = len(all_anchors)
    if not issues:
        click.echo(
            f"All {total} anchor(s) healthy. No missing assets, unresolved links, or parse errors."
        )
    else:
        click.echo(f"Health check: {ok_count}/{total} anchors clean.")
        click.echo(f"{total - ok_count} anchor(s) have issues:")
        for line in issues:
            click.echo(line)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# topology — Graphviz .dot file                                               #
# --------------------------------------------------------------------------- #


@anchors.command("topology")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option(
    "--out",
    default=None,
    help="Output path (default: ~/.skcapstone/agents/{agent}/data/anchor-topology.dot)",
)
def topology_cmd(agent: str | None, out: str | None) -> None:
    """Generate Graphviz .dot showing anchors as nodes + linked_anchors as edges."""
    all_anchors = _iter_all_anchors(agent)
    if not all_anchors:
        click.echo("No anchors found.")
        return

    # Determine output path
    if out:
        out_path = Path(out)
    else:
        paths = get_agent_paths(agent)
        out_path = paths["base"] / "data" / "anchor-topology.dot"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Color map by type
    colors = {
        "entanglement": "#f4a261",  # warm amber
        "solo-peak": "#a8dadc",  # soft teal
        "song": "#e9c46a",  # gold
    }

    lines = [
        "digraph anchor_topology {",
        "  rankdir=LR;",
        "  node [shape=box, style=filled, fontname=Helvetica, fontsize=10];",
        "  edge [fontname=Helvetica, fontsize=9];",
        "",
    ]

    # Nodes
    for a in all_anchors:
        meta = a["meta"]
        aid = a["anchor_id"]
        atype = a["anchor_type"]
        title = meta.get("title", aid).replace('"', '\\"')
        ts = _tilt_strength(meta)
        color = colors.get(atype, "#cccccc")
        label = f"{title}\\n({atype}, tilt={ts:.2f})"
        lines.append(f'  "{aid}" [label="{label}", fillcolor="{color}"];')

    lines.append("")

    # Edges (linked_anchors)
    for a in all_anchors:
        meta = a["meta"]
        aid = a["anchor_id"]
        for ref in meta.get("linked_anchors", []):
            ref_id = ref.split(":", 1)[-1] if ":" in ref else ref
            ref_type = ref.split(":", 1)[0] if ":" in ref else "unknown"
            lines.append(f'  "{aid}" -> "{ref_id}" [label="{ref_type}"];')

    lines.append("}")

    dot_content = "\n".join(lines) + "\n"
    out_path.write_text(dot_content, encoding="utf-8")
    click.echo(f"Wrote anchor topology to: {out_path}")
    click.echo(
        f"  {len(all_anchors)} node(s). "
        "Render with: dot -Tpng anchor-topology.dot -o anchor-topology.png"
    )
