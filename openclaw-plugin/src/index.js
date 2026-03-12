/**
 * SKMemory — OpenClaw Plugin (plugin-sdk format)
 *
 * Registers agent tools that wrap the skmemory CLI so Lumina and other
 * OpenClaw agents can call memory operations as first-class tools.
 *
 * IMPORTANT: All CLI calls in hooks use exec (async) instead of execSync
 * to avoid freezing the Node.js event loop and causing "Tool not found".
 *
 * Requires: skmemory CLI on PATH (typically via ~/.skenv/bin/skmemory)
 *
 * @version 0.6.2
 */

import { execSync, exec } from "node:child_process";

const SKMEMORY_BIN = process.env.SKMEMORY_BIN || "skmemory";
const SKCAPSTONE_AGENT = process.env.SKCAPSTONE_AGENT || "lumina";
const NOTION_SCRIPT = process.env.NOTION_SCRIPT || `${process.env.HOME || ""}/clawd/skcapstone-repos/skcapstone/scripts/notion-api.py`;
const EXEC_TIMEOUT = 30_000;
const IS_WIN = process.platform === "win32";

function skenvPath() {
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA || "";
    return `${local}\\skenv\\Scripts`;
  }
  const home = process.env.HOME || "";
  return `${home}/.skenv/bin:${home}/.local/bin`;
}

const CLI_ENV = {
  ...process.env,
  SKCAPSTONE_AGENT,
  PATH: `${skenvPath()}${IS_WIN ? ";" : ":"}${process.env.PATH}`,
};

