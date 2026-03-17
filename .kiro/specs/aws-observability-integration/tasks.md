# Implementation Plan: AWS Observability Integration via AI Sub-Agent

## Overview

Incremental implementation of the AI sub-agent observability system. The approach: configuration and data models first, then the ContextFileWriter (core Python component), then evidence store extensions, then controller integration, then Kiro hooks and steering files, then chart integration. Each task builds on the previous, with tests close to implementation.

## Tasks

- [x] 1. Add configuration fields and CLI arguments
  - [x] 1.1 Add `amp_workspace_id`, `cloudwatch_log_group`, and `eks_cluster_name` optional fields to `TestConfig` in `models.py`, defaulting to `None`
    - _Requirements: 8.1_
  - [x] 1.2 Add `--amp-workspace-id`, `--cloudwatch-log-group`, and `--eks-cluster-name` arguments to `parse_args()` in `cli.py`, wiring them into `TestConfig` construction in `main()`
    - _Requirements: 8.2, 8.3, 8.4, 8.5_

- [x] 2. Implement agent finding schema validation
  - [x] 2.1 Create `src/k8s_scale_test/agent_schema.py` with `validate_agent_finding(data: dict) -> tuple[bool, list[str]]` that checks for required fields (`finding_id`, `timestamp`, `source`, `severity`, `title`, `description`, `affected_resources`, `evidence`, `recommended_actions`) and validates the optional `review` field structure (`confidence`, `reasoning`, `alternative_explanations`, `checkpoint_questions`, `verification_results`)
    - _Requirements: 6.1, 6.2_
  - [x] 2.2 Write property test for agent finding schema validation
    - **Property 6: Agent finding schema validation**
    - **Validates: Requirements 6.1, 6.2**

- [x] 3. Implement Evidence Store extensions
  - [x] 3.1 Add `write_agent_context(run_id, context)`, `load_agent_context(run_id)`, and `load_agent_findings(run_id)` methods to `EvidenceStore` in `evidence.py`
    - `write_agent_context`: writes `agent_context.json` using existing `_write_json` pattern
    - `load_agent_context`: reads and parses `agent_context.json`, returns `None` if not present
    - `load_agent_findings`: globs `findings/agent-*.json`, parses each, skips malformed files with `log.warning`, returns list of dicts
    - _Requirements: 6.3, 6.4, 6.5, 6.6_
  - [x] 3.2 Write property test for agent findings round-trip
    - **Property 4: Agent findings round-trip through Evidence Store**
    - **Validates: Requirements 6.3, 6.4**
  - [x] 3.3 Write property test for malformed agent findings handling
    - **Property 5: Malformed agent findings are skipped gracefully**
    - **Validates: Requirements 6.6, 11.5**

- [x] 4. Implement ContextFileWriter
  - [x] 4.1 Create `src/k8s_scale_test/agent_context.py` with `ContextFileWriter` class
    - `__init__(evidence_store, run_id, config)`: stores run dir path, extracts observability config fields
    - `write_initial_context(namespaces, node_list)`: writes full `agent_context.json` with run_id, test_start, target_pods, namespaces, phase='initializing', observability fields (omitting None values), empty alerts/finding_summaries lists, evidence_dir path
    - `update_phase(phase, timestamp)`: reads existing context, updates `current_phase` and `phase_start`, writes back
    - `append_alert(alert)`: reads existing context, appends alert dict to `alerts` list, writes back
    - `append_finding_summary(finding)`: reads existing context, appends summary dict to `finding_summaries` list, writes back
    - All methods wrapped in try/except with `log.warning` on failure
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - [x] 4.2 Write property test for context file config mapping
    - **Property 1: Context file correctly maps TestConfig fields**
    - **Validates: Requirements 2.1, 2.6, 8.2, 8.3, 8.4, 8.5**
  - [x] 4.3 Write property test for context file phase updates
    - **Property 2: Context file phase updates are consistent**
    - **Validates: Requirements 2.2**
  - [x] 4.4 Write property test for context file appends
    - **Property 3: Context file appends accumulate correctly**
    - **Validates: Requirements 2.3, 2.4**

