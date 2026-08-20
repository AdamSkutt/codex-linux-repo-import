# Compatibility

The tool has two independent compatibility surfaces: Claude Code conversion through an experimental Codex App Server API, and Linux Desktop Project assignment through a private-state adapter.

## Claude import support matrix

| Environment | Plan | Import |
| --- | --- | --- |
| Linux, Python 3.11+, Codex CLI `0.133.0` | Tested | Tested experimental protocol |
| Another Codex CLI exposing `externalAgentConfig/import` | Available | Untested; App Server may reject a changed protocol |
| Codex CLI without the external-migration capability | Available | Refused by Codex startup/protocol validation |
| macOS or Windows | Not a supported target | Not supported |

`claude plan` parses only the Claude source layout and Codex import history, so it can still report candidates when conversion compatibility is unavailable. `claude import` uses `--enable external_migration app-server --listen stdio://`, initializes experimental API capability, and submits one `SESSIONS` item at a time. Successful completion must also produce a verifiable Codex import-ledger entry.

The current App Server detector defaults to sessions from the last 30 days and at most 50 sessions. This tool enumerates all direct Claude Code JSONL sessions itself and uses the App Server only for conversion, so those detector defaults do not truncate its plan. Source files remain read-only. App Server also validates imported paths against the active user's `~/.claude/projects`; alternate `--claude-projects` roots are therefore plan-only and never implemented by shadowing the process home.

## Project-grouping support matrix

| Environment | Scan and plan | Apply and rollback |
| --- | --- | --- |
| Linux Codex Desktop `26.803.81509` with the expected native Project schema | Supported | Tested adapter path |
| Another build with the expected native Project migration marker | Available | Refused by default; requires `--allow-untested` |
| Missing or invalid native Project schema | Limited diagnostics | Refused |
| macOS or Windows | Not a supported target | Not supported |

“Schema-compatible” is not the same as tested. A private Desktop-state field can change meaning without changing its basic JSON type, so the tool never silently promotes another build to tested status.

## Public API boundary

