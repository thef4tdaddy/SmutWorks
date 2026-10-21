import builtins
import contextlib
import getpass
import queue
import sys
import unittest

import encoding_queue
import terminal_dashboard as d


class TaskConsoleTests(unittest.TestCase):
    def events(self, console):
        result = []
        while True:
            try:
                result.append(console.events.get_nowait())
            except queue.Empty:
                return result

    def test_split_writes_and_progress_lines(self):
        console = d.TaskConsole()
        console.write("one")
        console.write(" two\nprogress\rnext")
        console.flush()
        self.assertEqual(
            self.events(console),
            [("line", "one two"), ("line", "progress"), ("line", "next")],
        )

    def test_secret_prompt_never_logs_answer(self):
        console = d.TaskConsole()
        console.answers.put("private-key")
        self.assertEqual(console.ask("API key: ", hidden=True), "private-key")
        self.assertNotIn("private-key", repr(self.events(console)))

    def test_worker_restores_prompts_and_output_on_error(self):
        console = d.TaskConsole()
        original = (builtins.input, getpass.getpass, sys.stdout, sys.stderr)
        outcome = {}

        def task():
            print("started")
            raise RuntimeError("test failure")

        d.worker(console, task, outcome)
        self.assertEqual(
            (builtins.input, getpass.getpass, sys.stdout, sys.stderr), original
        )
        self.assertTrue(outcome["error"])
        self.assertIn(("line", "Action stopped: test failure"), self.events(console))

    def test_subprocess_output_is_captured_in_pane(self):
        console = d.TaskConsole()
        with contextlib.redirect_stdout(console):
            result = encoding_queue.execute_encode(
                [
                    sys.executable,
                    "-c",
                    'import sys; print("progress"); print("detail", file=sys.stderr)',
                ]
            )
        console.flush()
        lines = [value for event, value in self.events(console) if event == "line"]
        self.assertEqual(result.returncode, 0)
        self.assertIn("progress", lines)
        self.assertIn("detail", lines)

    def test_cancelled_worker_reports_stop(self):
        console = d.TaskConsole()
        console.cancelled.set()
        outcome = {}
        d.worker(console, lambda: print("not executed"), outcome)
        self.assertTrue(outcome["cancelled"])


if __name__ == "__main__":
    unittest.main()
