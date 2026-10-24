"""Stash-backed duplicate review. Recommendations never apply file changes."""

import hashlib
import json
import math
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import encoding_queue as encoding
import organizer_features as features
import organizer_journal as journal

QUARANTINE = "smutworks quarantine"
FILE_FIELDS = "id path size width height video_codec duration bit_rate frame_rate fingerprints{type value} scenes{id}"
SCENE_FIELDS = (
    features.DETAIL_FIELDS.replace(
        "files{id path size}", "files{" + FILE_FIELDS + "}"
    ).replace("groups{group{id}}", "groups{group{id} scene_index}")
    + " updated_at play_history o_history interactive interactive_speed scene_markers{id}"
)
ITEMS = [
    ("24", "Find duplicate groups"),
    ("29", "Bulk duplicate cleanup"),
    ("25", "Review duplicates and choose keepers"),
    ("30", "Bulk quarantine restore / delete"),
    ("26", "Review quarantine / restore"),
    ("27", "Delete one quarantined file"),
    ("28", "Duplicate review settings"),
    ("32", "Recover pending quarantine moves"),
]
DESCRIPTIONS = {
    key: (
        "DUPLICATES / REVIEW",
        "Review Stash matches and multiple-file scenes. Recommendations are advisory. Primary selection, metadata merges, quarantine, restore, and deletion are separate confirmed actions.",
    )
    for key, _ in ITEMS
}
DESCRIPTIONS["29"] = (
    "DUPLICATES / BULK CLEANUP",
    "Preview recommended keepers and extras for the saved scan, then quarantine eligible copies with one confirmation. Unavailable, changed, shared, and ambiguous groups need review. Nothing is deleted.",
)
DESCRIPTIONS["30"] = (
    "DUPLICATES / BULK QUARANTINE",
    "Review a saved list and restore or delete all confirmed quarantined files with one confirmation. Each keeper and file is checked again before changes.",
)
DESCRIPTIONS["32"] = (
    "DUPLICATES / RECOVER MOVES",
    "Verify pending quarantine or restore moves. Refresh only the moved file in Stash when its path is stale; retain the original file ID and scene. No files are deleted.",
)
MODES = {
    "quality": "Best quality (heuristic)",
    "preferred": "Closest to preferred quality",
    "smallest": "Smallest meeting preferred quality",
    "manual": "Manual selection",
}
_ACTION_LOCK = threading.local()


def serialized_action(action):
    """Share the queue lock so file maintenance cannot race this app's encoder."""
    import functools

    @functools.wraps(action)
    def protected(o, *args, **kwargs):
        import fcntl

        if getattr(_ACTION_LOCK, "held", False):
            return action(o, *args, **kwargs)
        lock_path = o.ENCODING_FILE.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "The encoding queue or another file action is running. Finish it before duplicate changes."
                ) from None
            _ACTION_LOCK.held = True
            try:
                return action(o, *args, **kwargs)
            finally:
                _ACTION_LOCK.held = False

    return protected


def scene(api, scene_id):
    return api.gql(
        "query($id:ID!){findScene(id:$id){"
        + features.compatible_scene_fields(api, SCENE_FIELDS)
        + "}}",
        {"id": str(scene_id)},
    )["findScene"]


def scenes(api, scope):
    page = 1
    fields = features.compatible_scene_fields(api, SCENE_FIELDS)
    while True:
        print("Reading Stash duplicate candidates, page", page, flush=True)
        data = api.gql(
            'query($page:Int!,$scope:SceneFilterType!){findScenes(filter:{page:$page,per_page:100,sort:"id",direction:ASC},scene_filter:$scope){scenes{'
            + fields
            + "}}}",
            {"page": page, "scope": scope},
        )["findScenes"]["scenes"]
        yield from data
        if len(data) < 100:
            break
        page += 1


def file_owners(api, file):
    """Use fresh scene assignments, including on versions without VideoFile.scenes."""
    if "scenes" in features.stash_type_fields(api, "VideoFile"):
        if "scenes" not in file:
            raise RuntimeError(
                "Stash did not report file scene assignments; action blocked."
            )
        native_owners = {str(row["id"]) for row in file["scenes"]}
        if not native_owners:
            raise RuntimeError(
                "Could not verify file scene assignments; action blocked."
            )
        return native_owners
    # Older Stash joins the scene path filter through all attached files. Match
    # the actual file ID too; do not assume a pathname implies ownership.
    owners: Set[str] = set()
    page = 1
    while True:
        result = api.gql(
            'query($page:Int!,$path:String!){findScenes(filter:{page:$page,per_page:100,sort:"id",direction:ASC},'
            "scene_filter:{path:{value:$path,modifier:EQUALS}}){scenes{id files{id}}}}",
            {"page": page, "path": file["path"]},
        )["findScenes"]["scenes"]
        owners.update(
            str(row["id"])
            for row in result
            if any(
                str(value["id"]) == str(file["id"]) for value in row.get("files") or []
            )
        )
        if len(result) < 100:
            break
        page += 1
    if not owners:
        raise RuntimeError("Could not verify file scene assignments; action blocked.")
    return owners


def options(settings):
    if not isinstance(settings.get("duplicates", {}), dict):
        raise RuntimeError("Duplicate settings must be an object.")
    value = dict(
        {"mode": "preferred", "phash_distance": 0, "duration_diff": 2},
        **settings.get("duplicates", {}),
    )
    if (
        value["mode"] not in MODES
        or type(value["phash_distance"]) is not int
        or not 0 <= value["phash_distance"] <= 32
    ):
        raise RuntimeError(
            "Invalid duplicate recommendation mode or perceptual hash distance."
        )
    if (
        not isinstance(value["duration_diff"], (int, float))
        or not math.isfinite(value["duration_diff"])
        or not 0 <= value["duration_diff"] <= 60
    ):
        raise RuntimeError(
            "Duplicate duration difference must be between 0 and 60 seconds."
        )
    return value


def compatible_duration(files, tolerance=2):
    durations = [float(f.get("duration") or 0) for f in files]
    return (
        bool(durations)
        and all(math.isfinite(v) and v > 0 for v in durations)
        and max(durations) - min(durations) <= tolerance
    )


def info(file):
    return {
        "width": int(file.get("width") or 0),
        "height": int(file.get("height") or 0),
        "codec": str(file.get("video_codec") or file.get("codec") or "").lower(),
        "video_kbps": (
            round(float(file.get("bit_rate") or 0) / 1000)
            if "bit_rate" in file
            else file.get("video_kbps", 0)
        ),
        "frame_rate": float(file.get("frame_rate") or 0),
        "duration": float(file.get("duration") or 0),
        "hdr": file.get("hdr", False),
        "bit_depth": file.get("bit_depth", 0),
    }


