# Changelog

## 0.1.0-alpha.1

- Import the existing organizer, cleanup tools, Stash integration, encoding runner, and review/history features.
- Name the application SmutWorks; retain the old entry point and existing private settings.
- Rank filesystem and Stash encoding queues by estimated GiB saved, including previously saved queues at execution time.
- Add estimated output size, savings, and percentage to CSV reports.
- Pin the current filename, queue position, progress/speed/ETA, completed count, and processing stage above the scrolling terminal log.
- Preserve the pinned filename after failures for easier troubleshooting.
