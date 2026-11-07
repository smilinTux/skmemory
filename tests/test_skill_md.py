"""SKILL.md and the architecture docs describe main, not a plan (SKMEM-P0-09).

A doc that names a command or a path that does not exist is worse than no doc:
an agent copies it and fails. These tests make that drift a red build.

* every fenced code line in SKILL.md that starts with ``skmemory `` resolves to a
  real command through ``--help``, and every ``--flag`` on it is in that help;
* every repo path and module the docs name exists;
* the public docs carry no private host, address or home path, and do not
  present a retired backend as live.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from skmemory.cli import cli

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "SKILL.md"
DOCS = [
    "SKILL.md",
    "README.md",
    "SOP.md",
    "LOCAL-PG-STACK-ARCHITECTURE.md",
    "docs/ARCHITECTURE.md",
]

_FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)


def _code_lines(path: Path) -> list[str]:
    """Logical lines of every fenced block, with backslash continuations joined."""
    lines: list[str] = []
    for block in _FENCE.findall(path.read_text(encoding="utf-8")):
        pending = ""
        for raw in block.splitlines():
            line = raw.rstrip()
            if line.endswith("\\"):
                pending += line[:-1] + " "
                continue
            lines.append((pending + line).strip())
            pending = ""
    return lines


def _skmemory_invocations(path: Path) -> list[str]:
    out = []
    for line in _code_lines(path):
        line = re.sub(r"^(?:[A-Z_]+=\S+\s+)+", "", line)  # leading VAR=value
        if line.startswith("skmemory "):
            out.append(line.split(" #")[0].strip())
    return out


def _resolve(line: str):
    """Walk the click tree for ``skmemory ...``; return (command path, flags)."""
    try:
        tokens = shlex.split(line)[1:]
    except ValueError:
        tokens = line.split()[1:]
    group = cli
    path: list[str] = []
    flags: list[str] = []
    descending = True
    for tok in tokens:
        if tok.startswith("-"):
            flags.append(tok.split("=")[0])
            continue
        if not descending:
            continue  # an argument or an option value
        sub = getattr(group, "commands", {}).get(tok)
        if sub is None:
            if not path:
                raise AssertionError(f"`{tok}` is not a skmemory command (in {line!r})")
            descending = False
            continue
        path.append(tok)
        group = sub
    return path, flags


def _help(path: list[str]) -> str:
    result = CliRunner().invoke(cli, [*path, "--help"])
    assert result.exit_code == 0, f"skmemory {' '.join(path)} --help failed: {result.output}"
    return result.output


SKILL_COMMANDS = sorted(set(_skmemory_invocations(SKILL)))


def test_skill_md_has_commands() -> None:
    assert len(SKILL_COMMANDS) > 20, "the extractor found almost nothing; the doc or regex changed"


@pytest.mark.parametrize("line", SKILL_COMMANDS)
def test_skill_md_command_exists(line: str) -> None:
    path, flags = _resolve(line)
    # Every group word that was typed must be a command: a typo such as
    # `skmemory ledgr top` stops the walk at the root, which the line above catches.
    help_text = _help(path)
    top = _help([])
    for flag in flags:
        assert flag in help_text or flag in top, f"{flag} is not an option of `skmemory {path}`"


def test_skill_md_subcommands_in_tables_exist() -> None:
    """Command names in backticks in the CLI table (`skmemory x y`) exist too."""
    text = SKILL.read_text(encoding="utf-8")
    seen = 0
    for m in re.finditer(r"`(skmemory [a-z][^`\n]*)`", text):
        path, _ = _resolve(m.group(1))
        _help(path)
        seen += 1
    assert seen > 15


_REPO_DIRS = (
    "skmemory/",
    "docs/",
    "deploy/",
    "scripts/",
    "tests/",
    "skrsi/",
    "systemd/",
    ".github/",
    "seeds/",
)


def _named_paths(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    names = set()
    for m in re.finditer(r"`([A-Za-z0-9_./-]+)`", text):
        tok = m.group(1).rstrip(".,")
        if tok.startswith(_REPO_DIRS) and not tok.endswith("/") and "*" not in tok:
            names.add(tok)
    # Relative markdown links, `](./X.md)` or `](docs/X.md#anchor)`: a broken one is a
    # dead link in the rendered doc.
    base = path.parent
    for m in re.finditer(r"\]\((?!https?:|#|mailto:)([^)\s#]+)", text):
        target = (base / m.group(1)).resolve()
        try:
            names.add(str(target.relative_to(ROOT)))
        except ValueError:
            names.add(m.group(1))
    return names


def _exists(rel: str) -> bool:
    # `skmemory/foo.py` and `docs/x.md` are repo paths; a runtime path under
    # `deploy/` or a generated file is not named without a leading repo dir.
    return (ROOT / rel).exists()


@pytest.mark.parametrize("doc", DOCS)
def test_named_repo_paths_exist(doc: str) -> None:
    path = ROOT / doc
    assert path.exists(), f"{doc} is missing"
    missing = sorted(n for n in _named_paths(path) if not _exists(n))
    assert not missing, f"{doc} names paths that do not exist: {missing}"


@pytest.mark.parametrize("doc", DOCS)
def test_named_modules_exist(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")
    missing = []
    for mod in set(re.findall(r"python -m (skmemory(?:\.[a-z_0-9]+)+)", text)):
        if importlib.util.find_spec(mod) is None:
            missing.append(mod)
    assert not missing, f"{doc} runs modules that do not exist: {missing}"


_PRIVATE = re.compile(
    r"192\.168\.\d+\.\d+|\b10\.\d+\.\d+\.\d+\b|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+"
    r"|/home/[a-z0-9_-]+/|/Users/[A-Za-z0-9_-]+/|~/clawd|noroc2027|chiap\d+|chiwk\d+|ziowk\d+"
    r"|\.douno\.it|skstack\d+|(?<![0-9])\.(?:41|100|158)(?::\d+)?\b(?![-/.]\d)"
)


@pytest.mark.parametrize("doc", [*DOCS, "systemd/README.md"])
def test_public_docs_name_no_private_host_or_path(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")
    hits = sorted({m.group(0) for m in _PRIVATE.finditer(text)})
    assert not hits, f"{doc} names private hosts or paths: {hits}"


_LIVE_WORDS = re.compile(
    r"(?i)(pip install skmemory\[(?:chroma|skvector|skgraph)\]|docker compose up -d skvector"
    r"|docker compose up -d skgraph|export SKMEMORY_SK(?:VECTOR|GRAPH)_URL"
    r"|skmemory setup\b|skmemory routing\b|EndpointSelector|SKChromaBackend)"
)


@pytest.mark.parametrize("doc", DOCS)
def test_docs_do_not_present_retired_backends_as_live(doc: str) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")
    hits = sorted({m.group(0) for m in _LIVE_WORDS.finditer(text)})
    assert not hits, f"{doc} still instructs use of retired backends: {hits}"


def test_skill_md_does_not_pin_a_stale_version() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert not re.search(r"\*\*Version:\*\*\s*\d+\.\d+\.\d+", text)
    assert "should print 0.6.0" not in text.lower()


def test_skill_md_documents_every_registered_mcp_tool() -> None:
    from skmemory.mcp_server import TOOL_READER_POLICY

    text = SKILL.read_text(encoding="utf-8")
    missing = sorted(t for t in TOOL_READER_POLICY if t not in text)
    assert not missing, f"SKILL.md does not mention MCP tools: {missing}"
    assert "memory_stats" not in text, "memory_stats is not a registered tool"


def test_no_em_or_en_dashes_in_docs() -> None:
    for doc in [*DOCS, "systemd/README.md"]:
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "—" not in text and "–" not in text, f"{doc} has an em or en dash"