def fits_profile(value, profile, strict=False):
    return (
        value.get("codec", "").lower() in ("hevc", "h265", "h.265")
        and 0 < value.get("width", 0) <= profile["width"]
        and 0 < value.get("height", 0) <= profile["height"]
        and 0 < value.get("video_kbps", 0) < profile["min_video_kbps"]
        and not value.get("hdr")
        and (
            value.get("bit_depth") == 10
            if strict
            else value.get("bit_depth", 0) in (0, 10)
        )
    )


def recommend(files, mode, profile, tolerance=2):
    if mode == "manual":
        return None, "Manual selection requested."
    if not compatible_duration(files, tolerance):
        return (
            None,
            "Durations differ or are unknown: possible alternate cuts; review manually.",
        )
    if any(not info(f)["width"] or not info(f)["height"] for f in files):
        return None, "Missing technical metadata; review manually."
    if mode == "quality":
        # Bitrate is only a last-order heuristic, not a quality measurement across codecs.
        chosen = max(
            files,
            key=lambda f: (
                info(f)["width"] * info(f)["height"],
                info(f)["frame_rate"],
                info(f)["bit_depth"],
                info(f)["video_kbps"],
                -int(f["id"]),
            ),
        )
        reason = "Highest resolution, then recorded frame rate, known bit depth and bitrate. Codec/source differences require visual review."
    elif mode == "smallest":
        suitable = [f for f in files if fits_profile(info(f), profile)]
        if not suitable:
            return (
                None,
                "No existing file meets the preferred codec/resolution/bitrate limits.",
            )
        # Preserve as much resolution as this group can deliver within the target limits.
        reference = max(
            info(f)["width"]
            * info(f)["height"]
            * min(
                1,
                profile["width"] / info(f)["width"],
                profile["height"] / info(f)["height"],
            )
            ** 2
            for f in files
        )
        suitable = [
            f
            for f in suitable
            if info(f)["width"] * info(f)["height"] >= reference * 0.95
        ]
        if not suitable:
            return (
                None,
                "Only lower-resolution files fit; review the resolution tradeoff manually.",
            )
        chosen = min(suitable, key=lambda f: (int(f["size"]), str(f["id"])))
        reason = "Smallest file meeting the profile limits without losing available target resolution."
    else:

        def distance(f):
            value = info(f)
            fit = min(
                1,
                profile["width"] / value["width"],
                profile["height"] / value["height"],
            )
            wanted = value["width"] * value["height"] * fit**2
            resolution_penalty = abs(
                math.log(max(1, value["width"] * value["height"]) / max(1, wanted))
            )
            # Compare relative to the group's best usable resolution as well.
            best = max(
                info(v)["width"]
                * info(v)["height"]
                * min(
                    1,
                    profile["width"] / info(v)["width"],
                    profile["height"] / info(v)["height"],
                )
                ** 2
                for v in files
            )
            resolution_penalty += abs(math.log(max(1, wanted) / max(1, best)))
            return (
                resolution_penalty,
                value["codec"] not in ("hevc", "h265", "h.265"),
                value["bit_depth"] not in (0, 10),
                abs(value["video_kbps"] - profile["video_kbps"]),
                int(f["size"]),
                str(f["id"]),
            )

        chosen = min(files, key=distance)
        reason = "Closest available resolution, HEVC/10-bit preference, then target bitrate. Unknown bit depth is verified before file actions."
    return str(chosen["id"]), reason


