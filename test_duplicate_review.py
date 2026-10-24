import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import adult_content_organizer as o
import duplicate_review as d
import encoding_queue as q
import organizer_journal as j


class FakeAPI:
    def __init__(self, rows):
        self.rows = {str(row["id"]): copy.deepcopy(row) for row in rows}
        self.calls = []
        self.phash_groups = []
        self.fail_move = False
        self.lose_metadata = False

    def file(self, file_id):
        return next(
            (
                copy.deepcopy(f)
                for row in self.rows.values()
                for f in row["files"]
                if str(f["id"]) == str(file_id)
            ),
            None,
        )

    def scene(self, scene_id):
        return copy.deepcopy(self.rows.get(str(scene_id)))

    def move(self, file_id, folder):
        if self.fail_move:
            return False
        file = self.file(file_id)
        source = Path(file["path"])
        target = Path(folder) / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        for row in self.rows.values():
            for f in row["files"]:
                if str(f["id"]) == str(file_id):
                    f["path"] = str(target)
        return True

    def gql(self, query, variables=None, **kwargs):
        variables = variables or {}
        self.calls.append((query, copy.deepcopy(variables)))
        if "__type" in query:
            if variables.get("name"):
                return {
                    "__type": {
                        "fields": [{"name": v} for v in ("production_date", "scenes")]
                    }
                }
            return {"__type": {"inputFields": [{"name": "full"}]}}
        if "findDuplicateScenes" in query:
            return {"findDuplicateScenes": copy.deepcopy(self.phash_groups)}
        if "findScenes(" in query:
            return {"findScenes": {"scenes": copy.deepcopy(list(self.rows.values()))}}
        if "findScene(" in query:
            return {"findScene": copy.deepcopy(self.rows.get(str(variables["id"])))}
        if "sceneMerge(" in query:
            value = variables["input"]
            dest = self.rows[value["destination"]]
            old = [copy.deepcopy(dest)]
            for key in value["source"]:
                row = self.rows.pop(key)
                old.append(copy.deepcopy(row))
                dest["files"] += row["files"]
            for key, val in value["values"].items():
                if key.endswith("_ids") and key in (
                    "performer_ids",
                    "tag_ids",
                    "gallery_ids",
                ):
                    dest[
                        {
                            "performer_ids": "performers",
                            "tag_ids": "tags",
                            "gallery_ids": "galleries",
                        }[key]
                    ] = [{"id": v} for v in val]
                elif key == "groups":
                    dest[key] = [
                        dict(
                            group={"id": v["group_id"]},
                            scene_index=v.get("scene_index"),
                        )
                        for v in val
                    ]
                elif key == "custom_fields":
                    dest[key] = val["full"]
                else:
                    dest[key] = val
            for file in dest["files"]:
                file["scenes"] = [{"id": dest["id"]}]
            for key in ("play_history", "o_history"):
                dest[key] = [v for row in old for v in row.get(key) or []]
            if self.lose_metadata:
                dest["tags"] = []
            return {"sceneMerge": {"id": dest["id"]}}
        if "sceneUpdate(" in query:
            value = variables["input"]
            row = self.rows[value["id"]]
            row["files"].sort(
                key=lambda f: str(f["id"]) != str(value["primary_file_id"])
            )
            return {"sceneUpdate": copy.deepcopy(row)}
        if "deleteFiles(" in query:
            for key in variables["ids"]:
                file = self.file(key)
                Path(file["path"]).unlink()
                for row in self.rows.values():
                    row["files"] = [f for f in row["files"] if str(f["id"]) != key]
            return {"deleteFiles": True}
        raise AssertionError(query)


class DuplicateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.incoming = self.base / "incoming"
        self.media = self.base / "library"
        self.reports = self.base / "reports"
        for path in (self.incoming, self.media, self.reports):
            path.mkdir()
        p = patch.multiple(
            o,
            SHARE_ROOT=self.base,
            INCOMING_ROOT=self.incoming,
            MEDIA_ROOT=self.media,
            REPORTS_DIR=self.reports,
            ENCODING_FILE=self.reports / "encoding-preview.json",
            STASH_ROOT=str(self.base),
            STASH_ALIASES=[],
        )
        p.start()
        self.addCleanup(p.stop)
        self.settings = {"preferred_quality": q.config()}
        self.file1 = self.file(
            "1",
            "1",
            "original.mp4",
            size=400,
            codec="h264",
            rate=12000000,
            width=3840,
            height=2160,
        )
        self.file2 = self.file("2", "1", "preferred.mkv", size=200)
        self.row = self.row_value("1", [self.file1, self.file2])
        self.api = FakeAPI([self.row])
        p = patch.object(q, "probe", side_effect=self.probe)
        p.start()
        self.addCleanup(p.stop)

    def file(
        self,
        key,
        scene,
        name,
        size=200,
        codec="hevc",
        rate=5000000,
        width=1920,
        height=1080,
        duration=100,
    ):
        path = self.incoming / name
        path.write_bytes(b"x" * size)
        return dict(
            id=key,
            path=str(path),
            size=size,
            width=width,
            height=height,
            video_codec=codec,
            bit_rate=rate,
            duration=duration,
            frame_rate=24,
            fingerprints=[{"type": "md5", "value": "same-content"}],
            scenes=[{"id": scene}],
        )

    def row_value(self, key, files, **changes):
        row = dict(
            id=key,
            title="Example",
            files=files,
            urls=[],
            stash_ids=[],
            performers=[],
            tags=[],
            galleries=[],
            groups=[],
            custom_fields={},
            organized=False,
            play_duration=0,
            play_count=0,
            o_counter=0,
            play_history=[],
            o_history=[],
            updated_at="initial",
        )
        row.update(changes)
        return row

    def probe(self, path):
        file = self.api.file(
            next(
                f["id"]
                for row in self.api.rows.values()
                for f in row["files"]
                if f["path"] == str(path)
            )
        )
        return {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": file["video_codec"],
                    "width": file["width"],
                    "height": file["height"],
                    "bit_rate": str(file["bit_rate"]),
                    "pix_fmt": "yuv420p10le",
                }
            ],
            "format": {"duration": str(file["duration"])},
        }

    def document(self, method="attached"):
        return d.collect(o, self.api, method, self.settings)

    def mutations(self):
        return [call for call in self.api.calls if call[0].startswith("mutation")]

    def quarantine_one(self):
        self.api.rows["1"]["files"] = [
            self.api.rows["1"]["files"][1],
            self.api.rows["1"]["files"][0],
        ]
        document = self.document()
        d.quarantine(o, self.api, document, document["groups"][0], "1")
        return j.latest(self.reports / "duplicate-history.jsonl")[-1]

    def two_scenes(self):
        a = self.row_value(
            "1",
            [self.file1],
            tags=[{"id": "t1"}],
            performers=[{"id": "p1"}],
            urls=["a"],
            custom_fields={"a": "one"},
        )
        file = copy.deepcopy(self.file2)
        file["scenes"] = [{"id": "2"}]
        b = self.row_value(
            "2",
            [file],
            tags=[{"id": "t2"}],
            performers=[{"id": "p2"}],
            urls=["b"],
            custom_fields={"b": "two"},
        )
        self.api = FakeAPI([a, b])
        self.api.phash_groups = [copy.deepcopy([a, b])]
        return self.document("phash")


class LegacyAPI(FakeAPI):
    """Older schema: no Scene.production_date or VideoFile.scenes."""

    def gql(self, query, variables=None, **kwargs):
        variables = variables or {}
        if "production_date" in query or "fingerprints{type value} scenes{id}" in query:
            raise RuntimeError("Stash HTTP 422: unsupported field")
        if "__type" in query and variables.get("name"):
            self.calls.append((query, copy.deepcopy(variables)))
            return {"__type": {"fields": [{"name": "id"}]}}
        if "findScenes(" in query and "path" in variables:
            self.calls.append((query, copy.deepcopy(variables)))
            rows = [
                row
                for row in self.rows.values()
                if any(file["path"] == variables["path"] for file in row["files"])
            ]
            start = (variables["page"] - 1) * 100
            return {
                "findScenes": {
                    "scenes": [
                        {
                            "id": row["id"],
                            "files": [{"id": f["id"]} for f in row["files"]],
                        }
                        for row in rows[start : start + 100]
                    ]
                }
            }
        result = super().gql(query, variables, **kwargs)

        def strip(value):
            if isinstance(value, dict):
                value.pop("production_date", None)
                for file in value.get("files") or []:
                    file.pop("scenes", None)
                for nested in value.values():
                    strip(nested)
            elif isinstance(value, list):
                for nested in value:
                    strip(nested)

        strip(result)
        return result


