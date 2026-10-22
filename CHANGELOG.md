# Changelog

## 0.1.0-alpha.5

- Add adaptive ASCII branding and pinned per-file progress bars; keep progress updates out of log scrollback.
- Preserve ffprobe diagnostics and retry failed output reads. Retain failed outputs and continue independent jobs, stopping when the source/share becomes unavailable.
- Record encode attempts beside their sources so verification can be retried without running HandBrake again. Exclude known receipts, partials, existing targets, and embedded HandBrake outputs from encoding discovery.
- Add verification-only recovery across both roots, including explicitly selected older partials. Keep originals, validate source/output fingerprints and codec/runtime/resolution, and protect existing target files.

## 0.1.0-alpha.4

- Show scan and completion totals directly in the terminal, including counts, data sizes, skipped groups, keeper changes, and remaining work.
- Add bulk duplicate cleanup plans and one-confirmation quarantine, plus separate bulk restore/deletion. Reuse live ownership, metadata, and media guards; stop unexpected changes and journal progress.
- Exclude unavailable copies from keeper recommendations and savings estimates. Report scan counts and excluded groups, and allow the closest existing keeper even when it needs a later encode.

## 0.1.0-alpha.3

- Adapt duplicate scans and encode-output lookup to older Stash schemas without production dates or direct file ownership fields. Verify ownership through native scene-path queries before file changes.
- Add pinned Ruff, Black, mypy, and commitlint development checks, local pre-commit/commit-msg hooks, and GitHub Actions enforcement. Format the existing Python code and add collection annotations for gradual type checking.
- Remove the redundant Start Organizer.command launcher; use Start SmutWorks.command.
- Show GraphQL validation messages for HTTP errors, including duplicate scans rejected with HTTP 422.
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