def collect(o, api, method, settings):
    """Native duplicate filters supply candidates; exact keys form explicit groups."""
    opt = options(settings)
    buckets, all_scenes = [], {}
    attached_ids = set()
    excluded_groups = []

    def add(rows, basis):
        rows = [v for v in rows if v]
        for row in rows:
            all_scenes[str(row["id"])] = row
        if rows:
            buckets.append((set(str(v["id"]) for v in rows), basis))

    if method in ("attached", "all"):
        for row in scenes(
            api, {"file_count": {"value": 1, "modifier": "GREATER_THAN"}}
        ):
            attached_ids.add(str(row["id"]))
            add([row], "attached files")
    if method in ("phash", "all"):
        groups = api.gql(
            "query($distance:Int!,$diff:Float!){findDuplicateScenes(distance:$distance,duration_diff:$diff){"
            + features.compatible_scene_fields(api, SCENE_FIELDS)
            + "}}",
            {"distance": opt["phash_distance"], "diff": opt["duration_diff"]},
        )["findDuplicateScenes"]
        for group in groups:
            add(group, "perceptual hash (distance {})".format(opt["phash_distance"]))
    for kind in ("stash_id", "url", "title"):
        if method not in (kind, "all"):
            continue
        index: Dict[Any, Any] = {}
        for row in scenes(api, {"duplicated": {kind: True}}):
            if kind == "stash_id":
                keys = [
                    (v.get("endpoint", "").rstrip("/").casefold(), v.get("stash_id"))
                    for v in row.get("stash_ids") or []
                    if v.get("stash_id")
                ]
            elif kind == "url":
                keys = [v for v in row.get("urls") or [] if v]
            else:
                keys = (
                    [o.normalized(row.get("title") or "")]
                    if (row.get("title") or "").strip()
                    else []
                )
            for key in set(keys):
                index.setdefault(key, {})[str(row["id"])] = row
        for group in index.values():
            if len(group) > 1:
                add(list(group.values()), "same " + kind)
    if method in ("fingerprint", "all"):
        index = {}
        for row in scenes(api, {}):
            for file in row.get("files") or []:
                for fingerprint in file.get("fingerprints") or []:
                    if fingerprint["type"].lower() in (
                        "md5",
                        "sha256",
                    ) and fingerprint.get("value"):
                        key = (fingerprint["type"].lower(), fingerprint["value"])
                        bucket = index.setdefault(key, {"scenes": {}, "files": set()})
                        bucket["scenes"][str(row["id"])] = row
                        bucket["files"].add(str(file["id"]))
        for key, group in index.items():
            if len(group["files"]) > 1:
                add(list(group["scenes"].values()), "exact " + key[0])
    # Combine overlapping scene groups, recording every evidence type. Weak matches
    # remain visibly weak even when connected to a stronger pair.
    merged: List[Tuple[Set[str], Set[str]]] = []
    for ids, basis in buckets:
        bases = {basis}
        while True:
            match = next(
                (i for i, (other, _) in enumerate(merged) if ids & other), None
            )
            if match is None:
                break
            other, reasons = merged.pop(match)
            ids |= other
            bases |= reasons
        merged.append((ids, bases))
    result = []
    profile = encoding.profile(settings)
    for ids, bases in merged:
        rows = [all_scenes[key] for key in sorted(ids)]
        files = {}
        for row in rows:
            for file in row.get("files") or []:
                file = dict(file)
                if "scenes" not in file:
                    # Observed attachments suffice for recommendations. File
                    # changes separately verify ALL owners against Stash live.
                    file["scenes"] = [
                        {"id": str(owner["id"])}
                        for owner in rows
                        if any(
                            str(value["id"]) == str(file["id"])
                            for value in owner.get("files") or []
                        )
                    ]
                file["local_path"] = str(o.canonical(file["path"]))
                try:
                    path = features.checked_path(o, file["local_path"])
                    if any(
                        o.protected_name(part)
                        for root in (o.INCOMING_ROOT, o.MEDIA_ROOT)
                        if o.beneath(path, root)
                        for part in path.relative_to(root).parts
                    ):
                        continue
                    file["signature"] = journal.fingerprint(path)
                    if file["signature"]["size"] != int(file["size"]):
                        file["signature"] = None
                except (RuntimeError, OSError):
                    file["signature"] = None
                files[str(file["id"])] = file
        if len(files) < 2:
            excluded_groups.append(
                {
                    "scene_ids": sorted(ids),
                    "titles": [row.get("title") or "(untitled)" for row in rows],
                    "reason": "Fewer than two distinct files remain after protected-folder exclusions",
                    "attached_file_count": sum(
                        len(row.get("files") or []) for row in rows
                    ),
                    "review_file_count": len(files),
                }
            )
            continue
        available = [file for file in files.values() if file.get("signature")]
        if available:
            recommendation, why = recommend(
                available, opt["mode"], profile, opt["duration_diff"]
            )
            if len(available) != len(files):
                why += (
                    " Unavailable entries need path review; file actions are blocked."
                )
        else:
            recommendation, why = (
                None,
                "No available files; check the mounted share and Stash paths.",
            )
        group_id = hashlib.sha256(",".join(sorted(files)).encode()).hexdigest()[:12]
        merge_blocker = None
        if len(rows) > 1:
            try:
                merge_values(rows, rows[0]["id"])
            except RuntimeError as error:
                merge_blocker = str(error)
        result.append(
            {
                "merge_blocker": merge_blocker,
                "id": group_id,
                "scenes": rows,
                "files": list(files.values()),
                "basis": sorted(bases),
                "recommendation": recommendation,
                "reason": why,
                "mode": opt["mode"],
            }
        )
    return {
        "version": 1,
        "profile": profile,
        "options": opt,
        "groups": result,
        "roots": [str(o.INCOMING_ROOT), str(o.MEDIA_ROOT)],
        "scan_summary": {
            "attached_scene_candidates": len(attached_ids),
            "review_groups": len(result),
            "excluded_groups": len(excluded_groups),
            "groups_with_unavailable_files": sum(
                any(not f.get("signature") for f in g["files"]) for g in result
            ),
        },
        "excluded_groups": excluded_groups,
    }


def report(o, document):
    lines = [
        "SMUTWORKS DUPLICATE REVIEW",
        "",
        "Suggestions only. Matching titles/URLs/IDs and perceptual matches are not proof of identical cuts.",
        "Quality ordering is heuristic. Bitrate is not directly comparable across codecs.",
        "File removal requires separate confirmed quarantine and deletion actions.",
        "Profile: " + encoding.summary(document["profile"]),
        "",
    ]
    summary = document.get("scan_summary")
    if summary:
        lines += [
            "SCAN COUNTS",
            "Attached-file scene candidates: {} | Review groups: {} | Excluded groups: {} | Groups with unavailable files: {}".format(
                summary["attached_scene_candidates"],
                summary["review_groups"],
                summary["excluded_groups"],
                summary["groups_with_unavailable_files"],
            ),
            "",
        ]
    for number, group in enumerate(document["groups"], 1):
        choice = group.get("recommendation")
        potential = (
            sum(
                int(f["size"])
                for f in group["files"]
                if str(f["id"]) != choice and f.get("signature")
            )
            if choice
            else 0
        )
        lines += [
            "GROUP {} [{}]".format(number, group["id"]),
            "Evidence: " + ", ".join(group["basis"]),
            "Recommendation: " + str(choice or "manual review"),
            group["reason"],
            "Potential reduction after review/removal: {:.3f} GiB (not space already freed).".format(
                potential / 1024**3
            ),
        ]
        for file in group["files"]:
            value = info(file)
            lines += [
                "{} file {} | {}x{} | {} | {:.2f}s | {:.3f} GiB | ~{} kbps".format(
                    "*" if str(file["id"]) == choice else " ",
                    file["id"],
                    value["width"],
                    value["height"],
                    value["codec"],
                    value["duration"],
                    int(file["size"]) / 1024**3,
                    value["video_kbps"],
                ),
                "  " + file["local_path"],
                "  "
                + (
                    "Available"
                    if file.get("signature")
                    else "UNAVAILABLE: actions blocked"
                ),
            ]
        if group.get("merge_blocker"):
            lines.append("METADATA MERGE BLOCKED: " + group["merge_blocker"])
        lines += [""]
    if document.get("excluded_groups"):
        lines += ["EXCLUDED GROUPS"]
        for entry in document["excluded_groups"]:
            lines.append(
                "Scenes {} | {} | {} | {} attached files, {} included".format(
                    ", ".join(entry["scene_ids"]),
                    "; ".join(entry["titles"]),
                    entry["reason"],
                    entry["attached_file_count"],
                    entry["review_file_count"],
                )
            )
    o.save_document(o.REPORTS_DIR / "duplicate-review.json", document)
    path = features.text_report(o, "duplicate-review.txt", "\n".join(lines) + "\n")
    print("{} duplicate groups. See report: {}".format(len(document["groups"]), path))
    total_files = sum(len(group["files"]) for group in document["groups"])
    unavailable = sum(
        not f.get("signature") for group in document["groups"] for f in group["files"]
    )
    recommended = sum(bool(group.get("recommendation")) for group in document["groups"])
    possible_bytes = sum(
        int(f["size"])
        for group in document["groups"]
        if group.get("recommendation")
        for f in group["files"]
        if f.get("signature") and str(f["id"]) != group["recommendation"]
    )
    print(
        "Files in review: {} | Available: {} | Unavailable: {}".format(
            total_files, total_files - unavailable, unavailable
        )
    )
    print(
        "Groups with a suggested keeper: {} | Manual keeper review: {}".format(
            recommended, len(document["groups"]) - recommended
        )
    )
    print(
        "Potential reduction from available extras: {:.2f} GiB after review/deletion. Nothing changed.".format(
            possible_bytes / 1024**3
        )
    )
    if summary:
        print(
            "{} attached-file scene candidates; {} excluded groups; {} groups contain unavailable files. See scan counts/exclusions in the report.".format(
                summary["attached_scene_candidates"],
                summary["excluded_groups"],
                summary["groups_with_unavailable_files"],
            )
        )


