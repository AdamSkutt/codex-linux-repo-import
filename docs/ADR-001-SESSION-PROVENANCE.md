# ADR-001: Classify Codex Desktop parents by exact generic provenance

- **Status:** Accepted
- **Date:** 2026-08-20
- **Deciders:** Project maintainer

## Context

Codex Desktop can create tasks directly and can turn conversations from supported external agents into native Codex rollout files. Both can benefit from repository grouping, but their first `session_meta` record does not preserve a reliable external provider name.

Observed eligible parent-session metadata has two exact forms:

- Codex VS Code extension: `originator=codex_vscode`, `source=vscode`;
- Codex Desktop parent: `originator=Codex Desktop`, `source=vscode`.

The contradiction check found both known external imports and a direct Desktop task using the second pair. It would therefore be incorrect to label every match as an import. Subagent tasks use a structured `source` object. Broader matching on the word `vscode`, originator prefixes, or native catalog fields risks including unrelated tasks and changing Project rankings unexpectedly.

## Decision

Classify only the two exact metadata pairs above. Report them as `vscode-extension` and `codex-desktop`. Include both in scanning, ranking, planning, and apply by default.

Treat `codex-desktop` as a parent-task provenance, not proof of import. Do not infer a provider such as Claude Code or Cursor and do not read conversation bodies or the external-import history table during normal scans. Continue excluding structured subagent sources and every other originator/source pair.

## Options considered

### Option A: Exact generic parent provenance

| Dimension | Assessment |
| --- | --- |
| Complexity | Low |
| Privacy | Reads the same first metadata record only |
| False-positive risk | Low; exact allowlist |
| Provider detail | Generic |

- **Pros:** Preserves the existing privacy boundary, covers direct Desktop tasks and native imports, and avoids unsupported provider claims.
- **Cons:** The UI cannot distinguish direct Desktop work from Claude Code or Cursor imports.

### Option B: Parse `external_agent_config_imports`

| Dimension | Assessment |
| --- | --- |
| Complexity | Medium |
| Privacy | Requires reading JSON blobs containing source paths and titles |
| False-positive risk | Low for recorded imports |
| Provider detail | Specific when history is complete |

- **Pros:** Can expose provider labels.
- **Cons:** Expands the private-data read surface, couples scanning to another private schema, and may miss imported tasks when history is absent or changed.

### Option C: Accept every string source

| Dimension | Assessment |
| --- | --- |
| Complexity | Low |
| Privacy | Unchanged |
| False-positive risk | High |
| Provider detail | None |

- **Pros:** Captures more native tasks.
- **Cons:** Breaks the bounded product promise and may regroup ordinary Desktop, CLI, or exec tasks.

## Trade-off analysis

Provider labels are useful but not required for repository grouping. The core product needs a trustworthy task ID, timestamp, and working directory. Exact generic provenance delivers that value without reading more private state or guessing semantics.

## Consequences

- Direct Desktop conversations and conversations already imported through Codex Desktop can be grouped and ranked.
- Existing v0.2 extension-only JSON fields remain available with extension-only counts; new total and provenance fields describe the broader set.
- Provider-specific filtering is unavailable until Codex exposes reliable provenance in supported metadata.
- Any future provenance pair requires synthetic fixtures, an explicit compatibility decision, and tests.

## Action items

1. [x] Add exact provenance classification and synthetic coverage.
2. [x] Expose provenance counts in scan, plan, and doctor output.
3. [x] Document that this tool organizes native imports but does not ingest raw external-agent files.
