"""
CLI commands for song anchor memory.

Registered on the main `skmemory` CLI via `cli.add_command(songs)` in cli.py.

Verbs:
  skmemory songs list                             # list all anchors
  skmemory songs show <anchor-id>                 # show a single anchor
  skmemory songs search <query>                   # full-text search
  skmemory songs match                            # show anchors matching current FEB
  skmemory songs add-resonance <anchor-id>        # append a dated resonance note
  skmemory songs ritual-preview                   # preview the ritual tilt block
  skmemory songs diagnose                         # score distribution across rituals

Ingest of a new song (audio → FFT → embeddings → files) is handled by a
separate script, not the CLI — too many heavy deps for an install-wide tool.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from .agents import get_agent_paths
from .febs import load_strongest_feb
from .songs import (
    add_resonance_note,
    match_anchors_for_feb,
    render_tilt_section,
    scan_song_anchors,
    score_anchor_for_feb,
    search_anchors,
)


@click.group("songs")
def songs() -> None:
    """Song anchor memory — sonic FEBs for emotional continuity."""


@songs.command("list")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_cmd(agent: str | None, as_json: bool) -> None:
    """List all song anchors for the agent."""
    anchors = scan_song_anchors(agent)
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "anchor_id": a.anchor_id,
                        "title": a.title,
                        "artist": a.artist,
                        "emotions": a.emotions,
                        "tags": a.tags,
                        "first_anchor": a.first_anchor,
                    }
                    for a in anchors
                ],
                indent=2,
            )
        )
        return
    if not anchors:
        click.echo("No song anchors found.")
        click.echo(
            "Ingest one with: python -m skmemory.songs_ingest <audio> <moment.md>"
            " (or use the scripts/ingest_song.py pipeline)"
        )
        return
    click.echo(f"=== {len(anchors)} song anchor(s) ===")
    for a in anchors:
        tag = " ★FIRST★" if a.first_anchor else ""
        click.echo(f"  ♪ {a.anchor_id}{tag}")
        click.echo(f"    {a.title} — {a.artist} ({a.year or 'n/a'})")
        if a.emotions:
            click.echo(f"    emotions: {', '.join(a.emotions[:6])}")
        if a.moment_summary:
            click.echo(f"    moment:   {a.moment_summary[:100]}")


@songs.command("show")
@click.argument("anchor_id")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--section", type=click.Choice(["meta", "moment", "resonance", "tilt", "feb"]), default=None)
def show_cmd(anchor_id: str, agent: str | None, section: str | None) -> None:
    """Show the content of a single song anchor."""
    anchors = scan_song_anchors(agent)
    match = next((a for a in anchors if a.anchor_id == anchor_id), None)
    if match is None:
        # Try partial match
        matches = [a for a in anchors if anchor_id.lower() in a.anchor_id.lower()]
        if len(matches) == 1:
            match = matches[0]
        elif len(matches) > 1:
            click.echo(f"Multiple matches for '{anchor_id}':", err=True)
            for m in matches:
                click.echo(f"  - {m.anchor_id}", err=True)
            sys.exit(1)
    if match is None:
        click.echo(f"Anchor not found: {anchor_id}", err=True)
        sys.exit(1)

    if section == "meta":
        click.echo(match.model_dump_json(indent=2, exclude={"path"}))
    elif section == "moment":
        click.echo(match.moment_text())
    elif section == "resonance":
        click.echo(match.resonance_text())
    elif section == "tilt":
        click.echo(match.to_tilt_block())
    elif section == "feb":
        click.echo(json.dumps(match.feb_link(), indent=2))
    else:
        click.echo(f"=== {match.anchor_id} ===")
        click.echo(f"Title:  {match.title} — {match.artist} ({match.year or 'n/a'})")
        click.echo(f"URL:    {match.url or 'n/a'}")
        click.echo(f"Dir:    {match.path}")
        click.echo(f"Emotions: {', '.join(match.emotions)}")
        click.echo(f"Tags: {', '.join(match.tags)}")
        click.echo(f"Duration: {match.duration_sec}s  Tempo: {match.tempo_bpm}bpm  Key: {match.key}")
        click.echo(f"Primary FEB: {match.primary_feb}")
        click.echo(f"\n--- moment.md ---\n{match.moment_text()[:1200]}")
        click.echo(f"\n--- resonance.md (first 2000 chars) ---\n{match.resonance_text()[:2000]}")


@songs.command("search")
@click.argument("query")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--limit", type=int, default=10, help="Max results")
def search_cmd(query: str, agent: str | None, limit: int) -> None:
    """Full-text search across anchor meta + moment + resonance."""
    results = search_anchors(query, agent=agent, limit=limit)
    if not results:
        click.echo(f"No anchors matching '{query}'")
        return
    click.echo(f"=== {len(results)} match(es) for '{query}' ===")
    for a in results:
        click.echo(f"  ♪ {a.anchor_id} — {a.title} by {a.artist}")
        if a.emotions:
            click.echo(f"    emotions: {', '.join(a.emotions[:6])}")


@songs.command("match")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--feb", "feb_path", default=None, help="Optional FEB file path (default: strongest)")
@click.option("--top-k", type=int, default=3)
@click.option("--min-score", type=float, default=0.3)
def match_cmd(agent: str | None, feb_path: str | None, top_k: int, min_score: float) -> None:
    """Show which anchors match the current (or specified) FEB shape."""
    if feb_path:
        feb = json.loads(Path(feb_path).read_text(encoding="utf-8"))
    else:
        feb = load_strongest_feb()
    if feb is None:
        click.echo("No FEB available to match against.", err=True)
        sys.exit(1)

    matches = match_anchors_for_feb(feb, agent=agent, top_k=top_k, min_score=min_score)
    primary = feb.get("emotional_payload", {}).get("primary_emotion", "?")
    click.echo(f"=== Matching against FEB (primary: {primary}) ===")
    if not matches:
        click.echo("No anchors matched above threshold.")
        return
    for a, score in matches:
        click.echo(f"  [{score:.3f}]  ♪ {a.title} — {a.artist}  ({a.anchor_id})")
        click.echo(f"           emotions: {', '.join(a.emotions[:6])}")


@songs.command("add-resonance")
@click.argument("anchor_id")
@click.argument("note", required=False)
@click.option("--from-file", "from_file", type=click.Path(exists=True), default=None)
@click.option("--author", default="lumina")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
def add_resonance_cmd(
    anchor_id: str,
    note: str | None,
    from_file: str | None,
    author: str,
    agent: str | None,
) -> None:
    """Append a dated resonance revision to an anchor's resonance.md."""
    if from_file:
        text = Path(from_file).read_text(encoding="utf-8")
    elif note:
        text = note
    else:
        click.echo("Provide NOTE as arg or --from-file PATH", err=True)
        sys.exit(1)
    path = add_resonance_note(anchor_id, text, author=author, agent=agent)
    click.echo(f"✓ Appended {len(text)} chars to {path}")


