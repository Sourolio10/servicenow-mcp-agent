import pytest

from snow_mcp.query import QuerySyntaxError, apply, matches, parse

RECORDS = [
    {"number": "INC1", "state": "1", "priority": "1", "short_description": "VPN is down", "assigned_to": ""},
    {"number": "INC2", "state": "2", "priority": "3", "short_description": "Printer offline", "assigned_to": "Jo"},
    {"number": "INC3", "state": "6", "priority": "2", "short_description": "vpn drops often", "assigned_to": "Mei"},
]


def test_parse_and_and_or_grouping():
    query = parse("state=1^ORstate=2^priority<=2")
    assert len(query.groups) == 2
    assert len(query.groups[0]) == 2  # the OR joins the preceding group
    assert query.groups[1][0].operator == "<="


def test_parse_order_by():
    query = parse("state=1^ORDERBYDESCopened_at")
    assert query.order_by == [("opened_at", True)]
    assert len(query.groups) == 1


def test_empty_query_matches_everything():
    assert apply(RECORDS, "") == RECORDS
    assert apply(RECORDS, None) == RECORDS


def test_like_is_case_insensitive():
    assert [r["number"] for r in apply(RECORDS, "short_descriptionLIKEvpn")] == ["INC1", "INC3"]


def test_numeric_comparison_not_lexicographic():
    # "10" must be greater than "9" numerically, not compared as text
    records = [{"n": "9"}, {"n": "10"}]
    assert [r["n"] for r in apply(records, "n>9")] == ["10"]


def test_or_group_then_and():
    got = [r["number"] for r in apply(RECORDS, "state=1^ORstate=2^priority<=2")]
    assert got == ["INC1"]


def test_isempty_and_isnotempty():
    assert [r["number"] for r in apply(RECORDS, "assigned_toISEMPTY")] == ["INC1"]
    assert [r["number"] for r in apply(RECORDS, "assigned_toISNOTEMPTY")] == ["INC2", "INC3"]


def test_in_operator_with_and_without_spaces():
    assert [r["number"] for r in apply(RECORDS, "stateIN1,6")] == ["INC1", "INC3"]
    assert [r["number"] for r in apply(RECORDS, "state IN 1,6")] == ["INC1", "INC3"]


def test_not_equals_and_startswith():
    assert [r["number"] for r in apply(RECORDS, "state!=1")] == ["INC2", "INC3"]
    assert [r["number"] for r in apply(RECORDS, "short_descriptionSTARTSWITHPrinter")] == ["INC2"]


def test_ordering_and_limit():
    got = [r["number"] for r in apply(RECORDS, "^ORDERBYDESCpriority", limit=2)]
    assert got == ["INC2", "INC3"]


def test_journal_fields_are_searchable_as_text():
    record = {"work_notes": [{"value": "escalated to netops"}, {"value": "vendor engaged"}]}
    assert matches(record, parse("work_notesLIKEnetops"))


def test_unparseable_condition_raises():
    with pytest.raises(QuerySyntaxError):
        parse("this is not a condition")
