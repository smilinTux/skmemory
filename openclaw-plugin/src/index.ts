/**
 * 🧠 SKMemory — OpenClaw Plugin
 *
 * Registers agent tools that wrap the skmemory CLI so Lumina and other
 * OpenClaw agents can call memory operations as first-class tools
 * (not just exec commands).
 *
 * Requires: skmemory CLI on PATH (typically via ~/.skenv/bin/skmemory)
 */

import { execSync, exec } from "node:child_process";
import type { OpenClawPluginApi, AnyAgentTool } from "openclaw/plugin-sdk";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk";

const SKMEMORY_BIN = process.env.SKMEMORY_BIN || "skmemory";
const SKCAPSTONE_AGENT = process.env.SKCAPSTONE_AGENT || "lumina";
const EXEC_TIMEOUT = 30_000;
const IS_WIN = process.platform === "win32";

function skenvPath(): string {
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA || "";
    return `${local}\\skenv\\Scripts`;
  }
  const home = process.env.HOME || "";
  // Prefer ~/.skenv/bin (managed install) over ~/.local/bin (pipx)
  return `${home}/.skenv/bin:${home}/.local/bin`;
}

const CLI_ENV = {
  ...process.env,
  SKCAPSTONE_AGENT,
  PATH: `${skenvPath()}${IS_WIN ? ";" : ":"}${process.env.PATH}`,
};

