"""Tests for Tempo coach extensions."""

import json
from unittest.mock import MagicMock

from httpx import Response

from intervals_icu_mcp.tools.coach_extensions import (
    bulk_upsert_tagged_events,
    get_week_summary,
)


class TestGetWeekSummary:
    """Tests for get_week_summary tool."""

    async def test_happy_path(
        self,
        mock_config,
        respx_mock,
        mock_activity_data,
        mock_wellness_data,
        mock_event_data,
    ):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        respx_mock.get("/athlete/i123456/events").mock(
            return_value=Response(200, json=[mock_event_data])
        )
        respx_mock.get("/athlete/i123456/activities").mock(
            return_value=Response(200, json=[mock_activity_data])
        )
        respx_mock.get("/athlete/i123456/wellness").mock(
            return_value=Response(200, json=[mock_wellness_data])
        )

        result = await get_week_summary(week_start_date="2025-10-13", ctx=mock_ctx)
        response = json.loads(result)

        assert "data" in response
        assert response["data"]["week_start"] == "2025-10-13"
        assert response["data"]["week_end"] == "2025-10-19"
        assert len(response["data"]["planned"]) == 1
        assert len(response["data"]["actual"]) == 1
        assert len(response["data"]["wellness"]) == 1

        analysis = response["analysis"]
        assert analysis["planned_count"] == 1
        assert analysis["actual_count"] == 1
        assert analysis["total_planned_load"] == 100
        assert analysis["total_actual_load"] == 120
        assert analysis["load_delta"] == 20

        # Confirm wellness alias fields are surfaced correctly
        well = response["data"]["wellness"][0]
        assert well["resting_hr"] == 48
        assert well["sleep_secs"] == 28800
        assert well["sleep_score"] == 85.0

    async def test_invalid_date_format(self, mock_config):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        result = await get_week_summary(week_start_date="10/13/2025", ctx=mock_ctx)
        response = json.loads(result)

        assert "error" in response
        assert "YYYY-MM-DD" in response["error"]["message"]


class TestBulkUpsertTaggedEvents:
    """Tests for bulk_upsert_tagged_events tool."""

    @staticmethod
    def _sessions_payload() -> str:
        return json.dumps(
            [
                {
                    "session_id": "w1-mon-swim",
                    "start_date_local": "2026-04-27",
                    "name": "Technique swim",
                    "category": "WORKOUT",
                    "type": "Swim",
                    "moving_time": 2700,
                    "icu_training_load": 35,
                    "description": "4x50 drills",
                },
                {
                    "session_id": "w1-tue-bike",
                    "start_date_local": "2026-04-28",
                    "name": "Threshold block",
                    "category": "WORKOUT",
                    "type": "Ride",
                    "moving_time": 5400,
                    "icu_training_load": 95,
                },
            ]
        )

    async def test_creates_when_nothing_exists(self, mock_config, respx_mock):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        respx_mock.get("/athlete/i123456/events").mock(return_value=Response(200, json=[]))
        respx_mock.post("/athlete/i123456/events").mock(
            side_effect=lambda request: Response(
                200,
                json={
                    "id": hash(request.content) & 0xFFFF,
                    "start_date_local": "2026-04-27",
                    "name": "created",
                    "category": "WORKOUT",
                    "external_id": "p1/x",
                },
            )
        )

        result = await bulk_upsert_tagged_events(
            events_json=self._sessions_payload(), plan_id="p1", ctx=mock_ctx
        )
        response = json.loads(result)

        assert response["analysis"]["created_count"] == 2
        assert response["analysis"]["updated_count"] == 0
        assert response["analysis"]["error_count"] == 0

    async def test_updates_when_external_id_matches(self, mock_config, respx_mock):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        existing = [
            {
                "id": 7001,
                "start_date_local": "2026-04-27",
                "category": "WORKOUT",
                "name": "old swim",
                "external_id": "p1/w1-mon-swim",
            }
        ]
        respx_mock.get("/athlete/i123456/events").mock(return_value=Response(200, json=existing))
        respx_mock.put("/athlete/i123456/events/7001").mock(
            return_value=Response(
                200,
                json={
                    "id": 7001,
                    "start_date_local": "2026-04-27",
                    "category": "WORKOUT",
                    "name": "Technique swim",
                    "external_id": "p1/w1-mon-swim",
                },
            )
        )
        respx_mock.post("/athlete/i123456/events").mock(
            return_value=Response(
                200,
                json={
                    "id": 7002,
                    "start_date_local": "2026-04-28",
                    "category": "WORKOUT",
                    "name": "Threshold block",
                    "external_id": "p1/w1-tue-bike",
                },
            )
        )

        result = await bulk_upsert_tagged_events(
            events_json=self._sessions_payload(), plan_id="p1", ctx=mock_ctx
        )
        response = json.loads(result)

        assert response["analysis"]["created_count"] == 1
        assert response["analysis"]["updated_count"] == 1
        assert response["analysis"]["error_count"] == 0

    async def test_invalid_json(self, mock_config):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        result = await bulk_upsert_tagged_events(
            events_json="{not-json", plan_id="p1", ctx=mock_ctx
        )
        response = json.loads(result)
        assert "error" in response

    async def test_missing_required_field(self, mock_config):
        mock_ctx = MagicMock()
        mock_ctx.get_state.return_value = mock_config

        bad = json.dumps([{"start_date_local": "2026-04-27", "name": "x", "category": "WORKOUT"}])
        result = await bulk_upsert_tagged_events(events_json=bad, plan_id="p1", ctx=mock_ctx)
        response = json.loads(result)
        assert "error" in response
        assert "session_id" in response["error"]["message"]
