# Requirements Document

## Introduction

The Known Issues Knowledge Base (KB) is a persistent, queryable store of known failure patterns observed during EKS scale tests (10K–30K pods). Today, findings from the anomaly detector are ephemeral JSON files in timestamped run directories, and domain knowledge about common patterns (IPAMD MAC collisions, CoreDNS bottlenecks, Karpenter capacity errors, etc.) is hardcoded in the `scale-test-observability.md` steering file. The KB replaces both with a structured, searchable repository that the anomaly detector and MCP observability agents can query during live test runs to avoid re-investigating known patterns.

The KB uses DynamoDB for the entry index and structured lookups, S3 for full entry bodies with version history, and a local in-memory cache for zero-latency reads during the anomaly detection hot path. This architecture supports multi-machine access (CI pipelines, different EC2 instances), concurrent reads/writes, and a curation workflow across multiple users.

## Glossary

- **KB**: The Known Issues Knowledge Base — the persistent store of known failure pattern entries.
- **KB_Entry**: A single record in the KB representing one known failure pattern, including its signature, root cause, and recommended actions.
- **Signature**: A set of matching criteria (K8s event reasons, log snippets, metric thresholds) that uniquely identify a known failure pattern.
- **Finding**: The existing `Finding` dataclass produced by the `AnomalyDetector` — contains severity, symptom, events, metrics, diagnostics, and root cause.
- **Anomaly_Detector**: The `AnomalyDetector` class in `anomaly.py` that investigates alerts and produces Findings.
- **Evidence_Store**: The `EvidenceStore` class in `evidence.py` that persists test run artifacts to disk.
- **Steering_File**: The `scale-test-observability.md` file that currently contains hardcoded domain knowledge for MCP observability agents.
- **Similarity_Score**: A numeric value (0.0–1.0) representing how closely a Finding matches a KB_Entry signature.
- **Ingestion_Pipeline**: The component that converts raw Findings into candidate KB_Entries for review.
- **KB_Store**: The persistence layer that reads and writes KB_Entries to DynamoDB and S3, with a local in-memory cache for reads.
- **DynamoDB_Table**: The AWS DynamoDB table storing KB_Entry index records with a GSI on event_reasons for fast lookups.
- **S3_Bucket**: The AWS S3 bucket storing full KB_Entry JSON bodies with versioning enabled for entry history.
- **Local_Cache**: An in-memory `dict[str, KBEntry]` loaded at startup from DynamoDB, used for zero-latency reads during the investigation loop.

## Requirements

### Requirement 1: KB Entry Data Model

**User Story:** As a scale test operator, I want each known issue to be stored as a structured entry with a clear signature, root cause, and recommended actions, so that the system can match new findings against known patterns.

#### Acceptance Criteria

1. THE KB_Entry SHALL contain: a unique identifier, a human-readable title, a category (one of: networking, scheduling, capacity, runtime, storage, control-plane), a Signature object, a root cause description, a list of recommended actions, a severity level, a list of affected software versions (each specifying component name, optional min/max version range, and optional fixed-in version), a creation timestamp, a last-seen timestamp, an occurrence count, a status (one of: active, pending), optional review notes, optional alternative explanations, and optional checkpoint questions.
2. THE Signature SHALL contain: a list of K8s event reason strings to match, a list of log pattern regular expressions, a list of metric threshold conditions, and an optional list of affected Kubernetes resource kinds.
3. WHEN a KB_Entry is serialized to JSON, THE KB_Store SHALL produce a valid JSON document that can be deserialized back into an equivalent KB_Entry object (round-trip property).
4. THE KB_Entry SHALL implement the same `_SerializableMixin` interface (`to_dict` / `from_dict`) used by existing models in `models.py`.

### Requirement 2: KB Persistence via DynamoDB and S3

**User Story:** As a scale test operator, I want the knowledge base to be stored in DynamoDB and S3, so that entries are accessible from multiple machines, support concurrent access, and retain version history automatically.

#### Acceptance Criteria