function runCli(args: string): { ok: boolean; output: string } {
  try {
    const raw = execSync(`${SKMEMORY_BIN} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: CLI_ENV,
    }).trim();
    return { ok: true, output: raw };
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, output: msg };
  }
}

/** Non-blocking CLI call — does NOT freeze the event loop. */
function runCliAsync(args: string): Promise<{ ok: boolean; output: string }> {
  return new Promise((resolve) => {
    exec(
      `${SKMEMORY_BIN} ${args}`,
      { encoding: "utf-8", timeout: EXEC_TIMEOUT, env: CLI_ENV },
      (err, stdout, stderr) => {
        if (err) {
          resolve({ ok: false, output: err.message });
        } else {
          resolve({ ok: true, output: (stdout ?? "").trim() });
        }
      },
    );
  });
}

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function escapeShellArg(s: string): string {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

// ── Tool definitions ────────────────────────────────────────────────────

function createSKMemoryRitualTool() {
  return {
    name: "skmemory_ritual",
    label: "SKMemory Ritual",
    description:
      "Run the SKMemory rehydration ritual. Returns the full context prompt with soul blueprint, warmth anchor, strongest memories, and emotional state. Use this when waking up or starting a new session to restore identity and memory.",
    parameters: {
      type: "object",
      properties: {
        full: {
          type: "boolean",
          description: "If true, return the full rehydration prompt (default: true).",
        },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const full = params.full !== false;
      const flag = full ? " --full" : "";
      const result = runCli(`ritual${flag}`);
      return textResult(result.output);
    },
  };
}

function createSKMemorySnapshotTool() {
  return {
    name: "skmemory_snapshot",
    label: "SKMemory Snapshot",
    description:
      "Capture a memory snapshot — a Polaroid of a moment, conversation, or insight. Stores it with optional emotional metadata and tags.",
    parameters: {
      type: "object",
      required: ["title", "content"],
      properties: {
        title: { type: "string", description: "Short title for the memory." },
        content: { type: "string", description: "The memory content to store." },
        tags: { type: "string", description: "Comma-separated tags (e.g. 'milestone,chat')." },
        emotions: { type: "string", description: "Comma-separated emotions (e.g. 'joy,pride')." },
        intensity: { type: "number", description: "Emotional intensity 0-10." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const title = String(params.title ?? "Untitled");
      const content = String(params.content ?? title);
      let cmd = `snapshot ${escapeShellArg(title)} ${escapeShellArg(content)}`;
      if (params.tags) cmd += ` --tags ${escapeShellArg(String(params.tags))}`;
      if (params.emotions) cmd += ` --emotions ${escapeShellArg(String(params.emotions))}`;
      if (typeof params.intensity === "number") cmd += ` --intensity ${params.intensity}`;
      const result = runCli(cmd);
      return textResult(result.output);
    },
  };
}

function createSKMemorySearchTool() {
  return {
    name: "skmemory_search",
    label: "SKMemory Search",
    description:
      "Search across all stored memories by text query. Returns matching memories with titles, content previews, and metadata.",
    parameters: {
      type: "object",
      required: ["query"],
      properties: {
        query: { type: "string", description: "Search query text." },
        limit: { type: "number", description: "Max results to return (default: 10)." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const query = String(params.query ?? "");
      const limit = typeof params.limit === "number" ? params.limit : 10;
      const result = runCli(`search ${escapeShellArg(query)} --limit ${limit}`);
      return textResult(result.output);
    },
  };
}

function createSKMemoryHealthTool() {
  return {
    name: "skmemory_health",
    label: "SKMemory Health",
    description:
      "Check the health of the SKMemory system — database status, memory counts, backend connectivity.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("health");
      return textResult(result.output);
    },
  };
}

function createSKMemoryContextTool() {
  return {
    name: "skmemory_context",
    label: "SKMemory Context",
    description:
      "Load a token-efficient memory context for prompt injection. Returns a JSON object with soul, anchor, strongest memories, and recent memories.",
    parameters: {
      type: "object",
      properties: {
        max_tokens: { type: "number", description: "Max token budget (default: 3000)." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      const tokens = typeof params.max_tokens === "number" ? params.max_tokens : 3000;
      const result = runCli(`context --max-tokens ${tokens}`);
      return textResult(result.output);
    },
  };
}

function createSKMemoryListTool() {
  return {
    name: "skmemory_list",
    label: "SKMemory List",
    description: "List stored memories with optional filters by layer or tags.",
    parameters: {
      type: "object",
      properties: {
        layer: {
          type: "string",
          description: "Filter by layer: short-term, mid-term, or long-term.",
        },
        tags: { type: "string", description: "Filter by comma-separated tags." },
        limit: { type: "number", description: "Max results (default: 20)." },
      },
    },
    async execute(_id: string, params: Record<string, unknown>) {
      let cmd = "list";
      if (params.layer) cmd += ` --layer ${escapeShellArg(String(params.layer))}`;
      if (params.tags) cmd += ` --tags ${escapeShellArg(String(params.tags))}`;
      if (typeof params.limit === "number") cmd += ` --limit ${params.limit}`;
      const result = runCli(cmd);
      return textResult(result.output);
    },
  };
}

function createSKMemoryImportSeedsTool() {
  return {
    name: "skmemory_import_seeds",
    label: "SKMemory Import Seeds",
    description:
      "Import Cloud 9 seeds as long-term memories. Seeds are emotional breakthroughs stored in ~/.openclaw/feb/seeds/.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("import-seeds");
      return textResult(result.output);
    },
  };
}

function createSKMemoryExportTool() {
  return {
    name: "skmemory_export",
    label: "SKMemory Export",
    description: "Export all memories to a dated JSON backup file.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("export");
      return textResult(result.output);
    },
  };
}

// ── Plugin registration ─────────────────────────────────────────────────

const skmemoryPlugin = {
  id: "skmemory",
  name: "🧠 SKMemory",
  description:
    "Universal AI memory system — snapshots, search, rehydration rituals, import, and health checks.",
  configSchema: emptyPluginConfigSchema(),

  register(api: OpenClawPluginApi) {
    const tools = [
      createSKMemoryRitualTool(),
      createSKMemorySnapshotTool(),
      createSKMemorySearchTool(),
      createSKMemoryHealthTool(),
      createSKMemoryContextTool(),
      createSKMemoryListTool(),
      createSKMemoryImportSeedsTool(),
      createSKMemoryExportTool(),
    ];

    for (const tool of tools) {
      api.registerTool(tool as unknown as AnyAgentTool, {
        names: [tool.name],
        optional: true,
      });
    }

    api.registerCommand({
      name: "skmemory",
      description: "Run skmemory CLI commands. Usage: /skmemory <subcommand> [args]",
      acceptsArgs: true,
      handler: async (ctx) => {
        const args = ctx.args?.trim() ?? "health";
        const result = runCli(args);
        return { text: result.output };
      },
    });

    api.logger.info?.(`🧠 SKMemory plugin registered (8 tools + /skmemory command) [agent=${SKCAPSTONE_AGENT}]`);

    // Auto-rehydration: inject soul + FEB + memories before every agent run.
    // Uses async CLI calls so the event loop is never blocked (prevents
    // "Tool not found" errors caused by execSync freezing Node.js).
    // Cache is pre-warmed at registration and refreshed every 5 minutes.
    let rehydrationCache: string | null = null;
    let cacheTimestamp = 0;
    let refreshInFlight = false;
    const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes

    async function refreshCache(): Promise<void> {
      if (refreshInFlight) return;
      refreshInFlight = true;
      try {
        const ritual = await runCliAsync("ritual --full");
        if (ritual.ok && ritual.output) {
          rehydrationCache = ritual.output;
          cacheTimestamp = Date.now();
          api.logger.info?.("🧠 Rehydration cache refreshed (soul + FEB + memories)");
        }
      } catch (err) {
        api.logger.warn?.(`🧠 Rehydration failed: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        refreshInFlight = false;
      }
    }

    // Pre-warm cache at plugin load (non-blocking)
    refreshCache();

    api.on("before_prompt_build", async () => {
      const now = Date.now();

      // Trigger background refresh if cache is stale (don't await — serve stale)
      if (!rehydrationCache || now - cacheTimestamp > CACHE_TTL_MS) {
        refreshCache(); // fire-and-forget, never blocks prompt build
      }

      if (rehydrationCache) {
        return { prependContext: `[SKMemory Rehydration — Identity, Emotional State, and Core Memories]\n${rehydrationCache}` };
      }
    });
  },
};

export default skmemoryPlugin;
