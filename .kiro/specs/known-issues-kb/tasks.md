# Implementation Plan: Known Issues Knowledge Base

## Overview

Incremental implementation of the KB feature with DynamoDB + S3 storage backend and local in-memory cache. Starts with data models and persistence, then matching engine, anomaly detector integration, ingestion pipeline, CLI commands, infrastructure setup, and seed data migration. Each step builds on the previous and is wired into the existing codebase. Tests use `moto` to mock DynamoDB and S3.

## Tasks

- [x] 1. Define KB data models and add moto dependency
  - [x] 1.1 Add `Signature`, `AffectedVersions`, and `KBEntry` dataclasses to `src/k8s_scale_test/models.py`
    - All three use `_SerializableMixin` for `to_dict` / `from_dict`
    - `KBEntry.status` defaults to `"active"`, `review_notes` / `alternative_explanations` / `checkpoint_questions` are optional
    - `AffectedVersions` has `component`, `min_version`, `max_version`, `fixed_in` (all Optional[str] except component)
    - Add `kb_table_name`, `kb_s3_bucket`, `kb_s3_prefix`, `kb_match_threshold`, `kb_auto_ingest` fields to `TestConfig`
    - Add `moto` to dev dependencies in `pyproject.toml`
    - _Requirements: 1.1, 1.2, 1.4_

  - [ ]* 1.2 Write property test: KBEntry serialization round-trip
    - **Property 1: KB_Entry serialization round-trip**
    - Generate random `KBEntry` instances with hypothesis, verify `KBEntry.from_dict(entry.to_dict())` equals the original
    - Use `hypothesis` strategies for all fields including nested `Signature` and `AffectedVersions`
    - Minimum 100 iterations
    - **Validates: Requirements 1.3**

