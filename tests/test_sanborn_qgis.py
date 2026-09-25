#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import sanborn_qgis  # noqa: E402
from sanborn_review import REQUIRED_ARTIFACTS  # noqa: E402
from sanborn_qgis import (  # noqa: E402
    EXPECTED_CRS,
    GROUP_NAME,
    INDEX_LAYER_NAME,
    PROTECTED_PROJECT,
    VERIFICATION_METHOD,
    ManifestError,
    build_plan,
    execute_code_payload,
    generate_pyqgis_code,
    load_manifests,
    tile_number_from_name,
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_record(path: Path) -> dict:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": file_sha256(path)}


def write_manifest(folder: Path, tile: int, **changes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    raster = folder / f"Sanborn 1911 -- Tile {tile}_georeferenced.tif"
    raster.write_bytes(f"test raster for tile {tile}".encode("utf-8"))
    raster_digest = file_sha256(raster)
    source = folder / f"source-{tile}.jp2"
    points = folder / f"points-{tile}.points"
    source.write_bytes(f"source-{tile}".encode("utf-8"))
    points.write_bytes(f"points-{tile}".encode("utf-8"))
    source_digest = file_sha256(source)
    points_digest = file_sha256(points)
    seed_x = -9_393_000.0 + tile
    seed_y = 3_996_000.0
    tolerance = 250.0
    index_raster = folder / "index-raster.tif"
    index_preview = folder / "index-preview.png"
    if not index_raster.exists():
        index_raster.write_bytes(b"shared index raster evidence")
    if not index_preview.exists():
        index_preview.write_bytes(b"shared index preview evidence")
    index_evidence = {
        **file_record(index_raster),
        "width": 1000,
        "height": 1000,
        "geotransform": [-9394000, 1, 0, 3997000, 0, -1],
        "crs": EXPECTED_CRS,
    }
    preview_evidence = {
        **file_record(index_preview),
        "width": 1000,
        "height": 1000,
    }
    seed_file = folder / f"seed-{tile}.json"
    seed_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "index": index_evidence,
                "preview": preview_evidence,
                "seeds": [],
            }
        ),
        encoding="utf-8",
    )
    target_seed = {
        "status": "selected",
        "tile": tile,
        "map_x": seed_x,
        "map_y": seed_y,
        "suggested_max_distance": tolerance,
        "quality": "high",
        "review_required": False,
        "seed": {
            "tile": tile,
            "map_x": seed_x,
            "map_y": seed_y,
            "suggested_max_distance": tolerance,
            "quality": "high",
            "ambiguous": False,
            "crs": EXPECTED_CRS,
        },
        "provenance": {
            **file_record(seed_file),
            "index": index_evidence,
        },
    }
    expected_bbox = [
        seed_x - tolerance,
        seed_y - tolerance,
        seed_x + tolerance,
        seed_y + tolerance,
    ]
    ledger = raster.with_suffix(".georef.json")
    ledger_record = {
        "schema_version": 1,
        "source": {
            "path": str(source),
            "sha256": source_digest,
        },
        "points": {
            "path": str(points),
            "sha256": points_digest,
        },
        "transformation": {
            "target_crs": EXPECTED_CRS,
            "safety_limits": {
                "expected_target_seed": [seed_x, seed_y],
                "expected_target_bbox": expected_bbox,
                "max_target_seed_distance": tolerance,
            },
            "diagnostics": {
                "target_location_check": {
                    "passed": True,
                    "target_seed_within_tolerance": True,
                    "all_target_controls_inside_expected_bbox": True,
                    "expected_target_seed": [seed_x, seed_y],
                    "expected_target_bbox": expected_bbox,
                }
            },
        },
        "output": {
            "path": str(raster),
            "bytes": raster.stat().st_size,
            "sha256": raster_digest,
            "bands": ["Red", "Green", "Blue", "Alpha"],
            "band_checksums": [101, 102, 103, 104],
            "alpha_max": 255,
            "compression": "DEFLATE",
            "predictor": 2,
        },
        "protected_project": {
            "path": str(PROTECTED_PROJECT),
            "mtime_ns_before": 123456789,
            "mtime_ns_after": 123456789,
            "unchanged_during_run": True,
        },
    }
    ledger.write_text(json.dumps(ledger_record), encoding="utf-8")
    review_dir = folder / f"review-{tile}"
    review_dir.mkdir(exist_ok=True)
    artifacts = {}
    for key in REQUIRED_ARTIFACTS:
        artifact = review_dir / f"{key}.dat"
        artifact.write_bytes(f"{tile}-{key}".encode("utf-8"))
        artifacts[key] = file_record(artifact)
    osm = folder / "shared-osm.sqlite3"
    kauffman = folder / "shared-kauffman.tif"
    renderer = folder / "shared-renderer.py"
    osm.write_bytes(b"shared osm")
    kauffman.write_bytes(b"shared Kauffman")
    renderer.write_bytes(b"shared renderer")
    approval_token = hashlib.sha256(f"approval-{tile}".encode()).hexdigest()
    artifact_hashes = {key: artifacts[key]["sha256"] for key in REQUIRED_ARTIFACTS}
    review_record = {
        "schema_version": 3,
        "approved": True,
        "approval_token": approval_token,
        "source": {**file_record(source), "width": 100, "height": 100},
        "points": {**file_record(points), "controls": []},
        "safety_limits": {"target_seed_context": target_seed},
        "artifacts": artifacts,
        "provenance": {
            "osm_database": file_record(osm),
            "kauffman_map": file_record(kauffman),
            "renderer_code": {"test_renderer": file_record(renderer)},
            "font": {"pillow_builtin": True},
        },
    }
    approval_note = "Hashed local OSM street and Kauffman overlays checked"
    approval_record = {
        "schema_version": 3,
        "approval_token": approval_token,
        "approved_by": "Test reviewer",
        "approved_utc": "2026-07-15T00:00:00+00:00",
        "geographic_verification": {
            "reference_method": "local-osm-and-kauffman-packet",
            "note": approval_note,
            "artifact_sha256": artifact_hashes,
        },
    }
    review_json = review_dir / "review.json"
    approval_json = review_dir / "approval.json"
    review_json.write_text(json.dumps(review_record), encoding="utf-8")
    approval_json.write_text(json.dumps(approval_record), encoding="utf-8")
    record = {
        "schema_version": 3,
        "tile": tile,
        "path": str(raster),
        "raster_sha256": raster_digest,
        "ledger_path": str(ledger),
        "ledger_sha256": file_sha256(ledger),
        "source_sha256": source_digest,
        "points_sha256": points_digest,
        "review_dir": str(review_dir),
        "review_sha256": file_sha256(review_json),
        "approval_sha256": file_sha256(approval_json),
        "group": GROUP_NAME,
        "sort_key": tile,
        "brightness": 0,
        "gamma": 1.0,
        "contrast": 0,
        "opacity": 1.0,
        "expanded": False,
        "save_project": False,
        "review_token": approval_token,
        "geographic_verification": {
            "reference_method": VERIFICATION_METHOD,
            "note": approval_note,
            "artifact_sha256": artifact_hashes,
        },
        "target_seed": target_seed,
    }
    record.update(changes)
    manifest = folder / f"tile-{tile:04d}.json"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    return manifest