[OpenAI Projects documentation](https://learn.chatgpt.com/docs/projects) describes Projects as workspaces for related chats and context. The open-source [App Server protocol reference](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) exposes the experimental external-agent detection/import methods used for Claude conversion, but it does not provide an operation for assigning existing historical Desktop threads to a native local Project.

Accordingly, `claude import` is explicitly versioned as an experimental-protocol integration, while the separate `apply` command is a compatibility adapter over the local Linux Desktop state. If OpenAI adds a supported Project-assignment API, that API should replace the private-state adapter.

## Native state contract

The tested adapter validates the global state object and updates only these logical areas:

- local Project records and Project order;
- thread-to-Project assignments;
- the set of projectless thread IDs;
- per-Project thread order;
- the native Project sort preference and unified Project order when weighted sorting is enabled.

The current key names are:

```text
local-projects
project-order
thread-project-assignments
projectless-thread-ids
sidebar-project-thread-orders
electron-persisted-atom-state.flat-project-sidebar-preferences-v1
electron-persisted-atom-state.unified-sidebar-project-order-v1
```

Unknown state is preserved. The Project `apply` command does not write session JSONL files, the native thread SQLite database, or Chromium storage. `claude import` has a separate contract: Codex App Server creates native rollouts and updates its own catalog/import ledger while the source Claude JSONL remains unchanged.

When `--unlinked-project-root PATH` is supplied, apply may also create one private dedicated local directory outside native state. The backup manifest records the reviewed creation intent, but the directory is never treated as deletable importer-owned state and does not broaden the native-state key allowlist above.

Compatibility also requires the native local-Projects migration marker expected by the tested Desktop build. Missing required keys are initialized only where the tested schema permits it; wrong types and malformed Project records are rejected.

## Version detection and gates

On Debian-family installations, the tool asks the package database for the installed `chatgpt` version. `CODEX_DESKTOP_VERSION` can provide an explicit version string in unusual packaging environments, but doing so does not make an untested build tested.

Write commands enforce three distinct results:

- **tested** — version is exactly `26.803.81509` and state validation passes;
- **schema-compatible, untested** — the expected migration is present but the version is not the exact tested build; an explicit `--allow-untested` is required;
- **incompatible** — validation or the required schema marker fails; writing is refused.

`--allow-untested` never bypasses an incompatible result.

## Session provenance

Only parent sessions whose first metadata record matches one of these exact pairs are selected:

| Reported provenance | `originator` | `source` |
| --- | --- | --- |
| `vscode-extension` | `codex_vscode` | `vscode` |
| `codex-desktop` | `Codex Desktop` | `vscode` |

`codex-desktop` covers direct Desktop parent conversations and conversations converted into native rollouts by an external-agent import flow, including this tool's Claude conversion. They share the same exact first-record pair. That record does not retain a reliable provider name, so the later grouping scan does not claim that a native task came from Claude Code, Cursor, or another provider. Raw ingestion is a separate, explicitly selected Claude Code operation.

Subagent sessions use a structured source object and remain excluded. CLI, exec, ordinary Desktop, and every other originator/source pair are also excluded. Exact matching prevents forks or unrelated activity from inflating a Project's rank. See [ADR-001](ADR-001-SESSION-PROVENANCE.md).

Active and archived session trees are deduplicated by thread ID, with the active record winning. Archived sessions are reported but excluded from assignment and scoring by default.

## Unlinked fallback contract

The fallback is opt-in and available only to `plan` and `apply` through:

```text
--unlinked-project-root PATH
```

Omitting the flag preserves the existing behavior: ineligible groups are reported but untouched. Supplying it creates or reuses at most one local Project with the fixed native name `Unlinked Codex Chats` and the canonical target path as its single `rootPaths` entry.

The fallback candidate set contains only eligible `vscode-extension` or `codex-desktop` sessions whose post-mapping root classification is `missing` or `ambiguous`. Eligible Git/workspace groups and `unresolved` roots remain separate and are never folded into the fallback. Archived fallback candidates are excluded by default and included only when the existing global `--include-archived` option is supplied.

Native catalog membership is mandatory. A catalog-missing candidate receives no assignment, per-Project thread-order entry, or projectless-state mutation. An existing assignment is always preserved for fallback candidates; `--reassign` affects normal target Projects but cannot move a thread into `Unlinked Codex Chats`. The fallback Project is not created when every candidate is excluded as archived, catalog-missing, or already assigned elsewhere.

The fallback does not participate in repository/workspace scoring. A newly created fallback is appended after weighted Projects; a reused fallback keeps its existing position. Its chats are deterministically ordered newest-first with thread ID as the tie-break, and a repeated identical plan is idempotent. Existing fallback/native ordering and unknown fields remain preserved under the same compatibility rules as normal Projects.

`PATH` must be an absolute, canonicalizable, dedicated direct child of the current user's real, existing `~/Desktop`. Validation rejects path, symlink, repository, conflicting native-Project, source-root, protected Codex-data, and ancestor/descendant collisions. `plan` performs no directory mutation. `apply` creates the leaf with mode `0700` only after its write guards pass, then verifies its identity and emptiness before and after committing native state. Failures restore native state but preserve the directory. Rollback likewise never deletes it; an interruption-created empty directory is reusable by a fresh plan.

## Root resolution

Workspace paths are resolved locally. Git repositories, worktrees, and submodules are grouped by their enclosing local Git root. A non-Git workspace uses its exact canonical directory.

The tool intentionally does not:

- clone a missing repository;
- infer a moved path without `--map OLD=NEW`;
- combine different clones by remote URL;
- create Projects from broad roots such as the filesystem root, home directory, Desktop, Documents, or Downloads.

These cases remain visible in the plan as ineligible rather than being silently guessed.

With an explicit `--unlinked-project-root`, only the `missing` and `ambiguous` cases may instead be collected under the bounded fallback contract above. The option does not infer where a moved repository went and does not turn the fallback directory into repository authority.

## Adding compatibility for another Desktop build

Another Desktop build should be marked tested only after all of the following use synthetic, non-personal fixtures:

1. State schema and migration markers are compared with the currently tested build.
2. Project creation, reuse, assignment, conflict, ordering, and projectless removal are exercised.
3. Main-state and `.bak` parity is verified.
4. App-running, compare-and-swap, malformed-state, and interrupted-write guards are tested.
5. Exact rollback and later-state conflict cases pass.
6. A clean Desktop launch renders the expected native Projects and threads.
7. Unlinked selection, target collision checks, catalog enforcement, assignment preservation, unranked ordering, non-destructive apply failure handling, and rollback directory preservation pass.

Do not submit captured native state or session data as a fixture.
