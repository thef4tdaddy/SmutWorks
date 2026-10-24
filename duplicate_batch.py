"""Plan and run duplicate batches through the existing guarded Stash operations."""

import contextlib
import copy
import io
import json
from collections import Counter
from typing import Any, Dict

import duplicate_review as duplicates
import encoding_queue as encoding
import organizer_features as features
import organizer_journal as journal


def build_plan(document, settings):
    profile = encoding.profile(settings)
    mode = duplicates.options(settings)["mode"]
    plan = dict(copy.deepcopy(document), profile=profile, mode=mode, entries=[])
    occurrences = Counter(str(f["id"]) for g in document["groups"] for f in g["files"])
    for number, original in enumerate(document["groups"], 1):
        group = copy.deepcopy(original)
        group["mode"] = "manual" if group.get("mode") == "manual" else mode
        available = [
            f
            for f in group["files"]
            if f.get("signature") and journal.matches(f["local_path"], f["signature"])
        ]
        keeper, reason = duplicates.recommend(
            available, group["mode"], profile, document["options"]["duration_diff"]
        )
        group["recommendation"], group["reason"] = keeper, reason
        issue = None
        if len(group["scenes"]) != 1:
            issue = "Separate Stash scenes: manual merge review needed"
        elif len(available) != len(group["files"]):
            issue = "Unavailable/changed files: check paths or mounted share"
        elif len(group["files"]) < 2 or not keeper:
            issue = "Manual keeper review: " + reason
        elif len({f["local_path"] for f in group["files"]}) != len(group["files"]):
            issue = "Different file records point at the same local path: manual database review"
        elif group["scenes"][0].get("interactive"):
            issue = "Interactive scene: manual sidecar review"
        elif {str(f["id"]) for f in group["scenes"][0]["files"]} != {
            str(f["id"]) for f in group["files"]
        }:
            issue = "Some attached files are excluded from the scan"
        elif any(occurrences[str(f["id"])] > 1 for f in group["files"]):
            issue = "File appears in more than one group"
        elif any(
            {str(s["id"]) for s in f.get("scenes") or []}
            != {str(group["scenes"][0]["id"])}
            for f in group["files"]
        ):
            issue = "Shared or unverified file assignments"
        extras = [f for f in group["files"] if str(f["id"]) != keeper]
        plan["entries"].append(
            dict(
                number=number,
                group=group,
                issue=issue,
                extras=[str(f["id"]) for f in extras],
                bytes=sum(int(f["size"]) for f in extras) if not issue else 0,
            )
        )
    return plan


def report_plan(o, plan):
    eligible = [e for e in plan["entries"] if not e["issue"]]
    count = sum(len(e["extras"]) for e in eligible)
    lines = [
        "SMUTWORKS BULK DUPLICATE PLAN",
        "Keep rule: " + duplicates.MODES[plan["mode"]],
        "Profile: " + encoding.summary(plan["profile"]),
        "Quarantine retains files and uses the same disk space. Deletion is separate.",
        "Groups are checked again before changes; stale or ambiguous groups are skipped.",
        "",
    ]
    for entry in plan["entries"]:
        group = entry["group"]
        lines.append(
            "Group {} | {} | {}".format(
                entry["number"],
                group["scenes"][0].get("title") or group["id"],
                entry["issue"] or "READY",
            )
        )
        for file in group["files"]:
            lines.append(
                "  {} file {}: {}".format(
                    (
                        "KEEP"
                        if str(file["id"]) == group["recommendation"]
                        else "REVIEW" if entry["issue"] else "QUARANTINE"
                    ),
                    file["id"],
                    file["local_path"],
                )
            )
    o.save_document(o.REPORTS_DIR / "duplicate-bulk-plan.json", plan)
    path = features.text_report(o, "duplicate-bulk-plan.txt", "\n".join(lines) + "\n")
    print(
        "{} groups ready; {} extra files to quarantine; {} groups need review.".format(
            len(eligible), count, len(plan["entries"]) - len(eligible)
        )
    )
    print(
        "Potential space after later deletion: {:.2f} GiB. Quarantine frees no space.".format(
            sum(e["bytes"] for e in eligible) / 1024**3
        )
    )
    print("Keep rule:", duplicates.MODES[plan["mode"]])
    print("See report:", path)
    return count