def fresh_group(o, api, document, group):
    if document.get("version") != 1 or document.get("roots") != [
        str(o.INCOMING_ROOT),
        str(o.MEDIA_ROOT),
    ]:
        raise RuntimeError("Duplicate review paths changed. Scan again.")
    fresh = []
    for old in group["scenes"]:
        current = scene(api, old["id"])
        if current != old:
            raise RuntimeError(
                "Stash metadata or file assignments changed. Scan duplicates again."
            )
        fresh.append(current)
    for file in group["files"]:
        path = features.checked_path(o, file["local_path"])
        if not file.get("signature") or not journal.matches(path, file["signature"]):
            raise RuntimeError(
                "Reviewed file changed or is unavailable. Scan duplicates again."
            )
    return fresh


def verified_files(o, group, tolerance):
    values = []
    for file in group["files"]:
        path = features.checked_path(o, file["local_path"])
        value = encoding.details(encoding.probe(path))
        stored = info(file)
        if (
            value["width"] != stored["width"]
            or value["height"] != stored["height"]
            or abs(value["duration"] - stored["duration"]) > tolerance
        ):
            raise RuntimeError(
                "Fresh media dimensions/duration differ from Stash metadata. Rescan the file in Stash first."
            )
        if value["hdr"]:
            raise RuntimeError(
                "HDR detected. Review the HDR/SDR versions manually in Stash."
            )
        if not journal.matches(path, file["signature"]):
            raise RuntimeError("File changed during inspection.")
        values.append(value)
    if not compatible_duration(values, tolerance):
        raise RuntimeError(
            "Fresh inspection found differing/unknown durations. Alternate cuts need manual review."
        )
    return values


def merge_values(rows, destination):
    """Union relationships; preserve all scalar values or block conflicting metadata."""
    values: Dict[str, Any] = {"id": str(destination)}
    if any(row.get("interactive") for row in rows):
        raise RuntimeError(
            "Interactive scenes need manual merge review to preserve sidecar behavior."
        )
    for row in rows:
        if int(row.get("play_count") or 0) > len(row.get("play_history") or []) or int(
            row.get("o_counter") or 0
        ) > len(row.get("o_history") or []):
            raise RuntimeError(
                "Legacy history counters lack timestamps; merge manually to avoid losing counts."
            )
    for key in (
        "title",
        "code",
        "details",
        "director",
        "date",
        "production_date",
        "rating100",
        "resume_time",
    ):
        unique = []
        for row in rows:
            value = row.get(key)
            if value not in (None, "", 0) and value not in unique:
                unique.append(value)
        if len(unique) > 1:
            raise RuntimeError(
                "Conflicting {} metadata. Resolve it in Stash, then rescan.".format(key)
            )
        if unique:
            values[key] = unique[0]
    studios = {str(row["studio"]["id"]) for row in rows if row.get("studio")}
    if len(studios) > 1:
        raise RuntimeError("Conflicting studios. Resolve in Stash before merging.")
    if studios:
        values["studio_id"] = next(iter(studios))
    for key, field in [
        ("performers", "performer_ids"),
        ("tags", "tag_ids"),
        ("galleries", "gallery_ids"),
    ]:
        values[field] = sorted(
            {str(v["id"]) for row in rows for v in row.get(key) or []}
        )
    values["urls"] = sorted({v for row in rows for v in row.get("urls") or []})
    stash_ids = {
        (v["endpoint"], v["stash_id"])
        for row in rows
        for v in row.get("stash_ids") or []
    }
    values["stash_ids"] = [{"endpoint": a, "stash_id": b} for a, b in sorted(stash_ids)]
    groups: Dict[str, Any] = {}
    for row in rows:
        for value in row.get("groups") or []:
            key = str(value["group"]["id"])
            number = value.get("scene_index")
            if key in groups and groups[key] != number:
                raise RuntimeError("Conflicting group positions; resolve in Stash.")
            groups[key] = number
    values["groups"] = [
        dict(group_id=key, **({"scene_index": value} if value is not None else {}))
        for key, value in sorted(groups.items())
    ]
    custom: Dict[str, Any] = {}
    for row in rows:
        for key, value in (row.get("custom_fields") or {}).items():
            if key in custom and custom[key] != value:
                raise RuntimeError("Conflicting custom field: " + key)
            custom[key] = value
    values["organized"] = any(row.get("organized") for row in rows)
    values["play_duration"] = sum(float(row.get("play_duration") or 0) for row in rows)
    return values, custom


def verify_merged_metadata(updated, values, custom, originals):
    for key in (
        "title",
        "code",
        "details",
        "director",
        "date",
        "production_date",
        "rating100",
        "resume_time",
        "organized",
        "play_duration",
    ):
        if key in values and updated.get(key) != values[key]:
            raise RuntimeError(
                "Merged metadata not confirmed for "
                + key
                + ". Inspect Stash; history remains pending."
            )
    for key, field in [
        ("performers", "performer_ids"),
        ("tags", "tag_ids"),
        ("galleries", "gallery_ids"),
    ]:
        if {str(v["id"]) for v in updated.get(key) or []} != set(values[field]):
            raise RuntimeError(
                "Merged "
                + key
                + " not confirmed. Inspect Stash; history remains pending."
            )
    if set(updated.get("urls") or []) != set(values["urls"]) or {
        (v["endpoint"], v["stash_id"]) for v in updated.get("stash_ids") or []
    } != {(v["endpoint"], v["stash_id"]) for v in values["stash_ids"]}:
        raise RuntimeError(
            "Merged links/IDs not confirmed. Inspect Stash; history remains pending."
        )
    if custom and updated.get("custom_fields") != custom:
        raise RuntimeError(
            "Custom fields not confirmed after merge. Inspect Stash; history remains pending."
        )
    for key in ("play_history", "o_history"):
        if not {v for row in originals for v in row.get(key) or []} <= set(
            updated.get(key) or []
        ):
            raise RuntimeError(
                "Merged history not confirmed. Inspect Stash; history remains pending."
            )
    if (
        values.get("studio_id")
        and str((updated.get("studio") or {}).get("id")) != values["studio_id"]
    ):
        raise RuntimeError(
            "Merged studio not confirmed. Inspect Stash; history remains pending."
        )
    if not {
        str(v["id"]) for row in originals for v in row.get("scene_markers") or []
    } <= {str(v["id"]) for v in updated.get("scene_markers") or []}:
        raise RuntimeError(
            "Merged markers not confirmed. Inspect Stash; history remains pending."
        )
    actual = {
        (str(v["group"]["id"]), v.get("scene_index"))
        for v in updated.get("groups") or []
    }
    expected = {(str(v["group_id"]), v.get("scene_index")) for v in values["groups"]}
    if actual != expected:
        raise RuntimeError(
            "Merged group memberships not confirmed. Inspect Stash; history remains pending."
        )


