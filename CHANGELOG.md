# Changelog

## 0.1.0-alpha.3

- Replace the flat action menu with six sections, back navigation, local shortcuts, section-specific task sidebars, and session selection memory.
- Share one Preferred quality profile across encoding, candidate discovery, and duplicate recommendations. Migrate older encoding settings; preserve saved queue targets.
- Add optional minimum estimated savings in MiB and percent.
- Find duplicate groups through Stash perceptual matches, stored MD5/SHA256 fingerprints, Stash IDs, URLs, titles, and attached files.
- Add explained quality/preferred/smallest/manual recommendations, duration checks, and advisory savings reports.
- Add guarded native primary selection and metadata consolidation, explicit quarantine/restore, and separate deletion of a recorded quarantined non-primary file.
- Journal duplicate operations and metadata snapshots; verify files/metadata and share the encoding lock for file changes.
- Avoid redundant encodes when an existing duplicate copy passes fresh preferred-quality checks; weak matches never suppress encoding.
- Add regression tests for navigation, profile migration, recommendations, metadata retention, and file-operation guards.

## 0.1.0-alpha.2

- Add a standard Python package and `smutworks` command with help, version, and directory overrides.
- Configure local-to-Stash root mapping and optional equivalent path aliases instead of assuming fixed Docker mounts.
- Configure performer genders, organization subfolder, managed tag prefix, and optional StashDB delegation.
- Use generic defaults for new installations while preserving existing settings and organization behavior.
- Store new reports/history outside the application checkout and preserve existing report directories.
- Open reports using the system default application.
- Add example settings, setup/migration documentation, and regression tests.

## 0.1.0-alpha.1

- Import the existing organizer, cleanup tools, Stash integration, encoding runner, and review/history features.
- Name the application SmutWorks; retain the old entry point and existing private settings.
- Rank filesystem and Stash encoding queues by estimated GiB saved, including previously saved queues at execution time.
- Add estimated output size, savings, and percentage to CSV reports.
- Pin the current filename, queue position, progress/speed/ETA, completed count, and processing stage above the scrolling terminal log.
- Preserve the pinned filename after failures for easier troubleshooting.
