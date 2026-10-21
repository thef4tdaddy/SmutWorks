<p align="center"><img src="assets/smutworks-logo.svg" alt="SmutWorks — Adult Media Toolkit" width="100%"></p>

# SmutWorks

A terminal-based adult media toolkit for **Stash**, **Whisparr Sync**, and **HandBrakeCLI**.

**0.1.0-alpha.2 · macOS · Python 3.8+ · Python standard library only**

## Features

- Preview and organize incoming scenes using Stash titles and performers matching configurable gender preferences.
- Flag missing metadata while preserving your other Stash tags.
- Find empty directories and leftover images/small files in incoming and organized media folders.
- Compare multiple files attached to a Stash scene.
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

## Encoding

Default output is H.265 10-bit MKV, maximum 1920×1080, 5,000 kbps video, beside the source with ` - reencoded.mkv` appended. The first audio track is copied when supported, with AAC fallback. Settings are editable; saved queues retain their own target settings.

Both scan methods rank jobs by **estimated bytes saved**, rather than filename or percentage. Existing saved queues are also ranked when started. Estimates account for duration, target video bitrate, a conservative source audio/non-video allowance, and 1% container overhead. Audio passthrough, variable bitrate, and encoder behavior mean actual sizes can differ. Missing estimates sort last.

**Originals are kept.** Estimated savings describe a potential reduction if the source is eventually replaced; encoding alone uses additional disk space. The tool does not automatically delete originals. HDR sources are excluded/rechecked for manual review.

The fixed status panel shows the **queue file number** separately from HandBrake's internal task counter. It remains independent of log scrolling and changes stage while verifying outputs. Short terminals show fewer header lines.

Encoding currently reads and writes directly on the configured share. Keep the Mac awake and the network share connected. A failed encode or failed verification retains a hidden `.aco-partial-*.mkv` output for inspection; it is not automatically resumed or recovered. Remote monitoring and local staging are not implemented yet.

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
