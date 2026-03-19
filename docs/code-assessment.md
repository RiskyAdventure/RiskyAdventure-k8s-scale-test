# Code Assessment: Architectural Weaknesses & Technical Debt

This document captures verified findings from a critical assessment of the k8s-scale-test codebase. Each finding was independently verified by a sub-agent that read the relevant source code and traced the execution paths. No code changes are proposed here — this is a diagnostic record.

## Finding 1: Controller is a God Class

**Severity: High (structural)**
**Files: `controller.py` (1,830 lines, 25 methods, 12+ responsibilities)**

The `ScaleTestController` handles preflight orchestration, operator approval gates, Flux Git operations, deployment discovery and filtering, pod distribution math, monitoring pipeline setup, observer process management, scanner lifecycle, scaling execution, CL2 management (~400 lines / 5 methods), data verification (~170 lines), cleanup, drain, summary generation, diagnostics triggering, token management, and pod/node counting.

The `run()` method alone is ~460 lines orchestrating 9 phases inline. It imports from 16 internal modules, making it a massive fan-out hub.

Existing test guards (`test_health_sweep_is_separate_module`, `test_karpenter_check_is_separate_module`) show the team is aware and has already extracted some concerns. The same pattern should continue for:
- CL2 management → `cl2_manager.py`
- Data verification → `verification.py`
- Observer management → extract from controller
- Report formatting → `report.py` (currently 160 lines in `cli.py`)

---

## Finding 2: Cross-Event-Loop SharedContext Bug

**Severity: Medium (silent data loss)**
**Files: `controller.py:873`, `shared_context.py:29`, `anomaly.py:155`, `monitor.py:205`**

The diagnostics thread (controller.py:873) and the monitor's alert callback (monitor.py:205) both create new event loops via `asyncio.new_event_loop()` and run `anomaly.handle_alert()` on them. The `SharedContext` uses an `asyncio.Lock()` created on the main event loop. When `handle_alert()` calls `shared_ctx.query()` from a different event loop, the lock acquisition can raise `RuntimeError` on Python 3.10+ ("Task got Future attached to a different loop").

This error is silently caught by the `try/except` in anomaly.py:155-158, causing the investigation to proceed without scanner correlation data. The finding quality degrades silently — no crash, no log at ERROR level, just a WARNING that's easy to miss.

Additionally, `SharedContext.add()` is synchronous and never acquires the lock, while `query()` is async and does acquire it. Since they run on different threads/loops, the `asyncio.Lock` provides no actual cross-thread protection. CPython's GIL prevents crashes but stale reads are possible.

**Fix**: Replace `asyncio.Lock` with `threading.Lock` in SharedContext, and have `add()` acquire it too. This fixes both the cross-loop issue and the data race.

---

## Finding 3: Config Mutation During Preflight

**Severity: Low (fragile ordering)**
**Files: `controller.py:222-228`**

The controller temporarily mutates `self.config.target_pods` to inflate it for CL2 capacity checking, then restores it after preflight. This works today because `evidence_store.create_run()` (which snapshots config.json) is called before the inflation, and no threads are running yet.

However, correctness depends on call ordering with no enforcement. If someone reorders `create_run()` after the inflation block, `config.json` would silently record the inflated value. The restore is also not in a `try/finally` block.

**Fix**: Use `dataclasses.replace(self.config, target_pods=inflated_value)` to create a temporary config copy for preflight, eliminating the mutation entirely.

---

## Finding 4: Duplicate AMPMetricCollector Instances

**Severity: Low (resource waste, misleading comment)**
**Files: `controller.py:68`, `controller.py:391`, `controller.py:468`**

Two separate `AMPMetricCollector` instances are created with identical configuration:
1. Inside `_make_prometheus_executor()` (line 68) — used by the ObservabilityScanner
2. In the controller's `run()` method (line 391) — used by AnomalyDetector and FindingReviewer

Each instance creates its own `boto3.Session` and credential resolution chain. They don't interfere with each other, but the duplication wastes resources and causes redundant STS traffic during credential refresh windows.

