#!/bin/bash
# Independent observer — watches deployment status with kubectl.
# No Python, no app code, no logic that can break.
# Usage: ./scripts/observer.sh [namespace] [output_file]
#   Start this BEFORE running the scale test.
#   Ctrl+C when done. Compare output against rate_data.jsonl.

NS="${1:-pause}"
OUT="${2:-observer.log}"

echo "Observer: watching deployments in '$NS' → $OUT"
echo "timestamp,ready_replicas,replicas,available" > "$OUT"

kubectl get deployment -n "$NS" -w -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,REPLICAS:.status.replicas,AVAILABLE:.status.availableReplicas' --no-headers 2>&1 | while IFS= read -r line; do
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ),$line" | tee -a "$OUT"
done
