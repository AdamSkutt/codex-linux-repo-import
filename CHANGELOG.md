# Changelog

All notable changes to this project are documented here.

## [0.4.0] - 2026-08-20

### Added

- `claude plan` and `claude import --yes` for transferring direct Claude Code session history into native Codex tasks through Codex App Server's experimental migration API.
- Complete direct-session enumeration without the App Server detector's default 30-day/50-session window.
- Exact-hash and legacy-history deduplication, repeatable source selection, moved-workspace blocking diagnostics, JSON output, and post-import ledger verification.
- Private pre-import recovery snapshots containing online SQLite backup and the relevant Codex indexes, ledger, state, and prior changed-session rollout when present.
- A dedicated Claude import safety/compatibility guide.

### Security

- Claude sources are current-user-owned, regular, non-symlink, non-group/world-writable files and remain byte-for-byte unchanged.
- Import re-plans under an exclusive lock, verifies source hashes before and after conversion, refuses mutation while Codex Desktop is running, and launches App Server with offline proxy settings.
- Oversized files/records and sessions above the conservative 16,000-message guard are blocked by default.
- Plan output and manifests retain structural metadata only; message content is not emitted.

## [0.3.0] - 2026-08-20

### Added

- Exact `codex-desktop` provenance for direct Desktop conversations and native external-agent imports.
- Provenance counts in scan, plan, and doctor output.
- A one-command `pipx` installation path, before/after demo, architecture decision, and launch kit.

### Changed

- Repository grouping now includes both VS Code extension and eligible Codex Desktop parent conversations by default.
- Legacy v0.2 extension summary fields remain present and now retain extension-only semantics.

### Security

- Provider names are not guessed and no additional conversation or external-import history data is read.
- Subagent, CLI, exec, and ordinary Desktop tasks remain excluded by exact provenance matching.

## [0.2.0] - 2026-08-13

- Added the opt-in `Unlinked Codex Chats` fallback with bounded directory creation and guarded rollback behavior.

## [0.1.0] - 2026-08-11

- Added read-only discovery, repository grouping, deterministic ranking, dry-run planning, verified backup, apply, and rollback.
