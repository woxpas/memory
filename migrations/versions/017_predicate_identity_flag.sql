-- 017 — Predicates: identity_relevant flag.
--
-- Distinguishes durable identity facts ("Sid lives in Berlin", "Sid prefers
-- async communication") from episodic memory ("Sid talked to Alice yesterday",
-- "Sid decided X"). Phase 2.2.3 profile-fact-accretion uses this flag to
-- decide which extracted self-claims become entity_profile_facts rows powering
-- identity_card().
--
-- New predicates added later default to false (safe — won't auto-pollute the
-- profile). Curating identity is a deliberate act per predicate.
--
-- The taxonomy: identity = "who/what an entity IS" (durable). Memory =
-- "what an entity DID" (episodic). Examples per kind:
--   attribute      identity (mostly — except has_status which is transient)
--   preference     all identity (durable preferences are core to who you are)
--   locational     identity for static placement (located_in); memory for travel
--   quantitative   identity for rates/sizes; memory not currently seeded
--   relation       mostly identity (lives_in, works_at, friend_of, parent_of, …)
--                  except event-shaped (authored_by, sold_by → memory)
--   state          mostly memory (transient); is_deprecated is durable
--   temporal       mostly memory; born_on / anniversary_of are durable facts
--   communicative  all memory (events)
--   reference      all memory (citations)
--   other          all memory (default; the seeded 'other' predicates are
--                  past-tense events: bought, hired, launched, etc.)

ALTER TABLE predicates
  ADD COLUMN identity_relevant boolean NOT NULL DEFAULT false;

CREATE INDEX predicates_identity_idx ON predicates(identity_relevant)
  WHERE identity_relevant = true;

-- ─── Tag identity-relevant predicates (system-global only) ────────────────
UPDATE predicates SET identity_relevant = true
 WHERE tenant_id IS NULL
   AND canonical IN (
     -- attribute (durable traits; has_status is transient so excluded)
     'costs', 'has_color', 'has_count', 'has_quantity', 'has_size',
     'measures', 'weighs',
     -- preference (all 10 — durable)
     'believes', 'dislikes', 'enjoys', 'favours', 'hates', 'likes',
     'loves', 'prefers', 'suspects', 'thinks',
     -- locational (static placement only; travels_to/_from are events)
     'headquartered_in', 'located_in',
     -- quantitative (rates / requirements — durable for the entity)
     'consumes', 'produces', 'requires',
     -- relation (durable identity-shaped; authored_by + sold_by excluded as events)
     'belongs_to', 'born_in', 'collaborates_with', 'dating', 'depends_on',
     'friend_of', 'integrates_with', 'is_a', 'is_member_of', 'knows',
     'lives_in', 'manages', 'married_to', 'mentored_by', 'mentors',
     'owns', 'parent_of', 'powered_by', 'reports_to', 'roommates_with',
     'siblings_with', 'supports', 'works_at',
     -- state (only durable state; transient ones excluded)
     'is_deprecated',
     -- temporal (durable one-time facts about identity)
     'born_on', 'anniversary_of'
   );

INSERT INTO schema_migrations (version) VALUES ('017_predicate_identity_flag');
