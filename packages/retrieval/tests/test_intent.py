"""Unit tests for the rule-based intent classifier (woxpas_retrieval.intent).

~95% of queries are expected to route via these pure-Python rules without
touching the LLM fallback, so the routing table is worth pinning.
"""

from __future__ import annotations

import uuid

from woxpas_retrieval.intent import classify_by_rules
from woxpas_retrieval.types import Intent


def test_empty_query_is_uncertain_semantic() -> None:
    r = classify_by_rules("")
    assert r.intent is Intent.SEMANTIC
    assert r.confidence == 0.0
    assert r.uncertain is True


def test_doc_id_reference_is_file_lookup() -> None:
    r = classify_by_rules(f"doc:{uuid.uuid4()}")
    assert r.intent is Intent.FILE_LOOKUP
    assert r.confidence == 1.0


def test_entity_id_reference_is_entity_card() -> None:
    r = classify_by_rules(f"entity:{uuid.uuid4()}")
    assert r.intent is Intent.ENTITY_CARD
    assert r.confidence == 1.0


def test_file_phrase_is_file_lookup() -> None:
    assert classify_by_rules("contents of the report").intent is Intent.FILE_LOOKUP


def test_type_listing_wins_for_list_my_tasks() -> None:
    assert classify_by_rules("list my tasks").intent is Intent.TYPE_LISTING


def test_tell_me_about_is_entity_card() -> None:
    assert classify_by_rules("tell me about Sarah").intent is Intent.ENTITY_CARD


def test_time_phrase_is_timeline() -> None:
    assert classify_by_rules("what happened yesterday").intent is Intent.TIMELINE


def test_connection_phrase_is_graph() -> None:
    assert classify_by_rules("how is Ada connected to Bob").intent is Intent.GRAPH


def test_confidence_always_in_unit_interval() -> None:
    for q in ["", "list my tasks", "tell me about Sarah", "what happened yesterday"]:
        assert 0.0 <= classify_by_rules(q).confidence <= 1.0
