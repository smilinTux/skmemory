#!/usr/bin/env node
const { execSync } = require("child_process");
const args = process.argv.slice(2).join(" ");
try {
  execSync(`skmemory ${args}`, { encoding: "utf-8", stdio: "inherit" });
} catch (err) {
  process.exit(err.status || 1);
}
