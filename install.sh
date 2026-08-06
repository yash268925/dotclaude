#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTFILES_DIR="$REPO_DIR/dotfiles"
TARGET_DIR="${CLAUDE_HOME:-$HOME/.claude}"

TARGETS=(
  "CLAUDE.md"
  "settings.json"
  "statusline-command.sh"
  "scripts"
)

link() {
  local src="$1"
  local dest="$2"

  if [ -L "$dest" ]; then
    if [ "$(readlink "$dest")" = "$src" ]; then
      echo "ok      $dest"
      return
    fi
    rm "$dest"
  elif [ -e "$dest" ]; then
    mv "$dest" "$dest.bak"
    echo "backup  $dest -> $dest.bak"
  fi

  ln -s "$src" "$dest"
  echo "link    $dest -> $src"
}

mkdir -p "$TARGET_DIR"

for name in "${TARGETS[@]}"; do
  src="$DOTFILES_DIR/$name"
  if [ ! -e "$src" ]; then
    echo "skip    $src (not found)" >&2
    continue
  fi
  link "$src" "$TARGET_DIR/$name"
done

echo "done."