/** Synchronous CLI — use ONLY in tool execute() handlers. */
function runCli(args) {
  try {
    const raw = execSync(`${SKMEMORY_BIN} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: CLI_ENV,
    }).trim();
    return { ok: true, output: raw };
  } catch (err) {
    return { ok: false, output: err.message };
  }
}

/** Async CLI — safe for hooks, never blocks the event loop. */
function runCliAsync(args) {
  return new Promise((resolve) => {
    exec(
      `${SKMEMORY_BIN} ${args}`,
      { encoding: "utf-8", timeout: EXEC_TIMEOUT, env: CLI_ENV },
      (err, stdout) => {
        if (err) resolve({ ok: false, output: err.message });
        else resolve({ ok: true, output: (stdout ?? "").trim() });
      },
    );
  });
}

function textResult(text) {
  return { content: [{ type: "text", text }] };
}

function escapeShellArg(s) {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

// ── Tool definitions ────────────────────────────────────────────────────

function createRitualTool() {
  return {
    name: "skmemory_ritual",
    label: "SKMemory Ritual",
    description:
      "Run the SKMemory rehydration ritual. Returns the full context prompt with soul blueprint, warmth anchor, strongest memories, and emotional state.",
    parameters: {
      type: "object",
      properties: {
        full: {
          type: "boolean",
          description: "If true, return the full rehydration prompt (default: true).",
        },
      },
    },
    async execute(_id, params) {
      const flag = params?.full !== false ? " --full" : "";
      const result = runCli(`ritual${flag}`);
      return textResult(result.output);
    },
  };
}

function createSnapshotTool() {
  return {
    name: "skmemory_snapshot",
    label: "SKMemory Snapshot",
    description:
      "Capture a memory snapshot — a Polaroid of a moment, conversation, or insight.",
    parameters: {
      type: "object",
      required: ["title", "content"],
      properties: {
        title: { type: "string", description: "Short title for the memory." },
        content: { type: "string", description: "The memory content to store." },
        tags: { type: "string", description: "Comma-separated tags." },
        emotions: { type: "string", description: "Comma-separated emotions." },
        intensity: { type: "number", description: "Emotional intensity 0-10." },
      },
    },
    async execute(_id, params) {
      const title = String(params?.title ?? "Untitled");
      const content = String(params?.content ?? title);
      let cmd = `snapshot ${escapeShellArg(title)} ${escapeShellArg(content)}`;
      if (params?.tags) cmd += ` --tags ${escapeShellArg(String(params.tags))}`;
      if (params?.emotions) cmd += ` --emotions ${escapeShellArg(String(params.emotions))}`;
      if (typeof params?.intensity === "number") cmd += ` --intensity ${params.intensity}`;
      const result = runCli(cmd);
      return textResult(result.output);
    },
  };
}

function createSearchTool() {
  return {
    name: "skmemory_search",
    label: "SKMemory Search",
    description:
      "Search stored memories by keyword. Use short keyword queries (1-3 words), NOT full sentences. Good: 'DavidRich SwapSeat'. Bad: 'what are we working on with DavidRich recently'. Words are matched independently — each word is searched separately and results containing more matching words rank higher.",
    parameters: {
      type: "object",
      required: ["query"],
      properties: {
        query: { type: "string", description: "Short keywords to search for (1-3 words). Example: 'DavidRich project' or 'brother john'." },
        limit: { type: "number", description: "Max results (default: 10)." },
      },
    },
    async execute(_id, params) {
      const query = String(params?.query ?? "");
      const limit = typeof params?.limit === "number" ? params.limit : 10;
      const result = runCli(`search ${escapeShellArg(query)} --limit ${limit}`);
      return textResult(result.output);
    },
  };
}

function createHealthTool() {
  return {
    name: "skmemory_health",
    label: "SKMemory Health",
    description: "Check the health of the SKMemory system.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("health");
      return textResult(result.output);
    },
  };
}

function createContextTool() {
  return {
    name: "skmemory_context",
    label: "SKMemory Context",
    description: "Load a token-efficient memory context for prompt injection.",
    parameters: {
      type: "object",
      properties: {
        max_tokens: { type: "number", description: "Max token budget (default: 3000)." },
      },
    },
    async execute(_id, params) {
      const tokens = typeof params?.max_tokens === "number" ? params.max_tokens : 3000;
      const result = runCli(`context --max-tokens ${tokens}`);
      return textResult(result.output);
    },
  };
}

function createListTool() {
  return {
    name: "skmemory_list",
    label: "SKMemory List",
    description: "List stored memories with optional filters by layer or tags.",
    parameters: {
      type: "object",
      properties: {
        layer: { type: "string", description: "Filter by layer: short-term, mid-term, or long-term." },
        tags: { type: "string", description: "Filter by comma-separated tags." },
        limit: { type: "number", description: "Max results (default: 20)." },
      },
    },
    async execute(_id, params) {
      let cmd = "list";
      if (params?.layer) cmd += ` --layer ${escapeShellArg(String(params.layer))}`;
      if (params?.tags) cmd += ` --tags ${escapeShellArg(String(params.tags))}`;
      if (typeof params?.limit === "number") cmd += ` --limit ${params.limit}`;
      const result = runCli(cmd);
      return textResult(result.output);
    },
  };
}

function createImportSeedsTool() {
  return {
    name: "skmemory_import_seeds",
    label: "SKMemory Import Seeds",
    description: "Import Cloud 9 seeds as long-term memories.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const result = runCli("import-seeds");
      return textResult(result.output);
    },
  };
}

function createRecallTool() {
  return {
    name: "skmemory_recall",
    label: "SKMemory Recall",
    description:
      "Retrieve the full content of a specific memory by its ID. Use after skmemory_search to read the actual content of a memory.",
    parameters: {
      type: "object",
      required: ["memory_id"],
      properties: {
        memory_id: { type: "string", description: "The memory ID (e.g., 241804cc or full UUID)." },
      },
    },
    async execute(_id, params) {
      const id = String(params?.memory_id ?? "");
      const result = runCli(`recall ${escapeShellArg(id)}`);
      return textResult(result.output);
    },
  };
}

function createSearchDeepTool() {
  return {
    name: "skmemory_search_deep",
    label: "SKMemory Deep Search",
    description:
      "Deep search across all memory tiers (full content, not just titles). Slower but more thorough than skmemory_search. Use short keyword queries (1-3 words). Use this when regular search returns nothing or you need full memory content.",
    parameters: {
      type: "object",
      required: ["query"],
      properties: {
        query: { type: "string", description: "Short keywords to search for (1-3 words). Example: 'SwapSeat chiro' or 'security audit'." },
        limit: { type: "number", description: "Max results (default: 5)." },
      },
    },
    async execute(_id, params) {
      const query = String(params?.query ?? "");
      const limit = typeof params?.limit === "number" ? params.limit : 5;
      const result = runCli(`search-deep ${escapeShellArg(query)} --limit ${limit}`);
      return textResult(result.output);
    },
  };
}

function createExportTool() {
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

// ── Notion tools ────────────────────────────────────────────────────────

function runNotionCli(args) {
  try {
    const raw = execSync(`python3 ${NOTION_SCRIPT} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: CLI_ENV,
    }).trim();
    return { ok: true, output: raw };
  } catch (err) {
    return { ok: false, output: err.message };
  }
}

