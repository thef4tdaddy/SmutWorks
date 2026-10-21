"""Six-section terminal navigation; original action IDs remain internal only."""

import curses
import os
import sys
import textwrap
from typing import Dict, Optional

SECTIONS = [
    ("Organize", ["3", "4", "19"]),
    ("Metadata", ["5", "6", "14"]),
    ("Duplicates", ["24", "25", "18", "26", "27", "28"]),
    ("Encoding", ["17", "10", "11", "12", "22", "23"]),
    ("Cleanup", ["7", "13", "8"]),
    ("Settings & History", ["1", "2", "15", "9", "16", "20", "21"]),
]
DESCRIPTIONS = [
    "Preview and apply organization, or retry Whisparr Sync for one scene.",
    "Review missing information, update managed flags, and run Stash cleanup filters.",
    "Find groups, compare files, select keepers, consolidate metadata, and review quarantine.",
    "Build savings-ranked queues, encode, and review completed outputs.",
    "Review leftovers, collect images, and remove still-empty folders.",
    "Configure connections and preferred quality, read reports, and inspect or undo recorded actions.",
]
LAST_SECTION: Optional[int] = None
LAST_CHOICES: Dict[int, int] = {}


def section_for(action):
    return next((i for i, (_, actions) in enumerate(SECTIONS) if action in actions), 5)


def sidebar(action, items):
    names = dict(items)
    return [
        (key, names[key]) for key in SECTIONS[section_for(action)][1] if key in names
    ]


class Navigation:
    def __init__(self, section=None, choices=None):
        self.section = section
        self.home = section if section is not None else 0
        self.choices = dict(choices or {})
        self.index = self.choices.get(section, 0) if section is not None else self.home
        self.typed = ""

    def count(self):
        return (
            len(SECTIONS[self.section][1])
            if self.section is not None
            else len(SECTIONS) + 1
        )

    def key(self, key):
        if key in ("UP", "DOWN", "HOME", "END"):
            self.index = (
                {
                    "UP": self.index - 1,
                    "DOWN": self.index + 1,
                    "HOME": 0,
                    "END": self.count() - 1,
                }[key]
            ) % self.count()
            self.typed = ""
        elif key in ("BACK", "QUIT"):
            if self.section is None:
                return "0"
            self.choices[self.section] = self.index
            self.home = self.section
            self.section = None
            self.index = self.home
            self.typed = ""
        elif key == "BACKSPACE":
            self.typed = self.typed[:-1]
        elif key.isdigit():
            self.typed = (self.typed + key)[-2:]
        elif key in ("ENTER", "RIGHT"):
            if self.typed:
                number = int(self.typed)
                self.typed = ""
                if number == 0:
                    return self.key("BACK")
                if not 1 <= number <= self.count():
                    return None
                self.index = number - 1
            if self.section is None:
                if self.index == len(SECTIONS):
                    return "0"
                self.section = self.index
                self.home = self.index
                self.index = self.choices.get(self.section, 0)
            else:
                self.choices[self.section] = self.index
                return SECTIONS[self.section][1][self.index]
        return None


def choose(settings, previous, items, descriptions, version):
    global LAST_SECTION, LAST_CHOICES
    names = dict(items)
    nav = Navigation(LAST_SECTION, LAST_CHOICES)

    def remember(action):
        global LAST_SECTION, LAST_CHOICES
        LAST_SECTION = nav.section
        LAST_CHOICES = nav.choices
        return action

    def fallback():
        while True:
            print(
                "\nSmutWorks "
                + version
                + " / "
                + (SECTIONS[nav.section][0] if nav.section is not None else "Main menu")
            )
            labels = (
                [names[k] for k in SECTIONS[nav.section][1]]
                if nav.section is not None
                else [s[0] for s in SECTIONS] + ["Exit"]
            )
            for index, label in enumerate(labels, 1):
                print("{}  {}".format(index, label))
            value = input("Selection (0 goes back/exits): ").strip()
            if value.casefold() in ("q", "back", ""):
                action = nav.key("BACK")
            elif value.isdigit():
                nav.typed = value
                action = nav.key("ENTER")
            else:
                continue
            if action is not None:
                return remember(action)

    if (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or os.environ.get("TERM") == "dumb"
    ):
        return fallback()

    def screen(window):
        window.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while True:
            window.erase()
            height, width = window.getmaxyx()

            def write(y, x, text, style=0, limit=None):
                if not 0 <= y < height - 1:
                    return
                available = max(0, width - x - 1)
                if limit is not None:
                    available = min(available, limit)
                if available:
                    try:
                        window.addnstr(y, x, str(text), available, style)
                    except curses.error:
                        pass

            section = nav.section
            labels = (
                [names[k] for k in SECTIONS[section][1]]
                if section is not None
                else [s[0] for s in SECTIONS] + ["Exit"]
            )
            write(1, 2, "SMUTWORKS  " + version, curses.A_BOLD)
            write(
                2,
                2,
                "Main menu"
                + ("  /  " + SECTIONS[section][0] if section is not None else ""),
                curses.A_DIM,
            )
            write(
                4,
                2,
                (
                    ("Stash: " + settings["stash_url"])
                    if settings
                    else "Set up your connection in Settings & History."
                ),
                curses.A_DIM,
            )
            rows = max(1, height - 10)
            start = max(0, nav.index - rows + 1)
            split = width >= 90
            column = min(45, width // 2) if split else width - 3
            for offset, label in enumerate(labels[start : start + rows]):
                index = start + offset
                write(
                    6 + offset,
                    2,
                    "{} {}  {}".format(
                        ">" if nav.index == index else " ", index + 1, label
                    ),
                    curses.A_REVERSE if nav.index == index else 0,
                    column - 3,
                )
            if section is None:
                description = (
                    DESCRIPTIONS[nav.index]
                    if nav.index < len(SECTIONS)
                    else "Close SmutWorks."
                )
            else:
                description = descriptions[SECTIONS[section][1][nav.index]][1]
            if split:
                for offset, line in enumerate(
                    textwrap.wrap(description, max(10, width - column - 6))[
                        : max(1, height - 12)
                    ]
                ):
                    write(6 + offset, column + 2, line)
            write(
                height - 3,
                2,
                "↑/↓ Move   Enter/→ Open   Esc/← Back   Q "
                + ("Exit" if section is None else "Back"),
                curses.A_BOLD,
            )
            write(
                height - 2,
                2,
                (
                    "Selection: " + nav.typed
                    if nav.typed
                    else "Numbers are local to this menu."
                ),
                curses.A_DIM,
            )
            window.refresh()
            key = window.getch()
            mapping = {
                curses.KEY_UP: "UP",
                curses.KEY_DOWN: "DOWN",
                curses.KEY_LEFT: "BACK",
                curses.KEY_RIGHT: "RIGHT",
                curses.KEY_HOME: "HOME",
                curses.KEY_END: "END",
                curses.KEY_ENTER: "ENTER",
                10: "ENTER",
                13: "ENTER",
                27: "BACK",
                ord("q"): "QUIT",
                ord("Q"): "QUIT",
                curses.KEY_BACKSPACE: "BACKSPACE",
                127: "BACKSPACE",
                8: "BACKSPACE",
            }
            if key == 3:
                raise KeyboardInterrupt
            action = nav.key(mapping.get(key, chr(key) if 0 <= key < 256 else ""))
            if action is not None:
                return remember(action)

    try:
        return curses.wrapper(screen)
    except curses.error:
        return fallback()
