# Tempo coach extensions

This fork (`Sean-Koval/intervals-icu-mcp`, branch `tempo/coach-extensions`) adds tools that the local-first Tempo coaching agent needs but that don't fit upstream eddmann/intervals-icu-mcp's general-purpose surface. They live in two modules so pulling upstream stays trivial:

| Module | Tools |
| --- | --- |
| `src/intervals_icu_mcp/tools/coach_extensions.py` | `get_week_summary`, `bulk_upsert_tagged_events` |
| `src/intervals_icu_mcp/tools/workout_write.py` | `create_workout`, `update_workout`, `delete_workout` |
| `src/intervals_icu_mcp/tools/workout_dsl.py` | step → description DSL compiler (no MCP tool — used by `workout_write`) |

All tools are registered in `server.py`.

## Calendar / wellness

### `get_week_summary(week_start_date, athlete_id?)`
Single-call pull of `events + activities + wellness` for a Monday-anchored 7-day window. Tuned for the Tempo `plan-training-week` preflight brief.

### `bulk_upsert_tagged_events(events_json, plan_id, athlete_id?)`
Batch upsert keyed by `external_id = "<plan_id>/<session_id>"`. Idempotent — re-running `coach push-week` over the same plan converges to the desired state without duplicating events.

## Workout library — write tools

The intervals.icu API exposes `POST/PUT/DELETE /api/v1/athlete/{id}/workouts[/{workoutId}]` (see `openapi-spec.json`, schema `WorkoutEx`). These tools wrap that surface and add a step-DSL compiler so the Tempo session library (`knowledge/methodology/session-library.md`) can emit structured workouts without callers needing to hand-roll the description text intervals.icu expects.

### `create_workout(name, sport, steps_json, folder_id?, external_id?, description_prefix?, indoor?, tags?, athlete_id?)`

Compiles the step list to intervals.icu's plaintext description DSL (see schema below), then `POST /api/v1/athlete/{id}/workouts`. The workout is tagged `tempo:ext:<external_id>` so future calls can find it without storing the intervals.icu numeric id.

Returns:
```json
{
  "data": {
    "id": 42, "name": "...", "folder_id": 9001,
    "external_id": "bike_block:v1",
    "tags": ["tempo:ext:bike_block:v1"],
    "moving_time": 2100, "icu_training_load": 60,
    "description": "<server-echo of description>"
  },
  "analysis": {"step_count": 5, "duration_seconds": 2100, "training_load": 60}
}
```

### `update_workout(steps_json, workout_id?, external_id?, name?, sport?, folder_id?, description_prefix?, indoor?, tags?, athlete_id?)`

Idempotent edit. Provide either `workout_id` or `external_id`; in the latter case the tool lists workouts and matches the `tempo:ext:<external_id>` tag. Missing `name`/`sport` are filled from the existing record.

### `delete_workout(workout_id?, external_id?, athlete_id?)`

Delete by id or external_id. intervals.icu's DELETE returns an `int[]` (the workout plus any companions added when the workout was applied to a plan) — that array is surfaced as `data.deleted_ids`.

## Step DSL

Each step in `steps_json` is a JSON object:

| field | type | required | notes |
| --- | --- | --- | --- |
| `kind` | `"warmup" | "interval" | "recovery" | "cooldown" | "rest"` | yes | metadata only — the intervals.icu parser doesn't care about the label, but Tempo's session-library composer uses it. |
| `duration_s` | positive int | yes | integer seconds. Emitted as `Xm`, `Xs`, `XmYs`, or `Xh` (clean hours only). |
| `target_type` | `"power" | "hr"` | yes | `cadence` and `pace` are accepted by the model but not yet emitted reliably by the description DSL — deferred to v2 (use `workout_doc` directly if needed). |
| `target_zone` | `1..7` | one of these | Power emits `Z<n>`; HR emits `Z<n> HR`. |
| `target_low_pct` | int | one of these | Single value or low bound of range. |
| `target_high_pct` | int | optional with `target_low_pct` | High bound of range. |
| `note` | string | optional | Appended after the target on the same line — surfaces in the rendered workout. |

The DSL compiler emits one line per step (`- <duration> <target>`). Repeat collapse (`5x (... / ...)`) is intentionally not implemented: the live parser truncates at the first close-paren in our probe, so callers should expand repeats at the session-library level. This is a known v1 limitation.

### Example

```python
# Per the agreed call shape:
create_workout(
    name="tempo_bike_block_v1",
    sport="Ride",
    folder_id=12345,
    steps_json=json.dumps([
        {"kind":"warmup",   "duration_s":900, "target_type":"power", "target_low_pct":50, "target_high_pct":65},
        {"kind":"interval", "duration_s":900, "target_type":"power", "target_low_pct":76, "target_high_pct":88},
        {"kind":"recovery", "duration_s":300, "target_type":"power", "target_low_pct":50, "target_high_pct":60},
        {"kind":"interval", "duration_s":900, "target_type":"power", "target_low_pct":76, "target_high_pct":88},
        {"kind":"cooldown", "duration_s":300, "target_type":"power", "target_low_pct":50, "target_high_pct":60},
    ]),
    external_id="tempo:tempo_bike_block:v1",
)
```

Compiles to this description (intervals.icu parses each `- ...` line into a step in `workout_doc`):

```
- 15m 50-65%
- 15m 76-88%
- 5m 50-60%
- 15m 76-88%
- 5m 50-60%
```

### Verified syntax (live probes, May 2026)

| Token | Parsed as |
| --- | --- |
| `- 15m Z2` | `power_zone`, value 2, 900s |
| `- 15m 60% FTP` | `%ftp`, value 60 |
| `- 15m 50-65%` | `%ftp`, range 50..65 |
| `- 10m Z2 HR` | `hr_zone`, value 2 |
| `- 10m 70-80% LTHR` | `%lthr`, range 70..80 |
| `- 45s Z5` | seconds duration, power zone 5 |
| `- 1m30s Z4` | compound mm+ss duration, power zone 4 |
| `- 1h Z2` | hours duration, power zone 2 |

Forms that **do not** parse cleanly (avoid emitting):
- `1.5m` (decimal minutes)
- `1:30` (colon time)
- Bare `bpm` ranges for HR (`130-150bpm`)
- Repeat groups `5x (...)` past the first close paren

### Round-trip tag

`tempo:ext:<external_id>` is the only required tag. Callers may pass additional tags (e.g. `phase:base`, `library:bike_block`) via the optional `tags` argument; they're appended and persist across PUTs (any "extra tags" passed on an update replace the previous extras, but the `tempo:ext:*` tag is always preserved by `_build_workout_body`).

## Endpoints

| Tool | HTTP |
| --- | --- |
| `create_workout` | `POST /api/v1/athlete/{id}/workouts` |
| `update_workout` | `PUT /api/v1/athlete/{id}/workouts/{workoutId}` |
| `delete_workout` | `DELETE /api/v1/athlete/{id}/workouts/{workoutId}` |
| (lookup helper)  | `GET /api/v1/athlete/{id}/workouts` |

Bulk create (`POST /api/v1/athlete/{id}/workouts/bulk`) is out of scope for v1. Folder management (CRUD on folders) is also out of scope — Tempo assumes Sean creates a folder by hand and passes its id.