- [x] 5. Checkpoint - Ensure all core module tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Integrate ContextFileWriter into Controller
  - [x] 6.1 Modify `ScaleTestController.__init__` in `controller.py` to add `self._ctx_writer: ContextFileWriter | None = None`
    - _Requirements: 2.1_
  - [x] 6.2 In `ScaleTestController.run()`, after monitoring setup (Phase 5), create `ContextFileWriter` and call `write_initial_context()` with namespaces and node list. Wrap in try/except, set `_ctx_writer = None` on failure.
    - _Requirements: 2.1, 2.5, 11.4_
  - [x] 6.3 In `_execute_scaling_via_flux()`, call `self._ctx_writer.update_phase("scaling", ...)` at the start. In the hold-at-peak section, call `update_phase("hold-at-peak", ...)`. In cleanup, call `update_phase("cleanup", ...)`.
    - _Requirements: 2.2_
  - [x] 6.4 In the monitor alert callback path (inside `_safe_callback` or the alert handler), call `self._ctx_writer.append_alert(alert)`. In the anomaly detector finding path, call `self._ctx_writer.append_finding_summary(finding)`.
    - _Requirements: 2.3, 2.4_
  - [x] 6.5 In `_make_summary()`, call `self.evidence_store.load_agent_findings(run_id)` and include agent findings in the `TestRunSummary`. Add `agent_findings: Optional[List[Dict]] = None` field to `TestRunSummary` in `models.py`.
    - _Requirements: 9.1_
  - [x] 6.6 Write property test for agent findings in test run summary
    - **Property 7: Agent findings included in test run summary**
    - **Validates: Requirements 9.1**

- [x] 7. Create Kiro steering file
  - [x] 7.1 Create `.kiro/steering/scale-test-observability.md` with sections for: EKS scale test context, AMP metric patterns, CloudWatch log patterns, EKS cluster context, Agent Finding JSON schema (including review field), investigation strategies, and skeptical review process
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

- [x] 8. Create Kiro hook files
  - [x] 8.1 Create `.kiro/hooks/scale-test-proactive-scan.md` with file_change trigger on `agent_context.json` (phase is scaling or hold-at-peak), `askAgent` action with prompt instructing the agent to read context, scan AMP via Prometheus MCP tools, and write findings
    - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - [x] 8.2 Create `.kiro/hooks/scale-test-reactive-investigation.md` with file_change trigger on `agent_context.json` (new alert entries), `askAgent` action with prompt instructing the agent to investigate using all three MCP servers, correlate findings, and write investigation report
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - [x] 8.3 Create `.kiro/hooks/scale-test-skeptical-review.md` with file_change trigger on `findings/agent-*.json` (new finding without review field), `askAgent` action with prompt instructing the agent to independently verify claims, assign confidence, list alternatives, write checkpoint questions, and append review field to finding
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [x] 9. Create MCP server configuration documentation
  - [x] 9.1 Create `.kiro/docs/mcp-server-setup.md` documenting the three MCP server entries to add to `~/.kiro/settings/mcp.json`: `awslabs.prometheus-mcp-server`, `awslabs.cloudwatch-mcp-server`, `awslabs.eks-mcp-server` with their environment variables
    - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 10. Checkpoint - Ensure all integration tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Enhance chart with agent finding markers
  - [x] 11.1 Modify `generate_chart()` in `chart.py` to load `agent-*.json` files from the findings directory, and render them as vertical annotation lines on the Chart.js time axis (color-coded by severity: info=blue, warning=orange, critical=red) with tooltips showing finding title, description, and review confidence level
    - If no agent findings exist, skip the annotation section entirely
    - _Requirements: 9.1, 9.2, 9.3_
  - [x] 11.2 Write property test for chart agent finding markers
    - **Property 8: Chart renders agent finding markers when findings exist**
    - **Validates: Requirements 9.2, 9.3**

- [x] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties
- Unit tests validate specific examples and edge cases
- The Python tool changes are minimal — most of the "intelligence" lives in the Kiro hooks and steering file, not in Python code
- The ContextFileWriter is the only new Python module; everything else is extensions to existing modules
- Hook and steering files are markdown — no compilation or testing needed, but they should be reviewed for prompt quality
