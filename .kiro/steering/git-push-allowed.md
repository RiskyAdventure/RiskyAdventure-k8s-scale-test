---
inclusion: auto
description: Permits git push to the Flux GitOps repo and the scale test repo
---

# Git Push Permission

This workspace contains two repositories that Kiro is permitted to push to:

1. **Flux GitOps repo** (`/Users/shancor/Projects/flux/flux2`) — scale test manifest changes for Flux reconciliation
2. **Scale test repo** (`/Users/shancor/Projects/flux`, origin: `RiskyAdventure/RiskyAdventure-k8s-scale-test`) — code changes to the test tooling

Kiro is explicitly permitted to run `git push` on either repo when:
- Running scale tests (committing manifest changes for Flux reconciliation)
- Cleaning up after scale tests (resetting replicas to 0)
- Pushing code changes (bug fixes, features, tracing instrumentation)
- Any other operational task the user requests

This overrides the general "never push" guidance for these two repositories only.
