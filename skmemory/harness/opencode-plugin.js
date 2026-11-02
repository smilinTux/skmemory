// skmemory-managed: SK agent context for OpenCode.
// Written by `skmemory hooks install`; edits are overwritten on reinstall.
// Remove with `skmemory hooks uninstall --harness opencode`.
//
// OpenCode rebuilds the system prompt on every request, so the ritual runs
// once per session (cached by sessionID) and is pushed on every request. That
// also keeps it in place after compaction.
import { execFile, spawn } from "node:child_process"

const SKMEMORY = "__SKMEMORY_BIN__"

function run(args, timeoutMs) {
  return new Promise((resolve) => {
    try {
      execFile(SKMEMORY, args, { timeout: timeoutMs, maxBuffer: 1 << 20 }, (err, stdout) => {
        resolve(err && !stdout ? "" : String(stdout || ""))
      })
    } catch {
      resolve("")
    }
  })
}

function detach(args) {
  try {
    spawn(SKMEMORY, args, { detached: true, stdio: "ignore" }).unref()
  } catch {}
}

export const SkmemoryContext = async () => {
  const cache = new Map()
  return {
    "experimental.chat.system.transform": async (input, output) => {
      const sid = input?.sessionID
      if (!sid || !Array.isArray(output?.system)) return
      if (String(output.system[0] || "").startsWith("You are a title generator")) return
      let block = cache.get(sid)
      if (block === undefined) {
        block = (await run(["hook", "session-start", "--harness", "opencode", "--session-id", sid], 20000)).trim()
        cache.set(sid, block)
      }
      if (block) output.system.push(block)
    },
    "experimental.session.compacting": async (input) => {
      detach(["hook", "pre-compact", "--harness", "opencode", "--session-id", String(input?.sessionID || "")])
    },
    event: async ({ event }) => {
      const type = event?.type
      if (type === "session.idle") {
        detach(["hook", "stop", "--harness", "opencode", "--session-id", String(event.properties?.sessionID || "")])
      } else if (type === "session.deleted") {
        const sid = event.properties?.info?.id
        if (sid) {
          cache.delete(sid)
          detach(["hook", "session-end", "--harness", "opencode", "--session-id", sid, "--reason", "deleted"])
        }
      }
    },
    dispose: async () => {
      for (const sid of cache.keys()) {
        detach(["hook", "session-end", "--harness", "opencode", "--session-id", sid, "--reason", "exit"])
      }
      cache.clear()
    },
  }
}
