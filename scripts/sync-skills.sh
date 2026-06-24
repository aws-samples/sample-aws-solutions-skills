#!/usr/bin/env bash
#
# sync-skills.sh — Keep three SKILL.md copies in sync per skill.
#
# Workflow: edit `claude-code/skills/<name>/SKILL.md`, then run this script
# to copy the canonical version to kiro/ and quick/. CI then verifies md5.
#
# Usage:
#   scripts/sync-skills.sh                # sync all skills in repo
#   scripts/sync-skills.sh <skill-dir>    # sync one skill
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

sync_skill() {
  local skill_dir="$1"
  [ -d "$skill_dir/claude-code/skills" ] || return 0

  for src in "$skill_dir/claude-code/skills/"*/SKILL.md; do
    [ -f "$src" ] || continue
    local name
    name="$(basename "$(dirname "$src")")"

    local kiro_dst="$skill_dir/kiro/skills/$name/SKILL.md"
    local quick_dst="$skill_dir/quick/skills/$name/SKILL.md"

    mkdir -p "$(dirname "$kiro_dst")" "$(dirname "$quick_dst")"
    cp "$src" "$kiro_dst"
    cp "$src" "$quick_dst"

    local md5
    md5="$(md5sum "$src" | awk '{print $1}')"
    echo "✓ $name → synced ($md5)"
  done
}

verify_skill() {
  local skill_dir="$1"
  [ -d "$skill_dir/claude-code/skills" ] || return 0
  local fail=0

  for src in "$skill_dir/claude-code/skills/"*/SKILL.md; do
    [ -f "$src" ] || continue
    local name
    name="$(basename "$(dirname "$src")")"

    local kiro="$skill_dir/kiro/skills/$name/SKILL.md"
    local quick="$skill_dir/quick/skills/$name/SKILL.md"

    [ -f "$kiro" ] || { echo "✗ $skill_dir: missing $kiro"; fail=1; continue; }
    [ -f "$quick" ] || { echo "✗ $skill_dir: missing $quick"; fail=1; continue; }

    local h1 h2 h3
    h1="$(md5sum "$src" | awk '{print $1}')"
    h2="$(md5sum "$kiro" | awk '{print $1}')"
    h3="$(md5sum "$quick" | awk '{print $1}')"

    if [ "$h1" = "$h2" ] && [ "$h2" = "$h3" ]; then
      echo "✓ $skill_dir/$name: md5 $h1"
    else
      echo "✗ $skill_dir/$name: drift ($h1 / $h2 / $h3)"
      fail=1
    fi
  done

  return $fail
}

case "${1:-sync}" in
  verify)
    fail=0
    for skill in "$ROOT"/*-skill; do
      [ -d "$skill" ] || continue
      verify_skill "$skill" || fail=1
    done
    [ $fail -eq 0 ] && echo "All skills consistent." || { echo "DRIFT detected."; exit 1; }
    ;;
  sync|*)
    if [ "${1:-}" = "sync" ] || [ -z "${1:-}" ]; then
      for skill in "$ROOT"/*-skill "$ROOT"/template; do
        [ -d "$skill" ] || continue
        sync_skill "$skill"
      done
    else
      sync_skill "$1"
    fi
    ;;
esac
