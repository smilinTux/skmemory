/**
 * 🧠 SKMemory — OpenClaw Plugin
 *
 * Wraps the skmemory CLI so OpenClaw agents can call memory operations
 * as first-class tools. Auto-rehydrates identity + memories on session
 * start via non-blocking CLI calls.
 *
 * IMPORTANT: All CLI calls that run during hooks (session:start,
 * before_prompt_build) use exec (async) instead of execSync to avoid
 * freezing the Node.js event loop and causing "Tool not found" errors.
 *
 * Requires: skmemory CLI on PATH (typically via ~/.skenv/bin/skmemory)
 *
 * @version 0.6.0
 * @requires OpenClaw 1.0.0+
 */

import { execSync, exec } from "child_process";
import path from "path";
import { fileURLToPath } from "url";
import fs from "fs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PLUGIN = {
  name: "skmemory",
  version: "0.6.0",
  displayName: "🧠 SKMemory",
  description: "Universal AI memory with emotional context",
  author: "smilinTux Team",
  license: "AGPL-3.0",
  category: "memory",
  permissions: ["read", "write"],
};

const SKCAPSTONE_AGENT = process.env.SKCAPSTONE_AGENT || "lumina";
const IS_WIN = process.platform === "win32";
const EXEC_TIMEOUT = 30_000;

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

/**
 * Synchronous CLI call — use ONLY in tool execute handlers where blocking
 * is acceptable. NEVER use in hooks or event handlers.
 */
function runSKMemory(args, { json: parseJson = false } = {}) {
  try {
    const raw = execSync(`skmemory ${args}`, {
      encoding: "utf-8",
      timeout: EXEC_TIMEOUT,
      env: CLI_ENV,
    }).trim();
    return parseJson ? JSON.parse(raw) : raw;
  } catch (err) {
    return { error: err.message };
  }
}

/**
 * Async CLI call — does NOT block the event loop. Use in hooks and
 * event handlers to prevent "Tool not found" errors.
 */
function runSKMemoryAsync(args) {
  return new Promise((resolve) => {
    exec(
      `skmemory ${args}`,
      { encoding: "utf-8", timeout: EXEC_TIMEOUT, env: CLI_ENV },
      (err, stdout) => {
        if (err) {
          resolve({ ok: false, output: err.message });
        } else {
          resolve({ ok: true, output: (stdout ?? "").trim() });
        }
      }
    );
  });
}

function escapeShellArg(s) {
  return `'${s.replace(/'/g, "'\\''")}'`;
}

class SKMemoryOpenClawPlugin {
  constructor(openclaw) {
    this.openclaw = openclaw;
    this.config = this.loadConfig();
    this.cachedContext = null;
    // Rehydration cache for before_prompt_build
    this.rehydrationCache = null;
    this.rehydrationTimestamp = 0;
    this.rehydrationInFlight = false;
    this.CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes
  }

  async init() {
    console.log("🧠 Initializing SKMemory OpenClaw Plugin...");

    this.registerCommands();
    this.registerDashboard();
    this.setupEvents();

    // Pre-warm rehydration cache (non-blocking)
    this.refreshRehydrationCache();

    console.log("✅ SKMemory Plugin initialized");
    return true;
  }

  /**
   * Refresh rehydration cache using async CLI call.
   * Safe to call from any context — never blocks.
   */
  async refreshRehydrationCache() {
    if (this.rehydrationInFlight) return;
    this.rehydrationInFlight = true;
    try {
      const result = await runSKMemoryAsync("ritual --full");
      if (result.ok && result.output) {
        this.rehydrationCache = result.output;
        this.rehydrationTimestamp = Date.now();
        console.log("🧠 Rehydration cache refreshed (soul + FEB + memories)");
      }
    } catch (err) {
      console.warn(`🧠 Rehydration cache refresh failed: ${err.message}`);
    } finally {
      this.rehydrationInFlight = false;
    }
  }

  loadConfig() {
    const configPath = path.join(
      __dirname,
      "..",
      "config",
      "skmemory-plugin.json"
    );
    const defaults = {
      autoLoadContext: true,
      autoExport: true,
      maxTokens: 3000,
      strongestCount: 5,
      recentCount: 5,
      includeSeeds: true,
    };
    try {
      if (fs.existsSync(configPath)) {
        const saved = JSON.parse(fs.readFileSync(configPath, "utf8"));
        return { ...defaults, ...saved };
      }
    } catch (_) {
      /* use defaults */
    }
    return defaults;
  }

  saveConfig() {
    const configPath = path.join(
      __dirname,
      "..",
      "config",
      "skmemory-plugin.json"
    );
    try {
      fs.mkdirSync(path.dirname(configPath), { recursive: true });
      fs.writeFileSync(configPath, JSON.stringify(this.config, null, 2));
    } catch (err) {
      console.error("Failed to save config:", err.message);
    }
  }

