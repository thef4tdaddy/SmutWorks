"""Run terminal actions in a live output pane using only the Python standard library."""

import builtins
import collections
import contextlib
import curses
import getpass
import io
import queue
import re
import subprocess
import sys
import termios
import textwrap
import threading
from typing import Any, Deque, Dict, List, Optional, Tuple

import terminal_branding


class TaskConsole(io.TextIOBase):
    aco_capture_subprocess = True

    def __init__(self):
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.answers: "queue.Queue[Optional[str]]" = queue.Queue()
        self.cancelled = threading.Event()
        self.pending = ""

    def set_encoding_status(self, **values):
        self.events.put(("encoding_status", values))

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, text):
        if self.cancelled.is_set():
            raise KeyboardInterrupt
        # HandBrake progress uses carriage returns; treat these as fresh log lines.
        self.pending += str(text).replace("\r", "\n")
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if re.search(r"Encoding: task .*?\d+(?:\.\d+)?\s*%", line):
                match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
                self.set_encoding_status(
                    progress=line[line.index("Encoding:") :],
                    stage="Encoding",
                    percent=float(match.group(1)) if match else 0,
                )
                if line.startswith("Encoding:") and "[" not in line:
                    continue  # Progress stays pinned instead of flooding scrollback.
            self.events.put(("line", line))
        return len(text)

    def flush(self):
        if self.pending:
            self.events.put(("line", self.pending))
            self.pending = ""

    def ask(self, prompt="", hidden=False):
        self.flush()
        if self.cancelled.is_set():
            raise KeyboardInterrupt
        self.events.put(("prompt", (prompt, hidden)))
        answer = self.answers.get()
        if answer is None:
            raise EOFError
        # Secret answers are never sent to the output log.
        if not hidden:
            self.events.put(("line", prompt + answer))
        return answer


def worker(console, task, outcome):
    old_input, old_getpass = builtins.input, getpass.getpass
    try:
        builtins.input = console.ask
        getpass.getpass = lambda prompt="Password: ", stream=None: console.ask(
            prompt, hidden=True
        )
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
            outcome["result"] = task()
    except (KeyboardInterrupt, EOFError):
        outcome["cancelled"] = True
        console.set_encoding_status(stage="Stopped")
        console.events.put(
            (
                "line",
                "Task stopped. If a file move was in progress, check its location before retrying.",
            )
        )
    except Exception as error:
        outcome["error"] = True
        console.set_encoding_status(stage="Stopped: check log")
        message = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
        console.events.put(("line", "Action stopped: " + message))
    finally:
        builtins.input, getpass.getpass = old_input, old_getpass
        console.flush()
        console.events.put(("done", None))


def encoding_header(status, width):
    """Pinned lines are rendered separately from scrollback, including on errors."""
    filename = " ".join(str(status.get("filename", "")).split())
    lines = textwrap.wrap(
        "File {}/{}: {}".format(
            status.get("position", "?"), status.get("total", "?"), filename
        ),
        max(8, width),
    )
    # Keep a very long filename from consuming the whole pane.
    lines = lines[:2]
    percent = status.get("percent")
    if percent is not None:
        percent = max(0, min(100, float(percent)))
        slots = max(4, min(32, width - 20))
        filled = int(slots * percent / 100)
        lines.append(
            "Encode [{}{}] {:5.1f}%".format(
                "#" * filled, "-" * (slots - filled), percent
            )
        )
    lines.extend(
        textwrap.wrap(
            "Stage: {} | Completed: {} | Review: {}".format(
                status.get("stage", ""),
                status.get("completed", 0),
                status.get("failed", 0),
            ),
            max(8, width),
        )
    )
    if status.get("progress"):
        progress = re.sub(r"^Encoding: task \d+ of \d+,\s*", "", status["progress"])
        lines.extend(textwrap.wrap(progress, max(8, width))[:2])
    return lines


