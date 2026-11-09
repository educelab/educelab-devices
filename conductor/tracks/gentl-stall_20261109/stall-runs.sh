#!/usr/bin/env bash
# Repeats the GenTL stall configuration (track gentl-stall_20261109, Phase 2),
# logs every run, and stops at the first stall. For Git Bash or MSYS2 on the
# Windows rig, from the venv with educelab-devices installed.
#
# Usage: stall-runs.sh [-n RUNS] [-d gentl|spinnaker] [-l MB_S] [-k] LOG_DIR [SERIAL...]
#   Task 2.1: stall-runs.sh /e/gentl-stall
#   Task 2.3: stall-runs.sh /e/gentl-stall 20305113 20305122 20305114 20298452 20298479
#   Task 2.4: stall-runs.sh -d spinnaker -n 8 /e/gentl-stall
#   Task 3.3: stall-runs.sh -l 64 /e/gentl-stall
# -n runs (default 10), -d driver (default gentl), -l throughput limit per camera
# in MB/s (default 80), -k keeps going after a stall.
# Serials open in the order given; without them, in the producer's order.
set -u

usage() { sed -n '2,13p' "$0"; exit 2; }

runs=10
driver=gentl
limit=80
keep_going=0
while getopts "n:d:l:k" opt; do
  case $opt in
    n) runs=$OPTARG ;;
    d) driver=$OPTARG ;;
    l) limit=$OPTARG ;;
    k) keep_going=1 ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))
[ $# -ge 1 ] || usage
log_dir=$1
shift

case $driver in
  gentl) command=(genicam --backend gentl) ;;
  spinnaker) command=(spinnaker) ;;
  *) usage ;;
esac
command+=(--pixel-format Mono8 --exposure min --throughput-limit "$limit" --frames 100)
for serial in "$@"; do command+=(--serial "$serial"); done

mkdir -p "$log_dir"
stamp=$(date +%Y%m%d-%H%M%S)
summary="$log_dir/$stamp-$driver-summary.txt"
# Python buffers piped output; unbuffered, a stall's first failure shows at once.
export PYTHONUNBUFFERED=1
{
  echo "el-devices ${command[*]}"
  echo "educelab-devices $(python -c 'import educelab.devices as d; print(d.__version__)')," \
    "$(python --version 2>&1), $(hostname)"
} > "$summary"

for ((run = 1; run <= runs; run++)); do
  log=$(printf '%s/%s-%s-run%02d.log' "$log_dir" "$stamp" "$driver" "$run")
  echo "== run $run of $runs, $(date '+%Y-%m-%dT%H:%M:%S%z'): el-devices ${command[*]}" \
    | tee "$log"
  el-devices "${command[@]}" 2>&1 | tee -a "$log"
  code=${PIPESTATUS[0]}
  if grep -q "first failure" "$log"; then
    result=STALLED
  elif [ "$code" -ne 0 ]; then
    result="exit $code"
  else
    result=ok
  fi
  line="run $run: $result, $(date '+%Y-%m-%dT%H:%M:%S%z') ($(basename "$log"))"
  echo "$line" >> "$summary"
  echo "== $line"
  if [ "$result" = STALLED ] && [ "$keep_going" -eq 0 ]; then
    echo "Stopping: a camera stalled. Its counters are in $log."
    break
  fi
  sleep 5
done
echo "Summary: $summary"
