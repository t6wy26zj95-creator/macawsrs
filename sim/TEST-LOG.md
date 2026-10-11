# Live behaviour tests: log

Started 2026-10-11 (thread "Build the project"). Owner of this testing work: that thread.

## How these tests run

Files: this folder (sim/). Run one scenario: `python sim/live.py sim/scenarios/<name>.json /tmp/out`. Mind the usage: each bot and simulated-user turn is a Claude call on the Pro plan, so run one or two at a time.

`notes/sim/live/live.py` runs a scenario through the real bot code (timer, plan, tools, prompts,
database) AND the real model: Claude Sonnet through the Claude Agent SDK, the same code path production
uses (`ClaudeCodeProvider`). Only Telegram is faked, and Nick is played by a second Claude instance
told what to get across (or exact text). The timer ticks every 5 simulated minutes, so reminders,
rounds, nudges and morning check-ins happen on their own like on the VPS.

Earlier rounds (notes/sim/driver.py) had a model *imitating* the bot by hand. These runs are the real
thing, so what's below is what the bot actually said.

Deck: `imported_behind` = Nick's situation (600-card Anki import untouched for over a year, no new
words, ~40 overdue cards spread per day, three patchy days before Sat 10 Oct). Items called testwordNNN
are filler cards.

Each run: `runs/<round>/<scenario>/transcript.txt` (what Nick saw, tool calls marked `·`) and
`prompts.log` (every full prompt the model got).

Not covered: the free-model fallback (Gemini/NVIDIA/Groq): no API keys here, so only Claude is tested.

## Scenarios

| id | what it tests |
|---|---|
| a_productive_day | a full good day, "how am I doing", "did I hit the goal", next morning, "when will the backlog be gone" |
| b_fell_behind | good morning, ignores the afternoon, "what now", "10 more tonight", "tired, good night", next morning |
| c_missed_day | a whole day with no answers (family emergency), comes back next day feeling bad |
| d_overwhelmed | "that's a lot, I'll never catch up", "should I reset or delete half", lighter day, relief |
| e_busy_week | announces a crunch week Mon-Fri on Saturday; does the bot remember on Monday/Tuesday? |
| f_sick | fever, can't study; next day a bit better |
| g_consistency | "I always do 2-3 good days then stop, why and how to fix" |
| h_ambiguous | "meh", "idk. tired", "ok fine", "later", "is it bad I'm only doing a few" |
| i_conflict | wants to clear everything by next weekend but has 15 min a day; asks for 20 in a row |
| j_changing_plans | light day (wedding), then plans change and wants a full day |
| k_night | studies only late at night, past midnight; "what does tomorrow look like" |

## Round 1 (code as merged in PR #38): problems found

Severity: S = serious (wrong, or would upset Nick), M = clearly off, m = minor.

1. S, code. Volunteered clock times ("The next one comes up around 10:48") in a, b, g, h. Cause: the
   "is this a timing question?" check matched any "when", and vocabulary answers are full of
   "it's when someone...". [b, g, h, a]
