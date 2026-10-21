# Configuration

Run `smutworks` (or `python3 smutworks.py`) and choose **Connection and folder settings**. Stash and your files may be on different machines, but the files must be accessible to the machine running SmutWorks.

## Local paths and Stash paths

The local media root is a common parent of your incoming and organized folders. It can be a mounted NAS share, an external disk, or local storage.

For example:

| Setting | Example |
| --- | --- |
| Local media root | `/Volumes/Media` |
| Incoming relative folder | `incoming` |
| Organized relative folder | `library/scenes` |
| Same root as seen by Stash | `/data` |
| Optional aliases | none |

This maps local `/Volumes/Media/incoming/video.mp4` to Stash `/data/incoming/video.mp4`. A native Stash move to local `/Volumes/Media/library/scenes/Performer/unlinked` uses `/data/library/scenes/Performer/unlinked` as its API destination.

**Aliases must refer to the same entire root.** If Stash has two mounts of that root, such as `/data` and `/legacy-media`, add `/legacy-media` as an alias. It is not an alias if it points to only the incoming subfolder or a different share. Enter `-` to clear aliases. Most installations need just one Stash root.

Mappings apply to discovery, native moves, output lookups, primary-file selection, and history checks. This version supports one common local root with one primary Stash root and optional equivalent aliases. Arbitrary mappings between several independent local roots are not supported yet.

## Organization policy

- **Eligible performer genders:** `*` accepts any named performer. Alternatively enter Stash gender names separated by commas, such as `FEMALE,TRANSGENDER_FEMALE` or `MALE`. The alphabetically first eligible performer determines the folder.
- **Organization subfolder:** new installations use `unlinked`. A safe relative subfolder such as `manual/imported` also works.
- **Managed tag prefix:** new installations use `SmutWorks`, giving tags such as `SmutWorks - Needs title`. The app only removes its current managed tags. Changing the prefix does not rename or delete previously created tags.
- **StashDB delegation:** choose `yes` if Whisparr Sync should handle StashDB-linked scenes, or `no` to let SmutWorks organize those scenes using the same metadata requirements. Whisparr Sync is optional.

Existing nested performer folders are reused when unambiguous. New folders use a sanitized performer name. Symlinks, conflicting destinations, missing metadata, and ambiguous matches remain blocked for review.

The performer folder search depth defaults to 6 and can be changed from 1 to 30 using `performer_search_depth` in settings.

## Settings and reports

New installations use `~/Library/Application Support/SmutWorks/` for private settings, cache, locks, and a `reports/` directory. An existing installation with settings in `~/Library/Application Support/Adult Content Organizer/` keeps using that directory. An existing `reports/` directory beside the app is also reused for backward compatibility.

You can explicitly choose directories:

```sh
smutworks --config-dir "$HOME/.config/smutworks" --reports-dir "$HOME/Documents/SmutWorks Reports"
```

The equivalent environment variables are `SMUTWORKS_CONFIG_DIR` and `SMUTWORKS_REPORTS_DIR`. Command-line values take precedence. Do not put configuration containing API keys inside a public repository.

[`settings.example.json`](../settings.example.json) documents the format without real credentials. If copying it manually, place it as `settings.json` inside your chosen configuration directory and edit paths for your installation.

## Existing installations

Settings without `settings_version` are treated as the earlier organizer format. They retain `/media` plus `/incoming` aliases, female/transgender-female performer selection, the `not on stash` subfolder, `ACO -` tag names, and StashDB delegation. Opening setup preserves those values until you explicitly change them. No settings, tags, or media files are automatically moved or renamed during migration.

**Rebuild organization previews after changing mapping or organization settings.** Saved plans are revalidated before moves; stale destinations will not be applied.

## Platform scope

macOS is the supported platform in this alpha. Terminal UI, clipboard integration, report opening, and AAC fallback are built around macOS. This update makes installations configurable; it does not claim Windows or Linux support.