@serialized_action
def apply_primary(o, api, document, group, file_id, merge=False):
    require_recovered_moves(o)
    rows = fresh_group(o, api, document, group)
    if not any(str(v["id"]) == str(file_id) for v in group["files"]):
        raise RuntimeError("Choose a file in this group.")
    owners = [
        row for row in rows if any(str(v["id"]) == str(file_id) for v in row["files"])
    ]
    if len(owners) != 1:
        raise RuntimeError(
            "Keeper is attached to multiple scenes. Resolve assignments manually."
        )
    dest = owners[0]
    # Multiple-scene groups may contain files outside this review; never merge unseen files.
    if {str(v["id"]) for row in rows for v in row["files"]} != {
        str(v["id"]) for v in group["files"]
    }:
        raise RuntimeError(
            "Some scene files are excluded from this review. Resolve them before merging."
        )
    if len(rows) > 1 and not merge:
        raise RuntimeError("Merge these scenes first, or choose a single-scene group.")
    verified = verified_files(o, group, document["options"]["duration_diff"])
    if (
        group.get("mode") in ("preferred", "smallest")
        and str(file_id) == group.get("recommendation")
        and fits_profile(
            info(next(f for f in group["files"] if str(f["id"]) == str(file_id))),
            document["profile"],
        )
    ):
        chosen = verified[
            next(
                index
                for index, file in enumerate(group["files"])
                if str(file["id"]) == str(file_id)
            )
        ]
        if not fits_profile(chosen, document["profile"], strict=True):
            raise RuntimeError(
                "Fresh inspection found the recommended file does not meet the profile. Review it and switch to Manual selection to override."
            )
    rows = fresh_group(o, api, document, group)
    if any(row.get("interactive") for row in rows):
        raise RuntimeError("Interactive media requires manual sidecar review.")
    allowed = {str(row["id"]) for row in rows}
    if any(
        not file_owners(api, file) <= allowed for row in rows for file in row["files"]
    ):
        raise RuntimeError(
            "A file is also attached outside this group. Resolve shared assignments manually."
        )
    history = {
        "kind": "duplicate_primary",
        "status": "pending",
        "scenes_before": rows,
        "scene_id": str(dest["id"]),
        "old_primary": str(dest["files"][0]["id"]),
        "new_primary": str(file_id),
        "group": group["id"],
    }
    if len(rows) > 1:
        values, custom = merge_values(rows, dest["id"])
        if custom:
            fields = api.gql(
                'query{__type(name:"CustomFieldsInput"){inputFields{name}}}'
            )
            supported = {
                v["name"] for v in (fields.get("__type") or {}).get("inputFields") or []
            }
            if "full" not in supported:
                raise RuntimeError(
                    "This Stash version cannot preserve merged custom fields automatically. Merge manually."
                )
            values["custom_fields"] = {"full": custom}
        history["kind"] = "duplicate_merge"
        history["id"] = journal.append(
            o.REPORTS_DIR / "duplicate-history.jsonl", history
        )
        api.gql(
            "mutation($input:SceneMergeInput!){sceneMerge(input:$input){id}}",
            {
                "input": {
                    "source": [str(v["id"]) for v in rows if v["id"] != dest["id"]],
                    "destination": str(dest["id"]),
                    "values": values,
                    "play_history": True,
                    "o_history": True,
                }
            },
            timeout=600,
        )
        updated = scene(api, dest["id"])
        if {str(v["id"]) for v in updated["files"]} != {
            str(v["id"]) for v in group["files"]
        }:
            raise RuntimeError(
                "Unexpected file assignments after merge. Inspect Stash; history remains pending."
            )
        verify_merged_metadata(updated, values, custom, rows)
    else:
        history["id"] = journal.append(
            o.REPORTS_DIR / "duplicate-history.jsonl", history
        )
    features.set_primary(api, dest["id"], file_id)
    current = scene(api, dest["id"])
    for file in group["files"]:
        updated = next(
            (v for v in current["files"] if str(v["id"]) == str(file["id"])), None
        )
        if (
            not updated
            or o.canonical(updated["path"]) != Path(file["local_path"])
            or not journal.matches(file["local_path"], file["signature"])
        ):
            raise RuntimeError(
                "A hook changed file paths/content. Inspect Stash; history remains pending."
            )
    journal.append(
        o.REPORTS_DIR / "duplicate-history.jsonl", dict(history, status="done")
    )
    old = next(f for f in group["files"] if str(f["id"]) == history["old_primary"])
    new = next(f for f in group["files"] if str(f["id"]) == str(file_id))
    journal.append(
        features.move_history_path(o),
        dict(
            kind="primary_change",
            status="done",
            scene_id=str(dest["id"]),
            old_primary=history["old_primary"],
            new_primary=str(file_id),
            source=old["local_path"],
            target=new["local_path"],
            source_fingerprint=old["signature"],
            target_fingerprint=new["signature"],
            old_primary_path=old["local_path"],
            old_primary_fingerprint=old["signature"],
        ),
    )
    print(
        "Primary updated. All media retained. Rescan duplicates before further actions."
    )
    print(
        "History undo can restore the primary selection. It cannot recreate merged source scenes."
    )
    if len(rows) > 1:
        print(
            "Source scene records were merged; metadata snapshot is in duplicate history. Scene merges are not automatically undoable."
        )


def pending_moves(o):
    return [
        event
        for event in journal.latest(o.REPORTS_DIR / "duplicate-history.jsonl")
        if event["kind"] == "duplicate_quarantine"
        and event["status"] in ("pending", "restore_pending")
    ]


