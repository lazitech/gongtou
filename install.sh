#!/usr/bin/env bash
# Install Codex Flow Lite and (optionally) register the /gongtou Claude Code skill.
#
#   ./install.sh [--with-skills] [--migrate-r6] [ABSOLUTE_PREFIX]
#
# PREFIX defaults to ~/.local/share/codex-flow-lite. The command
# codex-flow-lite is linked into ~/.local/bin (override with BIN_DIR).
# An existing installation is moved to PREFIX-backup-<timestamp> first.
set -euo pipefail

with_skills=0
migrate=0
prefix=""
for arg in "$@"; do
  case "$arg" in
    --with-skills) with_skills=1 ;;
    --migrate-r6) migrate=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) [ -z "$prefix" ] || { echo "only one prefix may be given" >&2; exit 2; }; prefix="$arg" ;;
  esac
done
if [ "$migrate" = 1 ] && [ "$with_skills" = 0 ]; then
  echo "--migrate-r6 requires --with-skills" >&2; exit 2
fi

prefix="${prefix:-$HOME/.local/share/codex-flow-lite}"
case "$prefix" in /*) ;; *) echo "prefix must be an absolute path: $prefix" >&2; exit 2 ;; esac
bin_dir="${BIN_DIR:-$HOME/.local/bin}"
claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 - <<'PY' || { echo "Python 3.11+ is required" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v codex >/dev/null || echo "warning: codex CLI not found on PATH (tested with Codex 0.171.0)" >&2

if [ "$(cd "$src" && pwd -P)" = "$(mkdir -p "$prefix" && cd "$prefix" && pwd -P)" ]; then
  echo "source and prefix are the same directory; nothing to copy" >&2
else
  if [ -n "$(ls -A "$prefix")" ]; then
    backup="${prefix}-backup-$(date +%Y%m%d-%H%M%S)"
    mv "$prefix" "$backup"
    echo "Previous installation moved to $backup"
    mkdir -p "$prefix"
  fi
  for item in codex-flow lite defaults examples skills docs README.md LICENSE; do
    [ -e "$src/$item" ] && cp -R "$src/$item" "$prefix/"
  done
  find "$prefix" -name __pycache__ -prune -exec rm -rf {} +
  chmod +x "$prefix/codex-flow"
fi

mkdir -p "$bin_dir"
ln -sfn "$prefix/codex-flow" "$bin_dir/codex-flow-lite"
echo "Installed $bin_dir/codex-flow-lite -> $prefix/codex-flow"

if [ "$with_skills" = 1 ]; then
  python3 -I -B "$prefix/lite/migrate.py" "$claude_dir" "$prefix" "$migrate"
  echo "Registered skill $claude_dir/skills/gongtou -> $prefix/skills/gongtou"
fi

case ":$PATH:" in *":$bin_dir:"*) ;; *) echo "note: add $bin_dir to PATH" ;; esac