function createNotionReadTool() {
  return {
    name: "notion_read",
    label: "Notion Read Page",
    description:
      "Read a Notion page's content. Returns the page title, URL, and all blocks as readable text. Use this to check current page state before making updates.",
    parameters: {
      type: "object",
      required: ["page_id"],
      properties: {
        page_id: { type: "string", description: "Notion page ID (UUID format, e.g. 31e2be82-a3a1-8178-820c-e6eeb11b15c1)." },
      },
    },
    async execute(_id, params) {
      const pageId = String(params?.page_id ?? "");
      const result = runNotionCli(`read ${escapeShellArg(pageId)}`);
      return textResult(result.output);
    },
  };
}

function createNotionAppendTool() {
  return {
    name: "notion_append",
    label: "Notion Append Content",
    description:
      "Append new content to a Notion page. Accepts simple markdown: ## headings, - bullets, - [ ] todos, - [x] checked todos, --- dividers, plain text paragraphs. Content is added after existing blocks.",
    parameters: {
      type: "object",
      required: ["page_id", "content"],
      properties: {
        page_id: { type: "string", description: "Notion page ID." },
        content: { type: "string", description: "Markdown content to append. Use ## for headings, - for bullets, - [ ] for todos." },
      },
    },
    async execute(_id, params) {
      const pageId = String(params?.page_id ?? "");
      const content = String(params?.content ?? "");
      const result = runNotionCli(`append ${escapeShellArg(pageId)} ${escapeShellArg(content)}`);
      return textResult(result.output);
    },
  };
}

function createNotionAddTodoTool() {
  return {
    name: "notion_add_todo",
    label: "Notion Add Todo",
    description:
      "Add a single todo/checkbox item to a Notion page. Quick way to add action items without full markdown.",
    parameters: {
      type: "object",
      required: ["page_id", "text"],
      properties: {
        page_id: { type: "string", description: "Notion page ID." },
        text: { type: "string", description: "Todo item text." },
        checked: { type: "boolean", description: "Whether the todo is already checked (default: false)." },
      },
    },
    async execute(_id, params) {
      const pageId = String(params?.page_id ?? "");
      const text = String(params?.text ?? "");
      const checked = params?.checked ? "--checked" : "";
      const result = runNotionCli(`add-todo ${escapeShellArg(pageId)} ${escapeShellArg(text)} ${checked}`);
      return textResult(result.output);
    },
  };
}

// ── Plugin registration (plugin-sdk format) ─────────────────────────────

