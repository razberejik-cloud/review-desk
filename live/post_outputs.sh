#!/usr/bin/env bash
# Post the comments and label changes that live/status.py prepared.
set -e
[ -d live/out ] || exit 0
for f in live/out/*.md; do
  [ -e "$f" ] || continue
  n=$(basename "$f" .md)
  gh issue comment "$n" --body-file "$f"
done
if [ -f live/out/actions.txt ]; then
  gh label create overdue --color D93F0B --description "Alert passed its deadline" 2>/dev/null || true
  gh label create resolved --color 0E8A16 --description "Alert handled" 2>/dev/null || true
  while read -r n action; do
    case "$action" in
      resolve) gh issue edit "$n" --add-label resolved --remove-label overdue || true; gh issue close "$n" || true ;;
      reopen)  gh issue reopen "$n" || true; gh issue edit "$n" --remove-label resolved || true ;;
      overdue) gh issue edit "$n" --add-label overdue || true ;;
    esac
  done < live/out/actions.txt
fi
