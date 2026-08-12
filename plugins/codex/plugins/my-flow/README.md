# My Flow

My Flow is a Codex plugin that maintains bare project repositories and implementation worktrees under a remembered target directory, runs guarded frontend review rounds, restricts Git transport to one remembered SSH Host alias, refreshes remotes at agent startup, auto-allows routine access inside worktrees, and adds approval guardrails for GitHub publishing.

It provides:

- `hooks.json`: hooks that ask for a target workspace before work starts, fetch bare repositories at session startup, enforce work inside managed worktrees, and guard shell, git, gh, and GitHub tool calls.
- `scripts/target_workspace.py`: workspace configuration, layout initialization, bare repository refresh, and worktree enforcement.
- `scripts/allow_worktree_access.py`: scoped approval handling for browser, network, file reading, and file writing inside managed worktrees.
- `scripts/enforce_ssh_host.py`: blocks SSH and network Git transports that do not use the configured SSH Host alias.
- `scripts/require_github_push_approval.py`: a conservative detector for GitHub publishing attempts.
- `scripts/review_session.py`: starts, validates, and stops guarded UI and UX review rounds.
- `scripts/review_guard.py`: blocks reviewer reads outside the designated worktree and writes outside `.reviews`.
- `skills/my-flow/SKILL.md`: operating instructions for Codex workspace and publishing behavior.
- `skills/frontend-reviewer/SKILL.md`: coordinates separate UI and UX subagents for up to three rounds by default.

After a target is set, My Flow creates:

```text
<target>/bares/
<target>/worktrees/
```

Setup then asks once for an SSH `Host` alias and stores it alongside the target workspace. The value survives future tasks and is requested again only after `clear` or if the config is removed.

Each immediate child of `bares` that is a valid bare Git repository is fetched with `git fetch --all --prune` on `SessionStart`. For plain bare clones with no fetch refspec, My Flow initializes remote-tracking refs under `refs/remotes/<remote>/`; existing mirror refspecs are preserved. Implementation edits and shell commands are constrained to `worktrees`. The hooks also block common unapproved publish paths. They are guardrails, not an identity system; they rely on Codex hook execution and the host approval path.

Startup refresh and interactive Git operations require SSH remotes to use the persisted alias. HTTPS Git remotes, other SSH hosts, and `gh repo clone` are blocked because they bypass that alias. Local-path remotes and ordinary web browsing remain available.

Within `worktrees`, permission requests are automatically allowed for non-destructive browser, network, and file operations. Requests involving paths outside `worktrees`, destructive shell commands, or GitHub publishing continue through the normal policy or explicit approval flow. Chrome and in-app browser availability remain controlled by the Codex host.

Repository target:

```text
git@github.com:OWNER/my-flow.git
```

## Local Validation

From the plugin root:

```bash
python3 scripts/target_workspace.py status
python3 scripts/target_workspace.py set-host HOST_ALIAS
python3 scripts/target_workspace.py refresh
python3 scripts/require_github_push_approval.py < tests/fixtures/git-push.json
python3 scripts/require_github_push_approval.py < tests/fixtures/git-status.json
```