1. THE KB_Store SHALL persist each KB_Entry as a DynamoDB item in a configurable table (default: `scale-test-kb`) with `entry_id` as the partition key.
2. THE KB_Store SHALL persist the full KB_Entry JSON body to S3 at key `kb-entries/{entry_id}.json` in a configurable S3 bucket with versioning enabled.
3. THE DynamoDB_Table SHALL have a Global Secondary Index (GSI) on the `event_reasons` attribute to support fast lookups by K8s event reason.
4. WHEN a new KB_Entry is saved, THE KB_Store SHALL write to both DynamoDB and S3 in a write-through pattern, updating the Local_Cache after successful writes.
5. WHEN a KB_Entry is saved, THE KB_Store SHALL use a DynamoDB conditional write (condition expression on `entry_id`) to prevent silent overwrites from concurrent writers.
6. WHEN a KB_Entry JSON document in S3 is malformed or a DynamoDB item fails deserialization, THE KB_Store SHALL log a warning and skip the entry without crashing.
7. WHEN the S3 bucket has versioning enabled, THE S3_Bucket SHALL automatically retain previous versions of each KB_Entry JSON body on every PutObject, providing entry history for free.

### Requirement 3: Local In-Memory Cache

**User Story:** As a scale test operator, I want the KB to maintain a local in-memory cache of active entries, so that the anomaly detector hot path adds zero latency for KB lookups during live test runs.

#### Acceptance Criteria

1. WHEN the KB_Store is initialized, THE KB_Store SHALL perform a DynamoDB Scan of all active entries and load them into the Local_Cache.
2. WHILE the anomaly detection investigation loop is running, THE KB_Store SHALL serve all read operations (load_all, get, search, match) from the Local_Cache with zero network I/O.
3. WHEN a KB_Entry is saved or deleted, THE KB_Store SHALL update the Local_Cache immediately after the DynamoDB and S3 writes succeed (write-through caching).
4. WHEN the KB_Store is initialized and DynamoDB is unreachable, THE KB_Store SHALL log an error and start with an empty Local_Cache, allowing the anomaly detector to proceed with full investigation for all alerts.

### Requirement 4: Infrastructure Setup

**User Story:** As a scale test operator, I want a simple way to provision the DynamoDB table and S3 bucket configuration, so that the KB infrastructure is ready before first use.

#### Acceptance Criteria

1. WHEN a user runs the KB setup command, THE CLI SHALL create the DynamoDB table with the specified table name, partition key (`entry_id`), and GSI on `event_reasons`.
2. WHEN a user runs the KB setup command, THE CLI SHALL verify that the configured S3 bucket exists and has versioning enabled, logging a warning if versioning is not enabled.
3. IF the DynamoDB table already exists, THEN THE CLI SHALL skip table creation and log an informational message.
4. IF the S3 bucket does not exist, THEN THE CLI SHALL print an error message instructing the operator to create the bucket manually, and exit with a non-zero status code.

### Requirement 5: Signature Matching

**User Story:** As the anomaly detector, I want to match a new Finding against all KB_Entry signatures, so that known patterns are identified before launching a full investigation.

#### Acceptance Criteria

1. WHEN a Finding is compared against a KB_Entry Signature, THE KB SHALL compute a Similarity_Score between 0.0 and 1.0.
2. THE Similarity_Score SHALL weight event reason matches, log pattern matches, and metric threshold matches according to configurable weights that sum to 1.0.
3. WHEN the Similarity_Score exceeds a configurable threshold (default: 0.7), THE KB SHALL classify the Finding as matching that KB_Entry.
4. WHEN multiple KB_Entries match a Finding, THE KB SHALL return all matches ranked by descending Similarity_Score.
5. WHEN no KB_Entry matches a Finding, THE KB SHALL return an empty result list.
6. THE Signature matching SHALL complete within 500ms for a KB containing up to 500 entries.

### Requirement 6: Anomaly Detector Integration

**User Story:** As a scale test operator, I want the anomaly detector to consult the KB before performing a full investigation, so that known issues are resolved instantly without redundant SSM commands and API calls.

#### Acceptance Criteria

1. WHEN the Anomaly_Detector receives an alert, THE Anomaly_Detector SHALL query the KB with the collected K8s events before proceeding to SSM diagnostics.
2. WHEN the KB returns a match with Similarity_Score above the threshold from an active KB_Entry, THE Anomaly_Detector SHALL create a Finding with the KB_Entry root cause and mark it as resolved, skipping SSM and EC2 evidence collection.
3. WHEN the KB returns a match, THE Anomaly_Detector SHALL include a reference to the matched KB_Entry identifier in the Finding evidence_references.
4. WHEN the KB returns no match, THE Anomaly_Detector SHALL proceed with the full investigation pipeline unchanged.
5. WHEN a KB match is used, THE KB_Store SHALL update the matched KB_Entry last-seen timestamp and increment the occurrence count via a DynamoDB UpdateItem and S3 PutObject.
6. THE Anomaly_Detector SHALL only match against KB_Entries with active status, ignoring entries with pending status.

