# Docs Hygiene — Single Hymn Sheet (2026-05-21)

**Tier 2 — Operational reference. Always current.**
**Audience:** every Claude session that works in this repo, and Tomo.
**Purpose:** define what every doc is for, what to read first, and how to keep the doc tree from rotting into "lots of files nobody trusts." If you find yourself wondering "is this doc still true?", this file tells you how to figure that out.

---

## The 30-second version

There are five tiers of docs. Each has a clear purpose and lifecycle:

| Tier | What | When to update |
|---|---|---|
| **1 — TRUTH** | What's true *right now*. Read first, always. | Every session that changes project state. |
| **2 — REFERENCE** | How to do specific operational work. | Only when the operational thing changes. |
| **3 — STRATEGY** | Decisions + research, dated, frozen-in-time. | Never edited after the session that wrote them; archived when superseded. |
| **4 — HISTORICAL** | Session reviews + kickoffs for done/parked work. | Never edited. Moved to `_archive/` when the phase ships. |
| **5 — MEMORY** | Cross-session learnings (auto-loaded). | Updated as patterns emerge. |

**If you only read one doc to know where the project is right now: `.claude/next_session_pickup.md`.** Everything else either supports that doc or is history relative to it.

---

## Tier 1 — THE TRUTH (always current)

These three docs are the load-bearing trio. Every session reads them first. Every session that ends in a meaningful state change updates one or more of them.

| File | What it is | Who updates it |
|---|---|---|
| `CLAUDE.md` | Project orientation. The "router" — pointers to everything else. | Update when adding/removing major modules or changing top-level invariants. |
| `docs/north_star.md` | T5 macro plan + current phase. The strategic ladder. | Update when a phase ships, parks, or a new bottleneck emerges. |
| `.claude/next_session_pickup.md` | What's true today. What just shipped. What the next session does. | **Every session at end.** Overwritten — not appended. |

**Rule:** if Tier 1 contradicts any other doc, Tier 1 wins. The other doc is either historical or wrong and should be corrected/archived.

---

## Tier 2 — OPERATIONAL REFERENCE (current, but stable)

How to do specific things. Read when doing that thing.

| File | Scope |
|---|---|
| `.claude/handover_t5.md` | T5 ML pipeline operational reference: how to ship Batch changes, the test harness, troubleshooting. |
| `.claude/infrastructure/gpu_dev_box_runbook.md` | GPU dev box start/stop/SSH/sync workflow. |
| `.claude/playbook_*.md` | One-off operational playbooks (e.g. AWS Batch on-demand fallback). |
| `docs/dashboards.md` | Gold view catalogue + endpoint mapping. |
| `docs/business.md`, `docs/billing.md`, `docs/pricing_strategy.md` | Product behaviour + billing reference. |
| `docs/ops_runbook.md` | Every `/ops/*` endpoint reference. |
| `docs/env_vars.md` | Environment variable matrix. |
| `docs/technique.md`, `docs/support_bot.md`, `docs/llm_coach_design.md` | Feature reference. |
| `<module>/README.md` | Per-module orientation (file shape, entry points). |
| **This file** (`.claude/docs_hygiene.md`) | The tier system. |

**Rule:** edit in-place when the operational thing changes. Don't write a new dated doc for an update — just edit. The whole point of Tier 2 is "single source of truth, kept current."

---

## Tier 3 — STRATEGY / RESEARCH (frozen, dated, archived when superseded)

Decision docs and research outputs. The session that produced them dates them; future sessions read them once for context, then leave them alone.

Examples (the `.claude/strategy/` and `.claude/research/` dirs no longer exist — these were superseded and now live in `.claude/_archive/`):

- `.claude/_archive/t5_vs_sportai_2026-05-20.md`
- `.claude/_archive/infrastructure_audit_2026-05-20.md`
- `.claude/_archive/dual_submit_status_2026-05-20.md`
- `.claude/_archive/silver_bench_design_2026-05-21.md`
- `.claude/_archive/market_scan_2026-05-20.md`

**Rules:**

1. **Filename includes the date.** `<topic>_YYYY-MM-DD.md`. The date is when the doc was written, not the topic's date.
2. **First line is a tier marker.** Example:
   ```
   **Tier 3 — Strategy doc. Frozen-in-time as of 2026-05-20. Re-read for context, not for current state.**
   ```
3. **Never edited in-place after the session that produced them ends.** If a strategy doc is wrong or outdated, **write a new dated doc** that supersedes it, and add a "Superseded by `<newer>`" note at the top of the old one.
4. **Moved to `.claude/strategy/_archive/`** when explicitly superseded or the topic is now resolved.
5. **Cross-doc references use the dated filename**, so future readers see "this was the state then" without confusion.

---

