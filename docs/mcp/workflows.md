# MCP workflows — end-to-end agent sessions

How an agent actually uses Woxpas tools in a real conversation. These are
patterns, not prescriptions — agents can mix and match tools as needed.

---

## Workflow 1: First session with a new agent

The agent connects and gets oriented. It doesn't know anything about you yet.

```
1. resurface_get_context()
   → Thinking Brain frame: active entities, recent topics, open threads,
     overdue tasks, dormant ideas, open conflicts.

2. resurface_get_preferences()
   → How you like answers: short vs detailed, citations on/off, top tools
     by frequency, time-of-day patterns.

3. (agent now has context — proceeds to the actual task)
```

**Why this order:** Context first, preferences second. The agent needs to know
*what's happening* before it can apply *how you like it*.

---

## Workflow 2: Recall → drill down → verify

The most common pattern. User asks a question, agent finds relevant memory,
drills into specifics, gets a verified answer.

```
1. memory_recall("what do we know about the Phoenix migration?")
   → Returns ranked chunks + doc summaries. Intent auto-classified as
     semantic. Shows top 10 results with snippets and scores.

2. (agent reads snippets, finds a relevant chunk about "Bob owns the API
   gateway rewrite")

3. memory_get_chunks(["<chunk_id>"], context_window=2)
   → Gets full text of that chunk plus the 2 surrounding chunks for
     broader context.

4. memory_ask("who owns the API gateway rewrite?")
   → LLM synthesizes answer with citations. Verifier checks each claim.
     Returns: "Bob owns the API gateway rewrite. <c id='1'/>"
     Source [1]: chunk text with title and timestamp.

5. (agent now has a verified answer with provenance — can cite it
   confidently to the user)
```

**When to use recall vs ask:**
- `recall` — fast, cheap, returns raw chunks. Use when the agent just needs
  to find relevant context.
- `ask` — slower (LLM + verifier), returns a synthesized answer with
  citations. Use when the user wants a direct answer, not a list of snippets.

---

## Workflow 3: Ingest → verify → scope

The user shares new information. The agent ingests it, confirms it landed,
and organizes it.

```
1. memory_create_scope("Phoenix Migration", slug="phoenix-migration",
     description="Tracking the Phoenix service migration from monolith
     to microservices")

2. memory_ingest(
     content_text="Met with Alice today. Database migration is on track
       for June. Bob hit a snag with the API gateway — need to discuss
       rate limiting with the platform team.",
     suggested_title="Phoenix sync — Alice + Bob",
     suggested_scope_slugs=["phoenix-migration"],
     captured_by="user",
     source_channel="mcp_inbound",
   )
   → Returns 202 with doc_id. Pipeline runs async.

3. (wait a few seconds, then verify)
   memory_recall("Phoenix sync Alice Bob rate limiting")
   → If the doc is indexed, it appears in results.

4. memory_get_document_full("<doc_id>")
   → See full document with all chunks, status, and metadata.
```

**Why scopes matter:** Assigning documents to scopes at ingest time means
future queries can filter by scope, and the resurfacing layer can surface
scope-specific patterns. It's the difference between a flat pile of notes
and an organized knowledge base.

---

## Workflow 4: Conflict awareness loop

The agent checks for conflicts before answering, and records new information
in a way that lets Woxpas detect contradictions.

```
1. (user says "actually, we're switching to MongoDB")
   → Agent ingests the statement as usual.

2. (later, user asks about database choice)
   memory_recall("database choice")
   → Returns chunks about PostgreSQL AND the new MongoDB statement.

3. (agent notices the contradiction)
   memory_list_conflicts(kind="knowledge")
   → If Woxpas detected the conflict, it appears here with both sides,
     severity, and suggested resolution shape.

4. memory_get_conflict("<conflict_id>")
   → Full detail: both claims, their sources, timestamps, and the
     resolution options.

5. (agent presents the conflict to the user)
   "I found a contradiction: on Monday you said PostgreSQL, today you
    said MongoDB. Which is correct?"

6. (user picks MongoDB)
   memory_resolve_conflict("<conflict_id>", kind="pick",
     winner_claim_id="<mongodb_claim_id>",
     resolution_note="Team decided to switch. PostgreSQL claim was from
       exploratory phase.")

7. (Woxpas records the resolution. Future queries return MongoDB as the
   canonical answer, with PostgreSQL noted as a superseded fact.)
```

---

## Workflow 5: Prepared action lifecycle

Cross-agent action tracking — draft → approve → execute → track.

```
### Phase 1: Draft (any agent)

resurface_post_prepared_action(
  action_type="send_message",
  target="#general",
  content="Heads up: Phoenix migration window is June 1-14. Expect
    intermittent API latency during the cutover.",
  metadata={"priority": "high", "audience": "engineering"},
)
→ Returns {action_id, status: "drafted"}

### Phase 2: Review (any agent, any session)

resurface_list_prepared_actions(status="drafted")
→ Shows all drafted actions across all agents.

(you review and approve)
resurface_approve_prepared_action("<action_id>")
→ status: "approved"

### Phase 3: Execute (agent with the right connector)

resurface_list_prepared_actions(status="approved")
→ "There's an approved message to #general about the Phoenix migration."

(agent sends the message via Slack connector, then records it)
resurface_mark_executed("<action_id>")
→ status: "executed" ✓

### Phase 4: Audit (anytime)

resurface_list_prepared_actions(status="executed")
→ Full history: who drafted, who approved, who executed, timestamps.
```

---

## Workflow 6: Session wrap-up

What the agent does at the end of every session to keep memory fresh.

```
1. resurface_record_observation(
     skill_id="daily-standup-review",
     tool_calls_this_session=12,
     entities_mentioned=["Phoenix migration", "API gateway"],
     outcomes=["Decided to escalate rate limiting issue to platform team"],
     agent="claude-mcp",
   )
   → This feeds the pattern miner. Over time, Woxpas learns which tools
     you use together, what time of day you do certain tasks, and which
     entities cluster in the same sessions.

2. resurface_get_patterns()
   → What changed this session? Any new trending topics? Did any dormant
     ideas resurface? The agent can proactively tell you:
     "By the way, the 'GraphQL for v2 API' idea came up again today —
      that's the 4th mention this month. Want me to create a task for it?"

3. (agent doesn't need to do anything else — the dreaming worker runs
   continuously in the background, refreshing scores, detecting patterns,
   and reassigning scopes)
```

---

## Tool call frequency guide

| Frequency | Tools |
|---|---|
| **Every session** | `resurface_get_context`, `resurface_get_preferences` |
| **Every user query** | `memory_recall` or `memory_ask` |
| **Every new fact** | `memory_ingest` |
| **After every tool call** | `resurface_record_observation` |
| **Session end** | `resurface_get_patterns` |
| **As needed** | `memory_get_chunks`, `memory_get_document_full`, `memory_*_scope`, `memory_*_conflict` |
| **Rare / admin** | `memory_archive_*`, `resurface_*_prepared_action`, `resurface_upsert_persona` |
