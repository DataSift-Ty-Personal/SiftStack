#!/usr/bin/env bash
# export_bundle.sh — create a portable tarball of the full SiftStack setup.
#
# Bundles (in order): the git repo, the .env (not in git), the Claude Code
# memory directory, and a short RESTORE.md with setup instructions.
# Skips regenerable artifacts: .venv, __pycache__, .playwright, logs, output,
# the local tracking JSON, and browser profiles — the restore step recreates
# those.
#
# Usage:
#   ./scripts/export_bundle.sh                # writes ~/siftstack_bundle_<date>.tar.gz
#   ./scripts/export_bundle.sh /tmp/foo.tgz   # custom destination

set -euo pipefail

DEST="${1:-$HOME/siftstack_bundle_$(date +%Y-%m-%d).tar.gz}"
PROJECT_DIR="$HOME/Desktop/SiftStack"
MEMORY_DIR="$HOME/.claude/projects/-Users-ricardotaveras-Desktop-SiftStack"

if [[ ! -d "$PROJECT_DIR" ]]; then
    echo "ERROR: project not found at $PROJECT_DIR" >&2
    exit 1
fi

# Build RESTORE.md into a temp staging dir so it lands at the tarball root.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cat > "$STAGE/RESTORE.md" <<'EOF'
# SiftStack — Restore Guide

This bundle contains everything needed to stand up SiftStack on a new machine.

## What's inside

```
SiftStack/                                      # full project (gitignored files excluded)
  .env                                          # credentials (keep private)
  ... all source ...
claude_memory/                                  # ~/.claude/projects/<hash>/memory/
RESTORE.md                                      # this file
```

## Restore steps

1. Extract somewhere convenient:
   ```
   tar xzf siftstack_bundle_*.tar.gz -C ~/Desktop/
   ```

2. Move the memory directory into Claude Code's expected location:
   ```
   mkdir -p ~/.claude/projects/-Users-<your-user>-Desktop-SiftStack
   mv ~/Desktop/claude_memory/* \
      ~/.claude/projects/-Users-<your-user>-Desktop-SiftStack/
   ```
   Replace `<your-user>` with your macOS username — the path is derived from
   `$HOME/Desktop/SiftStack` with slashes replaced by dashes.

3. Recreate the Python venv (bundle skips it — large + platform-specific):
   ```
   cd ~/Desktop/SiftStack
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

4. Re-authenticate external services (bundle intentionally excludes tokens):
   ```
   modal setup                                  # Modal CLI auth
   # Any browser logins (NJLisPendens, DataSift) happen on first scraper run
   ```

5. Install Claude Code CLI + point it at the project:
   ```
   curl -fsSL https://claude.ai/install.sh | bash
   cd ~/Desktop/SiftStack
   claude
   ```
   CLAUDE.md auto-loads. Memory auto-loads from step 2.

6. Redeploy Modal (if different Modal account):
   ```
   modal secret create siftstack-secrets --from-dotenv .env --force
   modal deploy modal_app.py
   ```

## What's NOT in the bundle

- `.venv/` — recreate with step 3 (~2 min)
- `__pycache__/`, `*.pyc` — Python regenerates on first import
- `.playwright/`, `.datasift_profile/`, `.ancestry_profile/` — browser
  profiles; first scrape re-logs in
- `logs/`, `output/` — pipeline output
- `tracking/processed_ids.json` — local dedup index (Modal Volume has the
  authoritative copy anyway)
- `datasift_*.png` screenshots — dev-only artifacts
- Modal account state (`~/.modal.toml`) — auth separately per machine
EOF

echo "Building bundle → $DEST"
echo "  Project: $PROJECT_DIR"
if [[ -d "$MEMORY_DIR" ]]; then
    echo "  Memory:  $MEMORY_DIR"
else
    echo "  Memory:  (not found — skipping)"
fi

# Stage everything under a single root, then tar the root — avoids the
# --transform / -s portability headache between GNU tar and macOS bsdtar.
# rsync with --exclude handles the skip list cleanly in one pass.
STAGE_ROOT="$STAGE/bundle"
mkdir -p "$STAGE_ROOT"

rsync -a \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.playwright' \
    --exclude='.playwright-mcp' \
    --exclude='.datasift_profile' \
    --exclude='.ancestry_profile' \
    --exclude='.ancestry_page_loads.json' \
    --exclude='logs' \
    --exclude='output' \
    --exclude='tracking' \
    --exclude='apify_storage' \
    --exclude='storage' \
    --exclude='node_modules' \
    --exclude='.pytest_cache' \
    --exclude='datasift_*.png' \
    --exclude='NOD Week*' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.DS_Store' \
    "$PROJECT_DIR/" "$STAGE_ROOT/SiftStack/"

if [[ -d "$MEMORY_DIR" ]]; then
    rsync -a "$MEMORY_DIR/" "$STAGE_ROOT/claude_memory/"
fi

cp "$STAGE/RESTORE.md" "$STAGE_ROOT/RESTORE.md"

# Tar the contents of STAGE_ROOT (not the dir itself), so extracting
# produces SiftStack/, claude_memory/, RESTORE.md at the extract root —
# matching what RESTORE.md tells the user to expect.
tar czf "$DEST" -C "$STAGE_ROOT" .

SIZE=$(du -h "$DEST" | cut -f1)
echo ""
echo "✓ Bundle created: $DEST ($SIZE)"
echo ""
echo "To restore on another machine:"
echo "  tar xzf $(basename "$DEST") -C ~/Desktop/"
echo "  cat ~/Desktop/RESTORE.md   # follow restore steps"
