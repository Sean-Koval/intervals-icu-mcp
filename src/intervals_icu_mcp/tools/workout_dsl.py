"""Step-DSL → intervals.icu workout description compiler.

intervals.icu parses structured workouts out of the workout's ``description`` text
(NOT ``file_contents`` — that field expects FIT/ERG/MRC/ZWO binary uploads). This
module compiles a Tempo-flavoured structured step list into the description-line
syntax intervals.icu recognises.

Confirmed via live probe (May 2026) against
``POST /api/v1/athlete/{id}/workouts``:

* Each step renders as ``- <duration> <target>`` on its own line.
* Duration: integer ``Xm`` / ``Xs`` / ``XmYs`` / ``Xh``. Decimals and ``HH:MM``
  forms are rejected by the parser.
* Power target: ``Z<n>`` (1-7) → ``power_zone``; ``<lo>-<hi>%`` → ``%ftp`` range;
  ``<pct>%`` → ``%ftp`` single value.
* HR target: ``Z<n> HR`` → ``hr_zone``; ``<lo>-<hi>% LTHR`` → ``%lthr`` range.
  Bare ``bpm`` ranges are NOT reliably parsed — surface as a validation error.
* Repeats are emitted linearly in v1 (one line per executed interval). The
  intervals.icu ``Nx ( ... )`` syntax exists but the parser drops at the first
  close-paren in our test, so we punt collapse to v2.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

StepKind = Literal["warmup", "interval", "recovery", "cooldown", "rest"]
TargetType = Literal["power", "hr", "cadence", "pace"]

_VALID_KINDS: frozenset[str] = frozenset({"warmup", "interval", "recovery", "cooldown", "rest"})
_VALID_TARGET_TYPES: frozenset[str] = frozenset({"power", "hr", "cadence", "pace"})


class DSLCompileError(ValueError):
    """Raised when a step list cannot be compiled to intervals.icu DSL.

    Carries the offending step index so callers can produce line-pointing errors.
    """

    def __init__(self, message: str, step_index: int | None = None) -> None:
        self.step_index = step_index
        prefix = f"step[{step_index}]: " if step_index is not None else ""
        super().__init__(f"{prefix}{message}")


def _fmt_duration(seconds: int, step_index: int | None = None) -> str:
    """Render an integer-seconds duration in intervals.icu-recognised form.

    Examples:
        60      -> "1m"
        90      -> "1m30s"
        3600    -> "1h"
        3660    -> "61m"   # we never emit "1h1m" — hours only for clean hour values
        45      -> "45s"
    """
    if seconds <= 0:
        raise DSLCompileError(f"duration_s must be positive (got {seconds})", step_index)
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    mins, secs = divmod(seconds, 60)
    if secs == 0:
        return f"{mins}m"
    if mins == 0:
        return f"{secs}s"
    return f"{mins}m{secs}s"


def _coerce_int(v: Any, field: str, step_index: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DSLCompileError(f"{field} must be numeric (got {v!r})", step_index)
    iv = int(v)
    if iv != v:
        raise DSLCompileError(f"{field} must be a whole number (got {v!r})", step_index)
    return iv


def _fmt_target(step: dict[str, Any], step_index: int) -> str:
    target_type = step.get("target_type")
    if target_type is None:
        raise DSLCompileError("missing target_type", step_index)
    if target_type not in _VALID_TARGET_TYPES:
        raise DSLCompileError(
            f"target_type must be one of {sorted(_VALID_TARGET_TYPES)} (got {target_type!r})",
            step_index,
        )

    # Zone shorthand: power zone "Z<n>" or HR zone "Z<n> HR".
    zone = step.get("target_zone")
    if zone is not None:
        zn = _coerce_int(zone, "target_zone", step_index)
        if not 1 <= zn <= 7:
            raise DSLCompileError(f"target_zone must be 1..7 (got {zn})", step_index)
        if target_type == "power":
            return f"Z{zn}"
        if target_type == "hr":
            return f"Z{zn} HR"
        raise DSLCompileError(
            f"target_zone is only supported for target_type in {{'power', 'hr'}} (got {target_type!r})",
            step_index,
        )

    lo = step.get("target_low_pct")
    hi = step.get("target_high_pct")
    if lo is None and hi is None:
        raise DSLCompileError(
            "step must specify either target_zone, or target_low_pct/target_high_pct",
            step_index,
        )
    if lo is None or hi is None:
        # Single-value form: caller can pass either lo or hi alone.
        pct = _coerce_int(lo if lo is not None else hi, "target_*_pct", step_index)
        pct_token = f"{pct}%"
    else:
        lo_i = _coerce_int(lo, "target_low_pct", step_index)
        hi_i = _coerce_int(hi, "target_high_pct", step_index)
        if lo_i > hi_i:
            raise DSLCompileError(
                f"target_low_pct ({lo_i}) must be <= target_high_pct ({hi_i})",
                step_index,
            )
        pct_token = f"{lo_i}-{hi_i}%" if lo_i != hi_i else f"{lo_i}%"

    if target_type == "power":
        return pct_token
    if target_type == "hr":
        return f"{pct_token} LTHR"
    # cadence / pace are accepted at the API level but the description DSL
    # does not have well-tested syntax for them — punt to v2.
    raise DSLCompileError(
        f"target_type {target_type!r} is not supported in v1 of the DSL "
        "(only 'power' and 'hr' are emitted reliably; "
        "cadence/pace can be set via workout_doc directly).",
        step_index,
    )


def _validate_kind(step: dict[str, Any], step_index: int) -> str:
    kind = step.get("kind")
    if kind is None:
        raise DSLCompileError("missing kind", step_index)
    if kind not in _VALID_KINDS:
        raise DSLCompileError(
            f"kind must be one of {sorted(_VALID_KINDS)} (got {kind!r})",
            step_index,
        )
    return kind


def compile_steps_to_description(
    steps: Sequence[dict[str, Any]],
    *,
    prefix: str | None = None,
) -> str:
    """Compile a sequence of step dicts into intervals.icu's description DSL.

    Each step dict shape::

        {
            "kind": "warmup" | "interval" | "recovery" | "cooldown" | "rest",
            "duration_s": int,                          # > 0
            "target_type": "power" | "hr",              # cadence/pace deferred
            # Either zone shorthand:
            "target_zone": 1..7,
            # Or % range:
            "target_low_pct": int,
            "target_high_pct": int,
            "note": "free-form, appended after the target on the same line",
        }

    Returns the multi-line description string ready to be sent as the
    ``description`` field of a workout POST/PUT body.

    Raises ``DSLCompileError`` on malformed input. The error carries the offending
    step index so callers can produce line-pointing error messages.

    NOTE on repeats: v1 of this compiler emits one line per executed interval
    (linear expansion). The intervals.icu DSL accepts ``Nx (...)`` repeat groups
    but the live parser truncates at the first close-paren in our smoke probe,
    so we don't risk it. Callers that need explicit repeats should expand at the
    Tempo session-library level and pass the flat step list here.
    """
    if not steps:
        raise DSLCompileError("steps[] must not be empty")
    if not isinstance(steps, Sequence) or isinstance(steps, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise DSLCompileError("steps must be a list of step objects")

    lines: list[str] = []
    if prefix:
        lines.append(prefix)
        lines.append("")  # blank line between prefix and steps

    for i, step in enumerate(steps):
        if not isinstance(step, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise DSLCompileError("step must be an object", i)

        _validate_kind(step, i)  # validation only; the parser doesn't need the label
        duration_s = step.get("duration_s")
        if duration_s is None:
            raise DSLCompileError("missing duration_s", i)
        duration = _fmt_duration(_coerce_int(duration_s, "duration_s", i), step_index=i)
        target = _fmt_target(step, i)

        line = f"- {duration} {target}"
        note = step.get("note")
        if note:
            line = f"{line}  {note}"
        lines.append(line)

    return "\n".join(lines)


def validate_steps(steps: Sequence[dict[str, Any]]) -> None:
    """Run the same validation as ``compile_steps_to_description`` without emitting."""
    compile_steps_to_description(steps)
