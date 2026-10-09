# What Woxpas actually does for you

Concrete examples of sovereign personal memory in daily use. Each scenario
shows the problem, what Woxpas does, and the result.

---

## 1. Switch agents, keep your memory

**Problem:** You brainstorm in Claude Desktop, then switch to Cursor to write
code. Claude knows about the brainstorming session but Cursor doesn't. You
copy-paste context manually. It's fragile and incomplete.

**What Woxpas does:** Every agent connects to the same memory over MCP. When
you tell Claude "Sarah owns the API layer," Woxpas ingests it. When you ask
Cursor "who owns the API?" — Cursor gets the same answer from the same memory.

```
Claude Desktop                    Cursor
     │                               │
     │  "Sarah owns the API"         │  "who owns the API?"
     │         │                     │         │
     ▼         ▼                     ▼         ▼
          Woxpas ─── Postgres + Qdrant + object store
                       (same data, same tenant)
```

No export, no copy-paste, no "wait, which chat was that in?"

---

## 2. Anti-fade: things you'd forget, surfaced

**Problem:** You had an idea three weeks ago. A task you created is overdue.
A conflict you logged is still unresolved. You forgot all of them because
your chat history is a firehose.

**What Woxpas does:** The resurfacing layer runs continuously in the background
(dreaming worker). It tracks mention velocity, detects stale entities, finds
overdue tasks, and surfaces dormant ideas. Every agent session starts with a
context frame that includes what matters *now*.

```
Agent session start:
  → resurface_get_context()

  Thinking Brain frame:
    ⚠ Overdue tasks (2):
      - "Migrate auth service" — due last Tuesday
      - "Review Q3 budget" — 3 days overdue

    💡 Dormant ideas (1):
      - "GraphQL for v2 API" — last mentioned 3 weeks ago, 7 votes in favor

    📈 Trending topics:
      - "Phoenix migration" — 12 mentions this week, accelerating

    🔗 Open conflicts (1):
      - "Use PostgreSQL vs MongoDB for new service" — unresolved, 2 positions
```

The agent now has a running start — it knows what needs attention without
you having to remember to ask.

---

## 3. Citation-verified answers

**Problem:** An LLM tells you "the budget is 500K." You don't know if that's
from your actual notes or a hallucination. You have to manually cross-check.

**What Woxpas does:** `memory_ask` retrieves relevant chunks, passes them to
the LLM with explicit citation instructions, then *mechanically verifies* every
claim against its cited source. No "LLM-as-judge" — the verifier uses lexical
n-gram overlap + embedding cosine similarity.

```
> memory_ask "what's the Q3 budget for the Alpha project?"

  Answer:
  The Alpha project budget is 450K for Q3. <c id="1"/>

  Claims:        1 verified, 0 weak, 0 unverified

  Sources:
    [1] "Alpha Project Kickoff - 1784329926"
        "The budget is 450K for Q3. Key risk: dependency on the legacy
         auth service."
```

Green checkmark = the statement is confirmed against the source. If the LLM
invents a number, the verifier flags it. You get truth, not improv.

---

## 4. Conflict detection: "wait, that contradicts what I said before"

**Problem:** You tell an agent one thing on Monday ("use PostgreSQL") and
another thing on Thursday ("let's go with MongoDB"). No one notices the
contradiction. Two weeks later, two teams are building against different
databases.

**What Woxpas does:** The conflict detection stage runs after every extraction.
It compares new facts against existing knowledge in SQL — same predicate,
different object, overlapping valid time → conflict. It suggests resolution
shapes: pick one, coexistence (both true in different contexts), progression
(later fact supersedes earlier), or dismissed.

```
> memory_list_conflicts

  Open conflicts (2):

  [severity: high] Database choice: PostgreSQL vs MongoDB
    Source A (3 days ago): "Use PostgreSQL for the new service"
    Source B (today):       "Let's go with MongoDB"
    Suggested: pick (choose one and dismiss the other)

  [severity: low] Meeting time: Tuesday 2pm vs Tuesday 3pm
    Source A: "Standup at 2pm Tuesday"
    Source B: "Standup moved to 3pm Tuesday"
    Suggested: progression (later fact supersedes earlier)
```

You resolve the conflict and Woxpas records the resolution. The knowledge
graph now reflects the canonical truth, with provenance back to both sources.

---

## 5. Prepared actions across agents

**Problem:** Claude drafts an email but can't send it. You copy the text,
switch to Gmail, send it. The draft is lost. Next week you don't remember
whether you sent it.

**What Woxpas does:** Agents post prepared actions to Woxpas. Another agent
(or the same one later) lists pending actions, approves them, executes them,
and marks them complete. The full lifecycle is tracked.

```
Claude: "I'll draft the email for you."
  → resurface_post_prepared_action
    { action_type: "send_email", to: "team@example.com",
      subject: "Q3 Budget Approved", body: "..." }
  → status: drafted

You (reviewing in any agent):
  → resurface_list_prepared_actions (status: drafted)
  → "looks good"
  → resurface_approve_prepared_action
  → status: approved

Next session:
  → resurface_list_prepared_actions (status: approved)
  → "did we send that budget email?"
  → (agent sends via your email connector)
  → resurface_mark_executed
  → status: executed ✓
```

---

## 6. Daily session pattern

Here's what a typical agent session looks like with Woxpas wired in:

```
1. Session start
   resurface_get_context()        → what needs attention?
   resurface_get_preferences()    → how do I like answers?

2. Work
   memory_recall("...")           → find relevant past context
   memory_ask("...")              → get a verified answer
   memory_ingest("...")           → save new information

3. After each action
   resurface_record_observation() → build cross-agent patterns

4. Session end
   resurface_get_patterns()       → what changed this session?
```

The agent doesn't need to *know* about Woxpas internals. It just calls the
tools as part of its normal workflow. The memory layer is ambient.

---

## What Woxpas is not

- **Not a chat backup.** It doesn't store raw conversation transcripts. It
  extracts structured knowledge from what you tell it.
- **Not a search engine.** It doesn't index your entire hard drive. You
  explicitly ingest what you want remembered.
- **Not a cloud service.** Everything runs on your machine. Cloud LLMs are
  opt-in and gated per tenant.
