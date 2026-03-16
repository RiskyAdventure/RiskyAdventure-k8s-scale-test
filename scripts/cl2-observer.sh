#!/bin/bash
# Independent CL2 object creation observer.
# Uses a namespace watch (cheap) for real-time creation tracking,
# plus a periodic object count for the detail breakdown.
# Usage: ./scripts/cl2-observer.sh [output_file] [count_interval]
#   Start this BEFORE running the scale test with --cl2-preload.
#   Ctrl+C when done.

OUT="${1:-cl2-observer.log}"
INTERVAL="${2:-10}"

echo "CL2 Observer: watching cl2-test-* namespaces + counting objects every ${INTERVAL}s → $OUT"
echo "timestamp,namespaces,deployments,services,configmaps,secrets,total,delta,rate_per_s" > "$OUT"

# Background: periodic object count
prev_total=0
(
while true; do
    ns=$(kubectl get ns --no-headers 2>/dev/null | grep -c "cl2-test-")
    dep=$(kubectl get deployments -A --no-headers 2>/dev/null | grep -c "cl2-test-")
    svc=$(kubectl get services -A --no-headers 2>/dev/null | grep -c "cl2-test-")
    cm=$(kubectl get configmaps -A --no-headers 2>/dev/null | grep -c "cl2-test-")
    sec=$(kubectl get secrets -A --no-headers 2>/dev/null | grep -c "cl2-test-")
    total=$((ns + dep + svc + cm + sec))
    delta=$((total - prev_total))
    rate=$((delta / INTERVAL))

    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    echo "$ts,$ns,$dep,$svc,$cm,$sec,$total,$delta,$rate" >> "$OUT"

    if [ "$total" -gt 0 ] || [ "$prev_total" -gt 0 ]; then
        echo "$ts  ns=$ns dep=$dep svc=$svc cm=$cm sec=$sec total=$total (+$delta, ~${rate}/s)"
    fi

    prev_total=$total
    sleep "$INTERVAL"
done
) &
COUNT_PID=$!

# Foreground: namespace watch (lightweight, real-time)
kubectl get ns -w --no-headers 2>&1 | while IFS= read -r line; do
    case "$line" in
        *cl2-test-*) echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) NS: $line" ;;
    esac
done

kill $COUNT_PID 2>/dev/null
