#!/bin/bash
# Usage: run_round.sh <round-name> <scenario> [<scenario> ...]
# Snapshots the repo, then runs the scenarios 4 at a time against the snapshot.
set -e
cd "$(dirname "$0")"  # set -P below to fewer parallel runs to save usage
R=$1; shift
SNAP=/tmp/macaw-snap-$R
rm -rf $SNAP && cp -r /home/claude/macawsrs $SNAP
mkdir -p runs/$R
printf '%s\n' "$@" | xargs -P 1 -I{} sh -c "MACAW_REPO=$SNAP python live.py scenarios/{}.json runs/$R/{} > runs/$R/{}.out 2>&1"
echo "round $R done"
