"""Glue Data Catalog introspection with a process-local TTL cache.

The SQL agent needs the gold-layer schema in its prompt on every turn. Fetching it from
Glue each time costs an API round-trip per turn and — far more expensively — re-sends the
same few hundred tokens to the model on every single call.

The catalog changes when a Glue job runs, not when a user asks a question, so a one-hour
TTL is correct rather than merely convenient.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import boto3

from agents.config import AgentConfig, get_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    comment: str = ""


@dataclass(frozen=True)
class TableInfo:
    database: str
    name: str
    columns: tuple[ColumnInfo, ...]
    partition_keys: tuple[ColumnInfo, ...] = ()

    @property
    def qualified_name(self) -> str:
        return f"{self.database}.{self.name}"

    def to_ddl(self) -> str:
        """Render as CREATE TABLE-ish text.

        DDL rather than JSON on purpose: the model is being asked to write SQL, and a
        schema shown in the language of the task produces better SQL than the same
        information as a nested object.
        """
        lines = [f"CREATE TABLE {self.qualified_name} ("]
        body = [f"  {c.name} {c.type}{f'  -- {c.comment}' if c.comment else ''}" for c in self.columns]
        lines.append(",\n".join(body))
        lines.append(")")
        if self.partition_keys:
            lines.append(f"PARTITIONED BY ({', '.join(k.name for k in self.partition_keys)})")
        return "\n".join(lines)


class SchemaCache:
    """Thread-safe TTL cache over Glue GetTable."""

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or get_config()
        self._client = boto3.client("glue", region_name=self.config.region)
        self._lock = threading.Lock()
        self._tables: dict[str, TableInfo] = {}
        self._fetched_at: float = 0.0

    def get_tables(self, refresh: bool = False) -> list[TableInfo]:
        with self._lock:
            fresh = (time.time() - self._fetched_at) < self.config.schema_cache_ttl_seconds
            if self._tables and fresh and not refresh:
                return list(self._tables.values())

            tables: dict[str, TableInfo] = {}
            for qualified in self.config.gold_tables:
                database, _, name = qualified.partition(".")
                try:
                    tables[qualified] = self._describe(database, name)
                except Exception as exc:  # noqa: BLE001 - a missing table must not be fatal
                    # The gold tables do not exist until the pipeline has run once. The
                    # agent should degrade to "I can't see that table" rather than 500.
                    logger.warning("could not describe %s: %s", qualified, exc)

            if tables:
                self._tables = tables
                self._fetched_at = time.time()

            return list(self._tables.values())

    def _describe(self, database: str, name: str) -> TableInfo:
        response = self._client.get_table(DatabaseName=database, Name=name)["Table"]
        storage = response.get("StorageDescriptor", {})

        return TableInfo(
            database=database,
            name=name,
            columns=tuple(
                ColumnInfo(c["Name"], c.get("Type", "string"), c.get("Comment", ""))
                for c in storage.get("Columns", [])
            ),
            partition_keys=tuple(
                ColumnInfo(c["Name"], c.get("Type", "string"), c.get("Comment", ""))
                for c in response.get("PartitionKeys", [])
            ),
        )

    def schema_prompt(self, refresh: bool = False) -> str:
        """The schema block injected into the SQL-generation prompt."""
        tables = self.get_tables(refresh=refresh)
        if not tables:
            return "(no gold-layer tables are currently available)"
        return "\n\n".join(table.to_ddl() for table in tables)


_cache: SchemaCache | None = None


def get_schema_cache() -> SchemaCache:
    global _cache
    if _cache is None:
        _cache = SchemaCache()
    return _cache