def refresh_after_change(api, group, keeper, event=None):
    """Accept only the intended primary/path change, never arbitrary hook edits."""
    expected = copy.deepcopy(group["scenes"][0])
    if event:
        for file in expected["files"]:
            if str(file["id"]) == event["file_id"]:
                file["path"] = event["target"]
    current = duplicates.scene(api, expected["id"])

    def comparable(row):
        value = copy.deepcopy(row)
        value.pop("updated_at", None)
        value["files"] = sorted(value["files"], key=lambda f: str(f["id"]))
        return value

    if (
        not current
        or comparable(current) != comparable(expected)
        or str(current["files"][0]["id"]) != keeper
    ):
        raise RuntimeError(
            "Unexpected Stash hook/metadata change. Batch stopped; inspect the results before continuing."
        )
    group["scenes"] = [current]
    if event:
        group["files"] = [f for f in group["files"] if str(f["id"]) != event["file_id"]]


def save_results(o, results):
    o.save_document(o.REPORTS_DIR / "duplicate-bulk-results.json", results)
    features.text_report(
        o,
        "duplicate-bulk-results.txt",
        "\n".join(
            "{} | {} | {}".format(row["group"], row["status"], row.get("message", ""))
            for row in results["entries"]
        )
        + "\n",
    )


def show_results(o, results):
    done = sum(row["status"] == "done" for row in results["entries"])
    skipped = sum(row["status"] == "skipped" for row in results["entries"])
    print("\nBATCH {}".format(results["status"].upper()))
    if results.get("kind") == "cleanup":
        print(
            "Groups completed: {} / {} planned".format(done, results["planned_groups"])
        )
        print(
            "Groups skipped by fresh checks: {} | Unfinished: {}".format(
                skipped, results["planned_groups"] - done - skipped
            )
        )
        print(
            "Groups needing separate review: {}".format(
                results["initial_review_groups"] + skipped
            )
        )
        print(
            "Keeper selections confirmed: {} | Primary files changed: {}".format(
                results["keepers"], results["primary_changes"]
            )
        )
        print(
            "Extra files quarantined: {} ({:.2f} GiB)".format(
                results["quarantined"], results["quarantined_bytes"] / 1024**3
            )
        )
        print(
            "Space freed: 0 GiB. Quarantined files are retained until separately deleted."
        )
    else:
        print(
            "Files {}: {} / {} planned ({:.2f} GiB)".format(
                results["kind"],
                done,
                results["planned_files"],
                results["bytes_handled"] / 1024**3,
            )
        )
        print(
            "Files still needing attention: {}".format(results["planned_files"] - done)
        )
        if results["kind"] == "deleted":
            print(
                "Deleted size is logical file size; Stash trash/hard links can delay or reduce reclaimed space."
            )
    print("Details:", o.REPORTS_DIR / "duplicate-bulk-results.txt")


@duplicates.serialized_action
def apply_plan(o, api, plan):
    duplicates.require_recovered_moves(o)
    ready = [e for e in plan["entries"] if not e["issue"]]
    results: Dict[str, Any] = {
        "kind": "cleanup",
        "entries": [],
        "quarantined": 0,
        "quarantined_bytes": 0,
        "keepers": 0,
        "primary_changes": 0,
        "planned_groups": len(ready),
        "initial_review_groups": len(plan["entries"]) - len(ready),
        "status": "running",
    }
    save_results(o, results)
    for position, entry in enumerate(ready, 1):
        group = copy.deepcopy(entry["group"])
        record = {"group": group["id"], "status": "checking", "message": ""}
        results["entries"].append(record)
        save_results(o, results)
        print(
            "Group {}/{}: checking {} files".format(
                position, len(ready), len(group["files"])
            ),
            flush=True,
        )
        try:
            rows = duplicates.fresh_group(o, api, plan, group)
            if len(rows) != 1 or rows[0].get("interactive"):
                raise RuntimeError("Scene needs manual review")
            values = duplicates.verified_files(
                o, group, plan["options"]["duration_diff"]
            )
            if any(
                duplicates.file_owners(api, f) != {str(rows[0]["id"])}
                for f in rows[0]["files"]
            ):
                raise RuntimeError("File shared with another scene")
            # Verify the recommendation against freshly inspected media too.
            live = [
                dict(
                    f,
                    **values[i],
                    bit_rate=values[i]["video_kbps"] * 1000,
                    video_codec=values[i]["codec"],
                )
                for i, f in enumerate(group["files"])
            ]
            chosen, _ = duplicates.recommend(
                live, group["mode"], plan["profile"], plan["options"]["duration_diff"]
            )
            if chosen != group["recommendation"]:
                raise RuntimeError(
                    "Fresh inspection changed the recommended keeper; rescan/review"
                )
            keeper_index = next(
                i for i, f in enumerate(group["files"]) if str(f["id"]) == chosen
            )
            if (
                group["mode"] in ("preferred", "smallest")
                and duplicates.fits_profile(
                    duplicates.info(group["files"][keeper_index]), plan["profile"]
                )
                and not duplicates.fits_profile(
                    values[keeper_index], plan["profile"], strict=True
                )
            ):
                raise RuntimeError(
                    "Preferred profile could not be confirmed by fresh inspection; manual review needed"
                )
        except (RuntimeError, OSError) as error:
            record.update(status="skipped", message=str(error))
            save_results(o, results)
            continue
        except BaseException:
            results["status"] = "interrupted"
            record["status"] = "interrupted before changes"
            save_results(o, results)
            raise
        try:
            record["status"] = "applying"
            save_results(o, results)
            keeper = group["recommendation"]
            with contextlib.redirect_stdout(io.StringIO()):
                duplicates.apply_primary(o, api, plan, group, keeper)
                results["keepers"] += 1
                results["primary_changes"] += (
                    str(group["scenes"][0]["files"][0]["id"]) != keeper
                )
                refresh_after_change(api, group, keeper)
                for file_id in entry["extras"]:
                    event = duplicates.quarantine(o, api, plan, group, file_id)
                    results["quarantined"] += 1
                    results["quarantined_bytes"] += event["signature"]["size"]
                    save_results(o, results)
                    refresh_after_change(api, group, keeper, event)
            record["status"] = "done"
        except (RuntimeError, OSError) as error:
            record.update(status="stopped", message=str(error))
            results["status"] = "stopped"
            save_results(o, results)
            break
        except BaseException:
            results["status"] = "interrupted"
            record["status"] = "interrupted: inspect duplicate history"
            save_results(o, results)
            raise
        save_results(o, results)
    else:
        results["status"] = "complete"
    save_results(o, results)
    show_results(o, results)
    print(
        "Rescan before another duplicate cleanup. Use Bulk quarantine restore / delete when ready."
    )
    return results