class LegacyCompatibilityTests(DuplicateFixture):
    def setUp(self):
        super().setUp()
        self.api = LegacyAPI([self.row])

    def test_attached_scan_works_without_new_fields_and_is_read_only(self):
        document = self.document()
        self.assertEqual(document["groups"][0]["recommendation"], "2")
        self.assertEqual(self.mutations(), [])
        self.assertEqual(d.scene(self.api, "1"), document["groups"][0]["scenes"][0])
        self.assertEqual(sum("__type" in query for query, _ in self.api.calls), 2)

    def test_perceptual_scan_uses_compatible_selection(self):
        self.api.phash_groups = [[self.row]]
        self.assertEqual(len(self.document("phash")["groups"]), 1)

    def test_encoding_output_lookup_omits_unsupported_production_date(self):
        rows = d.features.exact_scenes(o, self.api, Path(self.file1["path"]))
        self.assertEqual([row["id"] for row in rows], ["1"])

    def test_legacy_primary_selection_still_checks_all_owners(self):
        document = self.document()
        shared = self.row_value("9", [copy.deepcopy(self.file1)])
        self.api.rows["9"] = shared
        with self.assertRaisesRegex(RuntimeError, "outside this group"):
            d.apply_primary(o, self.api, document, document["groups"][0], "2")
        self.assertEqual(self.mutations(), [])

    def test_legacy_quarantine_and_restore(self):
        event = self.quarantine_one()
        d.quarantine_action(o, self.api, event)
        self.assertTrue(Path(event["source"]).exists())

    def test_legacy_quarantine_and_delete(self):
        event = self.quarantine_one()
        d.quarantine_action(o, self.api, event, delete=True)
        self.assertFalse(Path(event["target"]).exists())

    def test_legacy_shared_file_blocks_quarantine(self):
        self.api.rows["1"]["files"].reverse()
        document = self.document()
        self.api.rows["9"] = self.row_value("9", [copy.deepcopy(self.file1)])
        with self.assertRaisesRegex(RuntimeError, "shared with other scenes"):
            d.quarantine(o, self.api, document, document["groups"][0], "1")
        self.assertTrue(Path(self.file1["path"]).exists())

    def test_owner_lookup_checks_ids_and_all_pages(self):
        self.api.rows = {
            str(i): self.row_value(str(i), [self.file1]) for i in range(101)
        }
        self.assertEqual(len(d.file_owners(self.api, self.file1)), 101)
        self.assertEqual(
            [args["page"] for query, args in self.api.calls if "path" in args], [1, 2]
        )
        wrong = dict(self.file1, id="999")
        with self.assertRaisesRegex(RuntimeError, "Could not verify"):
            d.file_owners(self.api, wrong)


