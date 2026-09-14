#!/bin/bash
# cluster/sync-hosts.sh -- keep the files deployed on the cluster and this repo in step.
#
#   ./cluster/sync-hosts.sh check     what differs, file by file and node by node (exit 1 if anything does)
#   ./cluster/sync-hosts.sh pull      host -> repo, to take edits made on a node (review with git diff)
#   ./cluster/sync-hosts.sh push      repo -> hosts, backing each host file up first (.bak-sync-<ts>)
#   ./cluster/sync-hosts.sh selftest  the conflict rules below, against fixed cases (no network)
#   add --force to pull / push to override the conflict guard
#
# Why: files deployed by hand drift. fleet.py was edited in place on Jean-Luc for weeks
# (see the jobs/reservations backport, 378acf6), and config.yaml, ctxproxy.py and the
# launchers only ever lived on the host.
#
# Conflict guard: a three-way comparison against the version last synced with each node,
# recorded in .git/sync-hosts.state by every push and pull -- NOT against HEAD. HEAD alone
# cannot tell "edited on the node" from "committed here, not deployed yet": both look like
# host != HEAD. On 2026-09-14 that made `check` advise pulling config.yaml back from the host
# right after a comment block had been deliberately removed from the repo.
#   host == last synced, repo changed   ->  REPO AHEAD   push deploys it
#   repo == last synced, host changed   ->  HOST EDITED  pull takes it
#   both changed since the last sync    ->  DIVERGED     resolve by hand, then --force
# An entry never synced from this clone falls back to HEAD and refuses when that is ambiguous.
#
# Runs on the Mac with the ssh aliases jean-luc / kathryn. What gets deployed where is
# cluster/MANIFEST.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
MODE="${1:-check}"; FORCE=0; [ "${2:-}" = "--force" ] && FORCE=1
case "$MODE" in check|pull|push|selftest) ;; *) echo "usage: $0 check|pull|push|selftest [--force]" >&2; exit 2 ;; esac

# classify LOCAL HOST HEAD BASE -> state. Arguments are sha256 strings; "" = missing / unknown.
classify() {
  local l=$1 h=$2 c=$3 b=$4
  if   [ -n "$l" ] && [ "$l" = "$h" ]; then echo same
  elif [ -z "$h" ]; then echo not-on-host
  elif [ -z "$l" ]; then echo not-in-repo
  elif [ -n "$b" ]; then
    if   [ "$h" = "$b" ]; then echo repo-ahead
    elif [ "$l" = "$b" ]; then echo host-edited
    else echo diverged; fi
  else                                    # never synced from this clone: best effort against HEAD
    if   [ "$h" = "$c" ]; then echo repo-ahead
    elif [ "$l" = "$c" ]; then echo unrecorded
    else echo diverged; fi
  fi
}
# may push|pull STATE -> success if the guard allows that direction without --force
may() {
  case "$1:$2" in
    push:repo-ahead|push:not-on-host|pull:host-edited|pull:not-in-repo) return 0 ;;
    *) return 1 ;;
  esac
}

if [ "$MODE" = selftest ]; then
  fail=0
  t() { local got; got=$(classify "$1" "$2" "$3" "$4"); [ "$got" = "$5" ] || { echo "FAIL classify(l=$1 h=$2 head=$3 base=$4) = $got, want $5"; fail=1; }; }
  #   local host head base   expected
  t   A     A    A    A      same
  t   B     A    B    A      repo-ahead    # committed here, host still holds the last push -- the 2026-09-14 case
  t   B     A    A    A      repo-ahead    # uncommitted edit here, host untouched
  t   A     C    A    A      host-edited   # someone edited the file on the node
  t   B     C    B    A      diverged
  t   A     ""   A    A      not-on-host
  t   ""    A    ""   ""     not-in-repo
  t   B     A    A    ""     repo-ahead    # no record, but the host still holds HEAD
  t   A     C    A    ""     unrecorded    # no record: node edit or undeployed commit? can't tell
  m() { if may "$1" "$2"; then r=allow; else r=refuse; fi; [ "$r" = "$3" ] || { echo "FAIL may $1 $2 = $r, want $3"; fail=1; }; }
  m push repo-ahead allow;   m push host-edited refuse;  m push diverged refuse;  m push unrecorded refuse
  m pull host-edited allow;  m pull repo-ahead refuse;   m pull diverged refuse;  m pull unrecorded refuse
  [ "$fail" = 0 ] && echo "selftest: all classification and guard cases pass"
  exit "$fail"
fi

STATE="$(git rev-parse --git-dir)/sync-hosts.state"     # per clone, never committed
state_get() { [ -f "$STATE" ] && awk -F'\t' -v n="$1" -v p="$2" '$1 == n && $2 == p {print $3}' "$STATE" | tail -1; }
state_set() {
  local tmp; tmp=$(mktemp)
  { [ -f "$STATE" ] && awk -F'\t' -v n="$1" -v p="$2" '!($1 == n && $2 == p)' "$STATE"; printf '%s\t%s\t%s\n' "$1" "$2" "$3"; } > "$tmp" && mv "$tmp" "$STATE"
}

