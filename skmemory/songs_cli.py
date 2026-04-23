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

Ingest of a new song (audio → FFT → embeddings → files) is handled by a
separate script, not the CLI — too many heavy deps for an install-wide tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

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