@duplicates.serialized_action
def manage_quarantine(o, api, entries, delete=False):
    results: Dict[str, Any] = {
        "kind": "deleted" if delete else "restored",
        "planned_files": len(entries),
        "bytes_handled": 0,
        "entries": [],
        "status": "running",
    }
    for position, event in enumerate(entries, 1):
        print(
            "{} {}/{}".format(
                "Deleting" if delete else "Restoring", position, len(entries)
            ),
            flush=True,
        )
        row = {"group": event["file_id"], "status": "checking", "message": ""}
        results["entries"].append(row)
        save_results(o, results)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                duplicates.quarantine_action(o, api, event, delete)
            row["status"] = "done"
            results["bytes_handled"] += event["signature"]["size"]
        except (RuntimeError, OSError) as error:
            row.update(status="stopped", message=str(error))
            results["status"] = "stopped"
            save_results(o, results)
            break
        except BaseException:
            row["status"] = "interrupted: inspect duplicate history"
            results["status"] = "interrupted"
            save_results(o, results)
            raise
        save_results(o, results)
    else:
        results["status"] = "complete"
    save_results(o, results)
    show_results(o, results)
    return results


def handle(choice, o, api, settings):
    if choice == "29":
        path = o.REPORTS_DIR / "duplicate-review.json"
        if not path.exists():
            raise RuntimeError("Run Find duplicate groups first.")
        plan = build_plan(json.loads(path.read_text()), settings)
        count = report_plan(o, plan)
        if count and input(
            "Type QUARANTINE {} to apply this plan (Enter cancels): ".format(count)
        ).strip() == "QUARANTINE " + str(count):
            apply_plan(o, api, plan)
    else:
        entries = [
            e
            for e in journal.latest(o.REPORTS_DIR / "duplicate-history.jsonl")
            if e["kind"] == "duplicate_quarantine" and e["status"] == "quarantined"
        ]
        if not entries:
            print("No recorded quarantined files.")
            return
        selection = duplicates.choose(
            [
                "Restore all recorded quarantined files",
                "Delete all recorded quarantined files",
            ],
            "Quarantine action",
        )
        if selection is None:
            return
        delete = selection == 1
        word = "DELETE" if delete else "RESTORE"
        print(
            "{} recorded files. Each keeper/path is checked again.".format(len(entries))
        )
        features.text_report(
            o,
            "duplicate-quarantine-batch.txt",
            "\n".join(e["target"] for e in entries) + "\n",
        )
        print("Full list:", o.REPORTS_DIR / "duplicate-quarantine-batch.txt")
        if delete:
            print(
                "Deletion removes these files through Stash; recovery depends on its trash configuration."
            )
        if input(
            "Type {} {} to confirm (Enter cancels): ".format(word, len(entries))
        ).strip() == "{} {}".format(word, len(entries)):
            manage_quarantine(o, api, entries, delete)
