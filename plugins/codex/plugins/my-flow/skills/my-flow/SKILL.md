---
name: my-flow
description: Maintain project bare repositories and implementation worktrees inside a remembered target directory, refresh remotes at agent startup, and enforce explicit user approval before GitHub publishing actions. Use at the start of Codex work, when the target workspace may be unset, when adding or using a project repository, or when a task may publish GitHub state.
---

# My Flow

Before doing any work, complete My Flow's one-time setup in this order.

At startup, the `SessionStart` hook runs `python3 scripts/target_workspace.py startup`. If a target is already configured, the command ensures the target, `bares`, and `worktrees` directories exist. If no target is configured, it initializes one from `MY_FLOW_STARTUP_PATH` when set, otherwise from the startup payload path such as `cwd` when Codex provides one. If no startup path is available, it leaves setup incomplete and the normal prompt-time guard asks the user for a target.

First, if the target is unset, ask the user for the directory. After the user provides it, persist it with:

```bash
python3 scripts/target_workspace.py set /absolute/path
```

Second, if the SSH Host is unset, ask which `Host` alias from their SSH config to use. Persist the exact alias with:

```bash
python3 scripts/target_workspace.py set-host HOST_ALIAS
```

Do not ask for either value again after it has been persisted. `status` reports both values. `clear` intentionally forgets both and restarts setup.

Setting the target creates this layout:

```text
<target>/
  bares/       # one bare Git repository per project
  worktrees/   # checked-out worktrees used for implementation
```

Keep each project's shared Git data in `<target>/bares/<project>.git`. Create implementation checkouts with `git --git-dir=<bare-repo> worktree add <target>/worktrees/<project>-<branch> <branch>`. Do not edit files in `bares` directly.

At agent startup, after startup setup, the `SessionStart` hook runs `git fetch --all --prune` against every valid immediate child repository in `bares`. Plain bare clones receive a missing fetch refspec that updates `refs/remotes/<remote>/*`; existing mirror or custom refspecs remain unchanged. If a refresh fails, report the affected repository and resolve the stale or inaccessible remote before relying on its refs.

At agent startup, My Flow also configures each valid immediate child worktree in `worktrees` to use this plugin's Git hooks. The `commit-msg` hook appends `Co-authored-by: GPT-5 <noreply@openai.com>` when the trailer is missing, including for `git commit -m`.

Use the configured SSH Host alias for every SSH and network Git remote, for example `git@HOST_ALIAS:owner/repository.git` or `ssh://git@HOST_ALIAS/owner/repository.git`. The SSH host hook blocks direct SSH commands, Git URLs, configured Git remotes, HTTPS/Git-protocol remotes, and `gh repo clone` when they bypass the selected alias. Local filesystem remotes remain allowed. Ordinary browser and web traffic is not restricted by the SSH Host policy.

After a target is set, run shell commands with `workdir` inside `<target>/worktrees` and keep file edits there. If Codex opened elsewhere, treat that as only the host launch location.

Inside a managed worktree, treat Chrome or the in-app browser, internet access, file reads, and file writes as available working capabilities. Use them directly when the task calls for them instead of asking the user for routine access approval. The `PermissionRequest` hook auto-allows non-destructive tool requests whose current directory and explicit file paths stay inside `worktrees`.

The access hook does not auto-allow destructive shell commands, paths outside `worktrees`, or GitHub publishing. Browser access still depends on Chrome or the in-app browser being installed and enabled by the Codex host; a plugin cannot install or override a host-disabled browser capability.

Before any GitHub publishing action, stop and get explicit user approval. The configured SSH Host does not replace publishing approval.

Publishing actions include:

- `git push`, including forced pushes, tag pushes, and pushes through alternate remotes.
- `gh repo sync`, `gh pr merge`, `gh pr ready`, `gh release create`, `gh release upload`, and `gh workflow run`.
- `gh api` calls that mutate Git refs, commits, tags, pull requests, or releases.
- GitHub connector or MCP calls that create, update, merge, publish, or delete remote GitHub state.

When approval is required, state the exact command or GitHub action, the target repository, branch or ref, and the expected remote change. Proceed only after the user approves that specific action.

If a hook blocks the action, request approval and rerun through Codex's approved command path. Do not work around the hook by changing remotes, wrapping the command, or using another tool that has the same publishing effect.