The comment on line 468 ("Reuses the same amp_collector and cloudwatch_fn as the scanner") is misleading — the FindingReviewer receives Instance 2, not the scanner's Instance 1.

**Fix**: Create `amp_collector` once, pass it to `_make_prometheus_executor` as a parameter, and fix the comment.

---

## Finding 5: Token Refresh Has No Cross-Component Coordination

**Severity: Medium (transient API failures)**
**Files: `monitor.py:173-192`, `events.py:148-164`, `controller.py:994-998`, `controller.py:1162-1168`**

Four independent callers refresh K8s tokens via `config._k8s_reload()`:

| Caller | Lock | Cooldown |
|--------|------|----------|
| Monitor watch threads | Per-Monitor `threading.Lock` | 30s |
| Event watcher threads | Per-EventWatcher `threading.Lock` | 30s |
| Observer thread | None | None |
| Controller main loop | None | 600s proactive timer |

The locks are per-component instances — they prevent thundering herd within each component but don't prevent cross-component races. When an EKS token expires, up to 4 concurrent calls to `kubernetes.config.load_kube_config()` can race on the global `Configuration` singleton, potentially producing a half-updated configuration (new token with old SSL context, or vice versa).

**Fix**: Create a single shared `TokenRefresher` class with one `threading.Lock` and one cooldown timer, used by all components.

---

## Finding 6: Observer Thread Shutdown is Unreliable

**Severity: Low (cosmetic)**
**Files: `controller.py:130-143`, `controller.py:1017-1029`**

`_ObserverHandle.terminate()` and `kill()` are both no-ops. The observer thread stops via `self._running = False`, but it sleeps for 10 seconds between polls. `_stop_observer` calls `thread.join(timeout=5)`, which can return before the thread exits (the thread may be mid-sleep). The thread is a daemon thread so it dies with the process, but it can write one stale data point to `observer.log` after the test "ends."

**Fix**: Use `threading.Event` instead of `_time.sleep(10)` so the thread can be woken immediately on shutdown.

---

## Finding 7: Diagnostics Thread Creates Separate Event Loop

**Severity: Medium (architectural smell)**
**Files: `controller.py:873-881`**

The diagnostics trigger in `_execute_scaling_via_flux` spawns a thread with `asyncio.new_event_loop()` to run `anomaly.handle_alert()`. This is a workaround to avoid blocking the main scaling loop, but it creates the cross-event-loop issues described in Finding 2.

The pattern of "spawn a thread with a new event loop to run an async function" appears in two places: the diagnostics thread (controller.py:873) and the monitor's alert callback (monitor.py:205). Both exist because the alert handler is async but needs to run without blocking the caller.

**Fix**: Use `asyncio.create_task()` on the main event loop instead of spawning threads. The alert handler already runs concurrently with the scaling loop since the scaling loop `await`s `asyncio.sleep(5)` every iteration, yielding control to other tasks.

---

## Finding 8: _print_report Contains Deep Model Knowledge

**Severity: Low (misplaced responsibility)**
**Files: `cli.py:579-738`**

The `_print_report` function (160 lines) in `cli.py` parses `evidence_references` strings to extract failure counts, iterates `k8s_events` to classify failure types, and splits `root_cause` strings on semicolons to extract node names. This is report generation logic that deeply understands the internal structure of `Finding` objects — it belongs in a dedicated `report.py` module, not in the CLI argument parser.

---

## Finding 9: _verify_run_data is Misplaced

**Severity: Low (misplaced responsibility)**
**Files: `controller.py:1175-1347`**

This 172-line method performs pure data validation (parsing JSONL, timestamp arithmetic, observer log parsing, cross-validation with tolerance thresholds) with zero dependency on controller state beyond `self.evidence_store._run_dir(run_id)`. It imports `json`, `Path`, and `datetime` locally — a sign the author knew it didn't belong here. It should be a standalone module that takes a run directory path and a `ScalingResult` as inputs.
