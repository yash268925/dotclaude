#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOTFILES_DIR="$REPO_DIR/dotfiles"
TARGET_DIR="${CLAUDE_HOME:-$HOME/.claude}"

TARGETS=(
  "agents"
  "CLAUDE.md"
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

# settings.json はリンクではなく、公開用ベースと gitignore 済みのローカル
# オーバーレイをマージした実ファイルとして生成する。
merge_settings() {
  local base="$DOTFILES_DIR/settings.json"
  local local_overlay="$DOTFILES_DIR/settings.local.json"
  local dest="$TARGET_DIR/settings.json"
  local tmp="$dest.tmp.$$"

  if [ ! -e "$base" ]; then
    echo "skip    $base (not found)" >&2
    return
  fi

  # 生成に失敗しても既存の settings.json を壊さないよう、一時ファイルに
  # 書き切ってから置き換える。
  if [ ! -e "$local_overlay" ]; then
    cp "$base" "$tmp"
  elif command -v jq >/dev/null 2>&1; then
    if ! jq -s '.[0] * .[1]' "$base" "$local_overlay" > "$tmp"; then
      rm -f "$tmp"
      echo "error   failed to merge $local_overlay (invalid JSON?)" >&2
      exit 1
    fi
  elif command -v python3 >/dev/null 2>&1; then
    if ! python3 - "$base" "$local_overlay" > "$tmp" <<'PY'
import json, sys

def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(a[k], v) if k in a else v
        return out
    return b

with open(sys.argv[1]) as f:
    base = json.load(f)
with open(sys.argv[2]) as f:
    overlay = json.load(f)
json.dump(merge(base, overlay), sys.stdout, ensure_ascii=False, indent=2)
PY
    then
      rm -f "$tmp"
      echo "error   failed to merge $local_overlay (invalid JSON?)" >&2
      exit 1
    fi
  else
    rm -f "$tmp"
    echo "error   jq (or python3) is required to merge $local_overlay" >&2
    exit 1
  fi

  if [ -L "$dest" ]; then
    rm "$dest"
  elif [ -e "$dest" ]; then
    if cmp -s "$tmp" "$dest"; then
      rm -f "$tmp"
      echo "ok      $dest"
      return
    fi
    mv "$dest" "$dest.bak"
    echo "backup  $dest -> $dest.bak"
  fi

  mv "$tmp" "$dest"
  echo "merge   $dest"
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

merge_settings

echo "done."