def rewrite_ledger(manifest: Path, mutate) -> None:
    record = json.loads(manifest.read_text(encoding="utf-8"))
    ledger = Path(record["ledger_path"])
    ledger_record = json.loads(ledger.read_text(encoding="utf-8"))
    mutate(ledger_record)
    ledger.write_text(json.dumps(ledger_record), encoding="utf-8")
    record["ledger_sha256"] = file_sha256(ledger)
    manifest.write_text(json.dumps(record), encoding="utf-8")


class FakeRasterLayer:
    def __init__(self, path: str, name: str, layer_id: str = "raster-1"):
        self._path = path
        self._name = name
        self._id = layer_id

    def source(self):
        return self._path

    def name(self):
        return self._name

    def id(self):
        return self._id


class FakeLayerNode:
    def __init__(self, layer, name: str | None = None, parent=None):
        self._layer = layer
        self._name = name if name is not None else layer.name()
        self._parent = parent

    def layer(self):
        return self._layer

    def name(self):
        return self._name

    def parent(self):
        return self._parent

    def layerId(self):
        return self._layer.id() if self._layer is not None else "unresolved"

    def clone(self):
        return FakeLayerNode(self._layer, self._name)


class FakeGroup:
    def __init__(self, children=None, parent=None):
        self._children = list(children or [])
        self._parent = parent
        for child in self._children:
            child._parent = self

    def children(self):
        return self._children

    def parent(self):
        return self._parent

    def removeChildNode(self, node):
        self._children.remove(node)
        node._parent = None

    def insertChildNode(self, index, node):
        self._children.insert(index, node)
        node._parent = self

    def reorderGroupLayers(self, layers):
        nodes = {node.layerId(): node for node in self._children}
        self._children = [nodes[layer.id()] for layer in layers]


