# Security policy

## Supported scope

Security fixes are applied to the latest release line. Native state writes are supported only for the Desktop builds explicitly listed in [Compatibility](docs/COMPATIBILITY.md); at present, the only tested build is Linux Codex Desktop `26.803.81509`.

## Reporting a vulnerability

Prefer GitHub's private **Report a vulnerability** flow on this repository's Security tab when it is available. If private reporting is unavailable, open a minimal issue asking for a private contact channel without publishing exploit details or personal data.

Include:

- the importer and Codex Desktop versions;
- the affected command and safety guard;
- minimal reproduction steps using synthetic data;
- the expected and observed result.

Never attach real session JSONL files, native state files, plans, backups, thread IDs, usernames, secrets, or full local paths.

Ordinary bugs that do not disclose sensitive information can be filed in the public issue tracker.
