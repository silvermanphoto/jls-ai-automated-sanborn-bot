"""Placement restrictions for results whose old geographic approval was withdrawn."""
from __future__ import annotations

import json
import math
from pathlib import Path

from sanborn_georeference import MAX_LEAVE_ONE_OUT_RMS_METRES, MIN_FIT_POINTS

SUPERSEDED_TILES = frozenset({486, 487, 493, 494})
REQUIRED_REFERENCE = "washington-rawson-topo-1958"

# Joel's precision-requirement override (engine CLAUDE.md, "Hard sheets - Joel,
# 2026-09-30"): these very hard sheets may sit tens of meters off at street level
# and are exempt from the 15 m leave-one-out pass test. The similarity fit, the
# three-point minimum and the scale gate still hold. Any other sheet needs
# Joel's own override, added here with his words and the date.
PRECISION_OVERRIDE_TILES = frozenset({77, 174, 178})
PRECISION_OVERRIDE_DATE = "2026-09-30"
PRECISION_OVERRIDE_QUOTE = (
    "this level of imprecision is acceptable for these very difficult to place tiles"
)


def precision_override(tile: object) -> dict | None:
    """The record bound into a packet and ledger when Joel's override applies."""
    if isinstance(tile, bool) or not isinstance(tile, int) or tile not in PRECISION_OVERRIDE_TILES:
        return None
    return {
        "tile": tile,
        "waives": "leave-one-out limit",
        "approved_by": "Joel Silverman",
        "date": PRECISION_OVERRIDE_DATE,
        "quote": PRECISION_OVERRIDE_QUOTE,
    }


def require_current_placement_evidence(tile: int, review: dict) -> None:
    """Call only after validating the packet lock and every evidence file hash.

    Archival integrity does not restore withdrawn geographic approval. These
    sheets need new original-topo measurements and withheld checks, bound into
    the reviewed packet. A note or a manifest flag cannot override this gate.
    The pass test is Joel's 2026-09-28 rule: at least three measured points and a
    leave-one-out error of 15 m RMS or less under the rotate-scale-shift fit.
    """
    if tile not in SUPERSEDED_TILES:
        return
    reason = (f"Tile {tile}'s earlier placement is superseded. Reopen to review-ready "
              "and build and approve a fresh comparison against the 1958 original "
              "topographic map (at least three measured street corners, leave-one-out error "
              "15 m RMS or less) before import or finishing.")
    provenance = review.get("provenance", {})
    evidence_record = provenance.get("historical_evidence")
    if not isinstance(evidence_record, dict):
        raise RuntimeError(reason)
    evidence = json.loads(Path(evidence_record["path"]).read_text(encoding="utf-8"))
    checks = evidence.get("independent_checks", {})
    if (not isinstance(checks.get("count"), int) or isinstance(checks.get("count"), bool)
            or any(not isinstance(checks.get(key), (int, float))
                   or isinstance(checks.get(key), bool)
                   or not math.isfinite(checks[key]) or checks[key] < 0
                   for key in ("rms_ground_metres", "max_ground_metres"))):
        raise RuntimeError(reason)
    if (evidence.get("tile") != tile
            or evidence.get("selection_origin") != "historical-reference"
            or evidence.get("reference_profile", {}).get("id") != REQUIRED_REFERENCE
            or provenance.get("historical_profile") != evidence.get("reference_profile")
            or provenance.get("historical_reference") != evidence.get("reference")
            or provenance.get("historical_measurement_preview") != evidence.get("reference_preview")
            or evidence.get("current_source", {}).get("sha256") != review.get("source", {}).get("sha256")
            or checks.get("method") != "leave-one-out"
            or checks.get("count", 0) < MIN_FIT_POINTS
            or checks.get("rms_ground_metres", float("inf")) > MAX_LEAVE_ONE_OUT_RMS_METRES
            or not {"historical_reference", "historical_overlay"}.issubset(review.get("artifacts", {}))
            or review.get("render_spec", {}).get("historical_reference", {}).get("independent_checks") != checks):
        raise RuntimeError(reason)
