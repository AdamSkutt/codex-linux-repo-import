# Safety and recovery

The default workflow is read-only. `doctor`, `scan`, and `plan` do not modify Codex sessions or Desktop state. Only `apply --yes` and `rollback ... --yes` write native Desktop state.

Native Project assignment has no public API today, so every write deserves the same care as a local data migration.

## Before applying

1. Run `./codex-linux-repo-import doctor` and resolve every incompatible result.
2. Run `./codex-linux-repo-import plan` and inspect the project roots, assignment conflicts, and summary.
3. Use `--map OLD=NEW` for a workspace that moved. Missing paths and overly broad roots are intentionally ineligible.
4. If you opt into the unlinked fallback, inspect its target path and selected chat counts. The target must be a dedicated leaf, not a directory containing existing work or personal files.
5. Fully quit Codex Desktop. Closing only the visible window may leave a process running.
6. Run `apply` with the same mapping and behavioral options used for the reviewed plan.

Example:

```bash
./codex-linux-repo-import plan --map /old/project=/new/project

# Fully quit Codex Desktop before continuing.
./codex-linux-repo-import apply \
  --map /old/project=/new/project \
  --yes
```

Do not run multiple importer instances concurrently, edit native state manually during an apply, or start Codex Desktop until the command finishes.

## Optional unlinked fallback

`--unlinked-project-root PATH` is an explicit `plan`/`apply` opt-in for chats that cannot be assigned to a trustworthy repository or workspace root. It creates at most one native Project named `Unlinked Codex Chats`.

Example:

```bash
./codex-linux-repo-import plan \
  --unlinked-project-root "/home/alice/Desktop/Unlinked Codex Chats"

# Fully quit Codex Desktop before continuing.
./codex-linux-repo-import apply \
  --unlinked-project-root "/home/alice/Desktop/Unlinked Codex Chats" \
  --yes
```

Selection is intentionally narrow:

- active eligible sessions from `missing` and `ambiguous` roots are merged;
- eligible repository/workspace sessions and `unresolved` paths are excluded;
- archived sessions are excluded unless `--include-archived` is also supplied;
- the native thread catalog is required, and catalog-missing threads are not written;
- any existing assignment is preserved, including when `--reassign` is supplied;
- if no session remains assignable, no fallback Project or directory is created.

The target path must be absolute and represent exactly one direct child of the current user's real, existing `~/Desktop`; the leaf may be missing or may already be a safe dedicated directory. The home, Desktop, parent, and any existing target must be current-user owned and not group/world writable. The target must not be a file, symlink, populated unrelated directory, repository, conflicting native Project root, scanned source root, or an ancestor/descendant of protected Codex data and session locations. Use a private, dedicated path such as `/home/alice/Desktop/Unlinked Codex Chats`. Plan output contains exact local paths and remains private.

`plan` validates and reports the intended directory operation but does not create the target. During `apply`, after the normal offline/version/catalog/state guards pass, the importer creates only that leaf with private `0700` permissions. It verifies the directory identity and expected emptiness immediately before and after the native-state commit. If that check or the native write fails, native state is restored and the directory is deliberately preserved.

The importer never deletes the fallback directory or anything inside it. This non-destructive rule also applies to rollback and interrupted applies. If an interruption leaves a newly created empty directory behind, generate a fresh plan; the next apply can safely reuse it. Delete it manually only after confirming the directory is empty and no native Project still refers to it.

## What the scanner reads

The scanner opens candidate session JSONL files and reads only their first line, with a size limit. A file is eligible only when the first record is `session_meta` and its provenance exactly identifies either a Codex VS Code extension parent session or a Codex Desktop parent session. The Desktop class can include direct tasks and native external-agent imports; provider identity is not inferred.

It does not read subsequent JSONL records, including prompts, model responses, commands, tool output, or patches. It never changes session files.

The metadata used for grouping can include:

- thread ID;
- start timestamp;
- working directory;
- originator and source;
- fork parent ID, when present.

## What apply writes

Apply changes only the importer-owned portions of the native global state required for local Projects, thread assignments, projectless-thread membership, and sidebar ordering. Unknown top-level state is preserved.

The write path includes:

- a guard that refuses to proceed while Codex Desktop is running;
- state-schema validation and a tested-version gate;
- an exclusive importer lock;
- a compare-and-swap hash check, which catches changes made after planning;
- a pre-apply backup of the main native state and its `.bak` companion when present;
- temporary-file writes, file and directory sync, then atomic rename;
- identical main and native `.bak` output;
- post-write parsing and hash verification;
- automatic restoration from the pre-apply backup if the write fails.

Session JSONL files and the Codex thread database are not written.

With `--unlinked-project-root`, apply may additionally create the one explicitly named private fallback leaf directory described above. It does not create missing parent directories, place user content in the fallback directory, or delete that directory later.

## Backups

List available backups with:

```bash
./codex-linux-repo-import backups
```

Each pre-apply backup contains:

- the original native state;
- the original native `.bak`, if one existed;
- a manifest with hashes and compatibility information;
- a mutation journal containing before/after values for only the state paths changed by the importer.
- the reviewed fallback-directory creation intent, when the plan included one.

Backups contain native state. Plain-text plans show local project paths, while `plan --json` also contains exact native thread/Project identifiers. Keep these artifacts private, do not commit them, and redact paths and identifiers from issue reports. If you redirect JSON to a file, create it under a private directory and use restrictive permissions such as `umask 077`.

## Rollback

Fully quit Codex Desktop, copy the desired backup ID from `backups`, and run:

```bash
./codex-linux-repo-import rollback BACKUP_ID --yes
```

Rollback uses the mutation journal to restore only paths changed by the selected import. Before restoring, it checks that each path still has the value written by that import. If Codex or another tool changed one of those paths later, rollback stops and reports a conflict instead of overwriting newer state.

Rollback never removes an unlinked fallback directory, whether it pre-existed or was created by the importer. It restores only the native state keys covered by the mutation journal. Inspect and remove an unused empty directory manually if desired.

A pre-rollback safety backup is created before restoration. The restored result is written atomically to both native state files and verified.

## High-risk overrides

These options weaken conservative defaults and should be used only after reviewing a fresh plan:

- `--reassign` replaces an existing thread-to-project assignment instead of reporting a conflict.
- `--include-archived` also assigns archived eligible conversations. Archived conversations still do not influence ranking.
- `--allow-untested` permits a schema-compatible but untested Desktop build. It does not bypass an incompatible schema result.

`--reassign` never steals an existing assignment into `Unlinked Codex Chats`; that fallback-specific protection cannot be overridden.

The tested adapter scope is exactly Linux Codex Desktop `26.803.81509`. Other builds—including other `26.803.*` builds—are untested and require `--allow-untested` when their schema is otherwise compatible. Upgrading Codex may change its private local state. Re-run `doctor` after every Desktop upgrade and do not assume a previous override remains safe.

## If something looks wrong

1. Do not repeatedly apply.
2. Fully quit Codex Desktop.
3. Run `backups` and preserve the most recent pre-apply backup.
4. Run rollback only if its selected state and scope are understood.
5. When reporting a bug, include versions and redacted diagnostics—not session files, state files, plans, backups, usernames, or full paths.
