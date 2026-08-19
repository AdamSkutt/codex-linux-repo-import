# Changelog

All notable changes to this project are documented here.

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
