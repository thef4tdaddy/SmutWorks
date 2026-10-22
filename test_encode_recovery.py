import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import encode_recovery as recovery
import encoding_queue as q
import terminal_dashboard as dashboard

OUTPUT = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "hevc",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p10le",
        }
    ],
    "format": {"duration": "120", "bit_rate": "5000000"},
}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = []
        for i in range(4):
            source = self.root / ("scene{}.mp4".format(i))
            source.write_bytes(b"original")
            os.utime(source, (time.time() - 1200,) * 2)
            stat = source.stat()
            self.jobs.append(
                {
                    "source": str(source),
                    "output": str(self.root / (source.stem + " - reencoded.mkv")),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "duration": 120,
                }
            )
        self.document = {
            "version": 1,
            "root": str(self.root),
            "target": q.config(),
            "jobs": self.jobs,
        }
        for name, value in (
            ("executable", "HandBrakeCLI"),
            ("encoder_for", "vt_h265_10bit"),
        ):
            mocked = patch.object(q, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        sleeper = patch.object(q.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def encode(self, arguments):
        Path(arguments[arguments.index("-o") + 1]).write_bytes(b"completed-video")
        return SimpleNamespace(returncode=0)

    def test_third_verification_failure_retains_partial_and_fourth_still_finishes(self):
        def probe(path):
            if ".scene2." in Path(path).name:
                raise RuntimeError("NAS read failed: Input/output error")
            return OUTPUT

        with patch.object(
            q, "execute_encode", side_effect=self.encode
        ) as encode, patch.object(q, "probe", side_effect=probe):
            result = q.run(self.document)
        self.assertEqual(encode.call_count, 4)
        self.assertIn("3 encoded/recovered", result)
        self.assertIn("1 failed or awaiting verification", result)
        receipt = recovery.load(Path(self.jobs[2]["source"]))
        self.assertTrue(receipt["handbrake_completed"])
        self.assertIn("Input/output error", receipt["error"])
        self.assertTrue(Path(receipt["partial"]).exists())
        self.assertFalse(Path(self.jobs[2]["output"]).exists())
        with patch.object(q, "execute_encode") as encode, patch.object(
            q, "probe", return_value=OUTPUT
        ):
            result = q.run(self.document)
        encode.assert_not_called()
        self.assertIn("1 encoded/recovered", result)
        self.assertTrue(Path(self.jobs[2]["output"]).exists())
        self.assertTrue(
            all(Path(j["source"]).read_bytes() == b"original" for j in self.jobs)
        )

    def test_transient_verification_error_is_retried(self):
        self.document["jobs"] = self.jobs[:1]
        with patch.object(q, "execute_encode", side_effect=self.encode), patch.object(
            q, "probe", side_effect=[RuntimeError("temporarily unavailable"), OUTPUT]
        ) as probe:
            result = q.run(self.document)
        self.assertEqual(probe.call_count, 2)
        self.assertIn("1 encoded/recovered", result)

    def test_pending_source_is_not_encoded_again_by_scan(self):
        self.document["jobs"] = self.jobs[:1]
        with patch.object(q, "execute_encode", side_effect=self.encode), patch.object(
            q, "probe", side_effect=RuntimeError("read failed")
        ):
            q.run(self.document)
        with patch.object(q, "probe", return_value=OUTPUT):
            document = q.scan(self.root, lambda n: n.startswith("."))
        self.assertNotIn(
            self.jobs[0]["source"], [j["source"] for j in document["jobs"]]
        )
        found = recovery.discover(
            q, [self.root], q.config(), lambda n: n.startswith(".")
        )
        self.assertEqual(len(found["jobs"]), 1)

    def test_completed_receipt_blocks_source_after_output_is_renamed(self):
        self.document["jobs"] = self.jobs[:1]
        with patch.object(q, "execute_encode", side_effect=self.encode), patch.object(
            q, "probe", return_value=OUTPUT
        ):
            q.run(self.document)
        Path(self.jobs[0]["output"]).rename(self.root / "organized-name.mkv")
        self.assertIn(
            "previously encoded", recovery.skip_reason(Path(self.jobs[0]["source"]))
        )

    def test_handbrake_metadata_is_skipped_even_with_plain_filename(self):
        data = copy.deepcopy(OUTPUT)
        data["format"]["tags"] = {"ENCODER": "HandBrake 1.10.0"}
        self.assertIn("already encoded by HandBrake", q.reason(q.details(data))[1])

    def test_source_change_blocks_recovery(self):
        self.document["jobs"] = self.jobs[:1]
        with patch.object(q, "execute_encode", side_effect=self.encode), patch.object(
            q, "probe", side_effect=RuntimeError("read failed")
        ):
            q.run(self.document)
        source = Path(self.jobs[0]["source"])
        source.write_bytes(b"changed source")
        with patch.object(q, "probe", return_value=OUTPUT):
            with self.assertRaisesRegex(RuntimeError, "Source changed"):
                recovery.finalize(q, source, recovery.load(source))

    def test_old_partial_requires_explicit_adoption_and_does_not_run_handbrake(self):
        self.document["jobs"] = self.jobs[:1]
        source = Path(self.jobs[0]["source"])
        partial = self.root / ("." + source.stem + ".aco-partial-old.mkv")
        partial.write_bytes(b"old output")
        with patch("builtins.input", side_effect=["1", "RECOVER"]), patch.object(
            q, "probe", return_value=OUTPUT
        ), patch.object(q, "execute_encode") as encode:
            result = recovery.recover(q, self.document)
        self.assertIn("1 outputs recovered", result)
        encode.assert_not_called()
        self.assertTrue(Path(self.jobs[0]["output"]).exists())

    def test_existing_target_is_not_overwritten_by_recovery(self):
        self.document["jobs"] = self.jobs[:1]
        with patch.object(q, "execute_encode", side_effect=self.encode), patch.object(
            q, "probe", side_effect=RuntimeError("read failed")
        ):
            q.run(self.document)
        source = Path(self.jobs[0]["source"])
        target = Path(self.jobs[0]["output"])
        target.write_bytes(b"keep")
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            recovery.finalize(q, source, recovery.load(source))
        self.assertEqual(target.read_bytes(), b"keep")

    def test_progress_bar_is_pinned_and_progress_does_not_flood_log(self):
        console = dashboard.TaskConsole()
        console.write("Encoding: task 1 of 1, 50.00 % (12 fps, ETA 00h01m00s)\n")
        events = list(console.events.queue)
        self.assertFalse(any(kind == "line" for kind, _ in events))
        status = next(value for kind, value in events if kind == "encoding_status")
        self.assertEqual(status["percent"], 50)
        self.assertIn("50.0%", "\n".join(dashboard.encoding_header(status, 60)))

    def test_probe_timeout_reports_file_and_timeout(self):
        import subprocess

        with patch.object(
            q.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffprobe", 60)
        ):
            with self.assertRaisesRegex(
                RuntimeError, "timed out after 60 seconds: example.mkv"
            ):
                q.probe(Path("example.mkv"))

    def test_probe_error_preserves_diagnostic(self):
        with patch.object(
            q.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=1, stderr="Input/output error", stdout=""
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "example.mkv: Input/output error"
            ):
                q.probe(Path("example.mkv"))

    def test_old_partial_cancel_leaves_original_and_partial_unmodified(self):
        self.document["jobs"] = self.jobs[:1]
        source = Path(self.jobs[0]["source"])
        partial = self.root / ("." + source.stem + ".aco-partial-old.mkv")
        partial.write_bytes(b"unverified")
        with patch("builtins.input", return_value=""):
            result = recovery.recover(q, self.document)
        self.assertIn("0 outputs recovered", result)
        self.assertTrue(partial.exists())
        self.assertFalse(recovery.receipt_path(source).exists())

    def test_publish_failure_removes_only_its_empty_reservation(self):
        self.document["jobs"] = self.jobs[:1]
        with patch.object(q, "execute_encode", side_effect=self.encode), patch.object(
            q, "probe", side_effect=RuntimeError("read failed")
        ):
            q.run(self.document)
        source = Path(self.jobs[0]["source"])
        receipt = recovery.load(source)
        original_replace = os.replace

        def replace(before, after):
            if str(before) == receipt["partial"]:
                raise OSError("NAS publish failed")
            return original_replace(before, after)

        with patch.object(recovery.os, "replace", side_effect=replace), patch.object(
            q, "probe", return_value=OUTPUT
        ):
            with self.assertRaisesRegex(OSError, "NAS publish failed"):
                recovery.finalize(q, source, receipt)
        self.assertFalse(Path(self.jobs[0]["output"]).exists())
        self.assertTrue(Path(receipt["partial"]).exists())
