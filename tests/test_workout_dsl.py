"""Tests for the step → intervals.icu DSL compiler."""

from __future__ import annotations

import pytest

from intervals_icu_mcp.tools.workout_dsl import (
    DSLCompileError,
    compile_steps_to_description,
)


class TestDurationFormatting:
    def test_minutes(self):
        out = compile_steps_to_description(
            [{"kind": "interval", "duration_s": 300, "target_type": "power", "target_zone": 4}]
        )
        assert out == "- 5m Z4"

    def test_seconds_only(self):
        out = compile_steps_to_description(
            [{"kind": "interval", "duration_s": 45, "target_type": "power", "target_zone": 5}]
        )
        assert out == "- 45s Z5"

    def test_mixed_minutes_seconds(self):
        out = compile_steps_to_description(
            [{"kind": "interval", "duration_s": 90, "target_type": "power", "target_zone": 4}]
        )
        assert out == "- 1m30s Z4"

    def test_clean_hour(self):
        out = compile_steps_to_description(
            [{"kind": "warmup", "duration_s": 3600, "target_type": "power", "target_zone": 2}]
        )
        assert out == "- 1h Z2"

    def test_non_clean_hour_uses_minutes(self):
        # 3660s = 61m — not "1h1m" (we never emit that compound form)
        out = compile_steps_to_description(
            [{"kind": "warmup", "duration_s": 3660, "target_type": "power", "target_zone": 2}]
        )
        assert out == "- 61m Z2"


class TestPowerTargets:
    def test_zone(self):
        out = compile_steps_to_description(
            [{"kind": "interval", "duration_s": 300, "target_type": "power", "target_zone": 4}]
        )
        assert out == "- 5m Z4"

    def test_pct_range(self):
        out = compile_steps_to_description(
            [
                {
                    "kind": "interval",
                    "duration_s": 600,
                    "target_type": "power",
                    "target_low_pct": 76,
                    "target_high_pct": 88,
                }
            ]
        )
        assert out == "- 10m 76-88%"

    def test_single_pct_both_equal(self):
        out = compile_steps_to_description(
            [
                {
                    "kind": "interval",
                    "duration_s": 300,
                    "target_type": "power",
                    "target_low_pct": 90,
                    "target_high_pct": 90,
                }
            ]
        )
        assert out == "- 5m 90%"

    def test_single_pct_only_low(self):
        out = compile_steps_to_description(
            [
                {
                    "kind": "interval",
                    "duration_s": 300,
                    "target_type": "power",
                    "target_low_pct": 80,
                }
            ]
        )
        assert out == "- 5m 80%"


class TestHRTargets:
    def test_zone(self):
        out = compile_steps_to_description(
            [{"kind": "interval", "duration_s": 600, "target_type": "hr", "target_zone": 2}]
        )
        assert out == "- 10m Z2 HR"

    def test_pct_lthr_range(self):
        out = compile_steps_to_description(
            [
                {
                    "kind": "interval",
                    "duration_s": 600,
                    "target_type": "hr",
                    "target_low_pct": 70,
                    "target_high_pct": 80,
                }
            ]
        )
        assert out == "- 10m 70-80% LTHR"


class TestMultiStep:
    def test_full_workout(self):
        steps = [
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
                "kind": "recovery",
                "duration_s": 300,
                "target_type": "power",
                "target_low_pct": 50,
                "target_high_pct": 60,
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
        out = compile_steps_to_description(steps)
        assert out == ("- 15m 50-65%\n- 15m 76-88%\n- 5m 50-60%\n- 15m 76-88%\n- 5m 50-60%")

    def test_with_prefix(self):
        out = compile_steps_to_description(
            [{"kind": "interval", "duration_s": 600, "target_type": "power", "target_zone": 4}],
            prefix="Tempo session: 4x10 @ FTP",
        )
        assert out == "Tempo session: 4x10 @ FTP\n\n- 10m Z4"

    def test_note_appended(self):
        out = compile_steps_to_description(
            [
                {
                    "kind": "interval",
                    "duration_s": 300,
                    "target_type": "power",
                    "target_zone": 4,
                    "note": "stay seated",
                }
            ]
        )
        assert out == "- 5m Z4  stay seated"


class TestValidation:
    def test_empty_steps_rejected(self):
        with pytest.raises(DSLCompileError, match="must not be empty"):
            compile_steps_to_description([])

    def test_missing_kind(self):
        with pytest.raises(DSLCompileError) as ei:
            compile_steps_to_description(
                [{"duration_s": 300, "target_type": "power", "target_zone": 4}]
            )
        assert ei.value.step_index == 0
        assert "kind" in str(ei.value)

    def test_invalid_kind(self):
        with pytest.raises(DSLCompileError, match="kind must be one of"):
            compile_steps_to_description(
                [{"kind": "sprint!", "duration_s": 300, "target_type": "power", "target_zone": 4}]
            )

    def test_missing_duration(self):
        with pytest.raises(DSLCompileError, match="duration_s"):
            compile_steps_to_description(
                [{"kind": "interval", "target_type": "power", "target_zone": 4}]
            )

    def test_zero_duration(self):
        with pytest.raises(DSLCompileError, match="positive"):
            compile_steps_to_description(
                [{"kind": "interval", "duration_s": 0, "target_type": "power", "target_zone": 4}]
            )

    def test_missing_target(self):
        with pytest.raises(DSLCompileError, match="target_zone"):
            compile_steps_to_description(
                [{"kind": "interval", "duration_s": 300, "target_type": "power"}]
            )

    def test_invalid_target_type(self):
        with pytest.raises(DSLCompileError, match="target_type must be one of"):
            compile_steps_to_description(
                [{"kind": "interval", "duration_s": 300, "target_type": "rpe", "target_zone": 4}]
            )

    def test_pace_target_punted(self):
        with pytest.raises(DSLCompileError, match="not supported in v1"):
            compile_steps_to_description(
                [
                    {
                        "kind": "interval",
                        "duration_s": 300,
                        "target_type": "pace",
                        "target_low_pct": 80,
                        "target_high_pct": 90,
                    }
                ]
            )

    def test_zone_out_of_range(self):
        with pytest.raises(DSLCompileError, match="1..7"):
            compile_steps_to_description(
                [{"kind": "interval", "duration_s": 300, "target_type": "power", "target_zone": 9}]
            )

    def test_inverted_range_rejected(self):
        with pytest.raises(DSLCompileError, match="must be <="):
            compile_steps_to_description(
                [
                    {
                        "kind": "interval",
                        "duration_s": 300,
                        "target_type": "power",
                        "target_low_pct": 90,
                        "target_high_pct": 80,
                    }
                ]
            )

    def test_step_not_dict(self):
        with pytest.raises(DSLCompileError, match="must be an object"):
            compile_steps_to_description(["bogus"])  # type: ignore[list-item]

    def test_error_carries_step_index(self):
        steps = [
            {"kind": "warmup", "duration_s": 600, "target_type": "power", "target_zone": 2},
            {"kind": "interval", "duration_s": -5, "target_type": "power", "target_zone": 4},
        ]
        with pytest.raises(DSLCompileError) as ei:
            compile_steps_to_description(steps)
        assert ei.value.step_index == 1
