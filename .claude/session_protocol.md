# Session Protocol — Boot / Close Checklists

**Tier 2 — Operational reference. Always current.**
**Audience:** every Claude session, every parallel agent.
**Purpose:** standardised start-of-session and end-of-session routines so handovers don't lose context. Sessions are getting shorter and more frequent; the cure is tighter handovers, not fewer sessions.

---

## The two lines Tomo pastes (canonical opening + closing prompts)

These are the only things Tomo needs to type. Everything else the agent does autonomously.

**Opening (start of session):**
```
Read docs/north_star.md "RULES OF THE GAME" and .claude/next_session_pickup.md,
then run the boot checklist in .claude/session_protocol.md. Acknowledge what
you're working on in one sentence before touching anything. Today's task: [TASK].
```

**Closing (end of session):**
```
Wrap up. Run the close checklist in .claude/session_protocol.md:
overwrite next_session_pickup.md with current state, archive any
docs that became historical, commit + push everything, then output
a 2-line session summary.
```

If you're an agent reading this: the opening line is your trigger to run the boot checklist below. The closing line is your trigger to run the close checklist. Both checklists are non-negotiable — they're how the project stays coherent across sessions.

---

## Boot checklist — run on EVERY session start

Run through this in the first ~5 minutes before doing any real work. Don't skip steps because "I already know."

1. ☐ **Read `docs/north_star.md` §"★ RULES OF THE GAME" FIRST** — the non-negotiable T5 architecture (bronze = source of truth, silver inherits/does-no-work, one-model-per-fact, build-first/train-last, keep-clean). The Rules govern HOW you work. **Then read `.claude/next_session_pickup.md`** — THE current-state doc (read its exec summary, expand if needed). It tells you WHAT to do. Build only in the vein of the Rules.
2. ☐ **Check `git log --oneline -10`** to see what's landed since the pickup file was last updated. The pickup file is overwritten at session-end, but parallel agents may have pushed commits in between.
3. ☐ **Acknowledge what you're working on, in user-visible text.** One sentence: "I'm working on X. The current phase is Y. Recent commits since pickup: Z." This catches misalignment early.
4. ☐ **Skim `docs/north_star.md` phase ladder** — confirm your understanding of which phase is active. If you're about to work on something the ladder doesn't show, stop and re-read.
5. ☐ **If touching T5 detector code:** run bench before any edits. Verify floor is locked (`a798eff0=20/24`, `880dff02=23/24`). If it's red on a clean checkout, something's broken upstream — investigate before touching anything.
6. ☐ **If touching `ml_pipeline/` code:** check who else might be in there. If you're a parallel agent and another session is active on adjacent files, coordinate explicitly via the pickup file or with Tomo.
7. ☐ **For parallel-agent sessions:** confirm your scope doesn't overlap. Different subdirectories or different files. If overlap risk exists, surface it to Tomo before starting.

---

## During-session principles

Don't write these down every time; they're load-bearing patterns:

- **One change per commit.** "Fix X" or "Add Y." Not "fix X, refactor Y, also rename Z." Future grep needs this.
- **Pull-rebase before push.** `git pull --rebase origin main` before every `git push origin main`. Catches parallel-agent races.
- **Default to editing existing docs, not writing new ones.** See `.claude/docs_hygiene.md` §"When NOT to write a new doc."
- **For any non-trivial change, run the bench locally first** if there's a bench that covers your change. See `.claude/sop.md` Routine 3 for the serve bench; future per-component benches when they land.
- **Surface concerns early.** If you're 30 minutes in and realise the original plan is wrong, say so. Don't burn another hour building the wrong thing.

---

## Close checklist — run on EVERY session end

Even if "we're just stopping for the night" — run this. Especially then.

1. ☐ **Overwrite `.claude/next_session_pickup.md`** with current state. Template below.
2. ☐ **Update `docs/north_star.md`** if any phase status changed (DONE / PARKED / new bottleneck). See SOP Routines 5 & 6.
3. ☐ **Archive any docs that became historical.** Per the doc tier system, phase kickoff docs for shipped phases move to `.claude/_archive/`. Session reviews stay in `.claude/` (they're already dated).
4. ☐ **Add a memory entry** (`memory/feedback_*.md` or `memory/project_*.md`) if a generalisable pattern emerged. Don't memory-stuff every session; only when there's a real learning a future session would benefit from. Index in `MEMORY.md`.
5. ☐ **`git status` — confirm nothing critical is left uncommitted.** Untracked artefacts in `ml_pipeline/training/visual_debug/` are expected (gitignored). Anything else: commit it or explicitly decide to leave it.
6. ☐ **`git pull --rebase origin main` then `git push origin main`.** Final sync.
7. ☐ **Output a 2-line session summary** to Tomo: what landed + what the next session does.

---

## The 200-word executive summary at the top of `next_session_pickup.md`

Every pickup file should start with this block. Future sessions read this first; they read the rest only if they need depth.

```markdown
# Next-session pickup — paste this verbatim into the next chat

## ⚡ Executive summary (read this first — 30 seconds)

**Today's date:** [YYYY-MM-DD]
**Phase active:** [e.g. Phase 5c — dual-submit pipeline]
**Bench:** `a798eff0=X/24, 880dff02=Y/24` — green/red
**What shipped last session:** [one sentence]
**What's blocked:** [one sentence, or "nothing"]
**Next session's job:** [one sentence — what to actually do]

If the above is enough, stop reading this file and go.

If you need more depth (e.g. you're inheriting a blocker, or you need to verify the bench claim), continue.

---

[Full pickup body below — read in order, etc.]
```

The point of the executive summary: a session that just needs to know "is the project still on track?" gets that in 30 seconds. A session that's inheriting a thorny bug reads the full file.

---

## Handover pattern between parallel agents (when there are two)

When two agents are working in different chats on adjacent work:

1. **Agent A's session ends first:** Agent A overwrites `next_session_pickup.md` with a section explicitly flagging Agent B's in-flight work — instance IDs, branches, files-not-to-touch. Agent A commits + pushes.
2. **Agent B picks up after:** Agent B reads the pickup, sees Agent A's notes, proceeds without re-deriving Agent A's work.
3. **If both agents are live simultaneously and need to communicate:** Tomo mediates — paste agent A's status to agent B, paste agent B's questions back. The session protocol can't replace Tomo as router yet (no agent-to-agent direct comms in this setup).

---

## Autonomy escalation — when an agent gets stuck

Two failure modes:
- **Stuck waiting for Tomo on something he didn't need to be involved in.** Cause: SOP unclear about whether Tomo's approval is needed. Cure: SOP doc Sec.1 "What requires Tomo" is the answer. If the action isn't on that list, just do it.
- **Stuck because the next step is genuinely ambiguous** (architectural decision, product call, etc.). Cure: write the question crisply, paste it for Tomo, end the session. Don't burn context on indecision.

If you find yourself thinking "I should ask Tomo but I'm not sure if I should bother him" — re-read SOP Sec.1. Most actions don't need him.

---

## Cross-references

- `.claude/sop.md` — the "when X, do Y" reference for routine ops
- `.claude/docs_hygiene.md` — the doc tier system + lifecycle
- `.claude/next_session_pickup.md` — current state (overwritten per session)
- `CLAUDE.md` "Start here" — the top-level routing table
- `MEMORY.md` — auto-memory index
