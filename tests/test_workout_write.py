"""Tests for the create_workout / update_workout / delete_workout MCP tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.workout_write import (
    create_workout,
    delete_workout,
    update_workout,
)


def _basic_steps() -> str:
    return json.dumps(
        [
            {
                "kind": "warmup",
                "duration_s": 900,
                "target_type": "power",
                "target_low_pct": 50,
                "target_high_pct": 65,
            },
            {
                "kind": "interval",
                "duration_s": 900,
                "target_type": "power",
                "target_low_pct": 76,
                "target_high_pct": 88,
            },
            {
                "kind": "cooldown",
                "duration_s": 300,
                "target_type": "power",
                "target_low_pct": 50,
                "target_high_pct": 60,
            },
        ]
    )


def _ctx(mock_config):
    ctx = MagicMock()
    ctx.get_state.return_value = mock_config
    return ctx


class TestCreateWorkout:
    async def test_happy_path(self, mock_config, respx_mock):
        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(
                200,
                json={
                    "id": 42,
                    "name": "tempo_bike_block_v1",
                    "type": "Ride",
                    "folder_id": 9001,
                    "tags": ["tempo:ext:bike_block:v1"],
                    "description": "compiled-by-server",
                    "moving_time": 2100,
                    "icu_training_load": 60,
                    "indoor": True,
                },
            )

        respx_mock.post("/athlete/i123456/workouts").mock(side_effect=handler)

        result = await create_workout(
            name="tempo_bike_block_v1",
            sport="Ride",
            steps_json=_basic_steps(),
            folder_id=9001,
            external_id="bike_block:v1",
            indoor=True,
            ctx=_ctx(mock_config),
        )
        response = json.loads(result)
        assert response["data"]["id"] == 42
        assert response["data"]["external_id"] == "bike_block:v1"
        assert response["analysis"]["step_count"] == 3

        body = captured["body"]
        assert isinstance(body, dict)
        # Description compiled from the step list
        assert body["description"] == "- 15m 50-65%\n- 15m 76-88%\n- 5m 50-60%"
        assert body["type"] == "Ride"
        assert body["folder_id"] == 9001
        assert body["indoor"] is True
        assert "tempo:ext:bike_block:v1" in body["tags"]

    async def test_extra_tags_appended(self, mock_config, respx_mock):
        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(200, json={"id": 1, "name": "n", "type": "Ride"})

        respx_mock.post("/athlete/i123456/workouts").mock(side_effect=handler)

        result = await create_workout(
            name="n",
            sport="Ride",
            steps_json=_basic_steps(),
            external_id="x",
            tags=json.dumps(["color:blue", "phase:base"]),
            ctx=_ctx(mock_config),
        )
        assert json.loads(result)["data"]["id"] == 1
        body = captured["body"]
        assert isinstance(body, dict)
        tags = body["tags"]
        assert tags[0] == "tempo:ext:x"
        assert "color:blue" in tags
        assert "phase:base" in tags

    async def test_invalid_steps_json(self, mock_config):
        result = await create_workout(
            name="n", sport="Ride", steps_json="not json", ctx=_ctx(mock_config)
        )
        resp = json.loads(result)
        assert "error" in resp
        assert "Invalid JSON" in resp["error"]["message"]

    async def test_steps_not_array(self, mock_config):
        result = await create_workout(
            name="n", sport="Ride", steps_json='{"not": "an array"}', ctx=_ctx(mock_config)
        )
        resp = json.loads(result)
        assert "error" in resp
        assert "array" in resp["error"]["message"].lower()

    async def test_malformed_step_carries_index(self, mock_config):
        bad = json.dumps(
            [
                {"kind": "warmup", "duration_s": 300, "target_type": "power", "target_zone": 2},
                {"kind": "interval", "duration_s": 300, "target_type": "power"},
            ]
        )
        result = await create_workout(name="n", sport="Ride", steps_json=bad, ctx=_ctx(mock_config))
        resp = json.loads(result)
        assert "error" in resp
        assert "step[1]" in resp["error"]["message"]

    async def test_missing_required_field_step(self, mock_config):
        bad = json.dumps([{"kind": "interval", "target_type": "power", "target_zone": 4}])
        result = await create_workout(name="n", sport="Ride", steps_json=bad, ctx=_ctx(mock_config))
        resp = json.loads(result)
        assert "error" in resp
        assert "duration_s" in resp["error"]["message"]

    async def test_api_5xx_surfaces_as_error(self, mock_config, respx_mock):
        respx_mock.post("/athlete/i123456/workouts").mock(return_value=Response(500, text="kaboom"))
        result = await create_workout(
            name="n",
            sport="Ride",
            steps_json=_basic_steps(),
            ctx=_ctx(mock_config),
        )
        resp = json.loads(result)
        assert "error" in resp
        assert resp["error"]["type"] == "api_error"


class TestUpdateWorkout:
    async def test_by_workout_id(self, mock_config, respx_mock):
        # GET to resolve missing name/sport (we omit them in the call)
        respx_mock.get("/athlete/i123456/workouts/77").mock(
            return_value=Response(
                200,
                json={
                    "id": 77,
                    "name": "existing-name",
                    "type": "Ride",
                    "folder_id": 5,
                    "indoor": True,
                    "tags": ["tempo:ext:foo"],
                },
            )
        )

        captured: dict[str, object] = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return Response(
                200,
                json={
                    "id": 77,
                    "name": "existing-name",
                    "type": "Ride",
                    "folder_id": 5,
                    "tags": ["tempo:ext:foo"],
                    "moving_time": 2100,
                    "icu_training_load": 60,
                },
            )

        respx_mock.put("/athlete/i123456/workouts/77").mock(side_effect=handler)

        result = await update_workout(
            steps_json=_basic_steps(),
            workout_id=77,
            external_id="foo",
            ctx=_ctx(mock_config),
        )
        resp = json.loads(result)
        assert resp["data"]["id"] == 77
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["id"] == 77
        assert body["name"] == "existing-name"
        assert body["description"] == "- 15m 50-65%\n- 15m 76-88%\n- 5m 50-60%"
        assert "tempo:ext:foo" in body["tags"]

    async def test_by_external_id_lookup(self, mock_config, respx_mock):
        listing = [
            {
                "id": 10,
                "name": "other",
                "type": "Ride",
                "folder_id": 5,
                "tags": ["tempo:ext:other"],
            },
            {
                "id": 11,
                "name": "match",
                "type": "Ride",
                "folder_id": 5,
                "indoor": True,
                "tags": ["tempo:ext:target"],
            },
        ]
        respx_mock.get("/athlete/i123456/workouts").mock(return_value=Response(200, json=listing))
        respx_mock.put("/athlete/i123456/workouts/11").mock(
            return_value=Response(
                200,
                json={
                    "id": 11,
                    "name": "match",
                    "type": "Ride",
                    "folder_id": 5,
                    "tags": ["tempo:ext:target"],
                },
            )
        )

        result = await update_workout(
            steps_json=_basic_steps(),
            external_id="target",
            ctx=_ctx(mock_config),
        )
        resp = json.loads(result)
        assert resp["data"]["id"] == 11

    async def test_external_id_not_found(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/workouts").mock(return_value=Response(200, json=[]))
        result = await update_workout(
            steps_json=_basic_steps(),
            external_id="nope",
            ctx=_ctx(mock_config),
        )
        resp = json.loads(result)
        assert "error" in resp
        assert resp["error"]["type"] == "not_found"

    async def test_missing_id_and_external_id(self, mock_config):
        result = await update_workout(steps_json=_basic_steps(), ctx=_ctx(mock_config))
        resp = json.loads(result)
        assert "error" in resp
        assert "workout_id" in resp["error"]["message"]

    async def test_404_on_explicit_id(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/workouts/999").mock(
            return_value=Response(404, json={"error": "missing"})
        )
        result = await update_workout(
            steps_json=_basic_steps(),
            workout_id=999,
            ctx=_ctx(mock_config),
        )
        resp = json.loads(result)
        assert resp["error"]["type"] == "not_found"


class TestDeleteWorkout:
    async def test_by_workout_id(self, mock_config, respx_mock):
        respx_mock.delete("/athlete/i123456/workouts/42").mock(
            return_value=Response(200, json=[42])
        )
        result = await delete_workout(workout_id=42, ctx=_ctx(mock_config))
        resp = json.loads(result)
        assert resp["data"]["id"] == 42
        assert resp["data"]["deleted_ids"] == [42]

    async def test_by_external_id(self, mock_config, respx_mock):
        listing = [
            {"id": 7, "name": "n", "type": "Ride", "tags": ["tempo:ext:wanted"]},
        ]
        respx_mock.get("/athlete/i123456/workouts").mock(return_value=Response(200, json=listing))
        respx_mock.delete("/athlete/i123456/workouts/7").mock(return_value=Response(200, json=[7]))
        result = await delete_workout(external_id="wanted", ctx=_ctx(mock_config))
        resp = json.loads(result)
        assert resp["data"]["id"] == 7

    async def test_external_id_not_found(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/workouts").mock(return_value=Response(200, json=[]))
        result = await delete_workout(external_id="ghost", ctx=_ctx(mock_config))
        resp = json.loads(result)
        assert resp["error"]["type"] == "not_found"

    async def test_missing_args(self, mock_config):
        result = await delete_workout(ctx=_ctx(mock_config))
        resp = json.loads(result)
        assert "error" in resp
