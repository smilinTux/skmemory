/**
 * @smilintux/skmemory
 *
 * SKMemory - Universal AI memory system.
 * JS/TS bridge to the Python skmemory package.
 * Install: pip install skmemory
 */

const { execSync } = require("child_process");

const VERSION = "0.5.0";
const PYTHON_PACKAGE = "skmemory";

function checkInstalled() {
  for (const py of ["python3", "python"]) {
    try {
      execSync(`${py} -c "import skmemory"`, { stdio: "pipe" });
      return true;
    } catch {}
  }
  return false;
}

function run(args) {
  return execSync(`skmemory ${args}`, { encoding: "utf-8" });
}

module.exports = { VERSION, PYTHON_PACKAGE, checkInstalled, run };
