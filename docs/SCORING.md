# Project scoring

`codex-linux-repo-import` gives every eligible project a deterministic score from 0 to 100. The score balances how often a project was used with how recently and consistently it was used.

Ranking is based on active Codex VS Code extension chats. Archived chats are discovered and reported but do not contribute to the score, including when `--include-archived` is used to assign them.

## Inputs

For one project:

| Symbol | Meaning |
| --- | --- |
| `N` | Number of active extension chats |
| `D` | Number of distinct local calendar days with an active chat |
| `age` | Days between the newest active chat and the scoring time |
| `span` | Days between the oldest and newest active chats |
| `Wactive` | Number of distinct ISO weeks containing an active chat |
| `Wspan` | Inclusive number of calendar weeks covered by the history |

Calendar days and weeks use the selected timezone. The default is `Europe/Istanbul`; choose another with `--timezone TIMEZONE` on `scan`, `plan`, or `apply`. Future timestamps are clamped to an age of zero.

## Components

Each component is normalized to the range 0 to 1:

```text
frequency  = min(1, log(1 + N) / log(21))
recency    = 2^(-age / 30)
activity   = min(1, log(1 + D) / log(31))
history    = min(1, span / 180)

continuity = 0                                           if N < 2 or span = 0
continuity = sqrt((Wactive / Wspan) * min(1, span / 90)) otherwise
```

The final score is:

```text
score = 100 * (
    0.35 * frequency
  + 0.30 * recency
  + 0.15 * activity
  + 0.10 * history
  + 0.10 * continuity
)
```

| Component | Weight | What it rewards |
| --- | ---: | --- |
| Frequency | 35% | Repeated work without allowing very large histories to dominate linearly |
| Recency | 30% | Current work; the contribution halves every 30 days |
| Active days | 15% | Work spread across distinct days rather than chat bursts |
| History span | 10% | A longer relationship with the project, capped at 180 days |
| Continuity | 10% | Activity distributed across the project's historical week span |

The logarithmic caps keep one very large project from crowding every other project out of view. Recency remains strong enough for a newly active project to move upward.

## Deterministic ordering

Projects are sorted by:

1. score, descending;
2. newest active chat timestamp, descending;
3. active chat count, descending;
4. canonical project path, bytewise ascending.

Imported chats inside a Project are sorted by start time descending, then thread ID ascending. Existing non-imported entries in a native Project remain after the imported entries in their previous relative order.

## Reproducible plans

The score depends on the plan's `--as-of` time and timezone. A JSON plan records the scoring time as `generated_at`, plus the timezone and normalized component values, so the ordering can be audited without reading conversation content. The JSON also contains local paths and native identifiers; keep it private.
