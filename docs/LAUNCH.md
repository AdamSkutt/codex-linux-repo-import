# Launch kit

This file contains ready-to-edit launch copy. Verify the current release tag, tested Codex Desktop build, screenshots, and installation command before posting. Do not cross-post every community at once; answer early questions where you post first.

## One-line description

Import Claude Code history into Codex, then group Codex VS Code/Desktop tasks into repository Projects on Linux—with dry-runs, deduplication, backups, and guarded writes.

## Show HN

**Title**

```text
Show HN: Import Claude Code history into Codex and group tasks by repo on Linux
```

**Body**

```text
I built codex-linux-repo-import after my Codex history appeared in the Linux desktop app but stayed flat instead of being grouped by repository. v0.4 also transfers raw Claude Code session history into native Codex tasks.

The Claude flow discovers all direct ~/.claude/projects session files, dry-runs first, skips exact prior imports, preserves source files, takes a private Codex recovery snapshot, and delegates conversion to Codex App Server's own experimental migration engine. It does not inherit Codex's current 30-day/50-session detector defaults.

The separate grouping flow reads only the first session_meta record from each native Codex rollout, resolves the recorded cwd to a Git root or workspace, ranks Projects by transparent activity signals, and prepares native Project assignments. It handles both Codex VS Code extension conversations and eligible Codex Desktop parent tasks.

The default workflow is read-only. Both mutation paths require --yes and a fully stopped Codex Desktop. Claude import rechecks source hashes and verifies Codex's import ledger; Project apply requires the exact tested Desktop build (or an explicit untested override), a native catalog check, compare-and-swap validation, and a verified backup. Project rollback is conflict-aware.

Claude conversion necessarily reads the selected messages, but never emits them in plans or modifies the source. The native Project scanner still reads metadata only. Cursor, Gemini, Claude Cowork/Desktop, and arbitrary exports are not supported.

Linux, Python 3.11+, MIT:
https://github.com/AdamSkutt/codex-linux-repo-import

I would especially value compatibility reports using synthetic/redacted evidence—never real session or state files.
```

## Reddit

Suggested communities must be checked for current self-promotion rules before posting. Adapt the title and lead instead of duplicating the same post verbatim.

**Title**

```text
I made a local-first Claude Code → Codex importer and repo grouper for Linux
```

**Body**

```text
My Claude Code history and Codex VS Code/Desktop tasks were split across tools, and the Codex Linux sidebar did not group historical tasks into repository Projects.

I made an independent, dependency-free Python tool with two bounded flows. `claude plan/import` discovers all direct Claude Code sessions, deduplicates them, leaves source JSONL untouched, takes a recovery snapshot, and uses Codex's own experimental conversion engine. `plan/apply` reads only native session metadata, resolves each cwd to its Git/workspace root, and writes allowlisted Project-assignment state after a dry-run.

Both write paths refuse to run while Codex Desktop is open. The grouping adapter is version-gated, backed up, and supports guarded rollback. Claude conversion is tested with Codex CLI 0.133.0; Project assignment is tested only with the exact Desktop build listed in the README.

Repo and demo: https://github.com/AdamSkutt/codex-linux-repo-import

Important limitation: the private-state apply adapter is tested only with the exact Linux Codex Desktop build listed in the README. Scan and plan remain read-only.
```

## GitHub Discussion or release announcement

**Title**

```text
v0.4.0: Import Claude Code history into Codex, then group it by repository
```

**Body**

```markdown
v0.4.0 adds direct Claude Code history migration while preserving the existing repository-grouping workflow.

Highlights:

- discovers all direct Claude Code sessions rather than only a recent detector window;
- dry-runs first and deduplicates exact and legacy prior imports;
- keeps Claude source JSONL byte-for-byte unchanged;
- delegates conversion to Codex App Server and verifies its completion ledger;
- creates a private recovery snapshot and blocks unsafe/oversized sources;
- keeps native Project grouping as a separate metadata-only flow.

Claude conversion necessarily reads selected message content to create native tasks, but content is never emitted in plans or manifests. Both mutation paths require a fully stopped Codex Desktop and explicit `--yes`.
```

## Launch checklist

- [ ] Merge the release PR after all CI jobs pass.
- [ ] Tag and publish the reviewed version.
- [ ] Confirm `pipx install git+https://github.com/AdamSkutt/codex-linux-repo-import.git` from a clean environment.
- [ ] Set `assets/codex-linux-repo-import-hero.png` as the GitHub social preview.
- [ ] Confirm the README demo renders on GitHub desktop and mobile widths.
- [ ] Update the repository description and topics to mention Claude Code import, Codex Linux, chat migration, and repository Projects.
- [ ] Post to one community first and remain available for questions.
- [ ] Never request or accept unredacted session, state, plan, or backup files in public issues.
