#!/usr/bin/env bash
# CI only: print the image families a push to main must rebuild, space-separated.
#   plan_families.sh <before-sha> <after-sha>
# All families when the fetcher, ci/ or the build workflow changed (or the range is unknown);
# otherwise each family whose docker/<family>/ changed, plus the families that copy files from
# docker/extras when that changed.
set -Eeuo pipefail

before="$1" after="$2"
all="$(python images.py families)"
if [[ -z "$before" || "$before" =~ ^0+$ ]] || ! git cat-file -e "$before^{commit}" 2>/dev/null; then
  echo "$all"
  exit 0
fi
changed="$(git diff --name-only "$before" "$after")"
if grep -qE '^((docker/fetch|ci)/|\.github/workflows/build\.yml)' <<<"$changed"; then
  echo "$all"
  exit 0
fi
extras=""
if grep -q '^docker/extras/' <<<"$changed"; then
  extras=" $(python images.py extras-families) "
fi
picked=()
for family in $all; do
  if grep -q "^docker/$family/" <<<"$changed" || [[ "$extras" == *" $family "* ]]; then
    picked+=("$family")
  fi
done
echo "${picked[*]:-}"
