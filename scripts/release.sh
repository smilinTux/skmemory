#!/usr/bin/env bash
# Usage: ./scripts/release.sh [patch|minor|major|X.Y.Z]
#
# Bumps version in pyproject.toml, commits, tags, and pushes.
# Pushing the tag triggers the publish workflow (PyPI + npm).
#
# Examples:
#   ./scripts/release.sh patch        # 0.9.3 → 0.9.4
#   ./scripts/release.sh minor        # 0.9.3 → 0.10.0
#   ./scripts/release.sh 1.0.0        # set explicit version

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYPROJECT="$REPO_ROOT/pyproject.toml"
PACKAGE_JSON="$REPO_ROOT/package.json"
BUMP="${1:-patch}"

# ── helpers ──────────────────────────────────────────────────────────────────

current_version() {
    python3 -c "
import re, sys
text = open('$PYPROJECT').read()
m = re.search(r'^version\s*=\s*\"([^\"]+)\"', text, re.MULTILINE)
if not m:
    sys.exit('ERROR: cannot find version in pyproject.toml')
print(m.group(1))
"
}

bump_version() {
    local current="$1" part="$2"
    python3 -c "
current = '$current'
part    = '$part'
major, minor, patch = map(int, current.split('.'))
if part == 'major':
    print(f'{major+1}.0.0')
elif part == 'minor':
    print(f'{major}.{minor+1}.0')
elif part == 'patch':
    print(f'{major}.{minor}.{patch+1}')
else:
    # validate explicit version
    parts = part.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f'Invalid version: {part!r}')
    print(part)
"
}

write_version() {
    local old="$1" new="$2"
    python3 -c "
import re
text = open('$PYPROJECT').read()
new_text = re.sub(r'^(version\s*=\s*)\"[^\"]+\"', r'\g<1>\"$new\"', text, count=1, flags=re.MULTILINE)
open('$PYPROJECT', 'w').write(new_text)
"
    # Sync package.json version too
    python3 -c "
import json
with open('$PACKAGE_JSON') as f:
    pkg = json.load(f)
pkg['version'] = '$new'
with open('$PACKAGE_JSON', 'w') as f:
    json.dump(pkg, f, indent=2)
    f.write('\n')
"
}

# ── main ─────────────────────────────────────────────────────────────────────

OLD_VERSION=$(current_version)
NEW_VERSION=$(bump_version "$OLD_VERSION" "$BUMP")

echo "skmemory release: $OLD_VERSION → $NEW_VERSION"
echo ""

# Confirm
read -rp "Proceed? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

write_version "$OLD_VERSION" "$NEW_VERSION"
echo "  Updated pyproject.toml: $OLD_VERSION → $NEW_VERSION"
echo "  Updated package.json:   $OLD_VERSION → $NEW_VERSION"

cd "$REPO_ROOT"
git add pyproject.toml package.json
git commit -m "chore: bump version to $NEW_VERSION"

TAG="v$NEW_VERSION"
git tag -a "$TAG" -m "Release $TAG"
echo "  Tagged: $TAG"

echo ""
echo "Pushing commit and tag to origin..."
git push
git push --tags

echo ""
echo "Done. GitHub Actions will now:"
echo "  1. Run tests"
echo "  2. Publish skmemory $NEW_VERSION to PyPI"
echo "  3. Publish @smilintux/skmemory $NEW_VERSION to npm"
