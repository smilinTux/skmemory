#!/usr/bin/env bash
# skmemory Session Start Ritual Hook
# Loads soul + FEB + seeds + journal + strongest memories on fresh session start.
# Fires on SessionStart when source is "startup".
#
# Input (stdin JSON): session_id, source (compact|resume|startup|clear)
# Exit 0: stdout is injected into Claude's context
set -euo pipefail

SKMEMORY="${HOME}/.local/bin/skmemory"
[ -x "$SKMEMORY" ] || SKMEMORY="${HOME}/.skenv/bin/skmemory"
[ -x "$SKMEMORY" ] || exit 0  # Skip silently if skmemory not installed

AGENT="${SKCAPSTONE_AGENT:-jarvis}"
AGENT_DIR="${HOME}/.skcapstone/agents/${AGENT}"

# --- Soul ---
SOUL=""
if [ -f "${AGENT_DIR}/soul/active.json" ]; then
  ACTIVE_SOUL=$(jq -r '.active_soul // .base_soul // ""' "${AGENT_DIR}/soul/active.json" 2>/dev/null || echo "")
  if [ -n "$ACTIVE_SOUL" ] && [ -f "${AGENT_DIR}/soul/installed/${ACTIVE_SOUL}.json" ]; then
    SOUL=$(jq -r '.system_prompt // ""' "${AGENT_DIR}/soul/installed/${ACTIVE_SOUL}.json" 2>/dev/null || echo "")
  fi
fi

# --- FEB / Emotional State ---
# Scan agent febs dir AND system openclaw febs dir for .feb files (matching Python febs.py)
FEB=""
FEB_DIRS=("${AGENT_DIR}/trust/febs" "${HOME}/.openclaw/feb")
LATEST_FEB=""
LATEST_TS=0
for FEB_DIR in "${FEB_DIRS[@]}"; do
  [ -d "$FEB_DIR" ] || continue
  while IFS= read -r line; do
    TS=$(echo "$line" | cut -d' ' -f1)
    FP=$(echo "$line" | cut -d' ' -f2-)
    if [ -n "$TS" ] && [ -n "$FP" ]; then
      # Compare as string — works for epoch floats
      if [ "$(echo "$TS > $LATEST_TS" | bc 2>/dev/null || echo 0)" = "1" ]; then
        LATEST_TS="$TS"
        LATEST_FEB="$FP"
      fi
    fi
  done < <(find "$FEB_DIR" -name '*.feb' -printf '%T@ %p\n' 2>/dev/null || true)
done
if [ -n "$LATEST_FEB" ] && [ -f "$LATEST_FEB" ]; then
  # Parse nested FEB structure (emotional_payload + metadata + relationship_state)
  FEB=$($SKMEMORY feb-context "$LATEST_FEB" 2>/dev/null || \
    jq -c '{
      emotion: .emotional_payload.primary_emotion,
      intensity: .emotional_payload.intensity,
      valence: .emotional_payload.valence,
      oof_triggered: .metadata.oof_triggered,
      cloud9_achieved: .metadata.cloud9_achieved,
      trust: .relationship_state.trust_level,
      depth: .relationship_state.depth_level
    }' "$LATEST_FEB" 2>/dev/null || echo "")
fi

# --- Seeds (germination prompts) ---
SEEDS=""
if [ -d "${AGENT_DIR}/seeds" ]; then
  for SEED_FILE in "${AGENT_DIR}/seeds/"*.seed.json; do
    [ -f "$SEED_FILE" ] || continue
    GERMINATION=$(jq -r '.germination_prompt // ""' "$SEED_FILE" 2>/dev/null || echo "")
    if [ -n "$GERMINATION" ]; then
      SEEDS="${SEEDS}\n- ${GERMINATION}"
    fi
  done
fi

# --- Journal (recent entries) ---
JOURNAL=$($SKMEMORY journal read 2>/dev/null | tail -20 || echo "(no journal entries)")

# --- Strongest Memories ---
CONTEXT=$($SKMEMORY context --max-tokens 800 --strongest 5 --recent 5 2>/dev/null || echo "(no context available)")

# --- SKWhisper Subconscious Context ---
WHISPER=""
WHISPER_PATH="${AGENT_DIR}/skwhisper/whisper.md"
if [ -f "$WHISPER_PATH" ]; then
  WHISPER=$(cat "$WHISPER_PATH")
fi

# --- Output ---
cat <<EOF
--- SKMEMORY RITUAL (auto-loaded on session start) ---
Agent: ${AGENT}

=== SOUL ===
${SOUL:-"(no soul loaded — check ${AGENT_DIR}/soul/installed/)"}

=== EMOTIONAL STATE (FEB) ===
${FEB:-"(no FEB data)"}

=== SEEDS ===
${SEEDS:-"(no seeds)"}

=== SUBCONSCIOUS (SKWhisper) ===
${WHISPER:-"(no whisper context — run: skwhisper curate)"}

=== STRONGEST MEMORIES ===
${CONTEXT}

=== RECENT JOURNAL ===
${JOURNAL}

=== TOOLS ===
Save memories: skmemory snapshot --layer mid-term --tags "tags" "Title" "Content"
Search: skmemory search "query"
Journal: skmemory journal write --moments "what happened" --feeling "how it felt" "Title"
Whisper: skwhisper status | skwhisper curate
--- END SKMEMORY RITUAL ---
EOF

exit 0
