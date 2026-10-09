"""Linking stage — Phase 2.2.

Subscribes to doc.extracted_facts. Three sequential phases (each lives in
its own helper class so they can evolve independently):

  1. Predicate normalization  — relations.predicate (raw) → predicate_canonical
                                via cosine match against the predicates registry.
                                Flips predicate_kind 'open' → 'typed'.
  2. Entity disambiguation     — merge same-type entities across docs via
                                cosine match on (name + context). Sets
                                merged_into_entity_id, emits entity.merged.
  3. Profile fact accretion    — claims with subject = self_entity AND
                                predicate.kind ∈ {attribute,preference,state}
                                roll into entity_profile_facts.

All three phases are no-LLM (BGE-M3 cosine + SQL only). Total cost ~500ms
per typical doc on Intel CPU.

Implementation order: 2.2.1 ships predicate normalization; 2.2.2 entity
merge; 2.2.3 profile fact accretion. Stage skeleton handles all three from
the start so events + run-row metrics shape stays stable.
"""

from woxpas_pipeline.link.stage import LinkingStage

__all__ = ["LinkingStage"]
