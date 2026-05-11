"""Tempo coach extension: create/update/delete library workouts.

Companion to ``coach_extensions.py``. Adds write-side workout-library tools that
the Tempo session-library compiler (epic tempo-d5e) calls to turn structured
session archetypes into intervals.icu workouts.

Three tools:

* ``create_workout`` — compile a step DSL to intervals.icu description form and
  POST a new workout. Tags the workout with ``tempo:ext:<external_id>`` so
  later compilation passes can find it.
* ``update_workout`` — idempotent. Looks up by ``external_id`` (or accepts an
  explicit ``workout_id``), then PUTs the new step list and metadata.
* ``delete_workout`` — by ``external_id`` or ``workout_id``. Returns the API's
  deleted-id array.

See ``COACH_EXTENSIONS.md`` for the DSL reference.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient
from ..response_builder import ResponseBuilder
from .workout_dsl import DSLCompileError, compile_steps_to_description

EXTERNAL_ID_TAG_PREFIX = "tempo:ext:"


def _ext_tag(external_id: str) -> str:
    return f"{EXTERNAL_ID_TAG_PREFIX}{external_id}"


def _find_workout_by_ext_id(workouts: list[Any], external_id: str) -> Any | None:
    """Return the first workout whose ``tags`` contain the tempo:ext:<id> marker."""
    needle = _ext_tag(external_id)
    for w in workouts:
        tags = getattr(w, "tags", None)
        if tags and needle in tags:
            return w
    return None


def _build_workout_body(
    *,
    name: str,
    description: str,
    sport: str,
    folder_id: int | None,
    indoor: bool | None,
    external_id: str | None,
    extra_tags: list[str] | None,
) -> dict[str, Any]:
    tags: list[str] = []
    if external_id:
        tags.append(_ext_tag(external_id))
    if extra_tags:
        for t in extra_tags:
            if t not in tags:
                tags.append(t)

    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "type": sport,
    }
    if folder_id is not None:
        body["folder_id"] = folder_id
    if indoor is not None:
        body["indoor"] = indoor
    if tags:
        body["tags"] = tags
    return body


def _parse_steps(steps_json: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(steps_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON for steps: {e!s}") from e
    if not isinstance(parsed, list):
        raise ValueError("steps must be a JSON array of step objects.")
    return parsed  # type: ignore[no-any-return]


async def create_workout(
    name: Annotated[str, "Workout name (e.g. 'tempo_bike_block_v1')."],
    sport: Annotated[str, "Activity type — 'Ride', 'Run', 'Swim', 'WeightTraining', etc."],
    steps_json: Annotated[
        str,
        (
            "JSON array of step objects. Each step: "
            '{"kind": "warmup|interval|recovery|cooldown|rest", '
            '"duration_s": <int seconds>, '
            '"target_type": "power|hr", '
            '"target_zone": <1..7>  OR  '
            '"target_low_pct": <int>, "target_high_pct": <int>, '
            '"note": "..."}. '
            "Power zones map to Z<n>; %-ranges to %FTP (power) / %LTHR (hr)."
        ),
    ],
    folder_id: Annotated[
        int | None,
        "Library folder ID to drop the workout into. Use get_workout_library to list folders.",
    ] = None,
    external_id: Annotated[
        str | None,
        (
            "Stable Tempo identifier used for round-trip lookup; persisted on the workout "
            "as a tag 'tempo:ext:<external_id>'. Recommended pattern: '<library_ref>:v<n>'."
        ),
    ] = None,
    description_prefix: Annotated[
        str | None,
        "Optional human-readable note prepended above the compiled step lines.",
    ] = None,
    indoor: Annotated[bool | None, "Indoor activity flag."] = None,
    tags: Annotated[
        str | None,
        "Optional JSON array of extra tags to attach in addition to the external_id tag.",
    ] = None,
    athlete_id: Annotated[
        str | None,
        "Athlete ID override; defaults to configured INTERVALS_ICU_ATHLETE_ID.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Create a structured workout in the athlete's intervals.icu library.

    Compiles the structured step list into the description-line DSL that
    intervals.icu parses on the server, then POSTs to
    ``/api/v1/athlete/{id}/workouts``. The workout is tagged with
    ``tempo:ext:<external_id>`` so ``update_workout``/``delete_workout`` can
    locate it later without storing the intervals.icu workout id.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        steps = _parse_steps(steps_json)
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    extra_tag_list: list[str] | None = None
    if tags is not None:
        try:
            extra_tag_list = json.loads(tags)
        except json.JSONDecodeError as e:
            return ResponseBuilder.build_error_response(
                f"Invalid JSON for tags: {e!s}", error_type="validation_error"
            )
        if not isinstance(extra_tag_list, list) or not all(
            isinstance(t, str)  # pyright: ignore[reportUnnecessaryIsInstance]
            for t in extra_tag_list  # pyright: ignore[reportUnknownVariableType]
        ):
            return ResponseBuilder.build_error_response(
                "tags must be a JSON array of strings.", error_type="validation_error"
            )

    try:
        description = compile_steps_to_description(steps, prefix=description_prefix)
    except DSLCompileError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    body = _build_workout_body(
        name=name,
        description=description,
        sport=sport,
        folder_id=folder_id,
        indoor=indoor,
        external_id=external_id,
        extra_tags=extra_tag_list,
    )

    try:
        async with ICUClient(config) as client:
            workout = await client.create_workout(body, athlete_id=athlete_id)

        return ResponseBuilder.build_response(
            data={
                "id": workout.id,
                "name": workout.name,
                "folder_id": workout.folder_id,
                "external_id": external_id,
                "tags": workout.tags,
                "moving_time": workout.moving_time,
                "icu_training_load": workout.icu_training_load,
                "description": workout.description,
            },
            analysis={
                "step_count": len(steps),
                "duration_seconds": workout.moving_time,
                "training_load": workout.icu_training_load,
            },
            query_type="create_workout",
            metadata={
                "message": f"Created workout {workout.id} ({name})",
            },
        )
    except ICUAPIError as e:
        return ResponseBuilder.build_error_response(e.message, error_type="api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e!s}", error_type="internal_error"
        )


async def update_workout(
    steps_json: Annotated[str, "JSON array of step objects (see create_workout for shape)."],
    workout_id: Annotated[
        int | None,
        "Explicit workout id. If omitted, external_id is used for lookup.",
    ] = None,
    external_id: Annotated[
        str | None,
        "Round-trip id used to find a previously-created workout (matches the "
        "'tempo:ext:<external_id>' tag). Required if workout_id is not given.",
    ] = None,
    name: Annotated[str | None, "New name. Falls back to existing name on the workout."] = None,
    sport: Annotated[str | None, "New sport. Falls back to existing type."] = None,
    folder_id: Annotated[int | None, "New folder. Falls back to existing folder_id."] = None,
    description_prefix: Annotated[
        str | None,
        "Optional human-readable note prepended above the compiled step lines.",
    ] = None,
    indoor: Annotated[bool | None, "Indoor flag. Falls back to existing value."] = None,
    tags: Annotated[
        str | None,
        "Optional JSON array of extra tags to set (replaces existing extra tags; "
        "the tempo:ext:<external_id> tag is always preserved).",
    ] = None,
    athlete_id: Annotated[
        str | None,
        "Athlete ID override; defaults to configured INTERVALS_ICU_ATHLETE_ID.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Idempotently update a workout in the athlete's library.

    Either ``workout_id`` or ``external_id`` must be supplied. When using
    ``external_id`` the server-side workout list is fetched and matched on the
    ``tempo:ext:<external_id>`` tag.
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    if workout_id is None and not external_id:
        return ResponseBuilder.build_error_response(
            "Either workout_id or external_id must be provided.",
            error_type="validation_error",
        )

    try:
        steps = _parse_steps(steps_json)
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    extra_tag_list: list[str] | None = None
    if tags is not None:
        try:
            extra_tag_list = json.loads(tags)
        except json.JSONDecodeError as e:
            return ResponseBuilder.build_error_response(
                f"Invalid JSON for tags: {e!s}", error_type="validation_error"
            )
        if not isinstance(extra_tag_list, list) or not all(
            isinstance(t, str)  # pyright: ignore[reportUnnecessaryIsInstance]
            for t in extra_tag_list  # pyright: ignore[reportUnknownVariableType]
        ):
            return ResponseBuilder.build_error_response(
                "tags must be a JSON array of strings.", error_type="validation_error"
            )

    try:
        description = compile_steps_to_description(steps, prefix=description_prefix)
    except DSLCompileError as e:
        return ResponseBuilder.build_error_response(str(e), error_type="validation_error")

    try:
        async with ICUClient(config) as client:
            target_id: int | None = workout_id
            existing_name = name
            existing_sport = sport
            existing_folder = folder_id
            existing_indoor = indoor

            if target_id is None:
                # Resolve via external_id tag.
                assert external_id is not None
                listed = await client.list_workouts(athlete_id=athlete_id)
                match = _find_workout_by_ext_id(listed, external_id)
                if match is None:
                    return ResponseBuilder.build_error_response(
                        f"No workout found with external_id={external_id!r}.",
                        error_type="not_found",
                    )
                target_id = match.id
                existing_name = existing_name or match.name
                existing_sport = existing_sport or match.type
                existing_folder = existing_folder or match.folder_id
                if existing_indoor is None:
                    existing_indoor = match.indoor

            assert target_id is not None  # narrowed by either branch above
            if existing_name is None or existing_sport is None:
                # Fall back to a GET to fill mandatory fields.
                current = await client.get_workout(target_id, athlete_id=athlete_id)
                existing_name = existing_name or current.name
                existing_sport = existing_sport or current.type
                existing_folder = (
                    existing_folder if existing_folder is not None else current.folder_id
                )
                if existing_indoor is None:
                    existing_indoor = current.indoor

            if existing_name is None or existing_sport is None:
                return ResponseBuilder.build_error_response(
                    "Could not determine workout name/sport for update.",
                    error_type="validation_error",
                )

            body = _build_workout_body(
                name=existing_name,
                description=description,
                sport=existing_sport,
                folder_id=existing_folder,
                indoor=existing_indoor,
                external_id=external_id,
                extra_tags=extra_tag_list,
            )
            body["id"] = target_id

            updated = await client.update_workout(target_id, body, athlete_id=athlete_id)

        return ResponseBuilder.build_response(
            data={
                "id": updated.id,
                "name": updated.name,
                "folder_id": updated.folder_id,
                "external_id": external_id,
                "tags": updated.tags,
                "moving_time": updated.moving_time,
                "icu_training_load": updated.icu_training_load,
                "description": updated.description,
            },
            analysis={
                "step_count": len(steps),
                "duration_seconds": updated.moving_time,
                "training_load": updated.icu_training_load,
            },
            query_type="update_workout",
            metadata={
                "message": f"Updated workout {updated.id} ({existing_name})",
            },
        )
    except ICUAPIError as e:
        # 404 surfaces as the canonical "Resource not found" — keep status hint.
        err_type = "not_found" if e.status_code == 404 else "api_error"
        return ResponseBuilder.build_error_response(e.message, error_type=err_type)
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e!s}", error_type="internal_error"
        )


async def delete_workout(
    workout_id: Annotated[
        int | None, "Explicit workout id. If omitted, external_id is used for lookup."
    ] = None,
    external_id: Annotated[
        str | None,
        "Tempo external_id (matches the 'tempo:ext:<external_id>' tag). "
        "Required if workout_id is not given.",
    ] = None,
    athlete_id: Annotated[
        str | None,
        "Athlete ID override; defaults to configured INTERVALS_ICU_ATHLETE_ID.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """Delete a workout from the athlete's library.

    Returns the API's array of deleted IDs (intervals.icu deletes companion
    workouts too if the target was applied to a plan).
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    if workout_id is None and not external_id:
        return ResponseBuilder.build_error_response(
            "Either workout_id or external_id must be provided.",
            error_type="validation_error",
        )

    try:
        async with ICUClient(config) as client:
            target_id = workout_id
            if target_id is None:
                assert external_id is not None
                listed = await client.list_workouts(athlete_id=athlete_id)
                match = _find_workout_by_ext_id(listed, external_id)
                if match is None:
                    return ResponseBuilder.build_error_response(
                        f"No workout found with external_id={external_id!r}.",
                        error_type="not_found",
                    )
                target_id = match.id

            deleted_ids = await client.delete_workout(target_id, athlete_id=athlete_id)

        return ResponseBuilder.build_response(
            data={
                "id": target_id,
                "external_id": external_id,
                "deleted_ids": deleted_ids,
            },
            query_type="delete_workout",
            metadata={
                "message": f"Deleted workout {target_id} (and {len(deleted_ids)} ids total)",
            },
        )
    except ICUAPIError as e:
        err_type = "not_found" if e.status_code == 404 else "api_error"
        return ResponseBuilder.build_error_response(e.message, error_type=err_type)
    except Exception as e:
        return ResponseBuilder.build_error_response(
            f"Unexpected error: {e!s}", error_type="internal_error"
        )