## Tier 4 — HISTORICAL (frozen forever, archived when the phase ships)

Per-session reviews, phase kickoffs, characterisation docs from previous phases. Frozen-in-time correct, but not active guidance.

Patterns in this repo:

| Pattern | Where it lives | Archive when |
|---|---|---|
| `.claude/session_YYYY-MM-DD*.md` | `.claude/` (current cycle) → `.claude/_archive/sessions/` (when the next phase ships) | Triggering phase ships |
| `.claude/phase<N>_kickoff.md`, `phase<N>_*.md` | `.claude/` (active phase) → `.claude/_archive/` (when phase DONE or PARKED) | Phase status changes |
| `docs/_investigation/*.md` | `docs/_investigation/` permanently | Already in the right place |
| `docs/_archive/*.md` | `docs/_archive/` permanently | Already archived |

**Rule:** the session that ships (or parks) a phase moves the related kickoff/planning/characterisation docs to `_archive/` **in the same commit**. Mention the move in the commit message: `docs: archive phase 5a kickoff (shipped 7d8bfaa)`.

**Why not delete?** Project history is debugging data. The cost to keep is ~10 KB; the cost of losing context for a future regression is hours of re-derivation.

---

## Tier 5 — AUTO-MEMORY

Self-managing system in `memory/` (per-machine, not in git for this project). Indexed by `MEMORY.md`. Loaded automatically into every session context. See `CLAUDE.md` §"auto memory" for the full rules.

**Rule:** if a memory entry references a file path or commit hash, verify it's still valid before acting on it. Memory entries are timestamped observations, not live state.

---

## When NOT to write a new doc

This is the rule most often broken. **Default to NOT writing a new file.** Reach for one of these first:

1. **Edit `CLAUDE.md`** if it's project-orientation info (where things live, what NOT to do).
2. **Edit the existing Tier 2 doc** if it's operational reference for an existing thing.
3. **Edit `next_session_pickup.md`** if it's "current state" info that the next session needs.
4. **Edit `MEMORY.md` + add a memory file** if it's a cross-session pattern or feedback rule.
5. **Edit `docs/north_star.md`** if it's a phase status change.
6. **Add a comment in code** if it's a non-obvious WHY for a specific code path.

Only write a NEW dated doc if:
- The info doesn't belong in any of the above
- It's a substantial decision or research output that future sessions will want to read in full
- You intend to never edit it again after this session

If you find yourself writing a doc that's ≤100 lines and could plausibly go in CLAUDE.md, **stop and put it in CLAUDE.md instead**.

---

## End-of-session checklist for every agent

Before declaring a session done, run through this list:

1. ☐ **Update `.claude/next_session_pickup.md`** — overwrite with current state, what shipped, open items, and the recommended next move.
2. ☐ **If a phase status changed**: update `docs/north_star.md` phase ladder + move related kickoff/planning docs to `_archive/`.
3. ☐ **If you wrote a Tier 3 doc**: confirm first line has the tier marker. Confirm date is in the filename.
4. ☐ **If a Tier 1 or Tier 2 doc went stale during the session**: edit in place, don't write a new dated doc.
5. ☐ **`git pull --rebase origin main` before `git push`** — catches parallel-agent commits.
6. ☐ **Commit message names what changed**, not what got built. Future grep is reading these.

---

## How to spot drift before it becomes a problem

Heuristics that signal "the docs are diverging":

- A doc claims a metric (e.g. bench 23/24) that the latest `bench_baseline.json` doesn't match → STALE
- A "Next move" or "TODO" list in a doc references work that's been completed → STALE
- Two docs in `.claude/strategy/` cover the same topic with the same date suffix → DUPLICATE
- A doc references a task_id that doesn't exist in `bronze.submission_context` anymore → STALE
- A phase kickoff doc still lives in `.claude/` while the phase is DONE in `north_star.md` → HISTORICAL (move to `_archive/`)
- `git log -- <file>.md` shows no edits in 30+ days AND the topic is active → either stale or actually unused (ORPHAN)

**Run a periodic audit** every ~10 sessions: enumerate all `.claude/*.md` and `docs/*.md`, tag each as `current/stale/historical/duplicate/orphan`, act on the non-current ones. An Explore agent does this well in ~20 min.

---

## What this file is NOT

- A reading list (that's `CLAUDE.md` "Start here").
- A current-state snapshot (that's `next_session_pickup.md`).
- A policy committee. The tier system is a tool, not a bureaucracy. Use it when it helps; ignore it when it doesn't.

---

## Cross-references

- `CLAUDE.md` "Start here — what to read first" — the routing table for new sessions.
- `.claude/next_session_pickup.md` — the always-current state.
- `docs/north_star.md` — the phase ladder.
- `MEMORY.md` — the auto-memory index.