def require_recovered_moves(o):
    if pending_moves(o):
        raise RuntimeError(
            "An earlier quarantine move is pending. Run Duplicates → Recover pending "
            "quarantine moves, then rescan before another cleanup."
        )


def confirm_move(o, api, event, restoring=False):
    """Retry reads, then reconcile only a verified move still recorded at its old path."""
    source = features.checked_path(o, event["target"] if restoring else event["source"])
    target = features.checked_path(o, event["source"] if restoring else event["target"])
    signature = event["signature"]

    def inspect():
        current = api.file(event["file_id"])
        problems = []
        if not current or str(current["id"]) != event["file_id"]:
            problems.append("Stash did not return the original file ID")
        elif o.canonical(current["path"]) != target:
            problems.append(
                "Stash path: {} (expected {})".format(current["path"], target)
            )
        if current and int(current["size"]) != signature["size"]:
            problems.append("Stash file size differs from the recorded file")
        if not journal.matches(target, signature):
            problems.append(
                "Destination size/timestamp does not match the recorded file"
            )
        if os.path.lexists(source):
            problems.append("Original path still exists: " + str(source))
        return current, problems

    current, problems = None, []
    for delay in (0, 0.5, 1):
        if delay:
            time.sleep(delay)
        current, problems = inspect()
        if not problems:
            return
    # Do not scan an unexpected file or accept a move based on filesystem checks alone.
    if (
        current
        and str(current["id"]) == event["file_id"]
        and o.canonical(current["path"]) == source
        and int(current["size"]) == signature["size"]
        and not os.path.lexists(source)
        and journal.matches(target, signature)
    ):
        print(
            "File moved; refreshing its stale Stash path with a targeted scan...",
            flush=True,
        )
        job_id = api.gql(
            "mutation($input:ScanMetadataInput!){metadataScan(input:$input)}",
            {
                "input": {
                    "paths": [o.stash_path(target)],
                    "rescan": False,
                    "scanGenerateCovers": False,
                    "scanGeneratePreviews": False,
                    "scanGenerateImagePreviews": False,
                    "scanGenerateSprites": False,
                    "scanGeneratePhashes": False,
                }
            },
        )["metadataScan"]
        features.wait_job(api, job_id)
        for delay in (0, 0.5, 1):
            if delay:
                time.sleep(delay)
            current, problems = inspect()
            if not problems:
                return
    raise RuntimeError(
        "Move verification pending: "
        + "; ".join(problems)
        + ". Run Duplicates → Recover pending quarantine moves."
    )


def validate_move_ownership(o, api, event):
    keeper = features.checked_path(o, event["keeper_path"])
    if not journal.matches(keeper, event["keeper_signature"]):
        raise RuntimeError("Keeper changed or is unavailable; recovery blocked.")
    row = scene(api, event["scene_id"])
    files = (row.get("files") or []) if row else []
    if (
        not files
        or str(files[0]["id"]) != event["keeper_id"]
        or o.canonical(files[0]["path"]) != keeper
    ):
        raise RuntimeError("Keeper/scene assignment changed; recovery blocked.")
    extra = next((f for f in files if str(f["id"]) == event["file_id"]), None)
    if not extra or file_owners(api, extra) != {event["scene_id"]}:
        raise RuntimeError("File scene assignments changed; recovery blocked.")


@serialized_action
def recover_moves(o, api):
    events = pending_moves(o)
    recovered = 0
    for event in events:
        try:
            source = features.checked_path(o, event["source"])
            target = features.checked_path(o, event["target"])
            if (
                not o.beneath(target, o.MEDIA_ROOT / QUARANTINE)
                or source == target
                or source.name != target.name
                or str(event["keeper_id"]) == str(event["file_id"])
                or Path(event["keeper_path"]) in (source, target)
            ):
                raise RuntimeError("Invalid recorded quarantine paths or keeper.")
            validate_move_ownership(o, api, event)
            restoring = event["status"] == "restore_pending"
            old_path, new_path = (target, source) if restoring else (source, target)
            current = api.file(event["file_id"])
            if (
                current
                and str(current["id"]) == event["file_id"]
                and o.canonical(current["path"]) == old_path
                and journal.matches(old_path, event["signature"])
                and not os.path.lexists(new_path)
            ):
                # The API never moved the file; release the pending action without moving it.
                status = "quarantined" if restoring else "cancelled"
                journal.append(
                    o.REPORTS_DIR / "duplicate-history.jsonl",
                    dict(event, status=status),
                )
                recovered += 1
                print(
                    "File {}: move was not applied; pending action cleared.".format(
                        event["file_id"]
                    )
                )
                continue
            confirm_move(o, api, event, restoring)
            validate_move_ownership(o, api, event)
            journal.append(
                o.REPORTS_DIR / "duplicate-history.jsonl",
                dict(event, status="restored" if restoring else "quarantined"),
            )
            recovered += 1
            print(
                "Recovered file {}: {}".format(
                    event["file_id"], "restored" if restoring else "quarantined"
                )
            )
        except (RuntimeError, OSError) as error:
            print("File {} still pending: {}".format(event["file_id"], error))
    print(
        "Moves recovered: {} | Still pending: {} | Files deleted: 0".format(
            recovered, len(events) - recovered
        )
    )
    print("Rescan duplicates before another cleanup.")
    return recovered