class DuplicateTests(DuplicateFixture):
    def test_attached_scan_is_read_only_with_recommendation(self):
        document = self.document()
        group = document["groups"][0]
        self.assertEqual(group["recommendation"], "2")
        self.assertEqual(self.mutations(), [])
        self.assertEqual(group["basis"], ["attached files"])
        d.report(o, document)
        text = (self.reports / "duplicate-review.txt").read_text()
        self.assertIn("not space already freed", text)
        self.assertTrue(Path(self.file1["path"]).exists())

    def test_preference_modes_and_manual_duration_block(self):
        files = self.document()["groups"][0]["files"]
        self.assertEqual(d.recommend(files, "quality", q.config())[0], "1")
        self.assertEqual(d.recommend(files, "preferred", q.config())[0], "2")
        self.assertEqual(d.recommend(files, "smallest", q.config())[0], "2")
        self.assertIsNone(d.recommend(files, "manual", q.config())[0])
        files[0]["duration"] = 120
        self.assertIsNone(d.recommend(files, "preferred", q.config())[0])
        files[0]["duration"] = 0
        self.assertIsNone(d.recommend(files, "quality", q.config())[0])

    def test_smallest_mode_does_not_prefer_720p_when_1080p_available(self):
        files = self.document()["groups"][0]["files"]
        files[0].update(video_codec="hevc", bit_rate=5000000)
        files[1].update(width=1280, height=720)
        self.assertIsNone(d.recommend(files, "smallest", q.config())[0])

    def test_stale_metadata_blocks_primary(self):
        document = self.document()
        self.api.rows["1"]["title"] = "changed"
        self.assertRaises(
            RuntimeError,
            d.apply_primary,
            o,
            self.api,
            document,
            document["groups"][0],
            "2",
        )
        self.assertEqual(self.mutations(), [])

    def test_stale_file_blocks_primary(self):
        document = self.document()
        Path(self.file1["path"]).write_bytes(b"changed")
        self.assertRaises(
            RuntimeError,
            d.apply_primary,
            o,
            self.api,
            document,
            document["groups"][0],
            "2",
        )
        self.assertEqual(self.mutations(), [])

    def test_primary_updates_keep_both_files(self):
        document = self.document()
        d.apply_primary(o, self.api, document, document["groups"][0], "2")
        self.assertEqual(self.api.rows["1"]["files"][0]["id"], "2")
        self.assertTrue(Path(self.file1["path"]).exists())
        self.assertTrue(Path(self.file2["path"]).exists())
        self.assertEqual(
            j.latest(self.reports / "duplicate-history.jsonl")[0]["status"], "done"
        )

    def test_different_cuts_block_actions_after_fresh_probe(self):
        document = self.document()
        with patch.object(
            d, "verified_files", side_effect=RuntimeError("Different durations")
        ):
            self.assertRaises(
                RuntimeError,
                d.apply_primary,
                o,
                self.api,
                document,
                document["groups"][0],
                "2",
            )
        self.assertEqual(self.mutations(), [])

    def test_quarantine_primary_is_blocked(self):
        document = self.document()
        self.assertRaises(
            RuntimeError,
            d.quarantine,
            o,
            self.api,
            document,
            document["groups"][0],
            "1",
        )
        self.assertTrue(Path(self.file1["path"]).exists())

    def test_quarantine_updates_stash_and_retains_file(self):
        event = self.quarantine_one()
        self.assertEqual(event["status"], "quarantined")
        self.assertTrue(Path(event["target"]).exists())
        self.assertFalse(Path(event["source"]).exists())
        self.assertEqual(self.api.file("1")["path"], event["target"])
        self.assertTrue(Path(self.file2["path"]).exists())

    def test_restore_quarantine_uses_stash(self):
        event = self.quarantine_one()
        d.quarantine_action(o, self.api, event)
        self.assertTrue(Path(event["source"]).exists())
        self.assertFalse(Path(event["target"]).exists())
        self.assertEqual(
            j.latest(self.reports / "duplicate-history.jsonl")[-1]["status"], "restored"
        )

    def test_restore_refuses_occupied_original(self):
        event = self.quarantine_one()
        Path(event["source"]).write_bytes(b"keep")
        self.assertRaises(RuntimeError, d.quarantine_action, o, self.api, event)
        self.assertEqual(Path(event["source"]).read_bytes(), b"keep")

    def test_delete_only_quarantined_nonprimary_and_keeper_stays(self):
        event = self.quarantine_one()
        d.quarantine_action(o, self.api, event, delete=True)
        self.assertFalse(Path(event["target"]).exists())
        self.assertTrue(Path(self.file2["path"]).exists())
        self.assertEqual(self.api.rows["1"]["files"][0]["id"], "2")
        self.assertEqual(
            j.latest(self.reports / "duplicate-history.jsonl")[-1]["status"], "deleted"
        )

    def test_delete_refuses_changed_keeper(self):
        event = self.quarantine_one()
        Path(self.file2["path"]).write_bytes(b"changed")
        self.assertRaises(RuntimeError, d.quarantine_action, o, self.api, event, True)
        self.assertTrue(Path(event["target"]).exists())

    def test_delete_refuses_changed_quarantine_file(self):
        event = self.quarantine_one()
        Path(event["target"]).write_bytes(b"changed")
        self.assertRaises(RuntimeError, d.quarantine_action, o, self.api, event, True)
        self.assertTrue(Path(event["target"]).exists())

    def test_quarantine_refuses_shared_file(self):
        self.api.rows["1"]["files"][0]["scenes"].append({"id": "other"})
        self.api.rows["1"]["files"].reverse()
        document = self.document()
        self.assertRaises(
            RuntimeError,
            d.quarantine,
            o,
            self.api,
            document,
            document["groups"][0],
            "1",
        )
        self.assertTrue(Path(self.file1["path"]).exists())

    def test_phash_query_uses_distance_and_duration(self):
        doc = self.two_scenes()
        query, args = next(
            call for call in self.api.calls if "findDuplicateScenes" in call[0]
        )
        self.assertEqual(args, {"distance": 0, "diff": 2})
        self.assertEqual(len(doc["groups"]), 1)

    def test_merge_unions_metadata_and_keeps_files_and_histories(self):
        doc = self.two_scenes()
        d.apply_primary(o, self.api, doc, doc["groups"][0], "2", merge=True)
        self.assertEqual(set(self.api.rows), {"2"})
        row = self.api.rows["2"]
        self.assertEqual(row["files"][0]["id"], "2")
        self.assertEqual({v["id"] for v in row["tags"]}, {"t1", "t2"})
        self.assertEqual(row["custom_fields"], {"a": "one", "b": "two"})
        self.assertEqual(set(row["urls"]), {"a", "b"})
        args = next(args for query, args in self.mutations() if "sceneMerge" in query)[
            "input"
        ]
        self.assertTrue(args["play_history"])
        self.assertTrue(args["o_history"])
        self.assertTrue(Path(self.file1["path"]).exists())

    def test_cross_scene_primary_requires_explicit_merge(self):
        doc = self.two_scenes()
        self.assertRaises(
            RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2"
        )
        self.assertEqual(self.mutations(), [])

    def test_conflicting_metadata_blocks_merge_without_mutations(self):
        doc = self.two_scenes()
        self.api.rows["1"]["title"] = "Different"
        self.api.phash_groups = [copy.deepcopy(list(self.api.rows.values()))]
        doc = self.document("phash")
        self.assertIn("Conflicting title", doc["groups"][0]["merge_blocker"])
        self.assertRaises(
            RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2", True
        )
        self.assertEqual(self.mutations(), [])

    def test_merge_detects_lost_metadata_and_leaves_pending_history(self):
        doc = self.two_scenes()
        self.api.lose_metadata = True
        self.assertRaises(
            RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2", True
        )
        self.assertEqual(
            j.latest(self.reports / "duplicate-history.jsonl")[-1]["status"], "pending"
        )

    def test_exact_hash_matches_multiple_files_on_same_scene(self):
        doc = self.document("fingerprint")
        self.assertEqual(len(doc["groups"]), 1)
        self.assertEqual(doc["groups"][0]["basis"], ["exact md5"])

    def test_no_encode_suppression_without_review(self):
        doc = {"jobs": [{"source": self.file1["path"]}], "skipped": {}}
        d.suppress_redundant_encodes(o, doc, self.settings)
        self.assertEqual(len(doc["jobs"]), 1)

    def test_existing_verified_preferred_duplicate_suppresses_encode(self):
        review = self.document()
        d.report(o, review)
        doc = {"jobs": [dict(source=self.file1["path"], duration=100)], "skipped": {}}
        d.suppress_redundant_encodes(o, doc, self.settings)
        self.assertEqual(doc["jobs"], [])
        self.assertEqual(doc["skipped"]["existing preferred duplicate"], 1)

    def test_title_only_review_does_not_suppress_encode(self):
        review = self.document()
        review["groups"][0]["basis"] = ["same title"]
        d.report(o, review)
        doc = {"jobs": [dict(source=self.file1["path"], duration=100)], "skipped": {}}
        d.suppress_redundant_encodes(o, doc, self.settings)
        self.assertEqual(len(doc["jobs"]), 1)

    def test_unknown_bit_depth_does_not_suppress_encode(self):
        review = self.document()
        d.report(o, review)
        doc = {"jobs": [dict(source=self.file1["path"], duration=100)], "skipped": {}}
        with patch.object(
            q,
            "details",
            return_value=dict(
                codec="hevc",
                width=1920,
                height=1080,
                video_kbps=5000,
                duration=100,
                hdr=False,
                bit_depth=0,
            ),
        ):
            d.suppress_redundant_encodes(o, doc, self.settings)
        self.assertEqual(len(doc["jobs"]), 1)

    def test_metadata_conflicts_and_legacy_counters(self):
        rows = [self.row, self.row_value("2", [], custom_fields={"same": "a"})]
        rows[0] = dict(self.row, custom_fields={"same": "b"})
        self.assertRaises(RuntimeError, d.merge_values, rows, "1")
        rows[0] = dict(self.row, play_count=2)
        self.assertRaises(RuntimeError, d.merge_values, rows, "1")

    def test_option_validation(self):
        for value in [
            {"mode": "invalid"},
            {"phash_distance": -1},
            {"duration_diff": float("nan")},
        ]:
            self.assertRaises(RuntimeError, d.options, {"duplicates": value})


