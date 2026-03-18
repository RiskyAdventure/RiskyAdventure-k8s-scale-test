#!/bin/bash
exec python3 -u -m k8s_scale_test run \
  --target-pods 30000 \
  --auto-approve \
  --aws-profile "shancor+test-Admin" \
  --flux-repo-path "/Users/shancor/Projects/flux/flux2" \
  --output-dir "./scale-test-results" \
  --cl2-preload mixed-workload \
  --cl2-timeout 3600 \
  --hold-at-peak 120 \
  --pending-timeout 900 \
  --amp-workspace-id "ws-641df7c9-35d5-4cda-89e0-dc2dc873262d" \
  --cloudwatch-log-group "/aws/containerinsights/tf-shane/dataplane" \
  --eks-cluster-name "tf-shane" \
  --cpu-limit-multiplier 2.0 \
  --memory-limit-multiplier 1.5 \
  --iperf3-server-ratio 50 \
  -v