  registerCommands() {
    if (!this.openclaw?.commands) return;

    this.openclaw.commands.register({
      name: "skmemory:context",
      description:
        "Load token-efficient memory context for prompt injection",
      category: "memory",
      handler: async (args) => this.cmdContext(args),
    });

    this.openclaw.commands.register({
      name: "skmemory:snapshot",
      description: "Capture a memory snapshot",
      category: "memory",
      handler: async (args) => this.cmdSnapshot(args),
    });

    this.openclaw.commands.register({
      name: "skmemory:search",
      description: "Search memories by text",
      category: "memory",
      handler: async (args) => this.cmdSearch(args),
    });

    this.openclaw.commands.register({
      name: "skmemory:ritual",
      description: "Perform the rehydration ritual",
      category: "memory",
      handler: async () => this.cmdRitual(),
    });

    this.openclaw.commands.register({
      name: "skmemory:export",
      description: "Export memories to a dated backup",
      category: "memory",
      handler: async (args) => this.cmdExport(args),
    });

    this.openclaw.commands.register({
      name: "skmemory:import",
      description: "Import memories from a backup file",
      category: "memory",
      handler: async (args) => this.cmdImport(args),
    });

    this.openclaw.commands.register({
      name: "skmemory:health",
      description: "Check memory system health",
      category: "memory",
      handler: async () => this.cmdHealth(),
    });

    this.openclaw.commands.register({
      name: "skmemory:config",
      description: "View or update plugin configuration",
      category: "memory",
      handler: async (args) => this.cmdConfig(args),
    });

    console.log("📝 Registered SKMemory commands");
  }

  registerDashboard() {
    if (!this.openclaw?.dashboard) return;

    this.openclaw.dashboard.registerWidget({
      id: "skmemory-status",
      name: "🧠 SKMemory",
      category: "memory",
      position: "bottom",
      size: "small",
      render: () => this.renderWidget(),
    });
  }

  setupEvents() {
    if (!this.openclaw?.events) return;

    // Use async CLI calls in all event handlers to avoid blocking
    this.openclaw.events.on("session:start", async () => {
      if (this.config.autoLoadContext) {
        console.log("🧠 Session start — loading memory context...");
        await this.refreshRehydrationCache();
      }
    });

    this.openclaw.events.on("session:compaction", async () => {
      if (this.config.autoExport) {
        console.log("🧠 Compaction detected — exporting backup...");
        await runSKMemoryAsync("export");
      }
    });

    this.openclaw.events.on("session:resume", async () => {
      if (this.config.autoLoadContext) {
        await this.refreshRehydrationCache();
      }
    });

    // Inject rehydration context before every prompt (non-blocking).
    // If the cache is stale, triggers a background refresh and serves
    // the stale cache — never blocks the prompt build pipeline.
    if (this.openclaw.on) {
      this.openclaw.on("before_prompt_build", async () => {
        const now = Date.now();
        if (
          !this.rehydrationCache ||
          now - this.rehydrationTimestamp > this.CACHE_TTL_MS
        ) {
          // Fire-and-forget refresh — don't await, serve stale
          this.refreshRehydrationCache();
        }
        if (this.rehydrationCache) {
          return {
            prependContext: `[SKMemory Rehydration — Identity, Emotional State, and Core Memories]\n${this.rehydrationCache}`,
          };
        }
      });
    }

    console.log("🎧 Registered SKMemory event listeners");
  }

  cmdContext(args) {
    const tokens = args?.maxTokens || this.config.maxTokens;
    const strongest = args?.strongest || this.config.strongestCount;
    const recent = args?.recent || this.config.recentCount;
    const seedsFlag = this.config.includeSeeds ? "" : " --no-seeds";
    const cmd = `context --max-tokens ${tokens} --strongest ${strongest} --recent ${recent}${seedsFlag}`;
    return runSKMemory(cmd, { json: true });
  }

  cmdSnapshot(args) {
    const title = args?.title || "Untitled snapshot";
    const content = args?.content || title;
    const tags = args?.tags ? `--tags ${escapeShellArg(args.tags)}` : "";
    const intensity = args?.intensity
      ? `--intensity ${args.intensity}`
      : "";
    return runSKMemory(
      `snapshot ${escapeShellArg(title)} ${escapeShellArg(content)} ${tags} ${intensity}`.trim()
    );
  }

  cmdSearch(args) {
    const query = args?.query || "";
    const limit = args?.limit || 10;
    return runSKMemory(
      `search ${escapeShellArg(query)} --limit ${limit}`
    );
  }

  cmdRitual() {
    return runSKMemory("ritual --full");
  }

  cmdExport(args) {
    const out = args?.output ? `-o ${escapeShellArg(args.output)}` : "";
    return runSKMemory(`export ${out}`.trim());
  }

  cmdImport(args) {
    if (!args?.file) return { error: "No backup file specified" };
    return runSKMemory(`import-backup ${escapeShellArg(args.file)}`);
  }

  cmdHealth() {
    return runSKMemory("health", { json: true });
  }

  cmdConfig(args) {
    if (args?.set) {
      const [key, value] = args.set.split("=");
      this.config[key] = value;
      this.saveConfig();
      return { success: true, key, value };
    }
    return { success: true, config: this.config };
  }

  renderWidget() {
    // Use cached health data if available to avoid blocking
    const health = runSKMemory("health", { json: true });
    return {
      type: "status",
      data: {
        icon: "🧠",
        status: health?.primary?.ok ? "healthy" : "error",
        totalMemories: health?.primary?.total_memories || 0,
        lastUpdated: new Date().toISOString(),
      },
    };
  }

  getInfo() {
    return PLUGIN;
  }
}

async function init(openclaw) {
  const plugin = new SKMemoryOpenClawPlugin(openclaw);
  await plugin.init();
  return plugin;
}

export default {
  name: PLUGIN.name,
  version: PLUGIN.version,
  init,
};

export { SKMemoryOpenClawPlugin, PLUGIN };