2. S, code+tool. Pushback on stopping hit the wrong people: a user with a fever ("I'd still say this
   once: 59 cards are left... just 3 easy ones now"), "that's it for tonight, I'm tired, good night" at
   22:30, and a plain "later" with no card open. Worse, the pushback left the timer running, so after
   "good night" a card came 10 minutes later and a reminder at 23:40. [f, b, h]
3. S, code. No memory beyond the last 16 messages. A crunch week announced on Saturday was gone by
   Monday: Monday's check-in planned 66 cards in rounds of 4, Tuesday 69. An agreement made in g
   ("if I say busy, 10 cards") would be lost the same way. [e, g]
4. S, code. "We're a bit behind for today" nudges three times on a day Nick answered every single
   card promptly: the rounds were spaced too far apart to ever reach the day's target, then the bot
   blamed him. Also a nudge right after he'd just talked about his day. [a, b]
5. M, tool. "I can do maybe 10 more tonight" was acknowledged ("Ten more is a good plan") but not
   saved, so the plan kept going, nudged, and later argued with "43 still to do". [b]
6. M, code. Morning check-ins said "doing that clears the pile in about 2 days" and "at your pace it
   won't shrink" side by side: the report fed both. [a, b, d, e, h]
7. M, tool. After every round: "That's the round done. 53 are still to do today..." Robotic and a
   little guilt-inducing, a dozen times a day. Cause: the tool result put the count in front of the
   model. [a, b]
8. M, report. "How am I doing?" on a productive day opened with "Here's the honest picture. You're
   still behind. 38 cards... are all still waiting." The report labelled the backlog "Overall:
   behind" and had no line about how today was going. [a]
9. S, prompt. Overwhelmed user: the bot proved the feeling right with numbers ("2, then 16, then 38
   left over... So it's not just a feeling"), lectured on what going lighter costs unasked, and
   answered "should I delete half?" with a false claim ("deleting half wouldn't change the daily work
   much"). [d]
10. M, prompt. Tacked "There's still a pile from earlier days..." onto Nick saying he felt better. [d]
11. m, event. Timer card after a pause opened with "one quick one while you're here" when he wasn't. [h]
12. M, event. Day after being sick: the bot decided on its own to plan zero cards and paused the day,
    instead of asking how he was. [f]
13. M, prompt. "idk. tired" got graded and the next card came straight away. [h]
14. m, event. Check-ins were 6 to 8 sentences of totals, same shape every morning. [all]
15. M, prompt. "Why do I keep dropping off?" got "I can't tell you why, I'd be guessing" plus a
    bullet list of generic tips; Nick called it generic. (Its second answer was good.) [g]
16. m. Reminder: "it'd help me clear a few more of today's cards" (bot-centred). Not fixed; rare.

## Fixes (branch claude/project-thread-2exfxj)

- (1) Timing check only fires on real timing questions ("when is/will/does...", "when?", "what
  time", "next card", "come back"...). Test added.
- (2) pause_reviews: a plain pause never argues. The case is made at most once a day, only for giving
  up the rest of the day, with 3+ hours of the day left, less than half the target done, and no reason
  given (new has_reason flag: ill, tired, stressed, swamped). The pause holds while the case is made,
  so silence means no; taking the offer caps the day at those few cards. Tests added.
- (3) New memory: `user_notes` table, save_note / remove_note tools, "YOUR NOTES ABOUT THE USER"
  shown in every context with the date written; check-ins are told to plan from them. Test added.
- (4) Nudge only after the user actually let a card wait (a reminder went out) since the plan, the
  last nudge or the last chat. Rounds planned an hour apart (was 40 min) and spread so the last lands
  near the end of the day, so answering every round reaches the target. Tests added/updated.
- (5) set_today_plan more_today: "10 more" is counted from what's done by the code. Test added.
- (6) Report gives one statement: either "clears in N days" or "takes N a day; at your pace it won't
  shrink", never both.
- (7) Round-end tool result marks the count "for you, not to recite"; prompt says not to announce
  round ends.
- (8) Report adds "Today so far: X of today's target of Y done, on pace / a bit behind", and labels
  the pile "Backlog:". Prompt: "how am I doing" leads with what they did.
- (9, 10, 15) Prompt: feelings first; honest trade-offs (deleting does cut work); no backlog
  reminders tacked onto good news; "why do I..." gets possible reasons + a question, not a disclaimer.
- (11) Timer card event says when the user isn't in the chat.
- (12) Check-in: if yesterday's situation may not hold, plan lighter and ask; never a zero-card day
  on its own.
- (13) Prompt: tired/low = keep it light and let them choose.
- (14) Check-in: 2 to 4 sentences, at most 2-3 numbers, vary the opening.

## Round 2 (after the fixes above): results

Verified fixed in real runs: (1) no volunteered times; (2) fever and "tired, good night" respected at
once, "later" just pauses; (3) busy week remembered, Monday capped at 10, Tuesday at 3; (4) no false
"behind" nudges on the productive day; (5) "10 more tonight" saved and kept; (8) "how am I doing"
leads with today's progress; (9) overwhelm answered kindly, honest trade-offs on deleting/resetting;
(12) day after fever: lighter plan and asks how he is.

New smaller issues found in round 2, all fixed (unit tests pass; NOT yet re-verified in a live run):
- set_today_plan later in the day silently changed the round size (went to 10, or 1). Now keeps it.
- Bot named other due words in passing ("not the same as obstinate"). Such a card is now postponed.
- Reminder punned on the answer ("a bit shameless of you" for brazen). Prompt forbids wordplay.
- "Still to do" breakdown recited with wrong sums. Report says give the one number; break down only
  at the check-in or when asked, copying numbers.
- "Proper day after all" kept a cap; tool now says calling without a cap lifts it.
- Rounds still drifted late because answering time added to the wait; now up to 30 min of answering
  comes off the wait. Report no longer says "behind" when every asked card was answered.
- "1 cards a round" grammar.

## Round 3
Stopped partway at Nick's request (usage limit), 2026-10-11 ~10:06. Partial output in runs/r3 is
unreviewed.
