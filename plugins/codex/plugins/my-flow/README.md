# My Flow

My Flow is a Codex plugin that maintains bare project repositories and implementation worktrees under a remembered target directory, restricts Git transport to one remembered SSH Host alias, refreshes remotes at agent startup, auto-allows routine access inside worktrees, and adds approval guardrails for GitHub publishing.

It provides:

- `hooks.json`: hooks that ask for a target workspace before work starts, fetch bare repositories at session startup, enforce work inside managed worktrees, and guard shell, git, gh, and GitHub tool calls.
- `scripts/target_workspace.py`: workspace configuration, layout initialization, bare repository refresh, and worktree enforcement.
- `scripts/allow_worktree_access.py`: scoped approval handling for browser, network, file reading, and file writing inside managed worktrees.
- `scripts/enforce_ssh_host.py`: blocks SSH and network Git transports that do not use the configured SSH Host alias.
- `scripts/require_github_push_approval.py`: a conservative detector for GitHub publishing attempts.
- `scripts/marketplace_boundaries.py`: creates guarded marketplace plugins and validates that every marketplace entry carries the canonical boundary.
- `templates/workspace_guard.py`: the canonical boundary copied into every newly scaffolded companion plugin.
- `git-hooks/commit-msg`: adds the required GPT-5 co-author trailer to new commit messages.
- `git-hooks/post-rewrite`: reports rewritten commits that lost the required trailer after amend or rebase operations.
- `git-hooks/pre-push`: rejects outgoing local commits that are still missing the required trailer.
- `skills/my-flow/SKILL.md`: operating instructions for Codex workspace and publishing behavior.

After a target is set, My Flow creates:

```text
<target>/bares/
<target>/worktrees/
```

Setup then asks once for an SSH `Host` alias and stores it alongside the target workspace. The value survives future tasks and is requested again only after `clear` or if the config is removed.

Each immediate child of `bares` that is a valid bare Git repository is fetched with `git fetch --all --prune` on `SessionStart`. For plain bare clones with no fetch refspec, My Flow initializes remote-tracking refs under `refs/remotes/<remote>/`; existing mirror refspecs are preserved. Implementation edits and shell commands are constrained to `worktrees`. The hooks also block common unapproved publish paths. They are guardrails, not an identity system; they rely on Codex hook execution and the host approval path.

Startup refresh and interactive Git operations require SSH remotes to use the persisted alias. HTTPS Git remotes, other SSH hosts, and `gh repo clone` are blocked because they bypass that alias. Local-path remotes and ordinary web browsing remain available.

Within `worktrees`, permission requests are automatically allowed for non-destructive browser, network, and file operations. Requests involving paths outside `worktrees`, destructive shell commands, or GitHub publishing continue through the normal policy or explicit approval flow. Chrome and in-app browser availability remain controlled by the Codex host.

Every plugin distributed by the My Flow marketplace carries a `PreToolUse` workspace guard. This keeps shell workdirs and implementation writes inside the configured `worktrees` directory even when a companion plugin is installed or invoked independently. Companion plugins require My Flow's persisted target configuration.

Create future marketplace plugins through the guarded scaffold command from the marketplace repository root:

```bash
plugins/codex/plugins/my-flow/scripts/marketplace_boundaries.py create PLUGIN_NAME --root .
```

The command delegates the base manifest to Codex's plugin creator, adds the canonical executable guard and universal hook, appends the marketplace entry, then validates the complete marketplace. Directly added or modified entries are checked again on My Flow session startup and by the shared `pre-push` hook. A plugin with a missing, non-executable, or modified guard is rejected.

Managed worktrees share My Flow's Git hooks. The commit hook adds the co-author trailer, the post-rewrite hook surfaces trailer loss immediately after rewritten history, and the pre-push hook is the final enforcement boundary that blocks publishing any newly introduced local commit without it.

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
python3 scripts/marketplace_boundaries.py validate
```
