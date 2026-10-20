# CRM-neutral snapshot contract, version 1

`snapshot.py` is a working **local JSON importer**, not a native Attio/Folk connector.
Your adapter or export mapping produces the same format regardless of the CRM. The
review engine, assistant proposal validation and recap then run without a CRM API call.

See the complete, fictional [example](../examples/neutral-snapshot.json).

## Envelope

| Field | Meaning |
|---|---|
| `schema_version` | String `"1"` |
| `source` | Name of the adapter/source, no credential |
| `as_of` | Actual capture timestamp in ISO format with timezone, never reset on import |
| `complete` | Must be `true`: the adapter finished collecting its declared selection |
| `expected_deals` | Integer matching the number of collected deals |
| `deals` | All selected open deals, no cap; IDs unique within this snapshot |
| `rules` | Optional positive integers `inactivity_days`, `stage_age_days` |
| `stage_rules` | Optional rule overrides keyed by stage ID |
| `fictional_example` | `true` only for synthetic fixtures |

Completion is the adapter's declaration. The importer checks counts, not the remote
CRM. A connector must verify pagination and scope before setting it. It must not
rename a failed collection as an empty successful export.

## Deal

Required: string `id`, `name`, `stage_id`, `stage_label`, `status: "open"`, `events` list,
and `coverage` (`direct`, `partial` or `verified`). Optional: HTTPS `url`,
`entered_stage_at`, `stage_context`, `next_step: {"text": "…", "due_at": null}`.
Dates must include timezone or be explicitly null. Stage entry cannot be in the future.

`direct` means only direct deal associations. `partial` means known missing sources;
the deal stays visible for review. `verified` means the adapter has checked the
associations it supplies; it does not certify that every conversation has been captured.
Use a snapshot per selected pipeline when stage IDs or rules differ between pipelines.

## Event

Every event needs a stable string `id`, a `kind`, `at` (ISO timestamp or null), and
`association` (`direct`, `verified`, `ambiguous`). `title` and `text` are optional strings.
`verified` requires a nonempty `association_reason` describing the adapter's evidence.
The importer preserves the supplied association; it does not discover links itself.
Ambiguous events are excluded from reasoning and produce a visible warning. Duplicate
IDs of the same kind in one deal are rejected, not counted twice.

| Kind | Extra fields |
|---|---|
| `email` | `direction`: `inbound` or `outbound`; explicit boolean `automated`; `status`: inbound `received`/`unknown`, outbound `sent`/`draft`/`unknown` |
| `meeting` | `status`: `scheduled`, `completed`, `cancelled`, `no_show`, `unknown`; `at` is start time |
| `call` | `status`: `completed`, `missed`, `unknown` |
| `task` | `status`: `open`, `completed`, `unknown`; `at` is due time |
| `note` | `text`: the note or note-taker excerpt already in the CRM |

Unknown or missing chronology produces a warning. The importer never uses a note's
creation date as evidence of a personal conversation. Meeting outcomes must come from
recorded status, not the assumption that a scheduled meeting occurred.

## Run locally

```bash
python3 snapshot.py --input /private/path/snapshot.json --output /private/path/analysis.json
python3 preview_recap.py --input /private/path/analysis.json --output /private/path/recap.json --timezone Europe/Paris
```

For assistant proposals, pass `analysis.json` and `analysis.meta.json` through
`assistant-review.txt` and `review.py`, as described in the quickstart. Evidence references
and snapshot hashes are generated automatically. A source older than 24 hours cannot
receive imported assistant proposals: re-collect it rather than editing its capture time.

## Extension targets

| Route | Delivered status |
|---|---|
| HubSpot API | Read-only connector; direct activities, optional indirect discovery with confirmed attribution; configurable stages. New indirect path tested offline only |
| Neutral JSON | Local import and review implemented; an export mapping is required |
| Attio API | Connector contribution needed; synthetic schema tests are not API validation |
| Folk API | Connector contribution needed; synthetic schema tests are not API validation |
| Any note taker | Its text can enter as a note; no vendor-specific note-taker API connection |
| Slack | Complete payloads and local preview; sending not implemented |
| Notion / Drive / mailbox | Optional extension targets, no live connector in this release |