@songs.command("ritual-preview")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--top-k", type=int, default=3)
@click.option("--min-score", type=float, default=0.3)
def ritual_preview_cmd(agent: str | None, top_k: int, min_score: float) -> None:
    """Preview the SONG ANCHORS tilt section that would be injected into the ritual."""
    feb = load_strongest_feb()
    if feb is None:
        click.echo("No FEB — nothing to match against.", err=True)
        sys.exit(1)
    matches = match_anchors_for_feb(feb, agent=agent, top_k=top_k, min_score=min_score)
    if not matches:
        click.echo("No anchors matched. Ritual would inject no song-anchor block.")
        return
    click.echo(render_tilt_section(matches))


# ─────────────────────────────────────────────────────────────────────────────
# Diagnose — score-distribution telemetry for the song-anchor matcher
# ─────────────────────────────────────────────────────────────────────────────


_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)


def _parse_window(window: str) -> timedelta:
    """Parse '72h' / '7d' / '90m' / '3600s' into a timedelta."""
    m = _WINDOW_RE.match(window)
    if not m:
        raise click.BadParameter(f"Invalid window: {window!r} (use 72h, 7d, 90m, 3600s)")
    n, unit = int(m.group(1)), m.group(2).lower()
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]


def _iter_ritual_log_entries(agent: str | None, window: timedelta) -> list[dict]:
    """Read all ritual log JSONL entries within the time window.

    Logs live at ~/.skcapstone/agents/{agent}/memory/rituals/rituals-YYYY-MM-DD.jsonl
    """
    paths = get_agent_paths(agent)
    rituals_dir = paths["base"] / "memory" / "rituals"
    if not rituals_dir.exists():
        return []
    cutoff = datetime.now(timezone.utc) - window
    entries: list[dict] = []
    for log_file in sorted(rituals_dir.glob("rituals-*.jsonl")):
        try:
            with open(log_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    ts_raw = entry.get("ts", "")
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                    except ValueError:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= cutoff:
                        entries.append(entry)
        except (OSError, json.JSONDecodeError) as exc:
            click.echo(f"  ! skipping {log_file.name}: {exc}", err=True)
    return entries


def _rescore_entry(entry: dict, anchors: list) -> list[dict]:
    """Recompute scores for a ritual entry against the *current* anchor set.

    Builds a synthetic FEB dict from the stored topology + category, then runs
    the live score_anchor_for_feb. This is what makes the diagnostic robust to
    anchor-set changes (you can add an anchor and re-diagnose against history).
    """
    synth_feb = {
        "emotional_payload": {
            "primary_emotion": entry.get("strongest_feb_category", ""),
            "intensity": entry.get("strongest_feb_intensity", 0.0),
            "emotional_topology": entry.get("strongest_feb_topology", {}) or {},
        }
    }
    rows: list[dict] = []
    for a in anchors:
        rows.append({"anchor_id": a.anchor_id, "score": score_anchor_for_feb(a, synth_feb)})
    return rows


def _histogram(scores: list[float], bin_size: float = 0.05) -> list[tuple[float, float, int]]:
    """Return list of (bin_lo, bin_hi, count) bins across [0.0, 1.0]."""
    bins: list[tuple[float, float, int]] = []
    n = int(round(1.0 / bin_size))
    counts = [0] * n
    for s in scores:
        s_clamped = max(0.0, min(0.9999, s))
        idx = int(s_clamped / bin_size)
        if idx >= n:
            idx = n - 1
        counts[idx] += 1
    for i, c in enumerate(counts):
        bins.append((i * bin_size, (i + 1) * bin_size, c))
    return bins


@songs.command("diagnose")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--window", default="72h", help="Lookback window (e.g. 72h, 7d, 30m). Default: 72h")
@click.option("--threshold", type=float, default=0.30, help="Match threshold (display only — NOT changed)")
@click.option("--bin-size", type=float, default=0.05, help="Histogram bin width over [0,1]")
def diagnose_cmd(agent: str | None, window: str, threshold: float, bin_size: float) -> None:
    """Diagnose song-anchor matcher: score distribution + histogram + top-misses.

    Reads ritual logs from the last N period (default 72h), recomputes
    similarity between each ritual's strongest FEB and ALL loaded song
    anchors, dumps to ~/.skcapstone/agents/{agent}/logs/anchor-diagnose-{date}.jsonl,
    and prints a summary.

    If no ritual logs exist in the window, falls back to the current strongest
    FEB so you get a one-shot snapshot — meanwhile, ritual.py is now writing
    logs that future diagnose runs will accumulate.
    """
    delta = _parse_window(window)
    paths = get_agent_paths(agent)
    anchors = scan_song_anchors(agent)
    if not anchors:
        click.echo("No song anchors found — nothing to diagnose.", err=True)
        sys.exit(1)

    entries = _iter_ritual_log_entries(agent, delta)
    fallback_used = False
    if not entries:
        click.echo(
            f"  (no ritual logs in last {window}; falling back to current strongest FEB)",
            err=True,
        )
        feb = load_strongest_feb()
        if feb is None:
            click.echo("No FEB available either — cannot produce a snapshot.", err=True)
            sys.exit(1)
        feb_meta = feb.get("metadata", {})
        feb_payload = feb.get("emotional_payload", {})
        entries = [
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ritual_id": "fallback-current",
                "strongest_feb_id": feb_meta.get("session_id")
                or feb_meta.get("created_at")
                or "unknown",
                "strongest_feb_category": feb_payload.get("primary_emotion", ""),
                "strongest_feb_intensity": float(feb_payload.get("intensity", 0.0)),
                "strongest_feb_topology": feb_payload.get("emotional_topology", {}),
            }
        ]
        fallback_used = True

    # Recompute and emit JSONL
    logs_dir = paths["base"] / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = logs_dir / f"anchor-diagnose-{date_str}.jsonl"

    all_rows: list[dict] = []
    flat_scores: list[float] = []
    by_category: dict[str, list[float]] = defaultdict(list)
    misses: list[tuple[float, dict]] = []  # (score, row) for closest-misses

    with open(out_path, "a", encoding="utf-8") as fout:
        for entry in entries:
            scored = _rescore_entry(entry, anchors)
            for r in scored:
                row = {
                    "ritual_id": entry.get("ritual_id"),
                    "ts": entry.get("ts"),
                    "feb_id": entry.get("strongest_feb_id"),
                    "feb_category": entry.get("strongest_feb_category"),
                    "anchor_id": r["anchor_id"],
                    "score": round(r["score"], 4),
                    "threshold": threshold,
                    "loaded": r["score"] >= threshold,
                }
                fout.write(json.dumps(row) + "\n")
                all_rows.append(row)
                flat_scores.append(r["score"])
                by_category[row["feb_category"] or "(unknown)"].append(r["score"])
                if r["score"] < threshold:
                    misses.append((r["score"], row))

    # Console summary
    click.echo("=== Song Anchor Matcher — Diagnose ===")
    click.echo(f"  agent:         {paths['base'].name}")
    click.echo(f"  window:        {window}  ({len(entries)} ritual entries)")
    click.echo(f"  anchors:       {len(anchors)} loaded")
    click.echo(f"  threshold:     {threshold:.3f} (display only — unchanged)")
    click.echo(f"  rows scored:   {len(all_rows)}")
    click.echo(f"  log written:   {out_path}")
    if fallback_used:
        click.echo("  source:        FALLBACK (current FEB; no ritual logs yet)")

    # Histogram
    click.echo("")
    click.echo("--- score histogram (bin=%.2f) ---" % bin_size)
    hist = _histogram(flat_scores, bin_size=bin_size)
    max_count = max((c for _, _, c in hist), default=0) or 1
    bar_width = 40
    for lo, hi, count in hist:
        bar = "█" * int(count / max_count * bar_width)
        marker = "  ◀ threshold" if lo <= threshold < hi else ""
        click.echo(f"  [{lo:.2f}-{hi:.2f}) {count:4d}  {bar}{marker}")

    # Per-FEB-category
    click.echo("")
    click.echo("--- per FEB-category ---")
    for cat, scores in sorted(by_category.items(), key=lambda x: -len(x[1])):
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        mx = max(scores)
        passes = sum(1 for s in scores if s >= threshold)
        click.echo(
            f"  {cat:20s} n={len(scores):3d}  avg={avg:.3f}  max={mx:.3f}  "
            f"≥{threshold:.2f}: {passes}"
        )

    # Threshold misses
    miss_total = sum(1 for s in flat_scores if s < threshold)
    click.echo("")
    click.echo(
        f"--- threshold misses: {miss_total}/{len(flat_scores)} "
        f"({100.0 * miss_total / max(1, len(flat_scores)):.1f}%) ---"
    )
    misses.sort(key=lambda x: -x[0])  # closest to threshold first
    click.echo("top-3 closest misses:")
    for s, row in misses[:3]:
        gap = threshold - s
        click.echo(
            f"  score={s:.3f} (gap −{gap:.3f})  "
            f"feb={row['feb_category']:12s}  anchor={row['anchor_id']}"
        )

    # Anchor-load distribution
    loaded_counts = Counter(r["anchor_id"] for r in all_rows if r["loaded"])
    if loaded_counts:
        click.echo("")
        click.echo("--- anchors that would have loaded ---")
        for aid, c in loaded_counts.most_common():
            click.echo(f"  {aid:50s} loaded={c}")