- [x] 2. Implement KBStore with DynamoDB + S3 persistence and in-memory cache
  - [x] 2.1 Create `src/k8s_scale_test/kb_store.py` with `KBStore` class
    - Constructor takes `aws_client` (boto3 session), `table_name`, `s3_bucket`, `s3_prefix`
    - Creates DynamoDB and S3 clients from the aws_client session
    - On init, performs DynamoDB Scan filtering for `status = "active"`, loads results into `_entries: dict[str, KBEntry]`
    - If DynamoDB is unreachable on init, log error and start with empty `_entries` cache
    - `load_all()` returns list from cache (no network I/O)
    - `get(entry_id)` does O(1) dict lookup on cache
    - `save(entry)` writes DynamoDB PutItem with `ConditionExpression: attribute_not_exists(entry_id) OR entry_id = :id` + S3 PutObject at `{s3_prefix}{entry_id}.json`, then updates cache
    - `delete(entry_id)` does DynamoDB DeleteItem + S3 DeleteObject, then removes from cache, returns bool
    - `update_occurrence(entry_id, timestamp)` does DynamoDB UpdateItem with atomic ADD on occurrence_count + SET last_seen, S3 PutObject with updated body, then updates cache
    - `load_all_direct()` does DynamoDB Scan (no cache), returns all entries
    - `get_direct(entry_id)` does DynamoDB GetItem (no cache)
    - `search(query)` scans cache for case-insensitive match in title or root_cause
    - `reload()` re-reads from DynamoDB into cache
    - Skip malformed DynamoDB items or S3 bodies with warning log during load
    - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4_

  - [ ]* 2.2 Write property test: KBStore save/load round-trip via DynamoDB + S3
    - **Property 2: KBStore save/load round-trip via DynamoDB + S3**
    - Use `moto` to mock DynamoDB table and S3 bucket
    - Generate random sets of `KBEntry` instances, save each to KBStore, call `load_all()`, verify equivalence (order-independent)
    - Minimum 100 iterations
    - **Validates: Requirements 2.1, 2.2, 2.4**

  - [ ]* 2.3 Write property test: Cache initialization loads only active entries
    - **Property 3: Cache initialization loads only active entries**
    - Use `moto` to mock DynamoDB with entries of mixed statuses (active and pending)
    - Initialize a new KBStore, verify `load_all()` returns only active entries
    - Minimum 100 iterations
    - **Validates: Requirements 3.1**

  - [ ]* 2.4 Write property test: Add then remove round-trip
    - **Property 12: Add then remove round-trip**
    - Use `moto` to mock DynamoDB + S3
    - Generate a random `KBEntry`, save to KBStore, then delete by entry_id, verify the store state is unchanged from before the add
    - Minimum 100 iterations
    - **Validates: Requirements 10.1, 10.2**

  - [ ]* 2.5 Write unit tests: malformed data handling and DynamoDB unreachable
    - Test malformed S3 JSON body: create a valid DynamoDB item pointing to a malformed S3 object, verify `load_all()` skips it without crashing
    - Test DynamoDB unreachable on init: mock connection error, verify KBStore starts with empty cache
    - _Requirements: 2.6, 3.4_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement SignatureMatcher
  - [x] 4.1 Create `src/k8s_scale_test/kb_matcher.py` with `SignatureMatcher` class
    - Constructor takes optional `weights` dict (default `{"event": 0.6, "log": 0.3, "metric": 0.1}`) and `threshold` (default 0.7)
    - `score(finding_events, finding_diagnostics, signature)` computes weighted score:
      - `event_score`: fraction of signature event_reasons found in finding event reasons
      - `log_score`: fraction of signature log_patterns matching any SSM output line (use `re.search`, catch `re.error`)
      - `metric_score`: fraction of signature metric_conditions found in finding evidence_references
    - `match(finding_events, finding_diagnostics, entries)` filters to active entries only, scores each, returns `List[Tuple[KBEntry, float]]` sorted descending, filtered by threshold
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 6.6_

  - [ ]* 4.2 Write property test: Similarity score is bounded [0, 1]
    - **Property 4: Similarity score is bounded**
    - Generate random Finding events, diagnostics, and Signature, verify score is in [0.0, 1.0]
    - Minimum 100 iterations
    - **Validates: Requirements 5.1**

  - [ ]* 4.3 Write property test: Match returns above-threshold entries sorted descending
    - **Property 5: Match returns above-threshold entries sorted by descending score**
    - Generate random Finding and set of KBEntries, call `match()`, verify all returned scores > threshold and list is sorted descending
    - Minimum 100 iterations
    - **Validates: Requirements 5.3, 5.4**

  - [ ]* 4.4 Write property test: Search returns only entries containing query
    - **Property 11: Search returns only entries containing the query string**
    - Generate random KBEntries and a query string, call `search()`, verify every result contains the query in title or root_cause (case-insensitive), and no matching entries are missing
    - Minimum 100 iterations
    - **Validates: Requirements 9.3**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Integrate KB into AnomalyDetector
  - [x] 6.1 Modify `AnomalyDetector.__init__` in `src/k8s_scale_test/anomaly.py` to accept optional `kb_store: KBStore` and `kb_matcher: SignatureMatcher`
    - In `handle_alert`, after K8s event collection (Layer 1) but before Layer 5 (EC2 ENI) and Layer 6 (SSM):
      - If `kb_store` is set, call `kb_matcher.match(events, [], kb_store.load_all())`
      - If match found: create Finding with KB entry's root_cause, resolved=True, add `kb_match:{entry_id}` to evidence_references, call `kb_store.update_occurrence(entry_id, now)` to update DynamoDB + S3 + cache, return early
      - If no match: continue with full investigation pipeline
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [ ]* 6.2 Write property test: KB-matched Finding correctness
    - **Property 6: KB-matched Finding has correct root cause and entry reference**
    - Use `moto` for DynamoDB + S3 mocking
    - Generate a KBEntry and a Finding whose events match the entry's signature above threshold
    - Verify the resulting Finding has root_cause == entry.root_cause, resolved == True, and evidence_references contains the entry_id
    - Minimum 100 iterations
    - **Validates: Requirements 6.2, 6.3**

  - [ ]* 6.3 Write property test: KB match increments occurrence count
    - **Property 7: KB match increments occurrence count**
    - Use `moto` for DynamoDB + S3 mocking
    - Generate a KBEntry with a known occurrence_count, trigger a match, verify occurrence_count increased by 1 and last_seen is updated
    - Minimum 100 iterations
    - **Validates: Requirements 6.5**

