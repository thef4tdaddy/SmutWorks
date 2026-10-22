<p align="center"><img src="assets/smutworks-logo.svg" alt="SmutWorks — Adult Media Toolkit" width="100%"></p>

# SmutWorks

A terminal-based adult media toolkit for **Stash**, **Whisparr Sync**, and **HandBrakeCLI**.

**0.1.0-alpha.5 · macOS · Python 3.8+ · Python standard library only**

Large duplicate collections can use **Duplicates → Bulk duplicate cleanup** to preview keepers and quarantine extras together. Bulk restore/deletion is a separate confirmed action; groups needing manual review are reported.

## Features

- Preview and organize incoming scenes using Stash titles and performers matching configurable gender preferences.
- Flag missing metadata while preserving your other Stash tags.
- Find empty directories and leftover images/small files in incoming and organized media folders.
- Review files attached to one scene and Stash duplicate groups found by perceptual hash, exact stored fingerprints, Stash IDs, URLs, or titles.
- Recommend keepers by quality, preferred encoding, or smallest matching file; manual selection is always available.
- Consolidate compatible metadata, set a primary file, and separately quarantine/restore/delete reviewed non-primary copies through Stash.
- Build encoding queues from Stash metadata or a cached filesystem scan.
- **Encode the largest estimated space savings first**, with estimated output size and reduction in the CSV report.
- **Keep the current filename, queue position, progress, speed, ETA, and processing stage above the scrolling log.** The filename remains visible after an error.
- Review finished encodes, make a reviewed encode primary in Stash, and inspect move/encode history.
- Retry Whisparr Sync for one selected StashDB-linked scene.

## Run

Install Python 3, HandBrakeCLI, and ffprobe (provided by FFmpeg). For example, with Homebrew:

```sh
brew install python handbrake ffmpeg
git clone -b develop https://github.com/thef4tdaddy/SmutWorks.git
cd SmutWorks
python3 smutworks.py
```

For a standard command-line installation from the checkout:

```sh
python3 -m pip install .
smutworks --help
smutworks
```

Alternatively, double-click **Start SmutWorks.command** (make it executable with `chmod +x 'Start SmutWorks.command'` if necessary). The older `adult_content_organizer.py` entry point remains supported.

Use **Connection and folder settings** to configure your own Stash URL, API key, mounted share, and relative incoming/organized paths. Mount your NAS share before running scans or encodes. The default paths are examples, not a requirement for your folder layout.

Configure the local media root and the equivalent root as seen by Stash; no particular NAS, hostname, or Docker mount name is required. Optional aliases support multiple mounts of the same root. See [configuration](docs/configuration.md) for examples and migration behavior. See [the feature guide](docs/legacy-guide.txt) for detailed menu behavior.

## Menus

**Organize · Metadata · Duplicates · Encoding · Cleanup · Settings & History**

Use arrows and Enter to navigate. Esc/Left goes back. Numbers are local to the current submenu, so there is no long numbered feature list. Selection is remembered within the session. See [navigation](docs/navigation.md).

## Preferred quality & encoding

Default output is H.265 10-bit MKV, maximum 1920×1080, 5,000 kbps video, beside the source with ` - reencoded.mkv` appended. The first audio track is copied when supported, with AAC fallback. Edit **Settings & History → Preferred quality profile** to set one shared standard for encoding, discovery, and duplicate recommendations. Saved queues retain their own target settings.

Both scan methods rank jobs by **estimated bytes saved**, rather than filename or percentage. Existing saved queues are also ranked when started. Estimates account for duration, target video bitrate, a conservative source audio/non-video allowance, and 1% container overhead. Audio passthrough, variable bitrate, and encoder behavior mean actual sizes can differ. Missing estimates sort last. Optional minimum savings in MiB and percent omit low-value conversions; 0 disables each cutoff. If both are enabled, both must pass. Smaller/lower-bitrate sources are not upscaled or inflated merely to match the profile.

**Originals are kept.** Estimated savings describe a potential reduction if the source is eventually replaced; encoding alone uses additional disk space. The tool does not automatically delete originals. HDR sources are excluded/rechecked for manual review.

The fixed status panel shows the **queue file number** separately from HandBrake's internal task counter. It remains independent of log scrolling and changes stage while verifying outputs. Short terminals show fewer header lines.

Encoding reads and writes directly on the configured share. Keep the Mac awake and the network share connected. The pinned status shows a per-file progress bar, speed/ETA, and separate encoding/verification stages. Verification retries transient read errors and retains detailed diagnostics. A failed file is retained while the queue continues; an unavailable source/share stops the queue.

Completed HandBrake attempts have hidden `.smutworks-encode-*.json` receipts beside their source. A rerun retries pending verification instead of encoding again. Both scanners exclude known attempts and partials; source receipts, existing outputs, and embedded HandBrake metadata prevent repeat processing of recognized files. Metadata can be removed by other tools, so arbitrary renamed older files without receipts/tags cannot always be identified.

For existing partials, open **Encoding → Retry verification / recover outputs**. It searches both roots independently of the current queue. Tracked successful attempts are rechecked; older untracked partials require explicit selection and confirmation. Verification checks readable metadata, HEVC, resolution, runtime, and unchanged files; it does not decode every frame or prove scene identity. Originals remain, and existing targets are never overwritten. Remote monitoring and local staging are not implemented yet.

## Duplicates

Open **Duplicates → Find duplicate groups**, then **Review duplicates and choose keepers**. Reports explain the match basis, recommendation, and potential reduction. Recommendations are advisory; duration differences and metadata conflicts require manual review.

Primary changes retain media. Merging combines compatible metadata and removes source scene records, with a snapshot saved first. Quarantine, restore, and deletion are separate confirmed actions; deletion is limited to a recorded, unchanged non-primary file with a verified keeper. See [the duplicate workflow](docs/duplicates.md) for details and limits.

Both encoding queue builders can use a current duplicate review to skip a redundant conversion when a verified preferred copy already exists. Metadata-only matches never suppress encodes. Scans and encodes do not delete media.

## Settings, reports, and upgrades

New installations save settings/cache under `~/Library/Application Support/SmutWorks/` and reports/history under its `reports/` subfolder. Existing installations keep their earlier settings and app-adjacent reports when present. Directory overrides are available through `--config-dir`, `--reports-dir`, and their environment variables. See [configuration](docs/configuration.md).

New organization policies use any named performer, an `unlinked` subfolder, and `SmutWorks -` managed metadata tags. Gender eligibility, subfolder names, tag prefix, and StashDB delegation can all be changed in setup. The alphabetically first eligible performer is used. Existing installations retain the earlier policy until changed explicitly.

Cleanup uses previews and separate apply actions. Images are collected into `leftover images/` under their respective root; only still-empty directories are removed. Cleanup does not automatically delete video files or partial encodes. Root-level loose-file cleanup remains a follow-up item.

## Development

```sh
python3 -m unittest discover -v
```

`develop` contains alpha work; `main` is reserved for stable releases. No stable release is claimed yet. Live NAS/API mutations require testing in your own setup; the automated suite uses local fixtures and mocked services.

Licensed under the repository's [GPL-3.0 license](LICENSE).
