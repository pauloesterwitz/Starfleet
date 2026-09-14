#!/bin/bash
# cluster/sync-hosts.sh -- keep the files deployed on the cluster and this repo in step.
#
#   ./cluster/sync-hosts.sh check    what differs, file by file and node by node (exit 1 if anything does)
#   ./cluster/sync-hosts.sh pull     host -> repo, to take edits made on a node (review with git diff)
#   ./cluster/sync-hosts.sh push     repo -> hosts, backing each host file up first (.bak-sync-<ts>)
#   add --force to pull / push to override the conflict guard
#
# Why: files deployed by hand drift. fleet.py was edited in place on Jean-Luc for weeks
# (see the jobs/reservations backport, 378acf6), and config.yaml, ctxproxy.py and the
# launchers only ever lived on the host.
#
# Conflict guard, both directions -- a copy only goes ahead when the side being
# overwritten still holds the version last committed here:
#   push refuses if the HOST file changed since HEAD (someone edited it on the node);
#   pull refuses if the REPO file has uncommitted edits.
# Otherwise one side's work silently replaces the other's.
#
# Runs on the Mac with the ssh aliases jean-luc / kathryn. What gets deployed where is
# cluster/MANIFEST.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
MODE="${1:-check}"; FORCE=0; [ "${2:-}" = "--force" ] && FORCE=1
case "$MODE" in check|pull|push) ;; *) echo "usage: $0 check|pull|push [--force]" >&2; exit 2 ;; esac

# -n: the loop below reads the manifest on stdin, and a plain ssh would swallow the rest of it.
SSH=(ssh -n -o BatchMode=yes -o ConnectTimeout=15)
TS=$(date +%Y%m%d-%H%M%S)
sha_local() { [ -f "$1" ] && shasum -a 256 "$1" | cut -d' ' -f1; }
sha_head()  { git cat-file -e "HEAD:$1" 2>/dev/null && git show "HEAD:$1" | shasum -a 256 | cut -d' ' -f1; }
sha_host()  { "${SSH[@]}" "$1" "sha256sum $2 2>/dev/null" | cut -d' ' -f1; }   # $2 keeps its ~ for the remote shell

problems=0; pushed=()
while read -r repo host nodes; do
  [ -z "${repo:-}" ] && continue
  case "$repo" in \#*) continue ;; esac
  rel="${host#\~/}"              # scp paths are relative to the remote home
  first=1
  for node in $nodes; do
    l=$(sha_local "$repo"); h=$(sha_host "$node" "$host"); c=$(sha_head "$repo")
    label=$(printf '%-46s %-9s' "$repo" "$node")
    if [ -n "$l" ] && [ "$l" = "$h" ]; then
      [ "$MODE" = check ] && echo "  same         $label"
      first=0; continue
    fi
    case "$MODE" in
      check)
        if   [ -z "$h" ];     then echo "  NOT ON HOST  $label"
        elif [ -z "$l" ];     then echo "  NOT IN REPO  $label"
        elif [ "$h" = "$c" ]; then echo "  REPO AHEAD   $label  (changed here since HEAD: push to deploy)"
        elif [ "$l" = "$c" ]; then echo "  HOST AHEAD   $label  (edited on the node: pull to take it)"
        else                       echo "  DIVERGED     $label  (both sides changed since HEAD)"
        fi
        problems=$((problems + 1)) ;;
      pull)
        if [ "$first" = 1 ]; then          # take a file from the first node listed only
          if [ -z "$h" ]; then
            echo "  skip         $label  (not on host)"
          elif [ -n "$c" ] && [ "$h" = "$c" ]; then
            echo "  repo ahead   $label  (host still holds HEAD: nothing to pull -- push to deploy)"
          elif [ -n "$l" ] && [ "$l" != "$c" ] && [ "$FORCE" = 0 ]; then
            echo "  REFUSED      $label  repo has uncommitted edits -- commit or stash them, or --force"
            problems=$((problems + 1))
          else
            mkdir -p "$(dirname "$repo")"
            scp -pq "$node:$rel" "$repo" </dev/null && echo "  pulled       $label"
          fi
        fi ;;
      push)
        if [ -z "$l" ]; then
          echo "  skip         $label  (not in repo)"
        elif [ -n "$h" ] && [ "$h" != "$c" ] && [ "$FORCE" = 0 ]; then
          echo "  REFUSED      $label  host changed since HEAD (edited on the node) -- pull it first, or --force"
          problems=$((problems + 1))
        else
          if [ -n "$h" ] && ! "${SSH[@]}" "$node" "cp -p $host $host.bak-sync-$TS"; then
            echo "  FAILED       $label  could not back the host file up -- not pushing"
            problems=$((problems + 1))
          elif "${SSH[@]}" "$node" "mkdir -p \$(dirname $host)" && scp -pq "$repo" "$node:$rel" </dev/null; then
            echo "  pushed       $label${h:+  (previous kept as .bak-sync-$TS)}"
            pushed+=("$repo")
          else
            echo "  FAILED       $label  copy failed"
            problems=$((problems + 1))
          fi
        fi ;;
    esac
    first=0
  done
done < cluster/MANIFEST

if [ "$MODE" = push ] && [ ${#pushed[@]} -gt 0 ]; then
  echo
  printf '%s\n' "${pushed[@]}" | grep -q 'cluster/\(llama-swap\|ds4\)/' &&
    echo "Kathryn picks up llama-swap/ds4 changes from kathryn-sync.timer within the hour (or: ssh jean-luc systemctl --user start kathryn-sync.service)."
  printf '%s\n' "${pushed[@]}" | grep -q '^fleet-ui/' &&
    echo "fleet.py changed: ssh jean-luc systemctl --user restart fleet-ui.service   (fleet.html alone needs only a browser refresh)"
  printf '%s\n' "${pushed[@]}" | grep -q 'ctxproxy.py' &&
    echo "ctxproxy.py changed: ssh jean-luc systemctl --user restart llama-swap-ctxproxy.service"
  printf '%s\n' "${pushed[@]}" | grep -q 'cluster/systemd/' &&
    echo "unit files changed: ssh jean-luc systemctl --user daemon-reload"
  echo "config.yaml needs nothing: llama-swap runs with -watch-config."
fi
[ "$problems" -eq 0 ]
