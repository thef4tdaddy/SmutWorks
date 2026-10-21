import copy
import json
from pathlib import Path
from unittest.mock import patch

import adult_content_organizer as o
import duplicate_batch as batch
import duplicate_review as d
import organizer_journal as journal
from test_duplicate_review import DuplicateFixture, LegacyAPI


class BatchTests(DuplicateFixture):
    def plan(self):
        return batch.build_plan(self.document(), self.settings)

    def test_preview_is_read_only_and_lists_keep_and_extras(self):
        plan = self.plan()
        self.assertEqual(batch.report_plan(o, plan), 1)
        self.assertEqual(plan["entries"][0]["group"]["recommendation"], "2")
        text = (self.reports / "duplicate-bulk-plan.txt").read_text()
        self.assertIn("KEEP file 2", text)
        self.assertIn("QUARANTINE file 1", text)
        self.assertEqual(self.mutations(), [])

    def test_batch_sets_keeper_quarantines_and_can_restore(self):
        result = batch.apply_plan(o, self.api, self.plan())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["quarantined_bytes"], 400)
        self.assertEqual(result["primary_changes"], 1)
        self.assertEqual(result["keepers"], 1)
        self.assertEqual(self.api.rows["1"]["files"][0]["id"], "2")
        self.assertTrue(Path(self.file2["path"]).exists())
        events = journal.latest(self.reports / "duplicate-history.jsonl")
        quarantines = [e for e in events if e["kind"] == "duplicate_quarantine"]
        self.assertEqual(quarantines[0]["status"], "quarantined")
        restored = batch.manage_quarantine(o, self.api, quarantines)
        self.assertEqual(restored["status"], "complete")
        self.assertTrue(Path(self.file1["path"]).exists())

    def test_completion_summary_shows_totals_in_terminal(self):
        import contextlib
        import io

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            batch.apply_plan(o, self.api, self.plan())
        text = output.getvalue()
        self.assertIn("Groups completed: 1 / 1 planned", text)
        self.assertIn("Extra files quarantined: 1", text)
        self.assertIn("Primary files changed: 1", text)
        self.assertIn("Space freed: 0 GiB", text)

    def test_individual_manual_override_is_preserved_in_bulk(self):
        document = self.document()
        document["groups"][0]["mode"] = "manual"
        plan = batch.build_plan(document, self.settings)
        self.assertIsNotNone(plan["entries"][0]["issue"])

    def test_multiple_extras_do_not_need_rescans_between_moves(self):
        third = self.file("3", "1", "third.mp4", size=300)
        self.api.rows["1"]["files"].append(third)
        result = batch.apply_plan(o, self.api, self.plan())
        self.assertEqual(result["quarantined"], 2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.api.rows["1"]["files"][0]["id"], "2")

    def test_unavailable_file_is_not_recommended_or_counted_as_savings(self):
        Path(self.file2["path"]).unlink()
        document = self.document()
        self.assertEqual(document["groups"][0]["recommendation"], "1")
        d.report(o, document)
        self.assertIn(
            "Potential reduction after review/removal: 0.000 GiB",
            (self.reports / "duplicate-review.txt").read_text(),
        )
        plan = batch.build_plan(document, self.settings)
        self.assertIn("Unavailable", plan["entries"][0]["issue"])
        self.assertEqual(batch.report_plan(o, plan), 0)
        self.assertEqual(self.mutations(), [])

    def test_closest_existing_h264_copy_can_be_kept_without_encoding(self):
        self.api.rows["1"]["files"][1]["video_codec"] = "h264"
        result = batch.apply_plan(o, self.api, self.plan())
        self.assertEqual(result["quarantined"], 1)

    def test_stale_plan_skips_without_mutations(self):
        plan = self.plan()
        self.api.rows["1"]["title"] = "changed after scan"
        result = batch.apply_plan(o, self.api, plan)
        self.assertEqual(result["entries"][0]["status"], "skipped")
        self.assertEqual(self.mutations(), [])

    def test_unexpected_hook_change_stops_after_primary_before_any_move(self):
        plan = self.plan()
        original = self.api.gql

        def changed(query, variables=None, **kwargs):
            result = original(query, variables, **kwargs)
            if "sceneUpdate(" in query:
                self.api.rows["1"]["title"] = "hook edited title"
            return result

        with patch.object(self.api, "gql", side_effect=changed):
            result = batch.apply_plan(o, self.api, plan)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["quarantined"], 0)
        self.assertTrue(Path(self.file1["path"]).exists())

    def test_cancelled_prompt_does_not_apply(self):
        o.save_document(self.reports / "duplicate-review.json", self.document())
        with patch("builtins.input", return_value=""), patch.object(
            batch, "apply_plan"
        ) as apply:
            batch.handle("29", o, self.api, self.settings)
        apply.assert_not_called()

    def test_separate_scenes_and_manual_preferences_stay_out_of_batch(self):
        document = self.two_scenes()
        plan = batch.build_plan(document, self.settings)
        self.assertIn("Separate", plan["entries"][0]["issue"])
        self.settings["duplicates"] = {"mode": "manual"}
        self.api.rows = {"1": copy.deepcopy(self.row)}
        self.assertIsNotNone(self.plan()["entries"][0]["issue"])

    def test_legacy_schema_batch_and_later_bulk_delete(self):
        self.api = LegacyAPI([self.row])
        result = batch.apply_plan(o, self.api, self.plan())
        self.assertEqual(result["quarantined"], 1)
        entries = [
            e
            for e in journal.latest(self.reports / "duplicate-history.jsonl")
            if e["kind"] == "duplicate_quarantine"
        ]
        deleted = batch.manage_quarantine(o, self.api, entries, delete=True)
        self.assertEqual(deleted["status"], "complete")
        self.assertTrue(Path(self.file2["path"]).exists())
        self.assertFalse(Path(entries[0]["target"]).exists())

    def test_failure_retains_pending_history_and_stops_batch(self):
        self.api.fail_move = True
        result = batch.apply_plan(o, self.api, self.plan())
        self.assertEqual(result["status"], "stopped")
        events = journal.latest(self.reports / "duplicate-history.jsonl")
        self.assertTrue(any(e["status"] == "pending" for e in events))
        saved = json.loads((self.reports / "duplicate-bulk-results.json").read_text())
        self.assertEqual(saved["status"], "stopped")

    def test_excluded_scene_is_counted_and_explained(self):
        hidden = self.incoming / ".hidden"
        hidden.mkdir()
        source = Path(self.file2["path"])
        target = hidden / source.name
        source.rename(target)
        self.api.rows["1"]["files"][1]["path"] = str(target)
        document = self.document()
        self.assertEqual(document["groups"], [])
        self.assertEqual(document["scan_summary"]["attached_scene_candidates"], 1)
        self.assertEqual(document["scan_summary"]["excluded_groups"], 1)
        d.report(o, document)
        self.assertIn(
            "EXCLUDED GROUPS", (self.reports / "duplicate-review.txt").read_text()
        )

    def test_batch_respects_existing_encoding_lock(self):
        import fcntl

        lock_path = o.ENCODING_FILE.with_suffix(".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "encoding queue"):
                batch.apply_plan(o, self.api, self.plan())
        self.assertEqual(self.mutations(), [])

    def test_two_database_records_for_one_path_are_not_treated_as_two_copies(self):
        first, second = self.api.rows["1"]["files"]
        second.update(path=first["path"], size=first["size"])
        plan = self.plan()
        self.assertIn("same local path", plan["entries"][0]["issue"])
        document = self.document()
        with self.assertRaisesRegex(RuntimeError, "same file path"):
            d.quarantine(o, self.api, document, document["groups"][0], "2")
        self.assertTrue(Path(first["path"]).exists())
