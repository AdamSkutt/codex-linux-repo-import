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

Compatibility also requires the native local-Projects migration marker expected by the tested Desktop build. Missing required keys are initialized only where the tested schema permits it; wrong types and malformed Project records are rejected.

## Version detection and gates

On Debian-family installations, the tool asks the package database for the installed `chatgpt` version. `CODEX_DESKTOP_VERSION` can provide an explicit version string in unusual packaging environments, but doing so does not make an untested build tested.

Write commands enforce three distinct results:

- **tested** — version is exactly `26.803.81509` and state validation passes;
- **schema-compatible, untested** — the expected migration is present but the version is not the exact tested build; an explicit `--allow-untested` is required;
- **incompatible** — validation or the required schema marker fails; writing is refused.

`--allow-untested` never bypasses an incompatible result.

## Session provenance

Only sessions whose first metadata record exactly identifies both the Codex VS Code originator and VS Code source are imported. Desktop-originated chats and subagent sessions are excluded even if another metadata field mentions VS Code. Exact matching prevents agent forks or unrelated Desktop activity from inflating a project's rank.

Active and archived session trees are deduplicated by thread ID, with the active record winning. Archived sessions are reported but excluded from assignment and scoring by default.

## Root resolution

Workspace paths are resolved locally. Git repositories, worktrees, and submodules are grouped by their enclosing local Git root. A non-Git workspace uses its exact canonical directory.

The tool intentionally does not:

- clone a missing repository;
- infer a moved path without `--map OLD=NEW`;
- combine different clones by remote URL;
- create Projects from broad roots such as the filesystem root, home directory, Desktop, Documents, or Downloads.

These cases remain visible in the plan as ineligible rather than being silently guessed.

## Adding compatibility for another Desktop build

Another Desktop build should be marked tested only after all of the following use synthetic, non-personal fixtures:

1. State schema and migration markers are compared with the currently tested build.
2. Project creation, reuse, assignment, conflict, ordering, and projectless removal are exercised.
3. Main-state and `.bak` parity is verified.
4. App-running, compare-and-swap, malformed-state, and interrupted-write guards are tested.
5. Exact rollback and later-state conflict cases pass.
6. A clean Desktop launch renders the expected native Projects and threads.

Do not submit captured native state or session data as a fixture.
