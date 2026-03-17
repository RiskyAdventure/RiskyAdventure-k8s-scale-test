"""KBStore — DynamoDB + S3 persistence with in-memory cache for the Known Issues KB."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from k8s_scale_test.models import KBEntry

logger = logging.getLogger(__name__)


class KBStore:
    """Persistence layer for KB entries backed by DynamoDB + S3 with a local cache.

    The local cache (``_entries``) is populated eagerly on init with all
    *active* entries from DynamoDB.  Read helpers (``load_all``, ``get``,
    ``search``) serve from the cache with zero network I/O.  Write helpers
    (``save``, ``delete``, ``update_occurrence``) use write-through: they
    persist to DynamoDB + S3 first, then update the cache only on success.

    Direct helpers (``load_all_direct``, ``get_direct``) bypass the cache
    and hit DynamoDB directly — intended for CLI use.
    """

    def __init__(
        self,
        aws_client,
        table_name: str = "scale-test-kb",
        s3_bucket: str = "",
        s3_prefix: str = "kb-entries/",
    ) -> None:
        self._table_name = table_name
        self._s3_bucket = s3_bucket
        self._s3_prefix = s3_prefix
        self._dynamodb = aws_client.client("dynamodb")
        self._s3 = aws_client.client("s3")
        self._entries: Dict[str, KBEntry] = {}

        # Eagerly load active entries into cache.
        try:
            self._load_active_into_cache()
        except (ClientError, EndpointConnectionError, NoCredentialsError, Exception) as exc:
            logger.error("Failed to load KB entries from DynamoDB on init: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------


    def _load_active_into_cache(self) -> None:
        """Scan DynamoDB for active entries and populate ``_entries``."""
        entries: Dict[str, KBEntry] = {}
        params = {
            "TableName": self._table_name,
            "FilterExpression": "#s = :active",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":active": {"S": "active"}},
        }
        while True:
            resp = self._dynamodb.scan(**params)
            for item in resp.get("Items", []):
                entry = self._item_to_entry(item)
                if entry is not None:
                    entries[entry.entry_id] = entry
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            params["ExclusiveStartKey"] = last_key
        self._entries = entries
        logger.info("Loaded %d active KB entries into cache", len(self._entries))

    def _item_to_entry(self, item: dict) -> Optional[KBEntry]:
        """Convert a DynamoDB item dict to a ``KBEntry`` by reading the S3 body.

        Returns ``None`` (with a warning log) if the S3 body is missing or
        malformed, or if the DynamoDB item itself is malformed.
        """
        try:
            s3_key = item.get("s3_key", {}).get("S", "")
            if not s3_key:
                entry_id = item.get("entry_id", {}).get("S", "<unknown>")
                logger.warning("Skipping KB entry %s: missing s3_key", entry_id)
                return None
            return self._read_s3_entry(s3_key)
        except Exception:
            entry_id = item.get("entry_id", {}).get("S", "<unknown>")
            logger.warning("Skipping malformed KB entry %s", entry_id, exc_info=True)
            return None

    def _read_s3_entry(self, s3_key: str) -> Optional[KBEntry]:
        """Read and deserialize a KBEntry JSON body from S3."""
        try:
            resp = self._s3.get_object(Bucket=self._s3_bucket, Key=s3_key)
            body = resp["Body"].read().decode("utf-8")
            data = json.loads(body)
            return KBEntry.from_dict(data)
        except (ClientError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to read/parse S3 object %s: %s", s3_key, exc)
            return None

    # ------------------------------------------------------------------
    # Read operations — served from Local_Cache (zero network I/O)
    # ------------------------------------------------------------------

    def load_all(self) -> List[KBEntry]:
        """Return all cached entries as a list."""
        return list(self._entries.values())

    def get(self, entry_id: str) -> Optional[KBEntry]:
        """O(1) dict lookup on the cache."""
        return self._entries.get(entry_id)

    def search(self, query: str) -> List[KBEntry]:
        """Case-insensitive search over cached entries' title and root_cause."""
        q = query.lower()
        return [
            e for e in self._entries.values()
            if q in e.title.lower() or q in e.root_cause.lower()
        ]

    # ------------------------------------------------------------------
    # Write operations — write-through: DynamoDB + S3 → cache
    # ------------------------------------------------------------------

    def save(self, entry: KBEntry) -> None:
        """Persist a KBEntry to DynamoDB + S3, then update the cache.

        Uses a conditional write to prevent silent overwrites from
        concurrent writers.
        """
        s3_key = f"{self._s3_prefix}{entry.entry_id}.json"

        # Build DynamoDB item
        ddb_item = self._entry_to_item(entry, s3_key)

        # DynamoDB PutItem with conditional expression
        self._dynamodb.put_item(
            TableName=self._table_name,
            Item=ddb_item,
            ConditionExpression="attribute_not_exists(entry_id) OR entry_id = :id",
            ExpressionAttributeValues={":id": {"S": entry.entry_id}},
        )

        # S3 PutObject — full JSON body
        self._s3.put_object(
            Bucket=self._s3_bucket,
            Key=s3_key,
            Body=json.dumps(entry.to_dict(), indent=2).encode("utf-8"),
            ContentType="application/json",
        )

        # Update cache on success
        self._entries[entry.entry_id] = entry

    def delete(self, entry_id: str) -> bool:
        """Delete a KBEntry from DynamoDB + S3, then remove from cache.

        Returns True if the entry existed and was deleted, False otherwise.
        """
        s3_key = f"{self._s3_prefix}{entry_id}.json"

        # DynamoDB DeleteItem
        self._dynamodb.delete_item(
            TableName=self._table_name,
            Key={"entry_id": {"S": entry_id}},
        )

        # S3 DeleteObject
        try:
            self._s3.delete_object(Bucket=self._s3_bucket, Key=s3_key)
        except ClientError:
            logger.warning("S3 delete failed for %s (may not exist)", s3_key)

        # Remove from cache
        removed = self._entries.pop(entry_id, None)
        return removed is not None

    def update_occurrence(self, entry_id: str, timestamp: datetime) -> None:
        """Atomically increment occurrence_count and update last_seen.

        Writes to DynamoDB (atomic ADD), then S3 (full body), then cache.
        """
        iso_ts = timestamp.isoformat()

        # DynamoDB UpdateItem — atomic ADD + SET
        self._dynamodb.update_item(
            TableName=self._table_name,
            Key={"entry_id": {"S": entry_id}},
            UpdateExpression="ADD occurrence_count :inc SET last_seen = :ts",
            ExpressionAttributeValues={
                ":inc": {"N": "1"},
                ":ts": {"S": iso_ts},
            },
        )

        # Update the in-memory entry, then write full body to S3
        entry = self._entries.get(entry_id)
        if entry is not None:
            entry.occurrence_count += 1
            entry.last_seen = timestamp
            s3_key = f"{self._s3_prefix}{entry_id}.json"
            self._s3.put_object(
                Bucket=self._s3_bucket,
                Key=s3_key,
                Body=json.dumps(entry.to_dict(), indent=2).encode("utf-8"),
                ContentType="application/json",
            )
        else:
            # Entry not in cache — read from DynamoDB to update S3
            fresh = self.get_direct(entry_id)
            if fresh is not None:
                s3_key = f"{self._s3_prefix}{entry_id}.json"
                self._s3.put_object(
                    Bucket=self._s3_bucket,
                    Key=s3_key,
                    Body=json.dumps(fresh.to_dict(), indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
                self._entries[entry_id] = fresh

    # ------------------------------------------------------------------
    # Direct operations — bypass cache (for CLI)
    # ------------------------------------------------------------------

    def load_all_direct(self) -> List[KBEntry]:
        """DynamoDB Scan returning all entries (no cache)."""
        entries: List[KBEntry] = []
        params = {"TableName": self._table_name}
        while True:
            resp = self._dynamodb.scan(**params)
            for item in resp.get("Items", []):
                entry = self._item_to_entry(item)
                if entry is not None:
                    entries.append(entry)
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            params["ExclusiveStartKey"] = last_key
        return entries

    def get_direct(self, entry_id: str) -> Optional[KBEntry]:
        """DynamoDB GetItem returning a single entry (no cache)."""
        try:
            resp = self._dynamodb.get_item(
                TableName=self._table_name,
                Key={"entry_id": {"S": entry_id}},
            )
        except ClientError:
            logger.warning("DynamoDB GetItem failed for %s", entry_id, exc_info=True)
            return None
        item = resp.get("Item")
        if item is None:
            return None
        return self._item_to_entry(item)

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Force re-read of active entries from DynamoDB into cache."""
        try:
            self._load_active_into_cache()
        except Exception as exc:
            logger.error("Failed to reload KB cache: %s", exc)

    # ------------------------------------------------------------------
    # Infrastructure helpers (static)
    # ------------------------------------------------------------------

    @staticmethod
    def setup_table(aws_client, table_name: str = "scale-test-kb") -> None:
        """Create the DynamoDB table with entry_id PK and event_reasons GSI.

        Skips creation if the table already exists.
        """
        ddb = aws_client.client("dynamodb")
        try:
            ddb.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": "entry_id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "entry_id", "AttributeType": "S"},
                    {"AttributeName": "event_reason", "AttributeType": "S"},
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "event-reasons-index",
                        "KeySchema": [
                            {"AttributeName": "event_reason", "KeyType": "HASH"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            logger.info("Created DynamoDB table %s", table_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceInUseException":
                logger.info("DynamoDB table %s already exists, skipping creation", table_name)
            else:
                raise

    @staticmethod
    def verify_bucket(aws_client, bucket: str) -> bool:
        """Check that the S3 bucket exists and has versioning enabled.

        Returns True if the bucket exists and versioning is enabled.
        """
        s3 = aws_client.client("s3")
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError:
            logger.error("S3 bucket %s does not exist or is not accessible", bucket)
            return False

        try:
            resp = s3.get_bucket_versioning(Bucket=bucket)
            status = resp.get("Status", "")
            if status != "Enabled":
                logger.warning(
                    "S3 bucket %s does not have versioning enabled (status=%s)",
                    bucket,
                    status or "Disabled",
                )
                return False
        except ClientError:
            logger.warning("Could not check versioning on bucket %s", bucket)
            return False

        return True

    # ------------------------------------------------------------------
    # DynamoDB item serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_to_item(entry: KBEntry, s3_key: str) -> dict:
        """Build a DynamoDB item dict from a KBEntry."""
        item: dict = {
            "entry_id": {"S": entry.entry_id},
            "title": {"S": entry.title},
            "category": {"S": entry.category},
            "status": {"S": entry.status},
            "severity": {"S": entry.severity.value if hasattr(entry.severity, "value") else str(entry.severity)},
            "root_cause": {"S": entry.root_cause},
            "occurrence_count": {"N": str(entry.occurrence_count)},
            "last_seen": {"S": entry.last_seen.isoformat()},
            "created_at": {"S": entry.created_at.isoformat()},
            "s3_key": {"S": s3_key},
        }
        # event_reasons as StringSet (from signature)
        reasons = entry.signature.event_reasons
        if reasons:
            item["event_reasons"] = {"SS": reasons}
        return item
