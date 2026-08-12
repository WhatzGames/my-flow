---
name: frontend-reviewer
description: Run structured frontend reviews with exactly two parallel subagents, one limited to UI quality and one limited to UX quality. Use when a user asks to review, audit, critique, or iteratively improve a web or app frontend in a My Flow managed worktree. Keep reviewer access read-only within the designated worktree, permit reports only in .reviews, and run no more than three rounds by default unless the user explicitly requests another limit.
---

# Frontend Reviewer

Coordinate independent UI and UX review passes while the parent agent owns fixes and round decisions.

## Set The Boundary

1. Resolve one immediate My Flow worktree as the designated review target.
2. Use `.reviews` as the only report directory. Treat similarly named directories such as `.review` or `.review.` as invalid.
3. Default to at most three rounds. Use another maximum only when the user explicitly requests it.
4. Prepare any local server or build needed for visual inspection before activating the review session. Reviewers may inspect only worktree files, `file://` pages inside the worktree, and an already-running localhost application. Do not give them internet or connector sources.

Start each round from the plugin root and retain the returned stop token only in the parent context:

```bash
python3 scripts/review_session.py start /absolute/managed/worktree --round 1 --max-rounds 3
```

The active session makes the plugin hook block reads outside the designated worktree and writes outside its `.reviews` directory. Never disclose the stop token to a reviewer.

## Run Two Reviewers

Spawn exactly two subagents in parallel for every round. Do not ask either reviewer to implement fixes.

Give both reviewers the absolute worktree path, current round, local preview URL when available, and these shared constraints:

- Read only inside the designated worktree. Use no web search, external URL, connector, or unrelated local path.
- Write only one role report under `.reviews/round-NN/`.
- Keep every shell workdir inside the designated worktree and use read-only commands only.
- Re-evaluate every finding from the preceding role report. Retain its ID and mark it `completed`, `open`, or `deferred-regression` with current evidence.
- Report only actionable findings. Keep summaries at 160 characters or fewer.

Assign the UI reviewer only visual interface quality: layout, hierarchy, typography, color, spacing, responsive behavior, visual consistency, component states, and visually observable accessibility. Assign IDs `UI-001`, `UI-002`, and so on.

Assign the UX reviewer only interaction and experience quality: task flows, navigation, labels, discoverability, feedback, error recovery, cognitive load, interaction accessibility, and content clarity. Assign IDs `UX-001`, `UX-002`, and so on.

Each reviewer writes `.reviews/round-NN/<role>.json` with this exact shape:

```json
{
  "role": "ui",
  "round": 1,
  "findings": [
    {
      "id": "UI-001",
      "severity": "high",
      "status": "open",
      "summary": "Primary action loses contrast in the disabled state.",
      "evidence": "src/components/CheckoutButton.tsx:42 and localhost checkout disabled state",
      "recommendation": "Raise disabled-state contrast while preserving a clear inactive cue.",
      "regression_risk": ""
    }
  ]
}
```

Use only `high`, `medium`, or `low` severity and `open`, `completed`, or `deferred-regression` status. Require a concise `regression_risk` when deferring an unsafe fix.

## Close And Improve Each Round

Wait for both reviewers, then validate their reports while the guard remains active:

```bash
python3 scripts/review_session.py validate /absolute/managed/worktree --round 1 --token PRIVATE_TOKEN
```

Validation requires every prior finding to be re-evaluated. From round two onward, unresolved findings must decrease. An equal or larger unresolved count is accepted only when every remaining item is `deferred-regression` with a concrete risk explanation.

Stop the guarded session with the private token:

```bash
python3 scripts/review_session.py stop /absolute/managed/worktree --token PRIVATE_TOKEN
```

The parent agent then re-evaluates the reports, rejects false positives, and applies safe fixes outside `.reviews`. Start the next guarded round only after those changes are ready.

Stop early when no unresolved safe finding remains. Never exceed the selected maximum. On the last round, leave `.reviews` in place with the role reports and summaries, and concisely report completed and deferred items to the user. Guard state remains private to My Flow outside the reviewer-writable worktree.
