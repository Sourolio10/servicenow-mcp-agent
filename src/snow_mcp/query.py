"""A small, dependency-free implementation of ServiceNow *encoded queries*.

ServiceNow's Table API filters records with a compact string called an encoded
query, e.g.::

    active=true^priority<=2^short_descriptionLIKEvpn^ORDERBYDESCopened_at

This module parses that grammar and evaluates it against plain dicts, so the
mock ITSM backend behaves like the real thing and the *same* query strings can
be forwarded verbatim to a live ServiceNow instance.

Supported grammar
-----------------
``^``            logical AND between conditions
``^OR``          logical OR with the preceding condition group
``^ORDERBY``     ascending sort, ``^ORDERBYDESC`` descending sort
Operators        ``=`` ``!=`` ``>`` ``<`` ``>=`` ``<=`` ``LIKE`` ``NOTLIKE``
                 ``STARTSWITH`` ``ENDSWITH`` ``IN`` ``NOTIN`` ``ISEMPTY``
                 ``ISNOTEMPTY``

Semantics follow ServiceNow: the outer expression is a conjunction of
disjunctive groups, i.e. ``a=1^ORa=2^b=3`` means ``(a=1 OR a=2) AND b=3``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Condition", "EncodedQuery", "parse", "matches", "apply"]

# Longest first so that ``>=`` is not mistaken for ``>``.
_UNARY_OPERATORS = ("ISNOTEMPTY", "ISEMPTY")
_BINARY_OPERATORS = (
    "STARTSWITH",
    "ENDSWITH",
    "NOTLIKE",
    "NOTIN",
    "LIKE",
    "IN",
    ">=",
    "<=",
    "!=",
    "=",
    ">",
    "<",
)

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")

# ServiceNow's magic full-text search field.
FULL_TEXT_FIELD = "123TEXTQUERY321"


class QuerySyntaxError(ValueError):
    """Raised when an encoded query cannot be parsed."""


@dataclass(frozen=True)
class Condition:
    field: str
    operator: str
    value: str = ""

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.field}{self.operator}{self.value}"


@dataclass
class EncodedQuery:
    """Conjunction of OR-groups plus optional ordering."""

    groups: list[list[Condition]] = field(default_factory=list)
    order_by: list[tuple[str, bool]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.groups


def _split_terms(encoded: str) -> list[tuple[str, str]]:
    """Split on ``^`` while keeping the joiner that introduced each term."""
    terms: list[tuple[str, str]] = []
    for index, raw in enumerate(encoded.split("^")):
        raw = raw.strip()
        if not raw:
            continue
        if index == 0:
            terms.append(("AND", raw))
            continue
        upper = raw.upper()
        if upper.startswith("ORDERBYDESC"):
            terms.append(("ORDERBYDESC", raw[len("ORDERBYDESC") :]))
        elif upper.startswith("ORDERBY"):
            terms.append(("ORDERBY", raw[len("ORDERBY") :]))
        elif upper.startswith("NQ"):
            # "New query" behaves like a top-level OR; treated as OR for
            # simplicity because the mock never needs nested precedence.
            terms.append(("OR", raw[2:]))
        elif upper.startswith("OR"):
            terms.append(("OR", raw[2:]))
        else:
            terms.append(("AND", raw))
    return terms


def _parse_condition(term: str) -> Condition:
    """Split a condition into field, operator and value.

    Two rules keep this unambiguous:

    * the **earliest** operator position wins (longest operator on a tie), so
      ``number=INC0010001`` splits on ``=`` at index 6 rather than on the ``IN``
      that happens to sit inside ``INC``;
    * word operators are matched **case-sensitively**, because ServiceNow always
      uppercases them, so a field named ``min_value`` is not read as ``IN``.
    """
    upper = term.upper()
    for operator in _UNARY_OPERATORS:
        if upper.endswith(operator):
            return Condition(term[: -len(operator)].strip(), operator)

    best: tuple[int, int, str] | None = None  # (position, -length, operator)
    for operator in _BINARY_OPERATORS:
        haystack = term if operator.isalpha() else upper
        position = haystack.find(operator)
        if position <= 0:
            continue
        candidate = (position, -len(operator), operator)
        if best is None or candidate < best:
            best = candidate

    if best is None:
        raise QuerySyntaxError(f"cannot parse condition: {term!r}")
    position, _, operator = best
    return Condition(
        term[:position].strip(),
        operator,
        term[position + len(operator) :].strip(),
    )


def parse(encoded: str | None) -> EncodedQuery:
    """Parse an encoded query string into an :class:`EncodedQuery`."""
    query = EncodedQuery()
    if not encoded or not encoded.strip():
        return query
    for joiner, term in _split_terms(encoded):
        if joiner == "ORDERBY":
            query.order_by.append((term.strip(), False))
        elif joiner == "ORDERBYDESC":
            query.order_by.append((term.strip(), True))
        elif joiner == "OR" and query.groups:
            query.groups[-1].append(_parse_condition(term))
        else:
            query.groups.append([_parse_condition(term)])
    return query


def _coerce(left: Any, right: str) -> tuple[Any, Any]:
    """Compare numerically when both sides look like numbers, else as text."""
    left_text = "" if left is None else str(left)
    if _NUMERIC_RE.match(left_text) and _NUMERIC_RE.match(right):
        return float(left_text), float(right)
    return left_text.lower(), right.lower()


def _field_value(record: dict[str, Any], name: str) -> Any:
    value = record.get(name)
    if isinstance(value, (list, tuple)):
        # Journal fields (work_notes/comments) are searched as their text.
        return " ".join(
            str(entry.get("value", entry)) if isinstance(entry, dict) else str(entry)
            for entry in value
        )
    return value


def _evaluate(record: dict[str, Any], condition: Condition) -> bool:
    # ServiceNow's full-text pseudo-field searches every column at once.
    if condition.field.upper() == FULL_TEXT_FIELD:
        haystack = " ".join(str(_field_value(record, key)) for key in record).lower()
        return all(word in haystack for word in condition.value.lower().split())

    raw = _field_value(record, condition.field)
    text = "" if raw is None else str(raw)
    operator = condition.operator
    needle = condition.value

    if operator == "ISEMPTY":
        return text == ""
    if operator == "ISNOTEMPTY":
        return text != ""
    if operator == "LIKE":
        return needle.lower() in text.lower()
    if operator == "NOTLIKE":
        return needle.lower() not in text.lower()
    if operator == "STARTSWITH":
        return text.lower().startswith(needle.lower())
    if operator == "ENDSWITH":
        return text.lower().endswith(needle.lower())
    if operator in ("IN", "NOTIN"):
        options = {item.strip().lower() for item in needle.split(",") if item.strip()}
        hit = text.lower() in options
        return hit if operator == "IN" else not hit

    left, right = _coerce(raw, needle)
    if operator == "=":
        return left == right
    if operator == "!=":
        return left != right
    try:
        if operator == ">":
            return left > right
        if operator == "<":
            return left < right
        if operator == ">=":
            return left >= right
        if operator == "<=":
            return left <= right
    except TypeError:  # pragma: no cover - mixed types never compare
        return False
    raise QuerySyntaxError(f"unsupported operator: {operator}")


def matches(record: dict[str, Any], query: EncodedQuery) -> bool:
    """True when ``record`` satisfies every OR-group in ``query``."""
    return all(
        any(_evaluate(record, condition) for condition in group)
        for group in query.groups
    )


def _sort_key(record: dict[str, Any], name: str) -> tuple[int, float, str]:
    raw = _field_value(record, name)
    text = "" if raw is None else str(raw)
    if _NUMERIC_RE.match(text):
        return (0, float(text), "")
    return (1, 0.0, text.lower())


def apply(
    records: Iterable[dict[str, Any]],
    encoded: str | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
    order_by: Sequence[tuple[str, bool]] | None = None,
) -> list[dict[str, Any]]:
    """Filter, sort and paginate ``records`` with an encoded query."""
    query = parse(encoded)
    selected = [record for record in records if matches(record, query)]

    ordering = list(order_by or query.order_by)
    for name, descending in reversed(ordering):
        selected.sort(key=lambda record: _sort_key(record, name), reverse=descending)

    if offset:
        selected = selected[offset:]
    if limit is not None:
        selected = selected[:limit]
    return selected