# -n: the loop below reads the manifest on stdin, and a plain ssh would swallow the rest of it.
SSH=(ssh -n -o BatchMode=yes -o ConnectTimeout=15)
TS=$(date +%Y%m%d-%H%M%S)
sha_local() { [ -f "$1" ] && shasum -a 256 "$1" | cut -d' ' -f1; }
sha_head()  { git cat-file -e "HEAD:$1" 2>/dev/null && git show "HEAD:$1" | shasum -a 256 | cut -d' ' -f1; }
sha_host()  { "${SSH[@]}" "$1" "sha256sum $2 2>/dev/null" | cut -d' ' -f1; }   # $2 keeps its ~ for the remote shell

refusal() {
  case "$1" in
    host-edited) echo "host was edited since the last sync -- pull it first, or --force" ;;
    repo-ahead)  echo "nothing to pull: the host still holds the last sync -- push to deploy" ;;
    diverged)    echo "both sides changed since the last sync -- resolve by hand, then --force" ;;
    unrecorded)  echo "no sync record in this clone: cannot tell a node edit from an undeployed commit -- compare, then --force" ;;
    *)           echo "$1" ;;
  esac
}

problems=0; pushed=()
while read -r repo host nodes; do
  [ -z "${repo:-}" ] && continue
  case "$repo" in \#*) continue ;; esac
  rel="${host#\~/}"              # scp paths are relative to the remote home
  first=1
  for node in $nodes; do
    l=$(sha_local "$repo"); h=$(sha_host "$node" "$host"); c=$(sha_head "$repo"); b=$(state_get "$node" "$repo")
    s=$(classify "$l" "$h" "$c" "$b")
    label=$(printf '%-46s %-9s' "$repo" "$node")
    if [ "$s" = same ]; then
      [ "$b" = "$l" ] || state_set "$node" "$repo" "$l"      # both sides equal: that IS the synced version
      [ "$MODE" = check ] && echo "  same         $label"
      first=0; continue
    fi
    case "$MODE" in
      check)
        case "$s" in
          not-on-host) echo "  NOT ON HOST  $label" ;;
          not-in-repo) echo "  NOT IN REPO  $label" ;;
          repo-ahead)  echo "  REPO AHEAD   $label  (push to deploy)" ;;
          host-edited) echo "  HOST EDITED  $label  (pull to take it)" ;;
          diverged)    echo "  DIVERGED     $label  (both sides changed since the last sync)" ;;
          unrecorded)  echo "  DIFFERS      $label  (no sync record here: node edit or undeployed commit -- compare first)" ;;
        esac
        problems=$((problems + 1)) ;;
      pull)
        if [ "$first" = 1 ]; then          # take a file from the first node listed only
          if [ "$s" = not-on-host ]; then
            echo "  skip         $label  (not on host)"
          elif may pull "$s" || [ "$FORCE" = 1 ]; then
            mkdir -p "$(dirname "$repo")"
            if scp -pq "$node:$rel" "$repo" </dev/null; then
              state_set "$node" "$repo" "$h"; echo "  pulled       $label"
            else echo "  FAILED       $label  copy failed"; problems=$((problems + 1)); fi
          elif [ "$s" = repo-ahead ]; then
            echo "  repo ahead   $label  ($(refusal "$s"))"
          else
            echo "  REFUSED      $label  $(refusal "$s")"; problems=$((problems + 1))
          fi
        fi ;;
      push)
        if [ "$s" = not-in-repo ]; then
          echo "  skip         $label  (not in repo)"
        elif may push "$s" || [ "$FORCE" = 1 ]; then
          if [ -n "$h" ] && ! "${SSH[@]}" "$node" "cp -p $host $host.bak-sync-$TS"; then
            echo "  FAILED       $label  could not back the host file up -- not pushing"; problems=$((problems + 1))
          elif "${SSH[@]}" "$node" "mkdir -p \$(dirname $host)" && scp -pq "$repo" "$node:$rel" </dev/null; then
            state_set "$node" "$repo" "$l"
            echo "  pushed       $label${h:+  (previous kept as .bak-sync-$TS)}"; pushed+=("$repo")
          else
            echo "  FAILED       $label  copy failed"; problems=$((problems + 1))
          fi
        else
          echo "  REFUSED      $label  $(refusal "$s")"; problems=$((problems + 1))
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
  printf '%s\n' "${pushed[@]}" | grep -q 'config.yaml' &&
    echo "config.yaml changed: llama-swap reloads it by itself (-watch-config)."
fi
[ "$problems" -eq 0 ]
