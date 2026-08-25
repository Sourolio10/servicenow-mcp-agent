"""In-memory ITSM record store with ServiceNow table semantics.

The store is intentionally faithful to a handful of ServiceNow behaviours that
matter when an LLM agent is driving it:

* records live in named *tables* (``incident``, ``kb_knowledge``, ``cmdb_ci`` ...)
* every record has a ``sys_id`` and a human-readable ``number``
* ``priority`` is derived from ``impact`` x ``urgency`` rather than being set
  directly, so an agent that tries to "just set priority to 1" is corrected
* ``state`` is a numeric code with a canonical label
* ``work_notes`` and ``comments`` are append-only journal fields

Reference fields (``caller_id``, ``assigned_to``, ``cmdb_ci`` ...) are stored as
*display values* rather than sys_ids. Real ServiceNow returns opaque 32-char
GUIDs, which burn tokens and invite hallucinated identifiers; the ServiceNow
backend performs the same normalisation on the way out.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clock import Clock, parse_stamp
from .query import apply as apply_query

SEED_PATH = Path(__file__).parent / "data" / "seed.json"

TABLES = ("incident", "kb_knowledge", "cmdb_ci", "cmdb_rel_ci", "sys_user", "sys_user_group")

NUMBER_PREFIX = {"incident": "INC", "kb_knowledge": "KB"}
NUMBER_WIDTH = 7

STATE_LABELS = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}
STATE_ALIASES = {
    "new": "1",
    "open": "1",
    "in progress": "2",
    "in-progress": "2",
    "work in progress": "2",
    "active": "2",
    "on hold": "3",
    "on-hold": "3",
    "pending": "3",
    "awaiting": "3",
    "resolved": "6",
    "closed": "7",
    "canceled": "8",
    "cancelled": "8",
}
ACTIVE_STATES = {"1", "2", "3"}

# ServiceNow's out-of-the-box impact x urgency -> priority matrix.
PRIORITY_MATRIX = {
    ("1", "1"): "1", ("1", "2"): "2", ("1", "3"): "3",
    ("2", "1"): "2", ("2", "2"): "3", ("2", "3"): "4",
    ("3", "1"): "3", ("3", "2"): "4", ("3", "3"): "5",
}
PRIORITY_LABELS = {
    "1": "1 - Critical",
    "2": "2 - High",
    "3": "3 - Moderate",
    "4": "4 - Low",
    "5": "5 - Planning",
}

CLOSE_CODES = (
    "Solved (Permanently)",
    "Solved (Work Around)",
    "Solved Remotely (Permanently)",
    "Not Solved (Not Reproducible)",
    "Closed/Resolved by Caller",
)

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "and", "or", "but", "to", "of",
    "in", "on", "at", "for", "with", "from", "by", "not", "no", "it", "this",
    "that", "when", "after", "all", "my", "our", "i", "we", "cannot", "cant",
    "unable", "issue", "problem", "error", "errors", "help", "please", "user",
}


class RecordNotFound(KeyError):
    """Raised when a referenced record does not exist."""


class ValidationError(ValueError):
    """Raised when a write would produce an invalid record."""


def _sys_id(table: str, key: str) -> str:
    return hashlib.md5(f"{table}:{key}".encode()).hexdigest()


def tokenize(text: str) -> set[str]:
    """Lowercase content words, used for lightweight similarity scoring."""
    words = re.findall(r"[a-z0-9][a-z0-9\-]{1,}", (text or "").lower())
    return {word for word in words if word not in _STOPWORDS and len(word) > 2}


def normalise_state(value: str | int | None) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text in STATE_LABELS:
        return text
    return STATE_ALIASES.get(text.lower())


def derive_priority(impact: str, urgency: str) -> str:
    return PRIORITY_MATRIX.get((str(impact), str(urgency)), "4")


@dataclass
class AuditEntry:
    """One mutation, recorded so evals can grade *what the agent changed*."""

    timestamp: str
    table: str
    operation: str
    key: str
    changes: dict[str, Any] = field(default_factory=dict)


class ITSMStore:
    """Thread-safe in-memory store seeded from ``data/seed.json``."""

    def __init__(self, seed_path: Path | str = SEED_PATH, clock: Clock | None = None):
        self.seed_path = Path(seed_path)
        self.clock = clock or Clock()
        self._lock = threading.RLock()
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.audit: list[AuditEntry] = []
        self.reset()

    # ------------------------------------------------------------------ setup

    def reset(self) -> None:
        """Reload the fixture, discarding every change made since startup."""
        with self._lock:
            raw = json.loads(self.seed_path.read_text())
            self.tables = {table: [] for table in TABLES}
            self.audit = []
            for table in TABLES:
                for record in raw.get(table, []):
                    self.tables[table].append(self._hydrate(table, copy.deepcopy(record)))

    def _hydrate(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        key = record.get("number") or record.get("name") or record.get("user_name")
        if not key:
            key = f"{table}:{len(self.tables.get(table, []))}"
        record.setdefault("sys_id", _sys_id(table, str(key)))
        record.setdefault("sys_created_on", record.get("opened_at", self.clock.stamp()))
        record.setdefault("sys_updated_on", record.get("sys_created_on"))
        if table == "incident":
            record.setdefault("work_notes", [])
            record.setdefault("comments", [])
            record.setdefault("resolved_at", "")
            record.setdefault("close_code", "")
            record.setdefault("close_notes", "")
            record["priority"] = derive_priority(record.get("impact", "3"), record.get("urgency", "3"))
            record["active"] = "true" if record.get("state") in ACTIVE_STATES else "false"
        if table == "cmdb_rel_ci":
            record.setdefault("sys_id", _sys_id(table, f"{record['parent']}->{record['child']}"))
        if table in ("sys_user", "sys_user_group", "cmdb_ci", "kb_knowledge"):
            record.setdefault("active", "true")
        return record

    # ------------------------------------------------------------------ reads

    def all(self, table: str) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(record) for record in self.tables.get(table, [])]

    def query(
        self,
        table: str,
        encoded_query: str | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
        fields: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = apply_query(self.tables.get(table, []), encoded_query, limit=limit, offset=offset)
            rows = [copy.deepcopy(row) for row in rows]
        if fields:
            keep = list(fields)
            rows = [{name: row.get(name, "") for name in keep} for row in rows]
        return rows

    def count(self, table: str, encoded_query: str | None = None) -> int:
        with self._lock:
            return len(apply_query(self.tables.get(table, []), encoded_query))

    def find(self, table: str, key: str) -> dict[str, Any] | None:
        """Look a record up by sys_id, number, name or user_name."""
        if not key:
            return None
        needle = str(key).strip().lower()
        with self._lock:
            for record in self.tables.get(table, []):
                for candidate in ("sys_id", "number", "name", "user_name"):
                    value = record.get(candidate)
                    if value and str(value).lower() == needle:
                        return copy.deepcopy(record)
        return None

    def get(self, table: str, key: str) -> dict[str, Any]:
        record = self.find(table, key)
        if record is None:
            raise RecordNotFound(f"{table} record {key!r} does not exist")
        return record

    # ----------------------------------------------------------------- writes

    def next_number(self, table: str) -> str:
        prefix = NUMBER_PREFIX.get(table, "REC")
        with self._lock:
            existing = [
                int(record["number"][len(prefix):])
                for record in self.tables.get(table, [])
                if str(record.get("number", "")).startswith(prefix)
                and record["number"][len(prefix):].isdigit()
            ]
        return f"{prefix}{(max(existing) + 1 if existing else 1):0{NUMBER_WIDTH}d}"

    def insert(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if table in NUMBER_PREFIX and not record.get("number"):
                record["number"] = self.next_number(table)
            stamp = self.clock.stamp()
            record.setdefault("opened_at", stamp)
            record["sys_created_on"] = stamp
            record["sys_updated_on"] = stamp
            hydrated = self._hydrate(table, record)
            self.tables.setdefault(table, []).append(hydrated)
            self.audit.append(
                AuditEntry(stamp, table, "insert", hydrated.get("number", hydrated["sys_id"]), dict(hydrated))
            )
            return copy.deepcopy(hydrated)

    def update(self, table: str, key: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for record in self.tables.get(table, []):
                if str(record.get("sys_id")) == str(key) or str(record.get("number", "")).lower() == str(key).lower():
                    changes = {
                        name: value
                        for name, value in patch.items()
                        if record.get(name) != value
                    }
                    record.update(patch)
                    stamp = self.clock.stamp()
                    record["sys_updated_on"] = stamp
                    if table == "incident":
                        record["priority"] = derive_priority(
                            record.get("impact", "3"), record.get("urgency", "3")
                        )
                        record["active"] = "true" if record.get("state") in ACTIVE_STATES else "false"
                    self.audit.append(
                        AuditEntry(stamp, table, "update", record.get("number", record["sys_id"]), changes)
                    )
                    return copy.deepcopy(record)
        raise RecordNotFound(f"{table} record {key!r} does not exist")

    def append_journal(self, table: str, key: str, field_name: str, value: str, author: str) -> dict[str, Any]:
        """Append to an append-only journal field such as ``work_notes``."""
        with self._lock:
            for record in self.tables.get(table, []):
                if str(record.get("sys_id")) == str(key) or str(record.get("number", "")).lower() == str(key).lower():
                    stamp = self.clock.stamp()
                    entry = {"created_on": stamp, "created_by": author, "value": value}
                    record.setdefault(field_name, []).append(entry)
                    record["sys_updated_on"] = stamp
                    self.audit.append(
                        AuditEntry(stamp, table, "journal", record.get("number", record["sys_id"]),
                                   {field_name: value})
                    )
                    return copy.deepcopy(record)
        raise RecordNotFound(f"{table} record {key!r} does not exist")

    # ------------------------------------------------------------- derivations

    def related_cis(self, name: str, direction: str = "both", depth: int = 2) -> dict[str, Any]:
        """Walk ``cmdb_rel_ci`` outward from a CI.

        ``upstream`` are the things this CI *depends on* (its children in the
        relationship table); ``downstream`` are the things that would be
        impacted if this CI failed.
        """
        relationships = self.all("cmdb_rel_ci")
        seen_up: dict[str, int] = {}
        seen_down: dict[str, int] = {}

        def walk(start: str, forward: bool, sink: dict[str, int]) -> None:
            frontier = [(start, 0)]
            while frontier:
                current, level = frontier.pop(0)
                if level >= depth:
                    continue
                for rel in relationships:
                    source = rel["parent"] if forward else rel["child"]
                    target = rel["child"] if forward else rel["parent"]
                    if source.lower() != current.lower():
                        continue
                    if target in sink and sink[target] <= level + 1:
                        continue
                    sink[target] = level + 1
                    frontier.append((target, level + 1))

        if direction in ("both", "upstream"):
            walk(name, True, seen_up)
        if direction in ("both", "downstream"):
            walk(name, False, seen_down)

        status_labels = {"1": "Operational", "2": "Non-Operational", "3": "Degraded", "6": "Retired"}

        def describe(names: dict[str, int]) -> list[dict[str, Any]]:
            out = []
            for ci_name, level in sorted(names.items(), key=lambda item: (item[1], item[0])):
                ci = self.find("cmdb_ci", ci_name) or {}
                status = str(ci.get("operational_status", ""))
                out.append({
                    "name": ci_name,
                    "hops": level,
                    "sys_class_name": ci.get("sys_class_name", ""),
                    "business_criticality": ci.get("business_criticality", ""),
                    "support_group": ci.get("support_group", ""),
                    "operational_status": status_labels.get(status, status),
                })
            return out

        return {"upstream": describe(seen_up), "downstream": describe(seen_down)}

    def similar_incidents(
        self, text: str, *, cmdb_ci: str | None = None, limit: int = 5, exclude: str | None = None
    ) -> list[dict[str, Any]]:
        """Rank incidents by keyword overlap, favouring ones already resolved."""
        needle = tokenize(text)
        if cmdb_ci:
            needle |= tokenize(cmdb_ci)
        scored: list[tuple[float, dict[str, Any]]] = []
        for record in self.all("incident"):
            if exclude and record.get("number", "").lower() == exclude.lower():
                continue
            haystack = tokenize(
                f"{record.get('short_description', '')} {record.get('description', '')} "
                f"{record.get('close_notes', '')} {record.get('cmdb_ci', '')}"
            )
            if not haystack or not needle:
                continue
            overlap = len(needle & haystack)
            if not overlap:
                continue
            score = overlap / len(needle | haystack)
            if cmdb_ci and record.get("cmdb_ci", "").lower() == cmdb_ci.lower():
                score += 0.25
            if record.get("state") in ("6", "7") and record.get("close_notes"):
                score += 0.15  # a solved lookalike is the useful one
            scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].get("number", "")))
        results = []
        for score, record in scored[:limit]:
            results.append({
                "number": record["number"],
                "short_description": record["short_description"],
                "state": STATE_LABELS.get(record.get("state", ""), record.get("state", "")),
                "cmdb_ci": record.get("cmdb_ci", ""),
                "close_code": record.get("close_code", ""),
                "close_notes": record.get("close_notes", ""),
                "opened_at": record.get("opened_at", ""),
                "similarity": round(min(score, 1.0), 3),
            })
        return results

    def search_knowledge(self, text: str, *, category: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        needle = tokenize(text)
        scored: list[tuple[float, dict[str, Any]]] = []
        for article in self.all("kb_knowledge"):
            if category and category.lower() not in article.get("category", "").lower():
                continue
            title_tokens = tokenize(article.get("short_description", ""))
            keyword_tokens = tokenize(article.get("keywords", ""))
            body_tokens = tokenize(article.get("text", ""))
            score = (
                3.0 * len(needle & title_tokens)
                + 2.0 * len(needle & keyword_tokens)
                + 1.0 * len(needle & body_tokens)
            )
            if score <= 0:
                continue
            scored.append((score, article))
        scored.sort(key=lambda item: (-item[0], item[1]["number"]))
        top = scored[:limit]
        peak = max((score for score, _ in top), default=1.0) or 1.0
        return [
            {
                "number": article["number"],
                "short_description": article["short_description"],
                "category": article.get("category", ""),
                "relevance": round(score / peak, 3),
                "snippet": article.get("text", "")[:280] + ("..." if len(article.get("text", "")) > 280 else ""),
            }
            for score, article in top
        ]

    def aggregate_incidents(self, group_by: str, encoded_query: str | None = None) -> list[dict[str, Any]]:
        buckets: dict[str, int] = {}
        for record in self.query("incident", encoded_query):
            key = str(record.get(group_by, "") or "(empty)")
            if group_by == "state":
                key = STATE_LABELS.get(key, key)
            elif group_by == "priority":
                key = PRIORITY_LABELS.get(key, key)
            buckets[key] = buckets.get(key, 0) + 1
        return [
            {"group": name, "count": count}
            for name, count in sorted(buckets.items(), key=lambda item: (-item[1], item[0]))
        ]

    def age_in_hours(self, record: dict[str, Any]) -> float:
        opened = parse_stamp(record.get("opened_at", ""))
        return round((self.clock.peek() - opened).total_seconds() / 3600.0, 2)