class MoveRecoveryTests(DuplicateFixture):
    def setUp(self):
        super().setUp()
        sleeper = patch.object(d.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def pending_move(self, restoring=False):
        event = self.quarantine_one()
        event["status"] = "restore_pending" if restoring else "pending"
        if restoring:
            self.api.move(event["file_id"], Path(event["source"]).parent)
        j.append(self.reports / "duplicate-history.jsonl", event)
        return event

    def latest_event(self):
        return j.latest(self.reports / "duplicate-history.jsonl")[-1]

    def stale_path(self, event, restoring=False):
        old_path = event["target"] if restoring else event["source"]
        for row in self.api.rows.values():
            for f in row["files"]:
                if str(f["id"]) == event["file_id"]:
                    f["path"] = old_path

    def scanning(self, event, update=True, restoring=False):
        original = self.api.gql

        def gql(query, variables=None, **kwargs):
            if "metadataScan(" not in query:
                return original(query, variables, **kwargs)
            self.api.calls.append((query, copy.deepcopy(variables)))
            expected = event["source"] if restoring else event["target"]
            self.assertEqual(variables["input"]["paths"], [o.stash_path(expected)])
            if update:
                for row in self.api.rows.values():
                    for f in row["files"]:
                        if str(f["id"]) == event["file_id"]:
                            f["path"] = expected
            return {"metadataScan": "repair-job"}

        return patch.object(self.api, "gql", side_effect=gql)

    def test_delayed_api_path_is_retried_without_scan(self):
        original = self.api.file
        calls = []

        def delayed(file_id):
            file = original(file_id)
            if file_id == "1" and d.QUARANTINE in file["path"]:
                calls.append(file_id)
                if len(calls) < 3:
                    file["path"] = self.file1["path"]
            return file

        with patch.object(self.api, "file", side_effect=delayed), patch.object(
            d.features, "wait_job"
        ) as wait:
            event = self.quarantine_one()
        self.assertEqual(event["status"], "quarantined")
        wait.assert_not_called()
        self.assertEqual(len(calls), 3)

    def test_recover_completed_move_does_not_move_again_and_is_idempotent(self):
        event = self.pending_move()
        with patch.object(self.api, "move") as move:
            self.assertEqual(d.recover_moves(o, self.api), 1)
            self.assertEqual(d.recover_moves(o, self.api), 0)
        move.assert_not_called()
        self.assertEqual(self.latest_event()["status"], "quarantined")
        self.assertTrue(Path(event["target"]).is_file())
        self.assertTrue(Path(event["keeper_path"]).is_file())

    def test_recover_stale_database_path_scans_only_moved_file(self):
        event = self.pending_move()
        self.stale_path(event)
        with self.scanning(event), patch.object(d.features, "wait_job") as wait:
            self.assertEqual(d.recover_moves(o, self.api), 1)
        wait.assert_called_once_with(self.api, "repair-job")
        self.assertEqual(self.latest_event()["status"], "quarantined")
        self.assertEqual(self.api.file(event["file_id"])["path"], event["target"])

    def test_recovery_with_legacy_scene_ownership_queries(self):
        event = self.pending_move()
        self.api = LegacyAPI(list(self.api.rows.values()))
        self.stale_path(event)
        with self.scanning(event), patch.object(d.features, "wait_job"):
            self.assertEqual(d.recover_moves(o, self.api), 1)
        self.assertEqual(self.latest_event()["status"], "quarantined")

    def test_database_size_mismatch_does_not_clear_pending(self):
        event = self.pending_move()
        for row in self.api.rows.values():
            for f in row["files"]:
                if f["id"] == event["file_id"]:
                    f["size"] += 1
        self.assertEqual(d.recover_moves(o, self.api), 0)
        self.assertEqual(self.latest_event()["status"], "pending")

    def test_quarantine_automatically_reconciles_a_stale_path(self):
        original_move = self.api.move

        def move(file_id, folder):
            old = self.api.file(file_id)["path"]
            result = original_move(file_id, folder)
            for row in self.api.rows.values():
                for f in row["files"]:
                    if f["id"] == file_id:
                        f["path"] = old
            return result

        original_gql = self.api.gql

        def gql(query, variables=None, **kwargs):
            if "metadataScan(" not in query:
                return original_gql(query, variables, **kwargs)
            event = self.latest_event()
            for row in self.api.rows.values():
                for f in row["files"]:
                    if f["id"] == event["file_id"]:
                        f["path"] = event["target"]
            return {"metadataScan": "repair-job"}

        with patch.object(self.api, "move", side_effect=move), patch.object(
            self.api, "gql", side_effect=gql
        ), patch.object(d.features, "wait_job"):
            event = self.quarantine_one()
        self.assertEqual(event["status"], "quarantined")

    def test_scan_that_does_not_reconcile_keeps_history_pending(self):
        event = self.pending_move()
        self.stale_path(event)
        with self.scanning(event, update=False), patch.object(d.features, "wait_job"):
            self.assertEqual(d.recover_moves(o, self.api), 0)
        self.assertEqual(self.latest_event()["status"], "pending")

    def test_failed_scan_keeps_history_pending(self):
        event = self.pending_move()
        self.stale_path(event)
        with self.scanning(event, update=False), patch.object(
            d.features, "wait_job", side_effect=RuntimeError("scan failed")
        ):
            self.assertEqual(d.recover_moves(o, self.api), 0)
        self.assertEqual(self.latest_event()["status"], "pending")

    def test_modified_target_blocks_scan_and_recovery(self):
        event = self.pending_move()
        self.stale_path(event)
        Path(event["target"]).write_bytes(b"changed")
        with patch.object(d.features, "wait_job") as wait:
            self.assertEqual(d.recover_moves(o, self.api), 0)
        wait.assert_not_called()
        self.assertEqual(self.latest_event()["status"], "pending")

    def test_recreated_source_blocks_scan_and_recovery(self):
        event = self.pending_move()
        self.stale_path(event)
        Path(event["source"]).write_bytes(b"new file")
        with patch.object(d.features, "wait_job") as wait:
            self.assertEqual(d.recover_moves(o, self.api), 0)
        wait.assert_not_called()

    def test_changed_keeper_blocks_recovery(self):
        event = self.pending_move()
        Path(event["keeper_path"]).write_bytes(b"changed")
        self.assertEqual(d.recover_moves(o, self.api), 0)
        self.assertEqual(self.latest_event()["status"], "pending")

    def test_shared_file_blocks_recovery(self):
        event = self.pending_move()
        for row in self.api.rows.values():
            for f in row["files"]:
                if f["id"] == event["file_id"]:
                    f["scenes"].append({"id": "other"})
        self.assertEqual(d.recover_moves(o, self.api), 0)

    def test_unmoved_attempt_can_be_cancelled_safely(self):
        event = self.pending_move()
        self.api.move(event["file_id"], Path(event["source"]).parent)
        self.assertEqual(d.recover_moves(o, self.api), 1)
        self.assertEqual(self.latest_event()["status"], "cancelled")
        self.assertTrue(Path(event["source"]).is_file())

    def test_interrupted_restore_can_be_recovered(self):
        event = self.pending_move(restoring=True)
        self.stale_path(event, restoring=True)
        with self.scanning(event, restoring=True), patch.object(d.features, "wait_job"):
            self.assertEqual(d.recover_moves(o, self.api), 1)
        self.assertEqual(self.latest_event()["status"], "restored")
        self.assertTrue(Path(event["source"]).is_file())

    def test_pending_move_blocks_another_cleanup(self):
        self.pending_move()
        with self.assertRaisesRegex(RuntimeError, "Recover pending"):
            d.require_recovered_moves(o)

    def test_recovery_refuses_a_different_file_id_at_same_path(self):
        self.pending_move()
        original = self.api.file

        def wrong_id(file_id):
            result = original(file_id)
            result["id"] = "unrelated"
            return result

        with patch.object(self.api, "file", side_effect=wrong_id):
            self.assertEqual(d.recover_moves(o, self.api), 0)
        self.assertEqual(self.latest_event()["status"], "pending")


class ExtraGuardTests(DuplicateFixture):
    def test_actions_share_the_encoding_queue_lock(self):
        import fcntl

        lock = o.ENCODING_FILE.with_suffix(".lock")
        doc = self.document()
        with lock.open("a") as output:
            fcntl.flock(output, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertRaises(
                RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2"
            )
        self.assertEqual(self.mutations(), [])

    def test_unknown_recommended_bit_depth_requires_manual_override(self):
        doc = self.document()
        with patch.object(
            d,
            "verified_files",
            return_value=[
                dict(d.info(f), bit_depth=0) for f in doc["groups"][0]["files"]
            ],
        ):
            self.assertRaises(
                RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2"
            )
        self.assertEqual(self.mutations(), [])

    def test_metadata_change_during_probe_blocks_primary(self):
        doc = self.document()

        def change(*args):
            self.api.rows["1"]["title"] = "Changed during inspection"
            return [dict(d.info(f), bit_depth=10) for f in doc["groups"][0]["files"]]

        with patch.object(d, "verified_files", side_effect=change):
            self.assertRaises(
                RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2"
            )
        self.assertEqual(self.mutations(), [])

    def test_hdr_blocks_primary(self):
        doc = self.document()
        with patch.object(
            q, "details", return_value=dict(d.info(self.file1), hdr=True)
        ):
            self.assertRaises(
                RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2"
            )
        self.assertEqual(self.mutations(), [])

    def test_wrong_duration_from_probe_blocks_actions(self):
        doc = self.document()
        values = [
            dict(d.info(self.file1), duration=100),
            dict(d.info(self.file2), duration=200),
        ]
        with patch.object(q, "details", side_effect=values):
            self.assertRaises(
                RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2"
            )
        self.assertEqual(self.mutations(), [])

    def test_changed_keeper_stash_path_blocks_delete(self):
        event = self.quarantine_one()
        new = self.incoming / "moved-keeper.mkv"
        Path(self.file2["path"]).rename(new)
        self.api.rows["1"]["files"][0]["path"] = str(new)
        self.assertRaises(RuntimeError, d.quarantine_action, o, self.api, event, True)
        self.assertTrue(Path(event["target"]).exists())

    def test_false_perceptual_chain_does_not_prove_pair(self):
        a = copy.deepcopy(self.file1)
        b = copy.deepcopy(self.file2)
        a["scenes"] = [{"id": "1"}]
        b["scenes"] = [{"id": "2"}]
        a["fingerprints"] = []
        b["fingerprints"] = []
        self.assertFalse(d.strong_pair(a, b, ["perceptual hash (distance 0)"]))

    def test_duplicate_primary_can_be_undone_without_deleting_media(self):
        import organizer_features as features

        doc = self.document()
        d.apply_primary(o, self.api, doc, doc["groups"][0], "2")
        event = j.latest(features.move_history_path(o))[-1]
        features.undo_record(o, self.api, event)
        self.assertEqual(self.api.rows["1"]["files"][0]["id"], "1")
        self.assertTrue(Path(self.file1["path"]).exists())
        self.assertTrue(Path(self.file2["path"]).exists())

    def test_stale_stash_size_makes_group_unavailable(self):
        self.api.rows["1"]["files"][0]["size"] = 999
        doc = self.document()
        self.assertIsNone(doc["groups"][0]["files"][0]["signature"])
        self.assertRaises(
            RuntimeError, d.apply_primary, o, self.api, doc, doc["groups"][0], "2"
        )
        self.assertEqual(self.mutations(), [])


if __name__ == "__main__":
    unittest.main()
