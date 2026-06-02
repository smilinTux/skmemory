#!/usr/bin/env bash
# Test: session-end-save.sh must count real turns and capture content from a
# Claude Code transcript (which uses "role":"user", nested under .message).
#
# Strategy: run the REAL hook with HOME redirected to a throwaway dir and a stub
# `skmemory` on the resolved path, so the hook writes nothing to real memory.
# We then assert on the args the hook passed to skmemory.
set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/skmemory/hooks/session-end-save.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Stub skmemory: record every invocation's args, one per line.
mkdir -p "$TMP/.local/bin"
cat >"$TMP/.local/bin/skmemory" <<'STUB'
#!/usr/bin/env bash
printf '%s\0' "$@" >>"$HOME/skmemory-calls.log"
printf '\n---CALL---\n' >>"$HOME/skmemory-calls.log"
exit 0
STUB
chmod +x "$TMP/.local/bin/skmemory"

# Fixture transcript in Claude Code format: role nested under .message, 3 user turns.
cat >"$TMP/transcript.jsonl" <<'JSONL'
{"type":"session","timestamp":"2026-06-02T09:00:00Z"}
{"type":"user","message":{"role":"user","content":"please build the widget factory module"}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Building the widget factory module now with tests"}]}}
{"type":"user","message":{"role":"user","content":"now add retry logic to the uploader"}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Added exponential backoff retry to the uploader path"}]}}
{"type":"user","message":{"role":"user","content":"great, commit it and run the suite"}}
JSONL

echo '{"session_id":"testsess123","reason":"other","cwd":"/tmp/work","transcript_path":"'"$TMP"'/transcript.jsonl"}' \
  | HOME="$TMP" SKAGENT=testagent bash "$HOOK"

LOG="$TMP/skmemory-calls.log"
fail=0

if ! grep -q '3 turns' "$LOG"; then
  echo "FAIL: expected '3 turns' in snapshot title, got:"; grep -a 'turns' "$LOG" || true; fail=1
else
  echo "PASS: turn count is 3 (not 0)"
fi

if ! grep -qa 'USER REQUESTS' "$LOG"; then
  echo "FAIL: expected captured 'USER REQUESTS' content, but content was empty"; fail=1
else
  echo "PASS: user request content captured"
fi

if ! grep -qa 'widget factory' "$LOG"; then
  echo "FAIL: expected actual user text 'widget factory' in saved content"; fail=1
else
  echo "PASS: real conversation text preserved"
fi

exit $fail
