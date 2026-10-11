# Live bot testing: handoff (2026-10-11)

Stopped at Nick's request to save usage. Nothing is running.

- Method: notes/sim/live/live.py runs scenarios through the real bot code AND the real Claude model
  (same SDK path as production); Nick is simulated by a second Claude. Scenarios in
  notes/sim/live/scenarios/, outputs in notes/sim/live/runs/r1, r2 (r3 partial, unreviewed).
  Run a round with `bash notes/sim/live/run_round.sh <name> <scenario>...` (4 in parallel; ~4 cores).
- Full log of tests, issues, fixes and what's verified: notes/sim/live/TEST-LOG.md.
- Code: branch claude/project-thread-2exfxj (pushed, commit f013048), 161 unit tests pass. No PR yet.
  Main fixes: user notes memory (save_note/remove_note), pushback only for giving up the day with no
  reason, nudges only after a real slip, rounds spaced to reach the target, "N more today",
  stricter timing-question check, report and prompt wording (feelings first, honest trade-offs).
- Left to do: review/rerun round 3 (incl. new scenarios l_streak, m_repeated_setbacks, n_routine,
  o_holiday) to verify the round-2 fixes live; open the PR; Nick deploys with update.sh after merge.
- Not testable here: free-model fallback (no API keys).
- Tests used Sonnet (BOT_MODEL env in live.py); production may run Opus 5.5 (CLAUDE_MODEL in .env). Set BOT_MODEL to match next time.
- Usage: run 1-2 scenarios at a time, spaced out, after asking Nick (he hit his limit 2026-10-11).