### Requirement 7: Finding Ingestion Pipeline

**User Story:** As a scale test operator, I want resolved findings from past runs to be automatically converted into candidate KB entries, so that the knowledge base grows from real operational data.

#### Acceptance Criteria

1. WHEN the Ingestion_Pipeline processes a resolved Finding (one with a non-null root_cause), THE Ingestion_Pipeline SHALL extract a Signature from the Finding K8s events, node diagnostics, and evidence references.
2. WHEN the extracted Signature matches an existing KB_Entry above the threshold, THE Ingestion_Pipeline SHALL update the existing entry last-seen timestamp and occurrence count instead of creating a duplicate.
3. WHEN the extracted Signature does not match any existing KB_Entry, THE Ingestion_Pipeline SHALL create a new candidate KB_Entry.
4. WHEN the source Finding has an associated skeptical review with high confidence, THE Ingestion_Pipeline SHALL create the KB_Entry with active status.
5. WHEN the source Finding has an associated skeptical review with medium confidence, THE Ingestion_Pipeline SHALL create the KB_Entry with pending status and include the review checkpoint questions and alternative explanations.
6. WHEN the source Finding has an associated skeptical review with low confidence, THE Ingestion_Pipeline SHALL skip KB_Entry creation and log the reason.
7. THE Ingestion_Pipeline SHALL extract event reasons from the Finding k8s_events list to populate the Signature event_reasons field.
8. THE Ingestion_Pipeline SHALL extract log patterns from the Finding node_diagnostics SSM output to populate the Signature log_patterns field.

### Requirement 8: Steering File Migration

**User Story:** As a scale test operator, I want the hardcoded domain knowledge in `scale-test-observability.md` to be migrated into KB entries, so that the steering file becomes a thin pointer to the KB and patterns are queryable.

#### Acceptance Criteria

1. WHEN the KB is initialized, THE KB SHALL contain seed entries for the known patterns currently documented in `scale-test-observability.md`: IPAMD IP allocation failures, VPC CNI MAC collisions, CoreDNS bottlenecks, Karpenter InsufficientCapacityError, image pull throttling, OOM kills, disk pressure, EC2 API throttling, and NVMe disk initialization failures.
2. THE Steering_File SHALL be updated to reference the KB for pattern details instead of containing the full pattern descriptions inline.
3. WHEN an MCP observability agent reads the Steering_File, THE Steering_File SHALL direct the agent to query the KB for known pattern matching before manual investigation.

### Requirement 9: KB Query CLI

**User Story:** As a scale test operator, I want to query the knowledge base from the command line, so that I can search, list, and inspect known issues without writing code.

#### Acceptance Criteria

1. WHEN a user runs the KB list command, THE CLI SHALL display all KB_Entries with their identifier, title, category, severity, status, and occurrence count.
2. WHEN a user runs the KB list command with a pending filter, THE CLI SHALL display only KB_Entries with pending status.
3. WHEN a user runs the KB search command with a text query, THE CLI SHALL return KB_Entries whose title or root cause description contains the query string (case-insensitive).
4. WHEN a user runs the KB show command with an entry identifier, THE CLI SHALL display the full KB_Entry including signature details, recommended actions, review notes, alternative explanations, and checkpoint questions.
5. WHEN a user runs the KB ingest command with a run directory path, THE CLI SHALL process all resolved findings in that directory through the Ingestion_Pipeline.
6. WHEN a user runs the KB approve command with an entry identifier, THE CLI SHALL change the KB_Entry status from pending to active.
7. IF the specified run directory does not exist, THEN THE CLI SHALL print an error message and exit with a non-zero status code.
8. THE CLI SHALL read and write directly to DynamoDB and S3 without using the Local_Cache.

### Requirement 10: KB Entry Lifecycle

**User Story:** As a scale test operator, I want to manually add, edit, and remove KB entries, so that I can curate the knowledge base with expert knowledge beyond what automated ingestion captures.

#### Acceptance Criteria

1. WHEN a user runs the KB add command with required fields (title, category, root cause, event reasons), THE CLI SHALL create a new KB_Entry and persist it to DynamoDB and S3.
2. WHEN a user runs the KB remove command with an entry identifier, THE CLI SHALL delete the corresponding KB_Entry from DynamoDB and S3.
3. IF the entry identifier does not exist in the KB_Store, THEN THE CLI SHALL print an error message and exit with a non-zero status code.