@serialized_action
def quarantine(o, api, document, group, file_id):
    require_recovered_moves(o)
    rows = fresh_group(o, api, document, group)
    if len(rows) != 1:
        raise RuntimeError("Merge to one scene before quarantining files.")
    row = rows[0]
    primary = row["files"][0]
    if str(primary["id"]) == str(file_id):
        raise RuntimeError("Cannot quarantine the primary file. Choose a keeper first.")
    file = next((v for v in group["files"] if str(v["id"]) == str(file_id)), None)
    keeper = next(
        (v for v in group["files"] if str(v["id"]) == str(primary["id"])), None
    )
    if file is None or keeper is None:
        raise RuntimeError("File/keeper is not part of this review.")
    if Path(file["local_path"]) == Path(keeper["local_path"]):
        raise RuntimeError(
            "Extra and keeper point at the same file path. Resolve duplicate database records manually."
        )
    verified_files(o, group, document["options"]["duration_diff"])
    row = fresh_group(o, api, document, group)[0]
    current_file = next(v for v in row["files"] if str(v["id"]) == str(file_id))
    if file_owners(api, current_file) != {str(row["id"])}:
        raise RuntimeError(
            "File is shared with other scenes. Resolve its assignments manually."
        )
    if row.get("interactive"):
        raise RuntimeError("Interactive media requires manual sidecar review.")
    source = features.checked_path(o, file["local_path"])
    target = o.MEDIA_ROOT / QUARANTINE / uuid.uuid4().hex / source.name
    features.checked_path(o, target)
    if os.path.lexists(target):
        raise RuntimeError("Quarantine destination already exists.")
    event = {
        "kind": "duplicate_quarantine",
        "status": "pending",
        "file_id": str(file_id),
        "scene_id": str(row["id"]),
        "keeper_id": str(keeper["id"]),
        "keeper_path": keeper["local_path"],
        "keeper_signature": keeper["signature"],
        "source": str(source),
        "target": str(target),
        "signature": file["signature"],
    }
    event["id"] = journal.append(o.REPORTS_DIR / "duplicate-history.jsonl", event)
    if not api.move(file_id, target.parent):
        raise RuntimeError("Stash did not confirm quarantine. Inspect history/paths.")
    confirm_move(o, api, event)
    validate_move_ownership(o, api, event)
    journal.append(
        o.REPORTS_DIR / "duplicate-history.jsonl", dict(event, status="quarantined")
    )
    print(
        "File quarantined through Stash; still attached and recoverable. No disk space freed yet. Rescan before another action."
    )
    return event


@serialized_action
def quarantine_action(o, api, event, delete=False):
    require_recovered_moves(o)
    if event.get("status") != "quarantined":
        raise RuntimeError("Only confirmed quarantine entries are eligible.")
    target = features.checked_path(o, event["target"])
    if not o.beneath(target, o.MEDIA_ROOT / QUARANTINE):
        raise RuntimeError("Recorded file is outside quarantine.")
    current = api.file(event["file_id"])
    if (
        not current
        or o.canonical(current["path"]) != target
        or not journal.matches(target, event["signature"])
    ):
        raise RuntimeError("Quarantined file changed or its Stash path differs.")
    row = scene(api, event["scene_id"])
    if not row:
        raise RuntimeError("The original scene no longer exists; review manually.")
    if delete:
        keeper = features.checked_path(o, event["keeper_path"])
        if (
            not row["files"]
            or str(row["files"][0]["id"]) != event["keeper_id"]
            or o.canonical(row["files"][0]["path"]) != keeper
            or not journal.matches(keeper, event["keeper_signature"])
        ):
            raise RuntimeError("Keeper changed or is unavailable. Deletion blocked.")
        value = encoding.details(encoding.probe(keeper))
        if (
            not math.isfinite(value["duration"])
            or value["duration"] <= 0
            or not journal.matches(keeper, event["keeper_signature"])
        ):
            raise RuntimeError("Keeper failed fresh inspection; deletion blocked.")
    file = next((v for v in row["files"] if str(v["id"]) == event["file_id"]), None)
    if not file or file_owners(api, file) != {event["scene_id"]}:
        raise RuntimeError("File scene assignments changed. Review manually.")
    if delete:
        journal.append(
            o.REPORTS_DIR / "duplicate-history.jsonl",
            dict(event, status="delete_pending"),
        )
        if not api.gql(
            "mutation($ids:[ID!]!){deleteFiles(ids:$ids)}",
            {"ids": [event["file_id"]]},
            timeout=600,
        )["deleteFiles"]:
            raise RuntimeError(
                "Stash did not confirm deletion. Inspect before retrying."
            )
        if target.exists():
            raise RuntimeError(
                "File still exists after deletion. Inspect Stash; history remains pending."
            )
        journal.append(
            o.REPORTS_DIR / "duplicate-history.jsonl", dict(event, status="deleted")
        )
        print(
            "Stash deleted the quarantined file (or moved it to its configured trash)."
        )
    else:
        source = features.checked_path(o, event["source"])
        if os.path.lexists(source):
            raise RuntimeError("Original location is occupied; nothing overwritten.")
        journal.append(
            o.REPORTS_DIR / "duplicate-history.jsonl",
            dict(event, status="restore_pending"),
        )
        if not api.move(event["file_id"], source.parent):
            raise RuntimeError("Restore not confirmed. Inspect paths.")
        confirm_move(o, api, event, restoring=True)
        validate_move_ownership(o, api, event)
        journal.append(
            o.REPORTS_DIR / "duplicate-history.jsonl", dict(event, status="restored")
        )
        print("Quarantined file restored.")


def strong_pair(a, b, bases):
    if {str(v["id"]) for v in a.get("scenes") or []} & {
        str(v["id"]) for v in b.get("scenes") or []
    }:
        return True

    def fingerprints(file):
        return {
            (v["type"].lower(), v["value"])
            for v in file.get("fingerprints") or []
            if v.get("value") and v["type"].lower() in ("md5", "sha256", "phash")
        }

    return any(
        kind in ("md5", "sha256")
        or kind == "phash"
        and "perceptual hash (distance 0)" in bases
        for kind, _ in fingerprints(a) & fingerprints(b)
    )


def suppress_redundant_encodes(o, document, settings):
    """Use a current reviewed group, never a title-only guess, to avoid another encode."""
    path = o.REPORTS_DIR / "duplicate-review.json"
    if not path.exists():
        document["duplicate_check"] = (
            "No duplicate review available; no files skipped for duplicates."
        )
        return document
    review = json.loads(path.read_text())
    if review.get("roots") != [str(o.INCOMING_ROOT), str(o.MEDIA_ROOT)]:
        document["duplicate_check"] = (
            "Duplicate review uses different paths; rescan to enable duplicate suppression."
        )
        return document
    by_source = {
        f["local_path"]: group for group in review["groups"] for f in group["files"]
    }
    kept = []
    skipped = 0
    profile = encoding.profile(settings)
    inspected = {}
    for job in document["jobs"]:
        group = by_source.get(job["source"])
        redundant = None
        if (
            group
            and compatible_duration(group["files"], review["options"]["duration_diff"])
            and all(
                basis
                in (
                    "attached files",
                    "exact md5",
                    "exact sha256",
                    "perceptual hash (distance 0)",
                )
                for basis in group["basis"]
            )
        ):
            source = next(
                (f for f in group["files"] if f["local_path"] == job["source"]), None
            )
            if (
                source
                and source.get("signature")
                and journal.matches(job["source"], source["signature"])
            ):
                for other in group["files"]:
                    if not strong_pair(source, other, group["basis"]):
                        continue
                    if (
                        other["local_path"] == job["source"]
                        or not other.get("signature")
                        or not fits_profile(info(other), profile)
                    ):
                        continue
                    if not journal.matches(other["local_path"], other["signature"]):
                        continue
                    try:
                        if other["local_path"] not in inspected:
                            inspected[other["local_path"]] = encoding.details(
                                encoding.probe(
                                    features.checked_path(o, other["local_path"])
                                )
                            )
                        value = inspected[other["local_path"]]
                        if (
                            fits_profile(value, profile, strict=True)
                            and abs(value["duration"] - job["duration"])
                            <= review["options"]["duration_diff"]
                        ):
                            redundant = other["local_path"]
                            break
                    except (
                        RuntimeError,
                        OSError,
                        ValueError,
                        subprocess.TimeoutExpired,
                    ):
                        pass
        if redundant:
            skipped += 1
            print("SKIP existing preferred duplicate:", job["source"], "->", redundant)
        else:
            kept.append(job)
    document["jobs"] = kept
    document["skipped"]["existing preferred duplicate"] = skipped
    document["duplicate_check"] = (
        "Checked existing duplicate review; {} redundant encodes omitted.".format(
            skipped
        )
    )
    print(document["duplicate_check"])
    return document


