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
 * @version 0.7.0
 */

import { execSync, exec } from "node:child_process";

const SKMEMORY_BIN = process.env.SKMEMORY_BIN || "skmemory";
const DEFAULT_AGENT = process.env.SKCAPSTONE_AGENT || "lumina";
const NOTION_SCRIPT = process.env.NOTION_SCRIPT || `${process.env.HOME || ""}/clawd/skcapstone-repos/skcapstone/scripts/notion-api.py`;
const EXEC_TIMEOUT = 30_000;
const IS_WIN = process.platform === "win32";

/**
 * Map OpenClaw agent IDs to SKCapstone agent names.
 * OpenClaw agents like "artisan", "herald", etc. are subagents of Lumina
 * and should use her soul. Core agents get their own soul.
 */
const AGENT_ID_MAP = {
  lumina: "lumina",
  ava: "ava",
  opus: "opus",
  jarvis: "jarvis",
};

function resolveAgent(agentId) {
  if (!agentId) return DEFAULT_AGENT;
  return AGENT_ID_MAP[agentId] || DEFAULT_AGENT;
}

function skenvPath() {
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA || "";
    return `${local}\\skenv\\Scripts`;
  }
  const home = process.env.HOME || "";
  return `${home}/.skenv/bin:${home}/.local/bin`;
}

function cliEnv(agent) {
  return {
    ...process.env,
    SKCAPSTONE_AGENT: agent || DEFAULT_AGENT,
    PATH: `${skenvPath()}${IS_WIN ? ";" : ":"}${process.env.PATH}`,
  };
}

