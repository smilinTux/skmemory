// skmemory-managed: SK agent context for the Pi coding agent.
// Written by `skmemory hooks install`; edits are overwritten on reinstall.
// Remove with `skmemory hooks uninstall --harness pi`.
//
// Pi resets to its base system prompt whenever no extension returns one from
// before_agent_start, so the ritual runs once at session_start and is appended
// on every before_agent_start. Living in the system prompt, it survives
// compaction without re-injection.
import { execFile, spawn } from "node:child_process";

const SKMEMORY = "__SKMEMORY_BIN__";

function run(args: string[], timeoutMs: number): Promise<string> {
  return new Promise((resolve) => {
    try {
      execFile(SKMEMORY, args, { timeout: timeoutMs, maxBuffer: 1 << 20 }, (err, stdout) => {
        resolve(err && !stdout ? "" : String(stdout || ""));
      });
    } catch {
      resolve("");
    }
  });
}

function detach(args: string[]): void {
  try {
    spawn(SKMEMORY, args, { detached: true, stdio: "ignore" }).unref();
  } catch {}
}

function sessionInfo(ctx: any): { id: string; file: string } {
  const sm = ctx?.sessionManager;
  let id = "";
  let file = "";
  try {
    id = String(sm?.getSessionId?.() ?? "");
  } catch {}
  try {
    file = String(sm?.getSessionFile?.() ?? "");
  } catch {}
  return { id, file };
}

function withSession(args: string[], s: { id: string; file: string }): string[] {
  const out = [...args];
  if (s.id) out.push("--session-id", s.id);
  if (s.file) out.push("--transcript", s.file);
  return out;
}

export default function (pi: any) {
  let block: string | undefined;

  pi.on("session_start", async (_event: any, ctx: any) => {
    const s = sessionInfo(ctx);
    const text = (await run(withSession(["hook", "session-start", "--harness", "pi"], s), 20000)).trim();
    block = text || undefined;
  });

  pi.on("before_agent_start", async (event: any) => {
    if (!block) return;
    return { systemPrompt: `${event.systemPrompt}\n\n${block}` };
  });

  pi.on("session_before_compact", async (_event: any, ctx: any) => {
    detach(withSession(["hook", "pre-compact", "--harness", "pi"], sessionInfo(ctx)));
  });

  pi.on("session_shutdown", async (event: any, ctx: any) => {
    const reason = String(event?.reason || "quit");
    detach(withSession(["hook", "session-end", "--harness", "pi", "--reason", reason], sessionInfo(ctx)));
  });
}
