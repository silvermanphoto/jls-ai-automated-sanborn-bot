# AI Automated Sanborn Bot

This project is the working method for locating, downloading, georeferencing, and checking individual 1911 Atlanta Sanborn fire-insurance sheets in QGIS.

The goal is speed without sacrificing geographic judgment. Each sheet is positioned with three strong, widely separated street intersections, checked against current OpenStreetMap streets and the 1921 Atlanta Kauffman map, then exported as a losslessly compressed GeoTIFF with transparent empty areas.

## What is here

- `AI AUTOMATED SANBORN BOT - MASTER INSTRUCTIONS.md` is the complete operating manual and safety checklist.
- `AI AUTOMATED SANBORN BOT - THREE PANEL COMPARISON PLAN.md` describes the planned faster comparison workspace.
- `1911 SANBORN DOWNLOADS/` holds reusable `.points` control records. The large source and finished map images remain local and are deliberately excluded from GitHub.
- `AGENTS.md` records the project rules and lessons future Codex sessions must follow.

## Current rendering formula

Completed Sanborn layers use 100% global opacity, Brightness +50, Gamma 1.2, and Contrast +20. Empty warped areas use a real alpha band; black map ink is never treated as transparency.

## Important safety rules

The original Sanborn image is read-only. Every georeferenced result receives a new filename. The protected `JLS Master Map File.qgz` project may be opened and used, but it must never be saved or closed by automation unless Joel explicitly authorizes that action.

This repository is private. Large JP2 and GeoTIFF images are not stored here because they would exceed practical GitHub limits and can be recovered from the Library of Congress or regenerated from the saved control points.

