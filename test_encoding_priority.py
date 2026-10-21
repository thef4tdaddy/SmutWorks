import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import encoding_queue as q
import terminal_dashboard as d


class PriorityTests(unittest.TestCase):
    def job(self, name, size, duration, rate=12000):
        return dict(source=name, size=size, duration=duration, video_kbps=rate)

    def test_savings_beats_alphabetical_and_percentage(self):
        jobs = [
            self.job("a-short", 900_000_000, 600),
            self.job("z-long", 5_400_000_000, 3600),
        ]
        ranked = q.rank_jobs(jobs)
        self.assertEqual(ranked[0]["source"], "z-long")
        self.assertNotIn("estimated_savings_bytes", jobs[0])
        self.assertGreater(
            ranked[0]["estimated_savings_bytes"], ranked[1]["estimated_savings_bytes"]
        )

    def test_estimate_keeps_large_passthrough_audio_allowance(self):
        job = self.job("audio", 2_000_000_000, 1000, 12000)
        ranked = q.rank_jobs([job])[0]
        self.assertEqual(
            ranked["estimated_output_bytes"], int((625_000_000 + 500_000_000) * 1.01)
        )

    def test_settings_change_estimate_and_unknowns_sort_last(self):
        jobs = [self.job("unknown", 123, 0), self.job("known", 900_000_000, 600)]
        default = q.rank_jobs(jobs)
        lower = q.rank_jobs(jobs, {"video_kbps": 3000})
        self.assertEqual(default[-1]["source"], "unknown")
        self.assertIsNone(default[-1]["estimated_savings_bytes"])
        self.assertGreater(
            lower[0]["estimated_savings_bytes"], default[0]["estimated_savings_bytes"]
        )

    def test_csv_reports_savings(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = dict(
                self.job("source", 900_000_000, 600),
                output="out",
                width=1920,
                height=1080,
                codec="h264",
                reason="reduce bitrate",
            )
            file = Path(tmp) / "queue.csv"
            q.write_csv(file, {"jobs": q.rank_jobs([job])})
            self.assertIn("Estimated savings GiB", file.read_text())

    def test_console_progress_does_not_forget_filename(self):
        console = d.TaskConsole()
        console.set_encoding_status(filename="A long movie.mkv", position=3, total=25)
        console.write("Encoding: task 1 of 1, 40.71 % (12 fps, ETA 00h25m45s)\n")
        console.set_encoding_status(stage="Verifying output", progress="")
        status = {}
        while not console.events.empty():
            event, value = console.events.get_nowait()
            if event == "encoding_status":
                status.update(value)
        header = "\n".join(d.encoding_header(status, 60))
        self.assertIn("File 3/25: A long movie.mkv", header)
        self.assertIn("Verifying output", header)
        self.assertNotIn("ETA", header)

    def test_progress_eta_is_in_header_not_only_log(self):
        lines = d.encoding_header(
            dict(
                filename="video",
                position=1,
                total=4,
                stage="Encoding",
                progress="Encoding: task 1 of 1, 25.0 % (50 fps, ETA 00h10m00s)",
            ),
            50,
        )
        self.assertIn("ETA 00h10m00s", "\n".join(lines))

    def test_worker_failure_keeps_identifying_status(self):
        console = d.TaskConsole()

        def fail():
            q.encoding_status(
                filename="problem.mkv", position=4, total=10, stage="Verifying output"
            )
            raise RuntimeError("probe failed")

        d.worker(console, fail, {})
        status = {}
        while not console.events.empty():
            event, value = console.events.get_nowait()
            if event == "encoding_status":
                status.update(value)
        self.assertEqual(status["filename"], "problem.mkv")
        self.assertEqual(status["stage"], "Stopped: check log")


if __name__ == "__main__":
    unittest.main()


class ScanPriorityTests(unittest.TestCase):
    def test_filesystem_scan_ranks_after_discovery(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, size in [
                ("a-short.mp4", 900_000_000),
                ("z-large.mp4", 5_400_000_000),
            ]:
                with (root / name).open("wb") as out:
                    out.truncate(size)
                os.utime(root / name, (time.time() - 1200,) * 2)

            def probe(path):
                return {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 1920,
                            "height": 1080,
                            "bit_rate": "12000000",
                        }
                    ],
                    "format": {
                        "duration": "600" if path.name.startswith("a") else "3600"
                    },
                }

            with patch.object(q, "probe", side_effect=probe):
                result = q.scan(root, lambda name: name.startswith("."))
            self.assertEqual(Path(result["jobs"][0]["source"]).name, "z-large.mp4")

    def test_stash_scan_ranks_without_probing(self):
        import os
        import time
        from unittest.mock import Mock

        import adult_content_organizer as o
        import organizer_features as features

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenes = []
            for number, (name, size, duration) in enumerate(
                [
                    ("a-short.mp4", 900_000_000, 600),
                    ("z-large.mp4", 5_400_000_000, 3600),
                ]
            ):
                source = root / name
                with source.open("wb") as out:
                    out.truncate(size)
                os.utime(source, (time.time() - 1200,) * 2)
                scenes.append(
                    {
                        "id": str(number),
                        "files": [
                            {
                                "id": str(number),
                                "path": str(source),
                                "width": 1920,
                                "height": 1080,
                                "video_codec": "h264",
                                "bit_rate": 12000000,
                                "duration": duration,
                            }
                        ],
                    }
                )
            api = Mock()
            api.filtered_scenes.return_value = scenes
            with patch.object(o, "SHARE_ROOT", root), patch.object(q, "probe") as probe:
                document = features.stash_candidates(o, api, q.config(), [root])
            probe.assert_not_called()
            self.assertEqual(Path(document["jobs"][0]["source"]).name, "z-large.mp4")
