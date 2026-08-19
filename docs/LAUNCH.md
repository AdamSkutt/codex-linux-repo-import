# Launch kit

This file contains ready-to-edit launch copy. Verify the current release tag, tested Codex Desktop build, screenshots, and installation command before posting. Do not cross-post every community at once; answer early questions where you post first.

## One-line description

Group Codex VS Code and Codex Desktop tasks—including native external-agent imports—into repository-based Projects on Linux, with dry-run, verified backup, and guarded rollback.

## Show HN

**Title**

```text
Show HN: Group Codex Linux conversations into repository Projects
```

**Body**

```text
I built codex-linux-repo-import after my Codex history appeared in the Linux desktop app but stayed flat instead of being grouped by repository.

It reads only the first session_meta record from each native Codex rollout, resolves the recorded cwd to a Git root or workspace, ranks Projects by transparent activity signals, and prepares the native Project assignments. It now handles both Codex VS Code extension conversations and eligible Codex Desktop parent tasks, including conversations already imported from supported external agents.

The default workflow is read-only: doctor, scan, and plan. apply requires an explicit --yes, a fully stopped Codex Desktop process, the exact tested Desktop build (or an explicit untested override), a native catalog check, compare-and-swap validation, and a verified backup. Rollback is conflict-aware.

It does not read message bodies, rewrite session JSONL, write the thread SQLite database, or convert raw Claude/Cursor/Gemini files itself.

Linux, Python 3.11+, MIT:
https://github.com/AdamSkutt/codex-linux-repo-import

I would especially value compatibility reports using synthetic/redacted evidence—never real session or state files.
```

## Reddit

Suggested communities must be checked for current self-promotion rules before posting. Adapt the title and lead instead of duplicating the same post verbatim.

**Title**

```text
I made a local-first tool to group Codex Linux tasks by repository
```

**Body**

```text
My Codex VS Code and Desktop tasks were present in Codex on Linux, but the sidebar did not group the historical tasks into repository Projects.

I made an independent Python tool that reads only session metadata, resolves each cwd to its Git/workspace root, shows a dry-run plan, then writes only the allowlisted native Project-assignment state after explicit confirmation. It creates a verified backup and supports guarded rollback.

The new release also recognizes Codex Desktop parent tasks, including native external-agent imports. It reports the common provenance as `codex-desktop` and does not guess Claude vs Cursor.

Repo and demo: https://github.com/AdamSkutt/codex-linux-repo-import

Important limitation: the private-state apply adapter is tested only with the exact Linux Codex Desktop build listed in the README. Scan and plan remain read-only.
```

## GitHub Discussion or release announcement

**Title**

```text
v0.3.0: Group Codex Desktop tasks alongside VS Code conversations
```

**Body**

```markdown
v0.3.0 expands repository grouping beyond Codex VS Code extension conversations.

Highlights:

- recognizes direct Codex Desktop tasks and native import rollouts with exact metadata matching;
- reports `vscode-extension` and `codex-desktop` provenance without guessing providers;
- keeps structured subagent tasks excluded;
- adds provenance counts to scan, plan, and doctor output;
- adds a pipx install path, a before/after demo, and explicit architecture/privacy documentation.

The safety boundary is unchanged: message bodies are not read, session files and the thread database are not written, and native Project assignment remains dry-run-first with backup and rollback.
```

## Launch checklist

- [ ] Merge the release PR after all CI jobs pass.
- [ ] Tag and publish the reviewed version.
- [ ] Confirm `pipx install git+https://github.com/AdamSkutt/codex-linux-repo-import.git` from a clean environment.
- [ ] Set `assets/codex-linux-repo-import-hero.png` as the GitHub social preview.
- [ ] Confirm the README demo renders on GitHub desktop and mobile widths.
- [ ] Update the repository description and topics to mention Codex Desktop tasks and native imports.
- [ ] Post to one community first and remain available for questions.
- [ ] Never request or accept unredacted session, state, plan, or backup files in public issues.
