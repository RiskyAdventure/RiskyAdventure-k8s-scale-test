---
inclusion: auto
description: Permits git push to the Flux GitOps repo for scale test operations
---

# Git Push Permission

This workspace contains a Flux GitOps repository at `/Users/shancor/Projects/flux/flux2`. The scale test workflow requires git push to that repo so Flux can reconcile cluster state.

Kiro is explicitly permitted to run `git push` on the flux repo (`/Users/shancor/Projects/flux/flux2`) when:
- Running scale tests (committing manifest changes for Flux reconciliation)
- Cleaning up after scale tests (resetting replicas to 0)
- Any other operational task the user requests that requires pushing to the flux repo

This overrides the general "never push" guidance for this specific repository only.