def run(task, items, selected, status=""):
    """Execute a callback with stdout, prompts and hidden input confined to a pane."""
    console = TaskConsole()
    outcome: Dict[str, Any] = {}
    original_terminal = termios.tcgetattr(sys.stdin.fileno())
    thread = threading.Thread(
        target=worker, args=(console, task, outcome), name="organizer-task"
    )

    def screen(window):
        window.keypad(True)
        window.timeout(100)
        # Receive Ctrl-C as a key; request cooperative cancellation and keep the UI
        # alive until any in-flight filesystem/API operation has returned.
        curses.raw()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        accent, highlight = curses.A_BOLD, curses.A_REVERSE
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_CYAN, background)
            curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE)
            accent = curses.color_pair(1) | curses.A_BOLD
            highlight = curses.color_pair(2) | curses.A_BOLD
        lines: Deque[str] = collections.deque(maxlen=4000)
        done, question, answer, scroll = False, None, "", 0
        encoding = {}
        revision = 0
        wrap_key = None
        wrapped: List[str] = []
        thread.start()
        while True:
            while True:
                try:
                    event, value = console.events.get_nowait()
                except queue.Empty:
                    break
                if event == "line":
                    lines.append(value)
                    revision += 1
                elif event == "encoding_status":
                    encoding.update(value)
                elif event == "prompt":
                    question, answer = value, ""
                elif event == "done":
                    done, question = True, None
            height, width = window.getmaxyx()
            window.erase()

            def write(y, x, text, style=0, limit=None):
                if not 0 <= y < height - 1:
                    return
                count = max(0, width - x - 1)
                if limit is not None:
                    count = min(count, limit)
                if count:
                    try:
                        window.addnstr(y, x, text, count, style)
                    except curses.error:
                        pass

            banner = terminal_branding.header(width, height, status)
            for offset, line in enumerate(banner):
                write(1 + offset, 2, line, accent if offset == 0 else curses.A_DIM)
            title_row = len(banner) + 2
            state = (
                "STOPPING..."
                if console.cancelled.is_set() and not done
                else (
                    (
                        "STOPPED"
                        if outcome.get("error") or outcome.get("cancelled")
                        else "FINISHED"
                    )
                    if done
                    else "WORKING..."
                )
            )
            split = width >= 80 and height >= 15
            x = min(47, width // 2) if split else 2
            body_top = title_row + 2
            body_rows = max(1, height - body_top - 5)
            if split:
                write(title_row, 2, "ACTIONS", accent)
                selected_index = next(
                    (i for i, item in enumerate(items) if item[0] == selected), 0
                )
                start = max(0, selected_index - body_rows + 1)
                for offset, (number, label) in enumerate(
                    items[start : start + body_rows]
                ):
                    write(
                        body_top + offset,
                        2,
                        "{} {:>2} {}".format(
                            ">" if number == selected else " ",
                            start + offset + 1,
                            label,
                        ).ljust(x - 5),
                        highlight if number == selected else curses.A_DIM,
                        x - 5,
                    )
                for y in range(title_row, height - 4):
                    try:
                        window.addch(y, x - 2, curses.ACS_VLINE)
                    except curses.error:
                        pass
            title = dict(items).get(selected, "Task")
            write(title_row, x, title, accent)
            write(title_row + 1, x, state, curses.A_DIM)
            available = max(8, width - x - 3)
            if wrap_key != (revision, available):
                wrapped = []
                for line in lines:
                    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).expandtabs(4)
                    wrapped.extend(
                        textwrap.wrap(line, available, replace_whitespace=False) or [""]
                    )
                wrap_key = (revision, available)
            log_top = body_top
            log_rows = body_rows
            if encoding.get("filename"):
                pinned = encoding_header(encoding, available)
                # Reserve log space even on smaller terminals.
                pinned = pinned[: max(1, body_rows - 1)]
                for offset, line in enumerate(pinned):
                    write(body_top + offset, x, line, accent)
                log_top += len(pinned) + 1
                log_rows = max(1, body_rows - len(pinned) - 1)
            end = max(0, len(wrapped) - scroll)
            start = max(0, end - log_rows)
            for offset, line in enumerate(wrapped[start:end]):
                write(log_top + offset, x, line)
            if question:
                prompt, hidden = question
                write(height - 4, x, prompt, accent)
                visible = "*" * len(answer) if hidden else answer
                write(
                    height - 3, x, "> " + visible[-max(1, available - 2) :], highlight
                )
                write(height - 2, 2, "ENTER Submit   ESC Cancel prompt", curses.A_DIM)
            else:
                write(
                    height - 3,
                    2,
                    (
                        "ENTER Menu   PgUp/PgDn Scroll   C Copy output"
                        if done
                        else "Live output   PgUp/PgDn Scroll   Ctrl-C Stop after current operation"
                    ),
                    accent,
                )
                if not done:
                    write(
                        height - 2,
                        2,
                        "The menu is paused while this task runs.",
                        curses.A_DIM,
                    )
            window.refresh()
            try:
                key = window.get_wch()
            except curses.error:
                continue
            if question:
                if key in ("\n", "\r", curses.KEY_ENTER):
                    console.answers.put(answer)
                    question, answer = None, ""
                elif key in ("\x1b", "\x03"):
                    console.answers.put(None)
                    question = None
                elif key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    answer = answer[:-1]
                elif key == "\x15":
                    answer = ""
                elif isinstance(key, str) and key.isprintable():
                    answer += key
            elif key in (curses.KEY_PPAGE, curses.KEY_UP):
                scroll = min(
                    max(0, len(wrapped) - 1),
                    scroll + (body_rows if key == curses.KEY_PPAGE else 1),
                )
            elif key in (curses.KEY_NPAGE, curses.KEY_DOWN):
                scroll = max(0, scroll - (body_rows if key == curses.KEY_NPAGE else 1))
            elif done and key in ("\n", "\r", "\x1b", "q", "Q", curses.KEY_ENTER):
                return
            elif done and key in ("c", "C"):
                subprocess.run(
                    ["/usr/bin/pbcopy"], input="\n".join(lines), text=True, check=False
                )
            elif key == "\x03" and not done:
                console.cancelled.set()
                lines.append("Stopping after the current operation returns...")
                revision += 1

    try:
        curses.wrapper(screen)
    finally:
        # Do not leave a mutating background task running after the dashboard closes.
        if thread.is_alive():
            console.cancelled.set()
            console.answers.put(None)
            thread.join()
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_terminal)
    return outcome.get("result")
