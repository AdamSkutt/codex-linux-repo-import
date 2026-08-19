# Compatibility

## Support matrix

| Environment | Scan and plan | Apply and rollback |
| --- | --- | --- |
| Linux Codex Desktop `26.803.81509` with the expected native Project schema | Supported | Tested adapter path |
| Another build with the expected native Project migration marker | Available | Refused by default; requires `--allow-untested` |
| Missing or invalid native Project schema | Limited diagnostics | Refused |
| macOS or Windows | Not a supported target | Not supported |

“Schema-compatible” is not the same as tested. A private Desktop-state field can change meaning without changing its basic JSON type, so the tool never silently promotes another build to tested status.

## Public API boundary

[OpenAI Projects documentation](https://learn.chatgpt.com/docs/projects) describes Projects as workspaces for related chats and context. [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server) and the open-source [App Server protocol reference](https://github.com/openai/codex/blob/f317dc8a17d30d8feb2c79add1d9d565be0402bf/codex-rs/app-server/README.md) expose thread-oriented operations, but currently do not provide a supported operation for assigning existing historical Desktop threads to a native local Project.

Accordingly, this project does not pretend to use a stable public import API. `apply` is a compatibility adapter over the local Linux Desktop state and is guarded as such. If OpenAI adds a supported project-assignment API, that API should replace the private-state adapter.

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

Unknown state is preserved. The tool does not write session JSONL files, the native thread SQLite database, or Chromium storage.

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

`codex-desktop` covers direct Desktop parent conversations and conversations converted into native rollouts by Desktop's external-agent import flow. They share the same exact first-record pair. That record does not retain a reliable provider name, so this tool does not claim that a task came from Claude Code, Cursor, or another provider. It does not ingest raw external-agent files itself.

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
