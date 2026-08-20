# Claude Code import

The `claude` command transfers direct Claude Code session history into native Codex tasks. Claude files remain read-only; conversion is delegated to Codex App Server's experimental external-agent migration API.

This is distinct from the repository-grouping flow:

- `claude plan` and `claude import --yes` discover and convert Claude Code history;
- `scan`, `plan`, and `apply --yes` organize eligible native Codex tasks into Linux Desktop Projects.

## Supported source

The default source is:

```text
~/.claude/projects/<project-directory>/*.jsonl
```

Only direct session JSONL files exactly one level below a project directory are considered. Nested subagent transcripts are excluded. Claude Cowork/Desktop, Cursor, Gemini, and arbitrary export formats are not supported.

`--claude-projects PATH` lets `claude plan` inspect an alternate source tree. Codex App Server currently validates imports against the active process user's real `~/.claude/projects`, so `claude import` refuses an alternate root instead of changing `HOME` or silently importing from a different data boundary.

The importer enumerates the direct files itself instead of using Codex's detection result. This deliberately avoids the current Codex detector defaults of 30 days and 50 sessions while retaining Codex's own parser and conversion engine for each selected file.

## Workflow

Preview the complete plan:

```bash
codex-linux-repo-import claude plan
```

Import the same eligible set:

```bash
# Fully quit Codex Desktop first.
codex-linux-repo-import claude import --yes
```

Use `--json` for machine-readable output. The output contains paths and timing metadata but not message bodies.

Select one or more exact source files:

```bash
codex-linux-repo-import claude plan \
  --source /home/alice/.claude/projects/-work-api/session-a.jsonl \
  --source /home/alice/.claude/projects/-work-ui/session-b.jsonl
```

Every `--source` path must be absolute, must resolve inside the active Claude projects root, and must be one of its direct session files.

The workspace path embedded in the Claude JSONL must still exist. Codex's
current Claude parser treats that embedded `cwd` as authoritative; the `cwd`
field supplied with the App Server migration item is not a path override.
Consequently, `claude plan` blocks moved-workspace sessions instead of exposing
a remap option that upstream would ignore. The importer will not rewrite the
Claude source to work around this limitation. After import, the separate native
Project-grouping `plan`/`apply` flow can still use `--map OLD=NEW`.

After conversion, optionally group the new native tasks:

```bash
codex-linux-repo-import plan

# Fully quit Codex Desktop before this separate native-state operation.
codex-linux-repo-import apply --yes
```

## Planning and eligibility

The planner streams each direct JSONL source and retains only structural metadata:

- canonical source path, size, modification time, and SHA-256;
- recorded and canonical working directory;
- count of importable user/assistant records;
- first and last valid message timestamps.

It does not retain titles or message content in its plan model. A source is blocked when it is a symlink, is not a regular current-user-owned file, is group/world writable, exceeds 512 MiB, contains an individual JSONL record above 32 MiB, has no usable workspace/messages/timestamp, or records a workspace directory that no longer exists.

Sessions above 16,000 importable message records are also blocked by default because very large external-agent histories can exceed Codex's current input-item limit. `--allow-large-sessions` bypasses only this count guard; every path, ownership, type, size, and source-integrity guard remains active.

## Deduplication

The planner checks both generations of Codex import history:

- `external_agent_session_imports.json`, using the exact source-content hash and imported thread ID;
- legacy `external_agent_config_imports` rows in `state_5.sqlite`, using the recorded source path and completion time.

An exact matching import is reported as `already_imported`. If the source gained new content, it becomes pending again and the prior imported rollout is included in the recovery snapshot. If an exact registry entry points to a thread missing from Codex's catalog, the source is blocked instead of duplicated.

## Mutation protocol

`claude import --yes` performs these steps:

1. Refuse mutation while Codex Desktop is running.
2. Acquire one private importer lock.
3. Rebuild the plan and require the selected source hashes and target paths to match the reviewed plan.
4. Create a private recovery snapshot.
5. Start the installed Codex App Server with the experimental migration capability and offline proxy settings.
6. Submit one `SESSIONS` migration item at a time.
7. Wait for Codex's completion notification and reject reported failures.
8. Verify that Codex recorded a matching import-ledger entry and native thread ID.
9. Re-hash the Claude source and record the result in the snapshot manifest.

The importer never writes, renames, truncates, or deletes a Claude source file. If a source changes between planning and conversion, that item is not imported. If it changes during conversion, the imported target is reported together with a `source-changed` status so a fresh plan can reconcile it.

The launched App Server receives standard HTTP proxy variables pointing to a closed loopback endpoint. Session conversion is local and does not require a network request.

## Recovery snapshots

Before native Codex mutation, the importer creates a private `0700` snapshot directory under:

```text
~/.codex/backups/codex-linux-repo-import/claude-<timestamp>-<pid>/
```

Files are written as `0600`. Depending on what exists, the snapshot contains:

- an online SQLite backup of `state_5.sqlite`;
- `.codex-global-state.json` and its companion backup;
- `session_index.jsonl`;
- `external_agent_session_imports.json`;
- any prior native rollout associated with a Claude source that changed;
- a manifest containing selected source hashes and per-session outcomes.

List it with `codex-linux-repo-import backups`. The general `rollback` command accepts only Project-assignment backups; it intentionally does not automate restoration of Claude imports. Native conversion can create a rollout and mutate several Codex-owned indexes, so recovery from a Claude snapshot is a manual last-resort procedure that must be performed with Codex fully stopped and with the exact affected artifacts understood. Preserve the snapshot and open a redacted issue before attempting recovery if the native history looks wrong.

## Compatibility and known limits

- The import path is tested with Codex CLI `0.133.0` on Linux.
- The external-agent migration methods are experimental and may change independently of the Desktop Project-state adapter.
- Claude's embedded workspace path is currently authoritative upstream; moved-workspace sessions are blocked rather than rewritten or silently imported under the wrong path.
- Current upstream behavior can assign import-time ordering timestamps instead of preserving the original Claude session time.
- Extremely large or heavily compacted histories may exceed Codex's input-item limit even below the record-count approximation. Import those with a selected `--source` only after preserving the recovery snapshot and understanding the risk.
- A successful import creates a native Codex task; provider identity is not reliably retained in the native rollout's first metadata record. The later grouping scanner therefore reports its generic native provenance as `codex-desktop`.

Run `codex-linux-repo-import claude import --help` for all options. Exit status `0` means every selected pending item succeeded, `2` means a blocked/failed item or invalid request, and `130` means interruption.