class FakeRoot(FakeGroup):
    def findLayers(self):
        found = []

        def visit(group):
            for child in group.children():
                if isinstance(child, FakeLayerNode):
                    found.append(child)
                elif isinstance(child, FakeGroup):
                    visit(child)

        visit(self)
        return found


def generated_helper_namespace():
    core = types.ModuleType("qgis.core")
    core.Qgis = type(
        "FakeQgis",
        (),
        {"RasterColorInterpretation": type("RasterColorInterpretation", (), {"AlphaBand": 6})},
    )
    core.QgsLayerTreeGroup = FakeGroup
    core.QgsLayerTreeLayer = FakeLayerNode
    core.QgsProject = type("FakeProjectClass", (), {})
    core.QgsRasterLayer = FakeRasterLayer
    core.QgsMultiBandColorRenderer = type("FakeMultiBandRenderer", (), {})
    core.QgsContrastEnhancement = type("FakeContrastEnhancement", (), {"NoEnhancement": 0})
    qgis = types.ModuleType("qgis")
    qgis.core = core
    namespace = {"PLAN": {}}
    definitions = sanborn_qgis._PYQGIS_BODY.split("SANBORN_QGIS_RESULT =", 1)[0]
    with mock.patch.dict(sys.modules, {"qgis": qgis, "qgis.core": core}):
        exec(definitions, namespace)
    return namespace


