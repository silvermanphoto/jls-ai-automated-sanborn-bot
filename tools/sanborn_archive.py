#!/usr/bin/env python3
"""Keep a dated local backup before changing a live QGIS layer stack.

QGIS projects can contain private service URLs, connection details and notes.
The public software repository must never automatically publish these files.
Backups live under the ignored _local directory; the master is only read.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "_local" / "qgis-project-backups"


class ArchiveError(RuntimeError):
    """Raised when the archive cannot be written, which stops the import."""


def archive_name(project: Path, when: date | None = None) -> str:
    """``JLS Master Map File 07.27.26.qgz`` -- Joel's dating style."""
    stamp = (when or date.today()).strftime("%m.%d.%y")
    return f"{project.stem} {stamp}{project.suffix}"


def archive_project(
    project: Path,
    when: date | None = None,
    archive_dir: Path | None = None,
    commit: bool = False,
) -> tuple[Path, bool]:
    """Copy the project to local backup storage under today's date.

    Returns the archive path and whether it replaced an earlier copy from the
    same day.
    """
    if commit:
        raise ArchiveError(
            "Automatic publication of QGIS project backups is disabled. "
            "Keep the backup local and separately review anything intended for sharing."
        )
    project = Path(project)
    if not project.is_file():
        raise ArchiveError(
            f"The QGIS project to archive does not exist: {project}"
        )

    target_dir = Path(archive_dir) if archive_dir else ARCHIVE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / archive_name(project, when)
    replaced = target.exists()

    # copy2 preserves the original's timestamps, so the archive records when
    # Joel last saved the project rather than when this ran.
    shutil.copy2(project, target)

    return target, replaced
