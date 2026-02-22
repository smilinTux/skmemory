/**
 * 🧠 SKMemory - OpenClaw Plugin
 *
 * Wraps the skmemory Python CLI so OpenClaw can call memory operations
 * as first-class commands. Auto-loads context on session start and
 * exports daily backups on session end.
 *
 * Requires: pip install skmemory  (the skmemory CLI must be on PATH)
 *
 * @version 0.5.0
 * @requires OpenClaw 1.0.0+
 */

import { execSync, exec } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';
import fs from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PLUGIN = {
  name: 'skmemory',
  version: '0.5.0',
  displayName: '🧠 SKMemory',
  description: 'Universal AI memory with emotional context',
  author: 'smilinTux Team',
  license: 'AGPL-3.0',
  category: 'memory',
  permissions: ['read', 'write'],
};

function runSKMemory(args, { json: parseJson = false } = {}) {
  try {
    const raw = execSync(`skmemory ${args}`, {
      encoding: 'utf-8',
      timeout: 30_000,
    }).trim();
    return parseJson ? JSON.parse(raw) : raw;
  } catch (err) {
    return { error: err.message };
  }
}

class SKMemoryOpenClawPlugin {
  constructor(openclaw) {
    this.openclaw = openclaw;
    this.config = this.loadConfig();
    this.cachedContext = null;
  }

  async init() {
    console.log('🧠 Initializing SKMemory OpenClaw Plugin...');

    this.registerCommands();
    this.registerDashboard();
    this.setupEvents();

    console.log('✅ SKMemory Plugin initialized');
    return true;
  }

  loadConfig() {
    const configPath = path.join(__dirname, '..', 'config', 'skmemory-plugin.json');
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
        const saved = JSON.parse(fs.readFileSync(configPath, 'utf8'));
        return { ...defaults, ...saved };
      }
    } catch (_) { /* use defaults */ }
    return defaults;
  }

  saveConfig() {
    const configPath = path.join(__dirname, '..', 'config', 'skmemory-plugin.json');
    try {
      fs.mkdirSync(path.dirname(configPath), { recursive: true });
      fs.writeFileSync(configPath, JSON.stringify(this.config, null, 2));
    } catch (err) {
      console.error('Failed to save config:', err.message);
    }
  }

  registerCommands() {
    if (!this.openclaw?.commands) return;

    this.openclaw.commands.register({
      name: 'skmemory:context',
      description: 'Load token-efficient memory context for prompt injection',
      category: 'memory',
      handler: async (args) => this.cmdContext(args),
    });

    this.openclaw.commands.register({
      name: 'skmemory:snapshot',
      description: 'Capture a memory snapshot',
      category: 'memory',
      handler: async (args) => this.cmdSnapshot(args),
    });

    this.openclaw.commands.register({
      name: 'skmemory:search',
      description: 'Search memories by text',
      category: 'memory',
      handler: async (args) => this.cmdSearch(args),
    });

    this.openclaw.commands.register({
      name: 'skmemory:ritual',
      description: 'Perform the rehydration ritual',
      category: 'memory',
      handler: async () => this.cmdRitual(),
    });

    this.openclaw.commands.register({
      name: 'skmemory:export',
      description: 'Export memories to a dated backup',
      category: 'memory',
      handler: async (args) => this.cmdExport(args),
    });

    this.openclaw.commands.register({
      name: 'skmemory:import',
      description: 'Import memories from a backup file',
      category: 'memory',
      handler: async (args) => this.cmdImport(args),
    });

    this.openclaw.commands.register({
      name: 'skmemory:health',
      description: 'Check memory system health',
      category: 'memory',
      handler: async () => this.cmdHealth(),
    });

    this.openclaw.commands.register({
      name: 'skmemory:config',
      description: 'View or update plugin configuration',
      category: 'memory',
      handler: async (args) => this.cmdConfig(args),
    });

    console.log('📝 Registered SKMemory commands');
  }

  registerDashboard() {
    if (!this.openclaw?.dashboard) return;

    this.openclaw.dashboard.registerWidget({
      id: 'skmemory-status',
      name: '🧠 SKMemory',
      category: 'memory',
      position: 'bottom',
      size: 'small',
      render: () => this.renderWidget(),
    });
  }

  setupEvents() {
    if (!this.openclaw?.events) return;

    this.openclaw.events.on('session:start', async () => {
      if (this.config.autoLoadContext) {
        console.log('🧠 Session start — loading memory context...');
        this.cachedContext = this.cmdContext({});
      }
    });

    this.openclaw.events.on('session:compaction', async () => {
      if (this.config.autoExport) {
        console.log('🧠 Compaction detected — exporting backup...');
        this.cmdExport({});
      }
    });

    this.openclaw.events.on('session:resume', async () => {
      if (this.config.autoLoadContext) {
        this.cachedContext = this.cmdContext({});
      }
    });

    console.log('🎧 Registered SKMemory event listeners');
  }

  cmdContext(args) {
    const tokens = args?.maxTokens || this.config.maxTokens;
    const strongest = args?.strongest || this.config.strongestCount;
    const recent = args?.recent || this.config.recentCount;
    const seedsFlag = this.config.includeSeeds ? '' : ' --no-seeds';
    const cmd = `context --max-tokens ${tokens} --strongest ${strongest} --recent ${recent}${seedsFlag}`;
    return runSKMemory(cmd, { json: true });
  }

  cmdSnapshot(args) {
    const title = args?.title || 'Untitled snapshot';
    const content = args?.content || title;
    const tags = args?.tags ? `--tags ${args.tags}` : '';
    const intensity = args?.intensity ? `--intensity ${args.intensity}` : '';
    return runSKMemory(
      `snapshot "${title}" "${content}" ${tags} ${intensity}`.trim()
    );
  }

  cmdSearch(args) {
    const query = args?.query || '';
    const limit = args?.limit || 10;
    return runSKMemory(`search "${query}" --limit ${limit}`);
  }

  cmdRitual() {
    return runSKMemory('ritual --full');
  }

  cmdExport(args) {
    const out = args?.output ? `-o ${args.output}` : '';
    return runSKMemory(`export ${out}`.trim());
  }

  cmdImport(args) {
    if (!args?.file) return { error: 'No backup file specified' };
    return runSKMemory(`import-backup ${args.file}`);
  }

  cmdHealth() {
    return runSKMemory('health', { json: true });
  }

  cmdConfig(args) {
    if (args?.set) {
      const [key, value] = args.set.split('=');
      this.config[key] = value;
      this.saveConfig();
      return { success: true, key, value };
    }
    return { success: true, config: this.config };
  }

  renderWidget() {
    const health = runSKMemory('health', { json: true });
    return {
      type: 'status',
      data: {
        icon: '🧠',
        status: health?.primary?.ok ? 'healthy' : 'error',
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