def choose(values, label):
    for index, value in enumerate(values, 1):
        print("{}  {}".format(index, value))
    value = input(label + " (blank cancels): ").strip()
    if not value:
        return None
    if not value.isdigit() or not 1 <= int(value) <= len(values):
        raise RuntimeError("Choose a listed number.")
    return int(value) - 1


def handle(choice, o, settings):
    api = o.API(settings)
    if choice == "32":
        count = len(pending_moves(o))
        if not count:
            print("No pending quarantine moves.")
        elif input(
            "Type RECOVER {} to verify moves and refresh stale Stash paths: ".format(
                count
            )
        ).strip() == "RECOVER " + str(count):
            recover_moves(o, api)
        return settings
    if choice in {"29", "30"}:
        import duplicate_batch

        duplicate_batch.handle(choice, o, api, settings)
        return settings
    if choice == "28":
        old = options(settings)
        names = list(MODES)
        index = choose([MODES[k] for k in names], "Default preference")
        if index is None:
            return settings
        try:
            value = {
                "mode": names[index],
                "phash_distance": int(
                    o.prompt(
                        "Perceptual hash distance (0 exact; larger is less certain)",
                        str(old["phash_distance"]),
                    )
                ),
                "duration_diff": float(
                    o.prompt(
                        "Maximum duration difference in seconds",
                        str(old["duration_diff"]),
                    )
                ),
            }
        except ValueError:
            raise RuntimeError("Use numeric distance/duration values.") from None
        result = dict(settings, duplicates=value)
        options(result)
        o.save_document(o.CONFIG_FILE, result)
        print("Duplicate preferences saved. Rescan to update recommendations.")
        return result
    if choice == "24":
        methods = [
            "attached",
            "phash",
            "fingerprint",
            "stash_id",
            "url",
            "title",
            "all",
        ]
        index = choose(
            [
                "Multiple files on one scene",
                "Stash perceptual hash matches",
                "Exact MD5/SHA256 fingerprints (all-scene metadata scan)",
                "Same Stash IDs",
                "Same URLs",
                "Same titles (weak evidence)",
                "All methods",
            ],
            "Detection method",
        )
        if index is not None:
            report(o, collect(o, api, methods[index], settings))
    elif choice == "25":
        path = o.REPORTS_DIR / "duplicate-review.json"
        if not path.exists():
            raise RuntimeError("Run Find duplicate groups first.")
        document = json.loads(path.read_text())
        current_profile = encoding.profile(settings)
        if document["profile"] != current_profile:
            document["profile"] = current_profile
            for value in document["groups"]:
                value["recommendation"], value["reason"] = recommend(
                    value["files"],
                    value["mode"],
                    current_profile,
                    document["options"]["duration_diff"],
                )
            report(o, document)
        groups = document["groups"]
        index = choose(
            [
                "{} | {} files | {}".format(g["id"], len(g["files"]), g["reason"])
                for g in groups
            ],
            "Group",
        )
        if index is None:
            return settings
        group = groups[index]
        for file in group["files"]:
            print(
                "File {} | {} | {}".format(file["id"], info(file), file["local_path"])
            )
        action = choose(
            [
                "Change recommendation preference",
                "Make a file primary (single scene)",
                "Merge scene metadata and choose primary",
                "Quarantine one non-primary file",
            ],
            "Action",
        )
        if action is None:
            return settings
        if action == 0:
            modes = list(MODES)
            selection = choose([MODES[k] for k in modes], "Preference")
            if selection is not None:
                group["mode"] = modes[selection]
                group["recommendation"], group["reason"] = recommend(
                    group["files"],
                    group["mode"],
                    encoding.profile(settings),
                    document["options"]["duration_diff"],
                )
                report(o, document)
            return settings
        selected = choose(
            ["{} | {}".format(f["id"], f["local_path"]) for f in group["files"]], "File"
        )
        if selected is None:
            return settings
        file_id = str(group["files"][selected]["id"])
        word = {1: "PRIMARY", 2: "MERGE", 3: "QUARANTINE"}[action]
        print(
            "MERGE keeps media but removes source scene records and their generated covers/previews; the keeper scene retains its artwork; conflicting metadata blocks it. History stores a snapshot. Quarantine moves through Stash. Inspect the report and files first."
        )
        if (
            input("Type {} {} to confirm: ".format(word, file_id)).strip()
            != word + " " + file_id
        ):
            return settings
        if action == 3:
            quarantine(o, api, document, group, file_id)
        else:
            apply_primary(o, api, document, group, file_id, merge=action == 2)
    else:
        entries = [
            v
            for v in journal.latest(o.REPORTS_DIR / "duplicate-history.jsonl")
            if v["kind"] == "duplicate_quarantine" and v["status"] == "quarantined"
        ]
        index = choose([v["target"] for v in entries], "Quarantined file")
        if index is None:
            return settings
        event = entries[index]
        delete = choice == "27"
        word = "DELETE" if delete else "RESTORE"
        print("Keeper:", event["keeper_path"])
        print("Quarantined file:", event["target"])
        if delete:
            print(
                "Deletion is permanent unless Stash has a trash path configured. Only this quarantined file is selected."
            )
        if (
            input("Type {} {} to confirm: ".format(word, event["file_id"])).strip()
            == word + " " + event["file_id"]
        ):
            quarantine_action(o, api, event, delete)
    return settings