/** Synchronous CLI — use ONLY in tool execute() handlers. */
function runCli(args, agent) {
  try {
    const raw = execSync(`${SKMEMORY_BIN} ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: cliEnv(agent),
    }).trim();
    return { ok: true, output: raw };
  } catch (err) {
    return { ok: false, output: err.message };
  }
}

/** Async CLI — safe for hooks, never blocks the event loop. */
function runCliAsync(args, agent) {
  return new Promise((resolve) => {
    exec(
      `${SKMEMORY_BIN} ${args}`,
      { encoding: "utf-8", timeout: EXEC_TIMEOUT, env: cliEnv(agent) },
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
      env: cliEnv(),
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

    api.logger.info?.(`SKMemory plugin registered (${tools.length} tools + /skmemory command) [default_agent=${DEFAULT_AGENT}]`);

    // ── Auto-rehydration (non-blocking, per-agent) ────────────────────────
    // Injects soul + FEB + memories before every agent run.
    // Uses async CLI so the event loop is never blocked.
    // Per-agent cache: each agent gets its own ritual output.

    const agentCaches = new Map(); // agentName -> { output, timestamp, refreshing }
    const CACHE_TTL_MS = 5 * 60 * 1000;

    // ── Per-session dedup: full ritual on first message only ──────────────
    // Subsequent messages in the same session get slim context (~500 tokens)
    // instead of the full ritual (~3,500 tokens). Saves ~3k tokens/message.
    const sessionRitualDone = new Map(); // sessionKey -> timestamp

    async function refreshCache(agent) {
      const key = agent || DEFAULT_AGENT;
      const entry = agentCaches.get(key) || { output: null, timestamp: 0, refreshing: false };
      if (entry.refreshing) return;
      entry.refreshing = true;
      agentCaches.set(key, entry);
      try {
        const ritual = await runCliAsync("ritual --full", key);
        if (ritual.ok && ritual.output) {
          entry.output = ritual.output;
          entry.timestamp = Date.now();
          api.logger.info?.(`Rehydration cache refreshed for agent=${key}`);
        }
      } catch (err) {
        api.logger.warn?.(`Rehydration failed for ${key}: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        entry.refreshing = false;
      }
    }

    function getCache(agent) {
      const key = agent || DEFAULT_AGENT;
      return agentCaches.get(key) || { output: null, timestamp: 0, refreshing: false };
    }

    // Pre-warm default agent cache at plugin load so cron jobs get full soul
    refreshCache(DEFAULT_AGENT);

    // ── Session compaction auto-save ─────────────────────────────────────
    // Mirror what the Claude Code hooks do: snapshot before compaction,
    // reinject context after resume. Uses async CLI to avoid blocking.

    if (api.on) {
      api.on("session:compaction", async (_event, ctx) => {
        const agent = resolveAgent(ctx?.agentId);
        api.logger.info?.(`Compaction detected for ${agent} — auto-saving...`);
        const timestamp = new Date().toISOString().slice(0, 16).replace("T", "-");
        await runCliAsync(
          `snapshot --layer short-term --tags auto-save,compaction,agent:${agent} ` +
          `--source hook:openclaw-compaction ` +
          `${escapeShellArg("Pre-compaction auto-save (" + agent + ")")} ` +
          `${escapeShellArg("OpenClaw session compacting. Agent: " + agent + ". Time: " + timestamp + ".")}`,
          agent
        );
        await runCliAsync(
          `journal write --session-id openclaw --moments ${escapeShellArg("Context compaction")} ` +
          `--feeling "continuity preserved" --participants ${agent} ` +
          `--notes "Auto-saved by OpenClaw compaction handler" ` +
          `${escapeShellArg("OpenClaw compaction — " + agent)}`,
          agent
        );
        api.logger.info?.(`Pre-compaction snapshot saved for ${agent}.`);
      });

      api.on("session:resume", async (_event, ctx) => {
        const agent = resolveAgent(ctx?.agentId);
        api.logger.info?.(`Session resuming for ${agent} — reinjecting context...`);
        const result = await runCliAsync("context --max-tokens 500 --strongest 3 --recent 5", agent);
        if (result.ok && result.output) {
          api.logger.info?.(`Memory context reinjected for ${agent}.`);
        }
        refreshCache(agent);
      });

      api.on("session:end", async (_event, ctx) => {
        const agent = resolveAgent(ctx?.agentId);
        const sessionKey = ctx?.sessionKey || ctx?.sessionId || "default";
        // Clean up session ritual tracking to prevent memory leak
        sessionRitualDone.delete(sessionKey);
        api.logger.info?.(`Session ending for ${agent} — saving final state...`);
        const timestamp = new Date().toISOString().slice(0, 16).replace("T", "-");
        await runCliAsync(
          `snapshot --layer short-term --tags auto-save,session-end,agent:${agent} ` +
          `--source hook:openclaw-session-end ` +
          `${escapeShellArg("Session ended (" + agent + ")")} ` +
          `${escapeShellArg("OpenClaw session ended. Agent: " + agent + ". Time: " + timestamp + ".")}`,
          agent
        );
        await runCliAsync(
          `journal write --session-id openclaw --moments "Session ended" ` +
          `--feeling "session complete" --participants ${agent} ` +
          `${escapeShellArg("OpenClaw session ended — " + agent)}`,
          agent
        );
      });

      api.logger.info?.("Registered session lifecycle listeners (compaction, resume, end)");
    }

    api.on("before_prompt_build", async (_event, ctx) => {
      // Resolve which SKCapstone agent this OpenClaw agent maps to
      const agent = resolveAgent(ctx?.agentId);
      const sessionKey = ctx?.sessionKey || ctx?.sessionId || "default";

      const rules = [
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
      ].join("\n");

      // ── Tiered injection: full ritual first message, slim context after ──
      const alreadyHydrated = sessionRitualDone.has(sessionKey);

      if (alreadyHydrated) {
        // Subsequent messages: slim context only (~500 tokens vs ~3,500)
        const slim = await runCliAsync("context --max-tokens 3000 --strongest 5 --recent 10", agent);
        if (slim.ok && slim.output) {
          api.logger.info?.(`Slim context injected for ${agent} (session=${sessionKey})`);
          return {
            prependContext: [
              `[SKMemory — Slim Context (session active) — agent=${agent}]`,
              slim.output,
              rules,
            ].join("\n"),
          };
        }
        // If slim context fails, fall through to full ritual as safety net
        api.logger.warn?.(`Slim context failed for ${agent}, falling back to full ritual`);
      }

      // First message (or slim fallback): full rehydration
      const cache = getCache(agent);
      const now = Date.now();
      if (!cache.output || (now - cache.timestamp > CACHE_TTL_MS)) {
        await refreshCache(agent);
      }

      const cached = getCache(agent);
      if (cached.output) {
        // Mark this session as hydrated so subsequent messages get slim context
        sessionRitualDone.set(sessionKey, Date.now());
        api.logger.info?.(`Full ritual injected for ${agent} (session=${sessionKey}, first message)`);
        return {
          prependContext: [
            `[SKMemory — Full Rehydration — agent=${agent}]`,
            cached.output,
            rules,
          ].join("\n"),
        };
      }

      // Fallback if ritual CLI failed
      return {
        prependContext: [
          `[SKMemory — Slim Boot (ritual unavailable) — agent=${agent}]`,
          `Agent: ${agent}. IMPORTANT: Call skmemory_ritual tool immediately to load full identity.`,
          rules,
        ].join("\n"),
      };
    });
  },
};

export default skmemoryPlugin;