class SanbornQgisPlanTests(unittest.TestCase):
    def setUp(self):
        sanborn_qgis._SHA256_CACHE.clear()

        def read_approval(review_dir, **kwargs):
            folder = Path(review_dir)
            return (
                json.loads((folder / "review.json").read_text(encoding="utf-8")),
                json.loads((folder / "approval.json").read_text(encoding="utf-8")),
            )

        self.approval_patch = mock.patch.object(
            sanborn_qgis, "require_approval", side_effect=read_approval
        )
        self.approval_patch.start()

    def tearDown(self):
        self.approval_patch.stop()

    def test_name_parser_ignores_1911_year(self):
        self.assertEqual(tile_number_from_name("Sanborn 1911 -- Tile 196_georeferenced"), 196)
        self.assertEqual(tile_number_from_name("1911 Sanborn 485 Shmuel"), 485)
        self.assertIsNone(tile_number_from_name("1911 Sanborn Index Orthorectified"))

    def test_hash_cache_deduplicates_shared_files_and_rehashes_after_change(self):
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp) / "shared-reference.tif"
            shared.write_bytes(b"first shared bytes")
            sanborn_qgis._SHA256_CACHE.clear()
            real_constructor = hashlib.sha256
            with mock.patch.object(
                sanborn_qgis.hashlib, "sha256", wraps=real_constructor
            ) as constructor:
                first = sanborn_qgis.sha256(shared)
                self.assertEqual(sanborn_qgis.sha256(shared), first)
                self.assertEqual(constructor.call_count, 1)
                shared.write_bytes(b"different shared bytes")
                second = sanborn_qgis.sha256(shared)
                self.assertNotEqual(second, first)
                self.assertEqual(constructor.call_count, 2)

    def test_live_hash_cache_rejects_conflicting_digest_for_one_canonical_path(self):
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp) / "shared-evidence.dat"
            shared.write_bytes(b"one canonical file")
            digest = file_sha256(shared)
            namespace = generated_helper_namespace()
            item = {
                "path": str(shared),
                "raster_sha256": digest,
                "ledger_path": str(shared),
                "ledger_sha256": "0" * 64,
                "source_path": str(shared),
                "source_sha256": digest,
                "points_path": str(shared),
                "points_sha256": digest,
                "review_path": str(shared),
                "review_sha256": digest,
                "approval_path": str(shared),
                "approval_sha256": digest,
                "review_artifacts": [],
                "additional_evidence": [],
            }
            with self.assertRaisesRegex(RuntimeError, "SHA-256 changed"):
                namespace["_sanborn_verify_file_hashes"](item)

    def test_many_manifests_are_sorted_numerically(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            paths = [write_manifest(folder, 236), write_manifest(folder, 154)]
            records = load_manifests(paths)
            self.assertEqual([record["tile"] for record in records], [154, 236])

            plan = build_plan(paths)
            self.assertEqual(plan["protected_project"], str(PROTECTED_PROJECT))
            self.assertEqual(plan["expected_project_crs"], EXPECTED_CRS)
            self.assertEqual(plan["expected_raster_crs"], EXPECTED_CRS)
            self.assertEqual(plan["index_layer"], INDEX_LAYER_NAME)
            self.assertEqual(plan["group"], GROUP_NAME)
            self.assertEqual(plan["style"]["alpha_band"], 4)
            self.assertEqual([item["tile"] for item in plan["tiles"]], [154, 236])
            self.assertEqual(
                plan["tiles"][0]["raster_sha256"],
                file_sha256(Path(plan["tiles"][0]["path"])),
            )
            self.assertEqual(
                plan["tiles"][0]["ledger_sha256"],
                file_sha256(Path(plan["tiles"][0]["ledger_path"])),
            )
            self.assertFalse(plan["save_project"])

    def test_duplicate_tile_is_rejected(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            paths = [write_manifest(Path(first), 154), write_manifest(Path(second), 154)]
            with self.assertRaises(ManifestError):
                load_manifests(paths)

    def test_manifest_must_preserve_rendering_and_no_save_contract(self):
        cases = (
            {"brightness": "invalid"},
            {"gamma": float("nan")},
            {"contrast": False},
            {"opacity": None},
            {"expanded": True},
            {"save_project": True},
            {"group": "Wrong group"},
            {"schema_version": 2},
        )
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                manifest = write_manifest(Path(temp), 154, **changes)
                with self.assertRaises(ManifestError):
                    build_plan([manifest])

    def test_completed_style_and_renderer_are_history_and_import_uses_current_style(self):
        with tempfile.TemporaryDirectory() as temp:
            path = write_manifest(Path(temp), 154, brightness=50, gamma=1.2, contrast=20)
            record = json.loads(path.read_text())
            review = json.loads((Path(record["review_dir"]) / "review.json").read_text())
            renderer = Path(next(iter(review["provenance"]["renderer_code"].values()))["path"])
            renderer.write_bytes(b"upgraded renderer")
            plan = build_plan([path])
            self.assertEqual(plan["style"], sanborn_qgis.STYLE)
            self.assertEqual(plan["tiles"][0]["recorded_style"]["brightness"], 50)
            self.assertNotIn(str(renderer), [item["path"] for item in plan["tiles"][0]["additional_evidence"]])

    def test_superseded_results_are_archivable_but_not_importable(self):
        for tile in (486, 487, 493, 494):
            with self.subTest(tile=tile), tempfile.TemporaryDirectory() as temp:
                path = write_manifest(Path(temp), tile)
                self.assertEqual(sanborn_qgis.load_manifest(path, for_import=False)["tile"], tile)
                with self.assertRaisesRegex(ManifestError, "superseded"):
                    build_plan([path])

    def test_raster_and_ledger_files_are_hash_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = write_manifest(Path(temp), 154)
            record = json.loads(manifest.read_text(encoding="utf-8"))
            Path(record["path"]).write_bytes(b"substituted raster")
            with self.assertRaisesRegex(ManifestError, "raster SHA-256"):
                build_plan([manifest])

        with tempfile.TemporaryDirectory() as temp:
            manifest = write_manifest(Path(temp), 154)
            record = json.loads(manifest.read_text(encoding="utf-8"))
            with Path(record["ledger_path"]).open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaisesRegex(ManifestError, "ledger SHA-256"):
                build_plan([manifest])

    def test_every_authorization_input_is_live_hash_verified(self):
        selectors = {
            "source": lambda manifest, review: Path(
                json.loads(Path(manifest["ledger_path"]).read_text())["source"]["path"]
            ),
            "points": lambda manifest, review: Path(
                json.loads(Path(manifest["ledger_path"]).read_text())["points"]["path"]
            ),
            "review": lambda manifest, review: Path(manifest["review_dir"]) / "review.json",
            "approval": lambda manifest, review: Path(manifest["review_dir"]) / "approval.json",
            "artifact": lambda manifest, review: Path(
                review["artifacts"][REQUIRED_ARTIFACTS[0]]["path"]
            ),
            "OSM database": lambda manifest, review: Path(
                review["provenance"]["osm_database"]["path"]
            ),
            "Kauffman map": lambda manifest, review: Path(
                review["provenance"]["kauffman_map"]["path"]
            ),
            "seed record": lambda manifest, review: Path(
                manifest["target_seed"]["provenance"]["path"]
            ),
            "seed index raster": lambda manifest, review: Path(
                manifest["target_seed"]["provenance"]["index"]["path"]
            ),
            "seed index preview": lambda manifest, review: Path(
                json.loads(
                    Path(manifest["target_seed"]["provenance"]["path"]).read_text()
                )["preview"]["path"]
            ),
        }
        for label, select in selectors.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                path = write_manifest(Path(temp), 154)
                manifest = json.loads(path.read_text(encoding="utf-8"))
                review = json.loads(
                    (Path(manifest["review_dir"]) / "review.json").read_text(encoding="utf-8")
                )
                target = select(manifest, review)
                with target.open("ab") as handle:
                    handle.write(b"tampered")
                sanborn_qgis._SHA256_CACHE.clear()
                with self.assertRaises(ManifestError):
                    build_plan([path])

    def test_manifest_hashes_must_be_lowercase_sha256_and_match_ledger_provenance(self):
        cases = (
            {"raster_sha256": "not-a-digest"},
            {"ledger_sha256": "A" * 64},
            {"source_sha256": "0" * 64},
            {"points_sha256": "f" * 64},
        )
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                manifest = write_manifest(Path(temp), 154, **changes)
                with self.assertRaises(ManifestError):
                    build_plan([manifest])

    def test_ledger_content_is_validated_even_when_ledger_hash_is_current(self):
        cases = (
            lambda ledger: ledger["output"].update(sha256="0" * 64),
            lambda ledger: ledger["output"].update(bands=["Red", "Green", "Blue"]),
            lambda ledger: ledger["transformation"].update(target_crs="EPSG:4326"),
            lambda ledger: ledger["protected_project"].update(unchanged_during_run=False),
            lambda ledger: ledger["protected_project"].update(
                path="/tmp/not-the-protected-project.qgz"
            ),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temp:
                manifest = write_manifest(Path(temp), 154)
                rewrite_ledger(manifest, mutate)
                with self.assertRaises(ManifestError):
                    build_plan([manifest])

    def test_only_exact_local_osm_and_kauffman_verification_is_accepted(self):
        rejected = (
            "local-osm",
            "local-kauffman",
            "qgis-osm-and-kauffman",
            "local-osm-and-kauffman-packet",
        )
        for method in rejected:
            with self.subTest(method=method), tempfile.TemporaryDirectory() as temp:
                manifest = write_manifest(
                    Path(temp),
                    154,
                    geographic_verification={
                        "reference_method": method,
                        "note": "A note alone is not enough.",
                        "artifact_sha256": {"overlay": "1" * 64},
                    },
                )
                with self.assertRaisesRegex(ManifestError, "must be exactly"):
                    build_plan([manifest])

    def test_filename_tile_must_match_manifest_tile(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            manifest = write_manifest(folder, 154)
            record = json.loads(manifest.read_text(encoding="utf-8"))
            wrong = folder / "Sanborn 1911 -- Tile 155_georeferenced.tif"
            wrong.write_bytes(b"wrong tile")
            record["path"] = str(wrong)
            manifest.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(ManifestError):
                build_plan([manifest])

    def test_generated_code_compiles_and_contains_live_safety_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = write_manifest(Path(temp), 154)
            plan = build_plan([manifest])
            code = generate_pyqgis_code(plan)
            compile(code, "<generated-sanborn-pyqgis>", "exec")

            required_fragments = (
                "QgsProject.instance()",
                "_sanborn_sha256(path)",
                "_sanborn_verify_file_hashes(item)",
                "_sanborn_preflight_project_tiles(project, root, incoming)",
                "_sanborn_source_filename_tile(layer)",
                "node.name()",
                "project.fileName()",
                "project.crs().authid()",
                "QgsRasterLayer(path, item[\"layer_name\"] + \" verification probe\", \"gdal\")",
                "Qgis.RasterColorInterpretation.AlphaBand",
                "project.addMapLayer(layer, False)",
                "brightness.setBrightness",
                "brightness.setGamma",
                "brightness.setContrast",
                "renderer.setOpacity",
                "renderer.setAlphaBand",
                "group.reorderGroupLayers",
                "group.setExpanded(True)",
                "node.setExpanded(False)",
                "_sanborn_restore_after_failure(project, root, rollback)",
                "project.removeMapLayer(layer_id)",
                "os.stat(protected_project).st_mtime_ns",
            )
            for fragment in required_fragments:
                self.assertIn(fragment, code)
            self.assertNotIn("QgsProject.write", code)
            self.assertNotIn("project.write(", code)
            self.assertNotIn("saveProject(", code)

            prepare = code.split("def prepare_sanborn_layers():", 1)[1]
            first_mutation = prepare.index("_sanborn_position_group(")
            hash_gate = "_sanborn_verify_file_hashes(item)"
            self.assertEqual(prepare.count(hash_gate), 2)
            first_hash_gate = prepare.index(hash_gate)
            second_hash_gate = prepare.index(hash_gate, first_hash_gate + 1)
            self.assertLess(first_hash_gate, first_mutation)
            self.assertGreater(second_hash_gate, prepare.index("# Postconditions:"))
            self.assertGreater(second_hash_gate, first_mutation)
            self.assertLess(
                prepare.index("_sanborn_preflight_project_tiles(project, root, incoming)"),
                first_mutation,
            )

    def test_generated_code_contains_whole_project_identity_and_rollback_gates(self):
        with tempfile.TemporaryDirectory() as temp:
            code = generate_pyqgis_code(build_plan([write_manifest(Path(temp), 154)]))
            required_messages = (
                "points to a different raster",
                "displays tile {} but its source filename",
                '"is tile {}".format(',
                "could not remove newly registered layer",
                "could not restore the original Sanborn group",
                "rollback was incomplete",
            )
            for message in required_messages:
                self.assertIn(message, code)

    def test_live_preflight_rejects_same_tile_different_path_anywhere_in_project(self):
        helpers = generated_helper_namespace()
        conflicting = FakeRasterLayer(
            "/tmp/archive/Sanborn 1911 -- Tile 154_georeferenced.tif",
            "Sanborn 1911 -- Tile 154_georeferenced",
        )
        root = FakeRoot([FakeLayerNode(conflicting)])
        project = type(
            "FakeProject",
            (),
            {"mapLayers": lambda self: {conflicting.id(): conflicting}},
        )()
        incoming = {
            154: {"path": "/tmp/current/Sanborn 1911 -- Tile 154_georeferenced.tif"}
        }
        with self.assertRaisesRegex(RuntimeError, "points to a different raster"):
            helpers["_sanborn_preflight_project_tiles"](project, root, incoming)

    def test_live_group_preflight_matches_displayed_and_source_filename_tiles(self):
        helpers = generated_helper_namespace()
        wrong_source = FakeRasterLayer(
            "/tmp/Sanborn 1911 -- Tile 236_georeferenced.tif",
            "Sanborn 1911 -- Tile 154_georeferenced",
        )
        group = FakeGroup(
            [FakeLayerNode(wrong_source, "Sanborn 1911 -- Tile 154_georeferenced")]
        )
        with self.assertRaisesRegex(RuntimeError, "displays tile 154.*source filename is tile 236"):
            helpers["_sanborn_check_group_contents"](group, {})

    def _preserved_pair_fixture(self, folder):
        full = folder / "Sanborn 1911 -- Tile 486_georeferenced_v4 1958 topo fit.tif"
        alpha = full.with_name(full.stem + " alpha" + full.suffix)
        full.write_bytes(b"corrected full sheet")
        alpha.write_bytes(b"corrected alpha sheet")
        decision = folder / "preserve-pair.json"
        decision.write_text(json.dumps({"schema_version": 1,
            "purpose": "preserve-existing-full-alpha-pair", "tile": 486,
            "approved_by": "Joel", "note": "Retain both previously corrected variants.",
            "variants": [{"role": role, "path": str(path.resolve()), "sha256": file_sha256(path)}
                         for role, path in (("full", full), ("alpha", alpha))]}))
        layers = [FakeRasterLayer(str(path.resolve()), "Sanborn 1911 - tile 486 (" + role + ")", layer_id=role)
                  for role, path in (("full", full), ("alpha", alpha))]
        group = FakeGroup([FakeLayerNode(layer) for layer in layers])
        helpers = generated_helper_namespace()
        helpers["PLAN"]["preserved_existing_variants"] = [sanborn_qgis.load_preserved_variant_pair(decision)]
        return decision, layers, group, helpers

    def test_explicit_existing_pair_survives_unrelated_import_and_stable_sort(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            decision, layers, group, helpers = self._preserved_pair_fixture(folder)
            manifest = write_manifest(folder, 495)
            plan = build_plan([manifest], preserve_existing_variants=decision)
            self.assertEqual(plan["preserved_existing_variants"][0]["tile"], 486)
            incoming = {495: plan["tiles"][0]}
            helpers["_sanborn_check_group_contents"](group, incoming)
            project = types.SimpleNamespace(mapLayers=lambda: {layer.id(): layer for layer in layers})
            helpers["_sanborn_preflight_project_tiles"](project, FakeRoot([group]), incoming)
            # Exercise the same post-insertion checks and ordering used by live
            # import, preserving both existing objects and their relative order.
            added = FakeRasterLayer(incoming[495]["path"], "Sanborn 1911 - tile 495", "new-495")
            group.insertChildNode(0, FakeLayerNode(added))
            earlier = FakeRasterLayer("/tmp/Tile 236.tif", "Sanborn 1911 - tile 236", "earlier-236")
            group.insertChildNode(0, FakeLayerNode(earlier))
            self.assertEqual(helpers["_sanborn_numeric_order"](group), [236, 486, 486, 495])
            helpers["_sanborn_check_group_contents"](group, incoming)
            self.assertEqual([node.layer() for node in group.children()][1:3], layers)
            self.assertEqual(sum(node.layerId() == "new-495" for node in group.children()), 1)

    def test_pair_requires_explicit_record_and_rejects_extra_or_repeated_variants(self):
        for change in ("no-record", "third-variant", "same-path", "arbitrary-other-tile"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                _, layers, group, helpers = self._preserved_pair_fixture(Path(temp))
                if change == "no-record":
                    helpers["PLAN"].clear()
                elif change == "third-variant":
                    group.insertChildNode(0, FakeLayerNode(FakeRasterLayer("/tmp/Tile 486_other.tif", "Tile 486", "third")))
                elif change == "same-path":
                    group._children[1] = FakeLayerNode(layers[0], parent=group)
                else:
                    for index in range(2):
                        group.insertChildNode(0, FakeLayerNode(FakeRasterLayer(f"/tmp/Tile 236_{index}.tif", "Tile 236", str(index))))
                with self.assertRaisesRegex(RuntimeError, "more than once"):
                    helpers["_sanborn_check_group_contents"](group, {})

    def test_preservation_record_does_not_authorize_importing_its_tile(self):
        with tempfile.TemporaryDirectory() as temp:
            decision, layers, group, helpers = self._preserved_pair_fixture(Path(temp))
            with mock.patch.object(sanborn_qgis, "load_manifests", return_value=[{"tile": 486}]):
                with self.assertRaisesRegex(ManifestError, "Cannot import a tile"):
                    build_plan([], preserve_existing_variants=decision)
            with self.assertRaisesRegex(RuntimeError, "more than once|cannot import"):
                helpers["_sanborn_check_group_contents"](group, {486: {"path": layers[0].source()}})

    def test_preserved_pair_requires_both_layers_already_loaded_and_current_hashes(self):
        for change in ("missing-pair", "missing-alpha", "changed-full", "changed-record"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                decision, layers, group, helpers = self._preserved_pair_fixture(Path(temp))
                if change == "missing-pair": group = None
                elif change == "missing-alpha": group._children.pop()
                elif change == "changed-full": Path(layers[0].source()).write_bytes(b"replaced pixels")
                else: decision.write_text(decision.read_text() + "\n")
                with self.assertRaisesRegex(RuntimeError, "not already present|evidence changed"):
                    helpers["_sanborn_check_group_contents"](group, {})

    def test_preservation_does_not_weaken_incoming_path_or_registry_duplicate_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, group, helpers = self._preserved_pair_fixture(Path(temp))
            incoming = {495: {"path": "/tmp/Tile 495_current.tif"}}
            other = FakeRasterLayer("/tmp/Tile 495_other.tif", "Tile 495", "other-495")
            project = types.SimpleNamespace(mapLayers=lambda: {other.id(): other})
            with self.assertRaisesRegex(RuntimeError, "points to a different raster"):
                helpers["_sanborn_preflight_project_tiles"](project, FakeRoot([group, FakeLayerNode(other)]), incoming)
            duplicates = [FakeRasterLayer(incoming[495]["path"], "Tile 495", str(index)) for index in range(2)]
            project = types.SimpleNamespace(mapLayers=lambda: {layer.id(): layer for layer in duplicates})
            with self.assertRaisesRegex(RuntimeError, "already registered more than once"):
                helpers["_sanborn_preflight_raster"](project, incoming[495])

    def test_preserved_pair_rejects_additional_registry_layers_or_tree_nodes_anywhere(self):
        for change in ("duplicate-registry", "duplicate-tree", "third-source", "unresolved-node"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                _, layers, group, helpers = self._preserved_pair_fixture(Path(temp))
                registry = {layer.id(): layer for layer in layers}
                root = FakeRoot([group])
                if change == "duplicate-registry":
                    extra = FakeRasterLayer(layers[0].source(), "Tile 486", "duplicate")
                    registry[extra.id()] = extra
                elif change == "duplicate-tree":
                    root.insertChildNode(0, FakeLayerNode(layers[0]))
                elif change == "third-source":
                    extra = FakeRasterLayer("/tmp/Tile 486_unapproved.tif", "Tile 486", "third")
                    registry[extra.id()] = extra
                else:
                    root.insertChildNode(0, FakeLayerNode(None, "Tile 486"))
                project = types.SimpleNamespace(mapLayers=lambda: registry)
                with self.assertRaisesRegex(RuntimeError, "exactly one full and one alpha"):
                    helpers["_sanborn_preflight_project_tiles"](project, root, {495: {"path": "/tmp/Tile 495.tif"}})

    def test_preservation_loader_rejects_invalid_authorization_and_variant_identity(self):
        for change in ("missing-note", "same-role", "wrong-tile", "hash-change", "not-alpha-sibling"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                decision, _, _, _ = self._preserved_pair_fixture(folder)
                record = json.loads(decision.read_text())
                if change == "missing-note": record["note"] = " "
                elif change == "same-role": record["variants"][1]["role"] = "full"
                elif change == "wrong-tile": record["tile"] = 487
                elif change == "hash-change": record["variants"][0]["sha256"] = "0" * 64
                else:
                    other = folder / "Tile 486_other.tif"
                    other.write_bytes(b"other")
                    record["variants"][1].update(path=str(other.resolve()), sha256=file_sha256(other))
                decision.write_text(json.dumps(record))
                with self.assertRaises(ManifestError):
                    sanborn_qgis.load_preserved_variant_pair(decision)

    def test_live_rollback_removes_new_layer_and_new_group(self):
        helpers = generated_helper_namespace()
        added = FakeRasterLayer(
            "/tmp/Sanborn 1911 -- Tile 154_georeferenced.tif",
            "Sanborn 1911 -- Tile 154_georeferenced",
            layer_id="new-layer",
        )
        active_group = FakeGroup()
        root = FakeRoot([active_group])

        class Project:
            def __init__(self):
                self.layers = {added.id(): added}
                self.removed = []

            def mapLayers(self):
                return self.layers

            def removeMapLayer(self, layer_id):
                self.removed.append(layer_id)
                self.layers.pop(layer_id)

        project = Project()
        errors = helpers["_sanborn_restore_after_failure"](
            project,
            root,
            {
                "existing_group": None,
                "active_group": active_group,
                "added_layer_ids": [added.id()],
                "reused": {},
            },
        )
        self.assertEqual(errors, [])
        self.assertEqual(project.removed, [added.id()])
        self.assertNotIn(active_group, root.children())

    def test_execute_code_payload_wraps_the_same_generated_code(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = write_manifest(Path(temp), 154)
            plan = build_plan([manifest])
            payload = execute_code_payload(plan)
            self.assertEqual(set(payload), {"code"})
            self.assertEqual(payload["code"], generate_pyqgis_code(plan))


if __name__ == "__main__":
    unittest.main()
