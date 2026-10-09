# Woxpas MCP tool inventory

28 tools (13 memory + 15 resurface). For end-to-end session walkthroughs,
see **[workflows.md](workflows.md)**. For what Woxpas actually does for you
day-to-day, see **[../examples.md](../examples.md)**.

## Session pattern (every agent should follow this)

```
Session start:
  resurface_get_context()         ← what needs attention right now?
  resurface_get_preferences()     ← how does this user like answers?

Every query:
  memory_recall(...) or memory_ask(...)

Every new information:
  memory_ingest(...)

After every tool call:
  resurface_record_observation(...)  ← build cross-agent patterns

Session end:
  resurface_get_patterns()         ← what changed this session?
```

## Memory (13)

| tool | when to use |
|---|---|
| `memory_recall` | Free-text query → chunks/cards/lists. Routes via `intent_hint`: semantic / entity_card / type_listing / file_lookup / graph / timeline. Default lean envelope. |
| `memory_ask` | Citation-verified synthesis. Expensive (LLM + verifier). Cloud models trigger the policy gate. Prefer `memory_recall` when chunks suffice. |
| `memory_get_chunks` | Hydrate chunk_ids to full text (batch up to 100). Optional `context_window` for surrounding chunks. |
| `memory_ingest` | Capture text content. Same pipeline as file upload. **Best practice**: pass `suggested_scope_slugs`. Returns 202-style — pipeline runs async. |
| `memory_get_document_full` | Full doc + ALL chunks + optional decrypted canonical CDM + optional raw bytes. Direct fetch — works on archived docs. |
| `memory_create_scope` | New scope/project/topic. Embeds name+description into Qdrant for auto-assignment. Idempotent on slug collision. |
| `memory_assign_to_scope` | Attach existing doc to scope. Idempotent. Multi-scope membership allowed. |
| `memory_unassign_from_scope` | Remove doc-scope membership. Idempotent. Doc may end up unscoped (valid per Track 1). |
| `memory_archive_document` | Exclude from retrieval (semantic + timeline + listing). KG entities preserved. |
| `memory_unarchive_document` | Restore archived doc. |
| `memory_list_conflicts` | List conflicts (state/severity/kind filters). Default: open + severity-ordered. |
| `memory_get_conflict` | Full conflict detail + per-side claims + suggested resolution shapes. |
| `memory_resolve_conflict` | Apply resolution: pick / coexistence / progression / dismissed. Discriminated by `kind`. |

## Resurface (15)

| tool | when to use |
|---|---|
| `resurface_get_context` | Thinking Brain — call once per session for the agent's background frame. ~50-100ms. |
| `resurface_get_patterns` | Dormant Brain — trending/fading/forgotten/recurring/attention signals. |
| `resurface_record_observation` | Log every tool/skill call. Memory builds cross-agent patterns over time. |
| `resurface_get_preferences` | Cross-agent user preferences (length, citations, top tools, time-bucketed). Read every turn. |
| `resurface_get_tool_suggestions` | Frequency-ranked tool hints. |
| `resurface_get_predicted_actions` | Memory-internal opportunities (overdue tasks, conflicts, dormant ideas, stale entities, unscoped docs) with urgency hints. |
| `resurface_get_predicted_questions` | Pre-cached synthesis answers (cloud-gated; empty when cloud disallowed). |
| `resurface_post_prepared_action` | Agent posts a composed action (e.g., email draft) for cross-agent persistence + approval. |
| `resurface_list_prepared_actions` | List prepared actions (default: drafted+approved). |
| `resurface_approve_prepared_action` | drafted → approved. |
| `resurface_reject_prepared_action` | drafted/approved → rejected. |
| `resurface_mark_executed` | approved → executed (after agent ran the action via its own connector). |
| `resurface_upsert_persona` | Deep-merge a behavioral persona/tone patch. Stored in `persona_tone`, not the knowledge graph. |
| `resurface_record_decision` | Record an agent decision for pattern mining over time. |
| `resurface_build_identity` | Cold-start self-service identity: one call writes user-asserted profile facts (bio, work, comm style, location, interests, expertise) straight to the graph at confidence 1.0 — no extraction wait. |

## Error codes

All errors carry `{"error": {"code": str, "message": str, "retriable": bool}}` in
the response text when `isError=true`.

| code | meaning |
|---|---|
| `tenant_not_set` | Server started without `WOXPAS_MCP_TENANT_ID` (fail-closed) |
| `not_found` | Tool, scope, doc, or conflict not found (or RLS-hidden) |
| `validation_failed` | Pydantic input validation failed (or required field per shape missing) |
| `invalid_state` | State-machine transition rejected (e.g., re-approve executed) |
| `cloud_policy_denied` | Tenant or request denies cloud egress; only fires on `memory_ask` |
| `precondition_failed` | Operation blocked by an upstream condition (e.g., duplicate doc) |
| `rate_limited` | Token-bucket exhausted; retriable after `retry_after_ms` (in message) |
| `internal_error` | Unhandled server-side error (always look at server stderr for details) |
