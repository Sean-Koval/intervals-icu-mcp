"""Tempo coach extensions.

Custom tools added for the Tempo local-first coaching agent
(https://github.com/Sean-Koval/tempo-coach — private). Kept in a separate
module so that pulling upstream eddmann/intervals-icu-mcp stays trivial.

Two tools:

1. ``get_week_summary`` — collects planned events, actual activities, and
   wellness entries for a 7-day window in one call. Shape tuned for the
   Tempo ``plan-training-week`` skill's ``preflight.py`` brief.

2. ``bulk_upsert_tagged_events`` — writes a batch of planned events all tagged
   with a ``plan_id`` (via ``external_id`` = ``"<plan_id>/<session_id>"``) so
   they can be attributed and re-synced round-trip. Upserts by matching
   ``external_id`` against events already on the calendar in the relevant
   date range.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..response_builder import ResponseBuilder

TEMPO_DESC_TAG = "[tempo]"


def _parse_date(s: str, field: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"{field} must be YYYY-MM-DD (got {s!r})") from e


async def get_week_summary(
    week_start_date: Annotated[
        str,
        "Monday of the target week (YYYY-MM-DD). Window runs start..start+6d inclusive.",
    ],
    athlete_id: Annotated[
        str | None,
        "Athlete ID override; defaults to configured INTERVALS_ICU_ATHLETE_ID.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Assemble a single planned+actual+wellness snapshot for one training week.

    Intended as the one-call data pull for Tempo's weekly planning preflight.
    Returns calendar events (planned workouts/notes/races), completed activities,
    and wellness rows for the 7-day window starting at ``week_start_date``.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        start = _parse_date(week_start_date, "week_start_date")
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    end = start + timedelta(days=6)
    oldest = start.isoformat()
    newest = end.isoformat()

    try:
        async with ICUClient(config) as client:
            events = await client.get_events(athlete_id=athlete_id, oldest=oldest, newest=newest)
            activities = await client.get_activities(
                athlete_id=athlete_id, oldest=oldest, newest=newest
            )
            wellness = await client.get_wellness(
                athlete_id=athlete_id, oldest=oldest, newest=newest
            )

        planned: list[dict[str, Any]] = [
            {
                "id": ev.id,
                "date": ev.start_date_local,
                "category": ev.category,
                "name": ev.name,
                "type": ev.type,
                "duration_seconds": ev.moving_time,
                "distance_meters": ev.distance,
                "planned_load": ev.icu_training_load,
                "description": ev.description,
                "external_id": ev.external_id,
            }
            for ev in events
        ]

        actual: list[dict[str, Any]] = [
            {
                "id": act.id,
                "start_date": str(act.start_date_local) if act.start_date_local else None,
                "type": act.type,
                "name": act.name,
                "duration_seconds": act.moving_time,
                "distance_meters": act.distance,
                "load": act.icu_training_load,
                "intensity": act.icu_intensity,
                "np": act.normalized_power,
                "avg_hr": act.average_heartrate,
                "avg_watts": act.average_watts,
                "elevation_gain_m": act.total_elevation_gain,
            }
            for act in activities
        ]

        wellness_rows: list[dict[str, Any]] = [
            {
                "date": w.id,
                "hrv": w.hrv,
                "resting_hr": w.resting_hr,
                "sleep_secs": w.sleep_secs,
                "sleep_score": w.sleep_score,
                "readiness": w.readiness,
                "body_weight_kg": w.weight,
                "soreness": w.soreness,
                "fatigue": w.fatigue,
                "stress": w.stress,
                "comments": w.comments,
                "ctl": w.ctl,
                "atl": w.atl,
                "tsb": w.tsb,
            }
            for w in wellness
        ]

        # Light-touch derivations so consumers don't re-sum basics.
        total_planned_load = sum(
            p["planned_load"] for p in planned if p.get("planned_load") is not None
        )
        total_actual_load = sum(a["load"] for a in actual if a.get("load") is not None)

        analysis = {
            "planned_count": len(planned),
            "actual_count": len(actual),
            "wellness_days": len(wellness_rows),
            "total_planned_load": total_planned_load,
            "total_actual_load": total_actual_load,
            "load_delta": total_actual_load - total_planned_load,
        }

        return ResponseBuilder.build_response(
            data={
                "week_start": oldest,
                "week_end": newest,
                "planned": planned,
                "actual": actual,
                "wellness": wellness_rows,
            },
            analysis=analysis,
            query_type="get_week_summary",
            metadata={"message": f"Week {oldest} .. {newest}"},
        )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e!s}", error_type="internal_error"
        )


def _build_external_id(plan_id: str, session_id: str) -> str:
    return f"{plan_id}/{session_id}"


def _ensure_tempo_description(description: str | None, plan_id: str, session_id: str) -> str:
    prefix = f"{TEMPO_DESC_TAG} plan={plan_id} session={session_id}"
    if description and not description.startswith(TEMPO_DESC_TAG):
        return f"{prefix}\n\n{description}"
    if description and description.startswith(TEMPO_DESC_TAG):
        # Already tagged — keep whatever's there but normalize the first line.
        lines = description.splitlines()
        body = "\n".join(lines[1:]) if len(lines) > 1 else ""
        return f"{prefix}\n\n{body}".rstrip() if body else prefix
    return prefix


async def bulk_upsert_tagged_events(
    events_json: Annotated[
        str,
        (
            "JSON array of session objects. Each object: "
            '{"session_id": "<stable id within plan>", "start_date_local": "YYYY-MM-DD", '
            '"name": "...", "category": "WORKOUT|NOTE|RACE|GOAL", '
            '"type": "Ride|Run|Swim|...", "moving_time": <seconds>, '
            '"distance": <meters>, "icu_training_load": <int>, '
            '"description": "..."} — session_id is required; '
            "everything else is passed through."
        ),
    ],
    plan_id: Annotated[
        str,
        "Tempo plan identifier (e.g. '2026-ironman-lake-placid'). Used for external_id attribution.",
    ],
    athlete_id: Annotated[
        str | None,
        "Athlete ID override; defaults to configured INTERVALS_ICU_ATHLETE_ID.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Upsert a batch of planned events keyed by (plan_id, session_id).

    Behaviour:

    * For each input session, ``external_id = "<plan_id>/<session_id>"`` and the
      description is prefixed with ``[tempo] plan=<id> session=<id>`` so planning
      metadata is human-visible on the intervals.icu calendar too.
    * Existing events in the relevant date range are fetched once; incoming
      sessions that match a prior ``external_id`` are routed through
      ``update_event``, otherwise through ``create_event``.
    * All writes are explicit — this tool never deletes events that aren't in
      the input. Use ``bulk_delete_events`` separately to prune.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        parsed = json.loads(events_json)
    except json.JSONDecodeError as e:
        return ResponseBuilder.build_error_response(
            f"Invalid JSON for events_json: {e!s}", error_type="validation_error"
        )

    if not isinstance(parsed, list) or not parsed:
        return ResponseBuilder.build_error_response(
            "events_json must be a non-empty JSON array.", error_type="validation_error"
        )

    sessions: list[dict[str, Any]] = parsed  # type: ignore[assignment]
    valid_categories = {"WORKOUT", "NOTE", "RACE", "GOAL"}

    normalized: list[dict[str, Any]] = []
    dates: list[date] = []
    for i, s in enumerate(sessions):
        if not isinstance(s, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            return ResponseBuilder.build_error_response(
                f"Session {i}: must be an object.", error_type="validation_error"
            )
        for req in ("session_id", "start_date_local", "name", "category"):
            if req not in s or not s[req]:
                return ResponseBuilder.build_error_response(
                    f"Session {i}: missing required field {req!r}.",
                    error_type="validation_error",
                )

        cat = str(s["category"]).upper()
        if cat not in valid_categories:
            return ResponseBuilder.build_error_response(
                f"Session {i}: category must be one of {sorted(valid_categories)}.",
                error_type="validation_error",
            )

        try:
            d = _parse_date(s["start_date_local"], f"session[{i}].start_date_local")
        except ValueError as e:
            return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

        session_id = str(s["session_id"])
        ext_id = _build_external_id(plan_id, session_id)
        description = _ensure_tempo_description(s.get("description"), plan_id, session_id)

        event_data: dict[str, Any] = {
            "start_date_local": s["start_date_local"],
            "name": s["name"],
            "category": cat,
            "description": description,
            "external_id": ext_id,
        }
        for pass_through in ("type", "moving_time", "distance", "icu_training_load"):
            if s.get(pass_through) is not None:
                event_data[pass_through] = s[pass_through]

        normalized.append({"session_id": session_id, "external_id": ext_id, "data": event_data})
        dates.append(d)

    oldest = min(dates).isoformat()
    newest = max(dates).isoformat()

    try:
        async with ICUClient(config) as client:
            existing = await client.get_events(athlete_id=athlete_id, oldest=oldest, newest=newest)
            by_ext: dict[str, int] = {ev.external_id: ev.id for ev in existing if ev.external_id}

            created: list[dict[str, Any]] = []
            updated: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []

            for n in normalized:
                ext_id = n["external_id"]
                data = n["data"]
                try:
                    if ext_id in by_ext:
                        event = await client.update_event(by_ext[ext_id], data)
                        updated.append(
                            {
                                "id": event.id,
                                "session_id": n["session_id"],
                                "external_id": ext_id,
                                "start_date": event.start_date_local,
                                "name": event.name,
                            }
                        )
                    else:
                        event = await client.create_event(data)
                        created.append(
                            {
                                "id": event.id,
                                "session_id": n["session_id"],
                                "external_id": ext_id,
                                "start_date": event.start_date_local,
                                "name": event.name,
                            }
                        )
                except ICUAPIError as e:
                    errors.append(
                        {"session_id": n["session_id"], "external_id": ext_id, "error": e.message}
                    )

        return ResponseBuilder.build_response(
            data={"created": created, "updated": updated, "errors": errors},
            analysis={
                "created_count": len(created),
                "updated_count": len(updated),
                "error_count": len(errors),
                "window": {"oldest": oldest, "newest": newest},
            },
            query_type="bulk_upsert_tagged_events",
            metadata={
                "plan_id": plan_id,
                "message": (
                    f"plan={plan_id}: {len(created)} created, "
                    f"{len(updated)} updated, {len(errors)} errors"
                ),
            },
        )

    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e!s}", error_type="internal_error"
        )
