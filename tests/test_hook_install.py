"""Tests for `skmemory hooks install|uninstall|status` (hook_install, codex_trust)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skmemory import codex_trust, hook_install

EXE = "/opt/venv/bin/skmemory"

USER_HOOKS = {
    "SessionStart": [
        {
            "matcher": "startup",
            "hooks": [
                {
                    "type": "command",
                    "command": "/home/u/clawd/skcapstone-repos/skmemory/skmemory/hooks/session-start-ritual.sh",
                    "timeout": 30,
                },
                {
                    "type": "command",
                    "command": "/home/u/.skenv/bin/skvault vault-status",
                    "timeout": 10,
                },
            ],
        },
        {
            "matcher": "compact",
            "hooks": [
                {
                    "type": "command",
                    "command": "/home/u/clawd/skcapstone-repos/skmemory/skmemory/hooks/post-compact-reinject.sh",
                }
            ],
        },
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "/home/u/clawd/projects/skwhisper/hooks/skwhisper-inject.sh",
                }
            ],
        },
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "/usr/bin/python3 /home/u/.claude/check_index.py --quiet",
                }
            ],
        },
        {
            "hooks": [
                {"type": "command", "command": "/home/u/.claude/hooks/shared-checkout-guard.sh"}
            ]
        },
        {"matcher": "", "hooks": [{"type": "command", "command": "/usr/local/bin/skcapstone"}]},
    ],
    "SessionEnd": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "/home/u/clawd/projects/skwhisper/hooks/skwhisper-save.sh",
                }
            ],
        },
        {
            "matcher": "",
            "hooks": [
                {"type": "command", "command": "/home/u/repo/skmemory/hooks/sk-activity-log.sh"}
            ],
        },
    ],
    "PreToolUse": [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "python3 /home/u/.claude/hooks/guard.py"}],
        }
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {"type": "command", "command": "python3 /home/u/.claude/hooks/no-dash-stop.py"}
            ],
        }
    ],
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(hook_install, "skmemory_bin", lambda: EXE)
    monkeypatch.setattr(codex_trust, "codex_hooks_list", lambda *a, **k: None)
    return tmp_path


def _write_settings(home: Path, extra: dict | None = None) -> Path:
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            {"model": "opus", "hooks": json.loads(json.dumps(USER_HOOKS)), **(extra or {})},
            indent=2,
        )
    )
    return p


def _commands(data: dict) -> list[str]:
    return [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]


def test_classifier():
    assert hook_install.is_managed_command("/x/skmemory/hooks/stop-checkpoint.sh")
    assert hook_install.is_managed_command("/x/skwhisper-inject.sh")
    assert hook_install.is_managed_command("/usr/bin/skcapstone")
    assert hook_install.is_managed_command("/opt/venv/bin/skmemory hook stop --harness claude")
    assert not hook_install.is_managed_command("/usr/bin/skcapstone gtd next")
    assert not hook_install.is_managed_command("/home/u/.skenv/bin/skvault vault-status")
    assert not hook_install.is_managed_command("python3 /x/check_index.py")


def test_claude_install_replaces_legacy_and_keeps_user_hooks(home):
    p = _write_settings(home)
    res = hook_install.install("claude")["claude"]
    assert res["action"] == "updated" and Path(res["backup"]).exists()
    data = json.loads(p.read_text())
    cmds = _commands(data)
    assert data["model"] == "opus"
    for keep in (
        "skvault vault-status",
        "check_index.py",
        "shared-checkout-guard.sh",
        "guard.py",
        "no-dash-stop.py",
    ):
        assert any(keep in c for c in cmds), keep
    for gone in (
        "session-start-ritual.sh",
        "post-compact-reinject.sh",
        "skwhisper-inject.sh",
        "skwhisper-save.sh",
        "sk-activity-log.sh",
    ):
        assert not any(gone in c for c in cmds), gone
    assert "/usr/local/bin/skcapstone" not in cmds
    ss = {
        g.get("matcher"): g
        for g in data["hooks"]["SessionStart"]
        if any("skmemory hook" in h["command"] for h in g["hooks"])
    }
    assert (
        ss["startup|clear"]["hooks"][0]["command"] == f"{EXE} hook session-start --harness claude"
    )
    assert (
        ss["compact|resume"]["hooks"][0]["command"] == f"{EXE} hook post-compact --harness claude"
    )
    for ev, event in (
        ("PreCompact", "pre-compact"),
        ("SessionEnd", "session-end"),
        ("Stop", "stop"),
    ):
        assert f"{EXE} hook {event} --harness claude" in [
            h["command"] for g in data["hooks"][ev] for h in g["hooks"]
        ]
    # legacy group that also held skvault keeps only skvault
    startup = [g for g in data["hooks"]["SessionStart"] if g.get("matcher") == "startup"][0]
    assert [h["command"] for h in startup["hooks"]] == ["/home/u/.skenv/bin/skvault vault-status"]
    assert any("skwhisper-inject.sh" in r for r in res["replaced"])


def test_claude_install_is_idempotent(home):
    p = _write_settings(home)
    hook_install.install("claude")
    first = p.read_text()
    res = hook_install.install("claude")["claude"]
    assert res["action"] == "unchanged"
    assert p.read_text() == first
    assert len(list(p.parent.glob("settings.json.bak-skmemory-*"))) == 1


def test_claude_reinstall_with_new_binary_path_replaces_old(home, monkeypatch):
    p = _write_settings(home)
    hook_install.install("claude")
    monkeypatch.setattr(hook_install, "skmemory_bin", lambda: "/new/bin/skmemory")
    hook_install.install("claude")
    cmds = _commands(json.loads(p.read_text()))
    assert not any(c.startswith(EXE) for c in cmds)
    assert sum(c.startswith("/new/bin/skmemory hook") for c in cmds) == 5


def test_dry_run_writes_nothing_and_returns_diff(home):
    p = _write_settings(home)
    before = p.read_text()
    res = hook_install.install("claude", dry_run=True)["claude"]
    assert res["action"] == "would-update"
    assert "+" in res["diff"] and "skwhisper-inject.sh" in res["diff"]
    assert p.read_text() == before
    assert not list(p.parent.glob("*.bak-skmemory-*"))


def test_fresh_home_creates_configs(home):
    res = hook_install.install("all")
    assert {r["action"] for r in res.values()} == {"created"}
    assert json.loads((home / ".claude" / "settings.json").read_text())["hooks"]["Stop"]
    assert (home / ".config" / "opencode" / "plugins" / "skmemory-context.js").exists()
    assert (home / ".pi" / "agent" / "extensions" / "skmemory-context.ts").exists()


def test_uninstall_removes_only_ours(home):
    p = _write_settings(home)
    hook_install.install("all")
    res = hook_install.uninstall("all")
    data = json.loads(p.read_text())
    cmds = _commands(data)
    assert not any("skmemory hook" in c for c in cmds)
    assert any("no-dash-stop.py" in c for c in cmds)
    assert res["opencode"]["action"] == "removed" and res["pi"]["action"] == "removed"
    assert hook_install.status("all")["claude"]["state"] == "not-installed"


def test_invalid_json_left_untouched(home):
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    res = hook_install.install("claude")["claude"]
    assert res["action"].startswith("error")
    assert p.read_text() == "{ not json"


def test_status_reports_legacy(home):
    _write_settings(home)
    st = hook_install.status("claude")["claude"]
    assert st["state"] == "legacy" and st["legacy"]
    hook_install.install("claude")
    assert hook_install.status("claude")["claude"]["state"] == "installed"


# ── OpenCode / Pi templates ──────────────────────────────────────────────────


def test_opencode_plugin_rendered_with_absolute_bin(home):
    hook_install.install("opencode")
    src = (home / ".config" / "opencode" / "plugins" / "skmemory-context.js").read_text()
    assert f'const SKMEMORY = "{EXE}"' in src
    assert "__SKMEMORY_BIN__" not in src and "clawd" not in src
    assert "experimental.chat.system.transform" in src
    assert hook_install.install("opencode")["opencode"]["action"] == "unchanged"


def test_opencode_honors_xdg_and_refuses_foreign_file(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    target = home / "xdg" / "opencode" / "plugins" / "skmemory-context.js"
    target.parent.mkdir(parents=True)
    target.write_text("// my own plugin")
    res = hook_install.install("opencode")["opencode"]
    assert res["action"].startswith("error") and target.read_text() == "// my own plugin"


def test_pi_extension_rendered(home, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / "pi"))
    hook_install.install("pi")
    src = (home / "pi" / "extensions" / "skmemory-context.ts").read_text()
    assert f'const SKMEMORY = "{EXE}"' in src
    assert 'pi.on("before_agent_start"' in src and "session_shutdown" in src


# ── Codex hooks.json + trust ─────────────────────────────────────────────────


def test_codex_hooks_json_shape_and_trust(home):
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model = "gpt-5"\n\n[mcp_servers.x]\ncommand = "x"\n')
    (codex / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/mine"}]}]}}
        )
    )
    res = hook_install.install("codex")["codex"]
    data = json.loads((codex / "hooks.json").read_text())
    assert set(data) == {"hooks"}
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "/usr/bin/mine"
    se = data["hooks"]["SessionEnd"][0]["hooks"][0]
    assert se["timeout"] == 3
    cfg = (codex / "config.toml").read_text()
    assert cfg.startswith('model = "gpt-5"')
    entries = codex_trust.our_trust_entries(codex / "hooks.json")
    assert len(entries) == 5
    for key, digest in entries.items():
        assert f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{digest}"' in cfg
    # user's own Stop hook is not trusted by us
    assert f"{codex / 'hooks.json'}:stop:0:0" not in entries
    assert res["trust"]["action"] in ("updated", "created")
    # idempotent
    again = hook_install.install("codex")["codex"]
    assert again["action"] == "unchanged" and again["trust"]["action"] == "unchanged"


def test_codex_hash_matches_known_vector():
    # Recipe from codex-rs hooks/src/engine/discovery.rs (rust-v0.162.1):
    # sha256 over compact sorted JSON of {event_name, matcher, hooks:[handler]}.
    import hashlib

    h = codex_trust.hook_hash("SessionStart", "startup", {"command": "/x hook", "timeout": 10})
    blob = '{"event_name":"session_start","hooks":[{"async":false,"command":"/x hook","timeout":10,"type":"command"}],"matcher":"startup"}'
    assert h == "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
    # Stop drops the matcher; SessionEnd clamps timeout to 3
    assert codex_trust.hook_hash("Stop", "", {"command": "c"}) == codex_trust.hook_hash(
        "Stop", None, {"command": "c"}
    )
    assert codex_trust.hook_hash(
        "SessionEnd", "", {"command": "c", "timeout": 30}
    ) == codex_trust.hook_hash("SessionEnd", "", {"command": "c", "timeout": 3})


def test_upsert_trust_replaces_existing_table():
    text = '[hooks.state."/a:stop:0:0"]\ntrusted_hash = "sha256:old"\n\n[other]\nx = 1\n'
    out = codex_trust.upsert_trust_toml(text, {"/a:stop:0:0": "sha256:new"})
    assert 'trusted_hash = "sha256:new"' in out and "sha256:old" not in out
    assert "[other]\nx = 1" in out


def test_codex_trust_opt_out(home, monkeypatch):
    monkeypatch.setenv("SKMEMORY_CODEX_NO_TRUST", "1")
    res = hook_install.install("codex")["codex"]
    assert res["trust"]["action"].startswith("skip")
    assert not (home / ".codex" / "config.toml").exists()


def test_register_hooks_delegates(home):
    from skmemory.register import register_hooks

    _write_settings(home)
    res = register_hooks(environments=["claude-code"])
    assert res["action"] == "updated"
    assert register_hooks(environments=["claude-code"])["action"] == "exists"
    assert register_hooks(environments=["cursor"])["action"] == "skip"
