#!/usr/bin/env python3
"""Keep a dated copy of Joel's QGIS project before any sheet is placed in it.

Placing a sheet changes the live project.  If something goes wrong -- a layer in
the wrong group, a style that did not take, an import that half-happened -- the
way back is a copy of the project as it stood beforehand.  This writes that copy
into the repository, so it is version-controlled and off this Mac rather than
sitting beside the original where the same accident would reach it.

One copy per working day.  A second import on the same day overwrites that day's
copy rather than adding another, so the archive stays a readable history of days
rather than a pile of near-identical files.  The file is small -- tens of
kilobytes -- because a QGIS project stores references to its layers, not the
maps themselves.

Nothing here ever writes Joel's master project.  It is only ever read.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "archive" / "qgis-projects"


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
    commit: bool = True,
) -> tuple[Path, bool]:
    """Copy the project into the repository under today's date.

    Returns the archive path and whether it replaced an earlier copy from the
    same day.
    """
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

    if commit:
        _commit(target, replaced)
    return target, replaced


def _commit(target: Path, replaced: bool) -> None:
    """Commit just this file.

    Deliberately narrow: this repository routinely holds unrelated work in
    progress, and an archive step must never sweep that into a commit.
    """
    verb = "Update" if replaced else "Add"
    message = f"{verb} the QGIS project archive for {target.stem.split()[-1]}"
    try:
        subprocess.run(
            ["git", "add", "--", str(target)],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", str(target)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if staged.returncode == 0:
            return  # Byte-identical to the copy already stored; nothing to record.
        subprocess.run(
            ["git", "commit", "-m", message, "--only", "--", str(target)],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, OSError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise ArchiveError(
            "The project was copied to the archive but could not be committed: "
            f"{detail.strip()}"
        ) from error

    # Pushing is best effort: without a network the archive still exists locally
    # and is committed, which is what protects the work.
    subprocess.run(
        ["git", "push"], cwd=REPO_ROOT, capture_output=True, text=True
    )
