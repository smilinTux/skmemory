#!/usr/bin/env bash
# install-systemd.sh — install skmemory per-user systemd units with choice
#
# Interactive or flag-driven. Idempotent. Safe to re-run.
#
# Usage:
#   scripts/install-systemd.sh                              # interactive prompts
#   scripts/install-systemd.sh --agents lumina,opus         # specific agents
#   scripts/install-systemd.sh --agents lumina --sync --fortress  # both timers
#   scripts/install-systemd.sh --agents lumina --no-fortress      # sync only
#   scripts/install-systemd.sh --uninstall --agents lumina        # remove

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_SRC="$REPO_DIR/systemd"
UNIT_DST="${HOME}/.config/systemd/user"

AGENTS=""
DO_SYNC=""        # "" | "yes" | "no"
DO_FORTRESS=""    # "" | "yes" | "no"
DO_TELEGRAM_HOOK="" # "" | "yes" | "no"
UNINSTALL="no"
NON_INTERACTIVE="no"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agents)        AGENTS="$2"; NON_INTERACTIVE="yes"; shift 2 ;;
    --sync)          DO_SYNC="yes"; NON_INTERACTIVE="yes"; shift ;;
    --no-sync)       DO_SYNC="no"; NON_INTERACTIVE="yes"; shift ;;
    --fortress)      DO_FORTRESS="yes"; NON_INTERACTIVE="yes"; shift ;;
    --no-fortress)   DO_FORTRESS="no"; NON_INTERACTIVE="yes"; shift ;;
    --telegram-hook) DO_TELEGRAM_HOOK="yes"; NON_INTERACTIVE="yes"; shift ;;
    --uninstall)     UNINSTALL="yes"; shift ;;
    -h|--help)
      sed -n '2,15p' "$0"
      exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

ask_yn() {
  local prompt="$1" default="$2" ans
  read -r -p "$prompt [$default]: " ans || true
  ans="${ans:-$default}"
  case "${ans,,}" in
    y|yes) return 0 ;;
    n|no)  return 1 ;;
    *)     return 1 ;;
  esac
}

if [[ "$NON_INTERACTIVE" == "no" ]]; then
  echo "skmemory systemd installer"
  echo "=========================="
  echo
  read -r -p "Which agents? (comma-separated, e.g. lumina,opus,jarvis): " AGENTS
  if [[ -z "$AGENTS" ]]; then echo "No agents given. Aborting." >&2; exit 1; fi
  ask_yn "Install bidirectional sync timer (skmemory-sync@, every 6h)?" "y" && DO_SYNC=yes || DO_SYNC=no
  ask_yn "Install fortress-verify timer (skmemory-fortress-verify@, daily 3 AM)?" "y" && DO_FORTRESS=yes || DO_FORTRESS=no
  if [[ "$DO_FORTRESS" == "yes" ]]; then
    ask_yn "Wire the sample Telegram alert hook (uses ~/.hermes/.env bot token)?" "n" && DO_TELEGRAM_HOOK=yes || DO_TELEGRAM_HOOK=no
  fi
fi

# Defaults for flag-driven runs
[[ -z "$DO_SYNC" ]] && DO_SYNC="yes"
[[ -z "$DO_FORTRESS" ]] && DO_FORTRESS="yes"
[[ -z "$DO_TELEGRAM_HOOK" ]] && DO_TELEGRAM_HOOK="no"
[[ -z "$AGENTS" ]] && { echo "ERROR: --agents required in non-interactive mode" >&2; exit 1; }

mkdir -p "$UNIT_DST"

# Make scripts executable (idempotent)
chmod +x "$REPO_DIR/scripts/fortress-verify.sh" 2>/dev/null || true
chmod +x "$REPO_DIR/scripts/fortress-alert-telegram.sh" 2>/dev/null || true

# Copy unit templates
copy_unit() {
  local name="$1"
  if [[ -f "$UNIT_SRC/$name" ]]; then
    cp "$UNIT_SRC/$name" "$UNIT_DST/$name"
    echo "  -> $UNIT_DST/$name"
  else
    echo "  !! missing source: $UNIT_SRC/$name" >&2
    return 1
  fi
}

if [[ "$UNINSTALL" == "yes" ]]; then
  echo "Uninstalling timers for: $AGENTS"
  IFS=',' read -ra A <<< "$AGENTS"
  for a in "${A[@]}"; do
    a="${a// /}"
    systemctl --user disable --now "skmemory-sync@${a}.timer" 2>/dev/null || true
    systemctl --user disable --now "skmemory-fortress-verify@${a}.timer" 2>/dev/null || true
  done
  systemctl --user daemon-reload
  echo "Done. (unit templates left in place; remove from $UNIT_DST manually if desired)"
  exit 0
fi

echo "Installing unit templates -> $UNIT_DST"
if [[ "$DO_SYNC" == "yes" ]]; then
  copy_unit "skmemory-sync@.service"
  copy_unit "skmemory-sync@.timer"
fi
if [[ "$DO_FORTRESS" == "yes" ]]; then
  copy_unit "skmemory-fortress-verify@.service"
  copy_unit "skmemory-fortress-verify@.timer"
fi

if [[ "$DO_TELEGRAM_HOOK" == "yes" ]]; then
  HOOK_LINK="${HOME}/.skenv/bin/skmemory-fortress-alert"
  mkdir -p "$(dirname "$HOOK_LINK")"
  ln -sf "$REPO_DIR/scripts/fortress-alert-telegram.sh" "$HOOK_LINK"
  echo "  -> symlinked $HOOK_LINK -> scripts/fortress-alert-telegram.sh"
fi

systemctl --user daemon-reload

echo
echo "Enabling timers for: $AGENTS"
IFS=',' read -ra A <<< "$AGENTS"
for a in "${A[@]}"; do
  a="${a// /}"
  [[ -z "$a" ]] && continue
  if [[ "$DO_SYNC" == "yes" ]]; then
    systemctl --user enable --now "skmemory-sync@${a}.timer"
    echo "  ✓ skmemory-sync@${a}.timer"
  fi
  if [[ "$DO_FORTRESS" == "yes" ]]; then
    systemctl --user enable --now "skmemory-fortress-verify@${a}.timer"
    echo "  ✓ skmemory-fortress-verify@${a}.timer"
  fi
done

echo
echo "Done. Verify with:"
echo "  systemctl --user list-timers 'skmemory-*'"
echo
if [[ "$DO_FORTRESS" == "yes" ]]; then
  echo "Trigger a fortress verify now (per agent):"
  for a in "${A[@]}"; do
    a="${a// /}"
    [[ -z "$a" ]] && continue
    echo "  systemctl --user start skmemory-fortress-verify@${a}.service"
  done
  echo
  echo "Logs: ~/.skcapstone/agents/<agent>/logs/fortress-verify.log"
  echo "Status: ~/.skcapstone/agents/<agent>/fortress/last-verify.{json,txt}"
fi
