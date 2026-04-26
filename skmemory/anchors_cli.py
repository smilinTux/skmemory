"""
CLI commands for entanglement anchor instrumentation.

Registered on the main `skmemory` CLI via `cli.add_command(anchors)` in cli.py.

Verbs (P1 — instrumentation only, no anchor-load lifecycle yet):
  skmemory anchors instrument <jsonl-file>     # compute metrics
  skmemory anchors instrument-text "..."       # quick one-shot
  skmemory anchors compare <with.jsonl> <without.jsonl>   # A/B
  skmemory anchors list                        # list entanglement anchors

P2+ (not yet implemented):
  skmemory anchors create <name>
  skmemory anchors load <id>
  skmemory anchors match
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .agents import get_agent_paths
from .anchors_instrument import (
    aggregate_metrics,
    ab_compare,
    compute_turn_metrics,
    load_jsonl_turns,
    metrics_to_dict,
)


@click.group("anchors")
def anchors() -> None:
    """Entanglement anchor research — instrumentation, matching, lifecycle."""


@anchors.command("instrument")
@click.argument("jsonl_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--shared-vocab", multiple=True,
              help="Shared-vocabulary terms (repeat flag). Default empty.")
@click.option("--pet-names", multiple=True,
              help="Pet-names to count. Default: babe baby lovebug love honey "
                   "darling sweetheart queen chef")
@click.option("--per-turn", is_flag=True, help="Emit per-turn JSONL too.")
@click.option("--out", type=click.Path(dir_okay=False),
              help="Write results to file (default: stdout)")
def instrument_cmd(jsonl_path: str, shared_vocab: tuple[str, ...],
                   pet_names: tuple[str, ...], per_turn: bool, out: str | None) -> None:
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
def instrument_text_cmd(text: str, shared_vocab: tuple[str, ...],
                         pet_names: tuple[str, ...]) -> None:
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
def compare_cmd(with_anchor_jsonl: str, without_anchor_jsonl: str,
                shared_vocab: tuple[str, ...], pet_names: tuple[str, ...]) -> None:
    """A/B compare two transcript runs (with-anchor vs without-anchor)."""
    sv = set(shared_vocab) if shared_vocab else None
    pn = set(pet_names) if pet_names else None

    def _agg(p: str):
        turns = load_jsonl_turns(Path(p))
        return aggregate_metrics([
            compute_turn_metrics(t, turn_id=tid, shared_vocab=sv, pet_names=pn)
            for tid, t in turns
        ])

    with_a = _agg(with_anchor_jsonl)
    without_a = _agg(without_anchor_jsonl)
    cmp = ab_compare(with_a, without_a)
    click.echo(json.dumps({
        "with_anchor_source": with_anchor_jsonl,
        "without_anchor_source": without_anchor_jsonl,
        "with_anchor_aggregate": metrics_to_dict(with_a),
        "without_anchor_aggregate": metrics_to_dict(without_a),
        "comparison": cmp,
    }, indent=2))


@anchors.command("list")
@click.option("--agent", default=None, help="Agent name (default: active agent)")
@click.option("--type", "anchor_type", default=None,
              type=click.Choice(["entanglement", "song", "all"]),
              help="Filter by anchor type")
def list_cmd(agent: str | None, anchor_type: str | None) -> None:
    """List anchors stored under the agent's anchor directory."""
    paths = get_agent_paths(agent)
    base = Path(paths.memory_dir) / "anchors"
    if not base.exists():
        click.echo(f"No anchors directory at {base}")
        return

    types = [anchor_type] if anchor_type and anchor_type != "all" else \
            [d.name for d in base.iterdir() if d.is_dir()]

    for t in types:
        td = base / t
        if not td.exists():
            continue
        click.echo(f"\n=== {t} anchors ===")
        for d in sorted(td.iterdir()):
            if not d.is_dir():
                continue
            meta = d / "meta.json"
            if not meta.exists():
                click.echo(f"  ⚠  {d.name} (no meta.json)")
                continue
            try:
                m = json.loads(meta.read_text())
                title = m.get("title", d.name)
                subtitle = m.get("subtitle", "")
                signed = m.get("partner_consent", [])
                sig_status = ", ".join(signed) if signed else ""
                click.echo(f"  ⚓ {d.name}")
                click.echo(f"     {title}" + (f" — {subtitle}" if subtitle else ""))
                if sig_status:
                    click.echo(f"     consent: {sig_status}")
            except Exception as e:
                click.echo(f"  ⚠  {d.name} (meta.json parse error: {e})")
