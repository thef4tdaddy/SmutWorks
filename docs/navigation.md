# Menus

SmutWorks now has six main sections. Actions retain stable internal identifiers for compatibility, but the displayed numbers are local to each submenu.

| Main section | Actions |
| --- | --- |
| Organize | Preview organization, move the saved preview, retry Whisparr Sync |
| Metadata | Preview/apply metadata flags, Stash diagnostic filters |
| Duplicates | Find groups, bulk cleanup, individual review/keepers, compare files, bulk quarantine restore/deletion, individual quarantine actions, settings |
| Encoding | Build a Stash or filesystem queue, start encoding, copy a command, review/promote outputs |
| Cleanup | Scan leftovers, collect images, remove empty folders |
| Settings & History | Connection/paths, connection test, preferred quality, reports, history, supported undo |

- **Up/Down:** select an item.
- **Enter/Right:** open a section or action.
- **Esc/Left:** return to the main menu; at the main menu this exits.
- **Q:** go back from a submenu, or exit from the main menu.
- **Number + Enter:** select a local menu item. **0** goes back/exits.

The last section and highlighted action are remembered during the current session. The task pane shows only actions from the active section. While a task runs, its menu is paused; PgUp/PgDn scroll its output.

During encoding, filename, queue position, processing stage, completion count, percentage, speed, and ETA remain pinned above the scrolling log. On short terminals, fewer header lines are shown. The filename remains available after an error.

The plain-terminal fallback uses the same section/submenu hierarchy.
