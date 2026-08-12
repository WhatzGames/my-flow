# Frontend Reviewer

Frontend Reviewer is a standalone Codex plugin in the My Flow marketplace. It coordinates exactly two parallel frontend review subagents: one focused on UI and one focused on UX.

Review findings are written concisely under the designated worktree's `.reviews` directory. The plugin defaults to at most three rounds, requires prior findings to be re-evaluated, and enforces a decreasing unresolved count unless every remaining item documents a concrete regression risk.

During an active round, its `PreToolUse` hook restricts reads to the designated My Flow worktree and writes to `.reviews`. Private session state and coordinator tokens remain outside reviewer-writable paths.

The plugin uses the target workspace configured by My Flow in `~/.codex/my-flow/config.json`.