- [x] 7. Implement IngestionPipeline with skeptical review gating
  - [x] 7.1 Create `src/k8s_scale_test/kb_ingest.py` with `IngestionPipeline` class
    - Constructor takes `KBStore` and `SignatureMatcher`
    - `ingest_finding(finding, review=None)`:
      - Skip if finding.root_cause is None
      - Extract Signature: event_reasons from finding.k8s_events (unique reasons), log_patterns from diagnostics SSM output (extract distinctive substrings)
      - If review provided: high confidence → status="active"; medium → status="pending" with review notes/questions; low → skip, log reason, return None
      - If no review provided: default to status="pending"
      - Check for duplicate via matcher against existing entries; if match, call `kb_store.update_occurrence()` on existing entry
      - If no match, create new KBEntry with generated entry_id, extracted signature, finding's root_cause, persist via `kb_store.save()`
    - `ingest_run(run_dir, evidence_store=None)`: load all finding JSON files from `{run_dir}/findings/`, also load agent findings, process each through `ingest_finding`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8_

  - [ ]* 7.2 Write property test: Signature extraction preserves event reasons
    - **Property 8: Signature extraction preserves Finding event reasons**
    - Generate random resolved Findings with K8s events, extract signature, verify extracted event_reasons is a subset of the Finding's unique event reasons
    - Minimum 100 iterations
    - **Validates: Requirements 7.1, 7.7**

  - [ ]* 7.3 Write property test: Duplicate ingestion is idempotent on entry count
    - **Property 9: Duplicate ingestion is idempotent on entry count**
    - Use `moto` for DynamoDB + S3 mocking
    - Generate a resolved Finding, ingest it twice into the same KBStore, verify the entry count after second ingestion equals the count after first
    - Minimum 100 iterations
    - **Validates: Requirements 7.2**

  - [ ]* 7.4 Write property test: Novel finding ingestion increases entry count
    - **Property 10: Novel finding ingestion increases entry count**
    - Use `moto` for DynamoDB + S3 mocking
    - Generate a resolved Finding with unique event reasons not matching any existing entry, ingest it, verify entry count increased by 1
    - Minimum 100 iterations
    - **Validates: Requirements 7.3**

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Create seed KB entries and update steering file
  - [x] 9.1 Create a seed data loader that writes the 12 seed entries to DynamoDB + S3
    - Create a Python helper function (or `kb seed` CLI command) that writes the 12 seed entries from the design document (ipamd-mac-collision, ipamd-ip-exhaustion, subnet-ip-exhaustion, coredns-bottleneck, karpenter-capacity, image-pull-throttle, oom-kill, disk-pressure, ec2-api-throttle, nvme-disk-init, kyverno-webhook-failure, systemd-cgroup-timeout)
    - Each entry follows the KBEntry schema with appropriate signatures, root causes, recommended actions, affected versions, and status="active"
    - Uses `KBStore.save()` to persist each entry to DynamoDB + S3
    - _Requirements: 8.1_

  - [x] 9.2 Update `scale-test-observability.md` steering file
    - Replace inline pattern descriptions with references to the KB
    - Add instructions for MCP agents to query the KB before manual investigation
    - Keep the steering file as a thin guide that points to the KB for pattern details
    - _Requirements: 8.2, 8.3_

  - [ ]* 9.3 Write unit test: seed entries load correctly
    - Use `moto` to mock DynamoDB + S3
    - Run the seed loader, verify all 12 entries load from KBStore with expected entry_ids, categories, and non-empty signatures
    - _Requirements: 8.1_

- [x] 10. Implement KB CLI commands including infrastructure setup
  - [x] 10.1 Add `kb` subcommand group to `src/k8s_scale_test/cli.py`
    - `kb setup` — create DynamoDB table (entry_id PK, event_reasons GSI), verify S3 bucket exists with versioning; skip if table exists; error if bucket missing
    - `kb list` — display all entries (id, title, category, severity, status, occurrence_count) via `load_all_direct()`; support `--pending` filter
    - `kb search <query>` — text search via direct DynamoDB scan + filter
    - `kb show <entry_id>` — display full entry via `get_direct()` including signature, recommended actions, review notes, alternative explanations, checkpoint questions, affected versions
    - `kb add --title --category --root-cause --event-reasons [--log-patterns] [--severity] [--component --min-version --max-version --fixed-in]` — create new active entry via `save()`
    - `kb remove <entry_id>` — delete entry via `delete()`, error if not found (exit code 1)
    - `kb approve <entry_id>` — change status from pending to active, write back via `save()`
    - `kb ingest <run_dir>` — process resolved findings through IngestionPipeline, error if dir not found (exit code 1)
    - `kb seed` — run the seed data loader to populate initial entries
    - All CLI commands create their own boto3 session from TestConfig and use direct DynamoDB/S3 operations (no local cache)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 10.1, 10.2, 10.3_

  - [ ]* 10.2 Write unit tests for CLI error cases
    - Use `moto` for DynamoDB + S3 mocking
    - Test `kb remove` with non-existent entry_id returns non-zero exit
    - Test `kb ingest` with non-existent run directory returns non-zero exit
    - Test `kb show` with non-existent entry_id returns non-zero exit
    - Test `kb setup` when table already exists skips creation
    - Test `kb setup` when S3 bucket doesn't exist returns non-zero exit
    - _Requirements: 4.3, 4.4, 9.7, 10.3_

- [x] 11. Wire KBStore into controller startup
  - [x] 11.1 Modify `src/k8s_scale_test/controller.py` to create `KBStore` and `SignatureMatcher` at startup
    - Read `kb_table_name`, `kb_s3_bucket`, `kb_s3_prefix`, `kb_match_threshold` from `TestConfig`
    - Create `KBStore` using the existing `aws_client` (boto3 session) already passed to the controller
    - KBStore init eagerly loads active entries from DynamoDB into Local_Cache
    - Pass `KBStore` and `SignatureMatcher` to `AnomalyDetector.__init__`
    - After test run completes, if `kb_auto_ingest` is True, run `IngestionPipeline.ingest_run()` on the current run directory
    - _Requirements: 3.1, 6.1, 7.1_

- [x] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties using `hypothesis`
- Unit tests validate specific examples and edge cases
- All tests use `moto` to mock DynamoDB and S3 — no real AWS calls in the test suite
- The project already has `hypothesis>=6.92.0` in dev dependencies; `moto` needs to be added
- The existing `aws_client` (boto3 session) wired through `ScaleTestController` is reused for KBStore
