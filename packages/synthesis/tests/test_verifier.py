"""Unit tests for the citation-verifier primitives (woxpas_synthesis.verifier).

The verifier is the mechanical check that keeps synthesis honest — a citation
is only accepted if the claim is lexically or semantically grounded in the
cited span. These cover the deterministic building blocks.
"""

from __future__ import annotations

from woxpas_synthesis.verifier import (
    _content_tokens,
    _cosine,
    _lexical_overlap,
    _tokenize,
)


def test_cosine_identical_normalized_vectors_is_one() -> None:
    assert _cosine([0.6, 0.8], [0.6, 0.8]) == 1.0


def test_cosine_orthogonal_is_zero() -> None:
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_length_mismatch_is_zero() -> None:
    assert _cosine([1.0], [1.0, 0.0]) == 0.0


def test_cosine_clamped_to_unit_interval() -> None:
    # Anti-correlated vectors would give a negative dot product; clamp to 0.
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == 0.0


def test_tokenize_lowercases_and_splits() -> None:
    assert _tokenize("Sarah LED the Migration!") == ["sarah", "led", "the", "migration"]


def test_content_tokens_drop_stopwords_and_singletons() -> None:
    assert _content_tokens("Sarah led the migration") == ["sarah", "led", "migration"]


def test_lexical_overlap_is_stopword_insensitive() -> None:
    # "the" between "led" and "migration" in the chunk must not break the match.
    assert _lexical_overlap("Sarah led migration", "Sarah led the migration yesterday")


def test_lexical_overlap_rejects_unrelated_claim() -> None:
    assert not _lexical_overlap("penguins orbit mars", "Sarah led the migration")


def test_lexical_overlap_empty_claim_is_false() -> None:
    assert not _lexical_overlap("the the the", "Sarah led the migration")