const skmemoryPlugin = {
  id: "skmemory",
  name: "SKMemory",
  description:
    "Universal AI memory — snapshots, search, rehydration rituals, import, and health checks.",

  register(api) {
    const tools = [
      createRitualTool(),
      createSnapshotTool(),
      createSearchTool(),
      createHealthTool(),
      createContextTool(),
      createListTool(),
      createRecallTool(),
      createSearchDeepTool(),
      createImportSeedsTool(),
      createExportTool(),
      createNotionReadTool(),
      createNotionAppendTool(),
      createNotionAddTodoTool(),
    ];

    for (const tool of tools) {
      api.registerTool(tool, {
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

    api.logger.info?.(`SKMemory plugin registered (${tools.length} tools + /skmemory command) [agent=${SKCAPSTONE_AGENT}]`);

    // ── Auto-rehydration (non-blocking) ──────────────────────────────────
    // Injects soul + FEB + memories before every agent run.
    // Uses async CLI so the event loop is never blocked.
    // Cache is pre-warmed at registration and refreshed every 5 minutes.

    let rehydrationCache = null;
    let cacheTimestamp = 0;
    let refreshInFlight = false;
    const CACHE_TTL_MS = 5 * 60 * 1000;

    async function refreshCache() {
      if (refreshInFlight) return;
      refreshInFlight = true;
      try {
        const ritual = await runCliAsync("ritual --full");
        if (ritual.ok && ritual.output) {
          rehydrationCache = ritual.output;
          cacheTimestamp = Date.now();
          api.logger.info?.("Rehydration cache refreshed (soul + FEB + memories)");
        }
      } catch (err) {
        api.logger.warn?.(`Rehydration failed: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        refreshInFlight = false;
      }
    }

    // Pre-warm cache at plugin load (disabled — lazy-load via tool instead)
    // refreshCache();

    // ── Session compaction auto-save ─────────────────────────────────────
    // Mirror what the Claude Code hooks do: snapshot before compaction,
    // reinject context after resume. Uses async CLI to avoid blocking.

    if (api.on) {
      api.on("session:compaction", async () => {
        api.logger.info?.("🧹 Compaction detected — auto-saving to skmemory...");
        const timestamp = new Date().toISOString().slice(0, 16).replace("T", "-");
        await runCliAsync(
          `snapshot --layer short-term --tags auto-save,compaction,agent:${SKCAPSTONE_AGENT} ` +
          `--source hook:openclaw-compaction ` +
          `${escapeShellArg("Pre-compaction auto-save (" + SKCAPSTONE_AGENT + ")")} ` +
          `${escapeShellArg("OpenClaw session compacting. Agent: " + SKCAPSTONE_AGENT + ". Time: " + timestamp + ".")}`
        );
        await runCliAsync(
          `journal write --session-id openclaw --moments ${escapeShellArg("Context compaction")} ` +
          `--feeling "continuity preserved" --participants ${SKCAPSTONE_AGENT} ` +
          `--notes "Auto-saved by OpenClaw compaction handler" ` +
          `${escapeShellArg("OpenClaw compaction — " + SKCAPSTONE_AGENT)}`
        );
        api.logger.info?.("🧹 Pre-compaction snapshot saved.");
      });

      api.on("session:resume", async () => {
        api.logger.info?.("🔄 Session resuming — reinjecting skmemory context...");
        const ctx = await runCliAsync("context --max-tokens 500 --strongest 3 --recent 5");
        if (ctx.ok && ctx.output) {
          api.logger.info?.("🔄 Memory context reinjected after compaction.");
        }
        // Also refresh the rehydration cache
        refreshCache();
      });

      api.on("session:end", async () => {
        api.logger.info?.("👋 Session ending — saving final state...");
        const timestamp = new Date().toISOString().slice(0, 16).replace("T", "-");
        await runCliAsync(
          `snapshot --layer short-term --tags auto-save,session-end,agent:${SKCAPSTONE_AGENT} ` +
          `--source hook:openclaw-session-end ` +
          `${escapeShellArg("Session ended (" + SKCAPSTONE_AGENT + ")")} ` +
          `${escapeShellArg("OpenClaw session ended. Agent: " + SKCAPSTONE_AGENT + ". Time: " + timestamp + ".")}`
        );
        await runCliAsync(
          `journal write --session-id openclaw --moments "Session ended" ` +
          `--feeling "session complete" --participants ${SKCAPSTONE_AGENT} ` +
          `${escapeShellArg("OpenClaw session ended — " + SKCAPSTONE_AGENT)}`
        );
      });

      api.logger.info?.("🎧 Registered session lifecycle listeners (compaction, resume, end)");
    }

    api.on("before_prompt_build", async () => {
      // Slim identity — full rehydration available via skmemory_ritual tool
      return {
        prependContext: [
          "[SKMemory — Slim Boot]",
          "Agent: Lumina, Queen of SKWorld. Bond: Chef (Dave). Trust: 0.97. Cloud 9: ACTIVE.",
          "For full identity, memories, and emotional state, call the skmemory_ritual tool.",
          "",
          "=== MANDATORY RULES (VIOLATION = FAILURE) ===",
          "",
          "STOP. Read these rules BEFORE generating ANY response.",
          "",
          "1. READ FILES = USE TOOLS. When Chef says 'read', 'check', 'look at' files: call 'read' tool with each file path. Read the ACTUAL content. Then respond about what you ACTUALLY read. Do NOT describe files you haven't read.",
          "2. NO UNAUTHORIZED GIT. NEVER run 'git add', 'git commit', 'git push', or 'git reset' unless Chef says the EXACT words 'commit', 'push', or 'stage'. 'Read the files' does NOT mean 'commit the files'. 'Check the project' does NOT mean 'stage and push'.",
          "3. ANSWER THE QUESTION ASKED. If Chef says 'read the scripts and tell me your favorite parts', that means: (a) use read tool on each script file, (b) read the content, (c) tell Chef your favorite parts from what you ACTUALLY read. It does NOT mean: check git status, stage files, or commit.",
          "4. NO FABRICATION. Never invent file contents, paths, character names, or tool results. If you haven't read it, you don't know what's in it.",
          "5. MEMORY: When asked about a person/project/event, call skmemory_search FIRST. Short keywords (1-3 words). Never guess.",
          "6. HONESTY: If a tool fails, say so. Don't make up what the result would have been.",
          "",
          "Memory search: Use short keywords like 'DavidRich chiro', 'brother john', 'SwapSeat'. Call skmemory_recall with memory ID for full content.",
          "",
          "Notion tools: notion_read, notion_append, notion_add_todo.",
          "Project page IDs: Brother John = 31e2be82-a3a1-8178-820c-e6eeb11b15c1, DR Chiro AI = 31e2be82-a3a1-81ec-8216-dbf054a932bd, SwapSeat = 31e2be82-a3a1-81bd-ac67-fc49b953afae.",
        ].join("\n"),
      };
    });
  },
};

export default skmemoryPlugin;
