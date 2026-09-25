"""Placement restrictions for results whose old geographic approval was withdrawn."""
from __future__ import annotations

import json
import math
from pathlib import Path

SUPERSEDED_TILES = frozenset({486, 487, 493, 494})
REQUIRED_REFERENCE = "washington-rawson-topo-1958"


def require_current_placement_evidence(tile: int, review: dict) -> None:
    """Call only after validating the packet lock and every evidence file hash.

    Archival integrity does not restore withdrawn geographic approval. These
    sheets need new original-topo measurements and withheld checks, bound into
    the reviewed packet. A note or a manifest flag cannot override this gate.
    """
    if tile not in SUPERSEDED_TILES:
        return
    reason = (f"Tile {tile}'s earlier placement is superseded. Reopen to review-ready "
              "and build and approve a fresh comparison against the 1958 original "
              "topographic map with three independent street checks before import or finishing.")
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
            or checks.get("count", 0) < 3
            or checks.get("rms_ground_metres", float("inf")) > 5
            or checks.get("max_ground_metres", float("inf")) > 10
            or not {"historical_reference", "historical_overlay"}.issubset(review.get("artifacts", {}))
            or review.get("render_spec", {}).get("historical_reference", {}).get("independent_checks") != checks):
        raise RuntimeError(reason)
