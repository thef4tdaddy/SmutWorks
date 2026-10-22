"""Durable encode receipts and verification-only recovery; originals are retained."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import organizer_journal as journal


def receipt_path(source):
    source = Path(source)
    key = hashlib.sha256(source.name.encode()).hexdigest()[:24]
    return source.parent / (".smutworks-encode-" + key + ".json")


def save(source, receipt):
    path = receipt_path(source)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".smutworks-state-", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(receipt, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load(source):
    path = receipt_path(source)
    if not os.path.lexists(path):
        return None
    if path.is_symlink():
        raise RuntimeError("Encode receipt is a symlink; inspect " + str(path))
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as error:
        raise RuntimeError("Cannot read encode receipt: " + str(path)) from error
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("job"), dict)
        or data["job"].get("source") != str(source)
        or not isinstance(data.get("partial"), str)
        or not isinstance(data.get("source_fingerprint"), dict)
        or not isinstance(data.get("target_settings"), dict)
    ):
        raise RuntimeError("Invalid encode receipt; inspect " + str(path))
    return data


def legacy_partials(source):
    source = Path(source)
    prefix = "." + source.stem[:60] + ".aco-partial-"
    return sorted(
        p
        for p in source.parent.iterdir()
        if p.name.startswith(prefix) and p.suffix == ".mkv"
    )


def skip_reason(source):
    source = Path(source)
    receipt = load(source)
    if receipt:
        if receipt.get("status") == "done":
            if journal.matches(source, receipt.get("source_fingerprint")):
                return "previously encoded source (receipt)"
            return "encode receipt needs review: source changed"
        return "unfinished encode: retry verification/review before encoding again"
    if legacy_partials(source):
        return "partial output exists: use Retry verification / recover outputs"
    return None


def finalize(q, source, receipt, journal_path=None):
    """Validate a completed attempt before publishing its final name."""
    source = Path(source)
    job = receipt["job"]
    target = Path(job["output"])
    partial = Path(receipt["partial"])
    expected = source.with_name(source.stem + " - reencoded.mkv")
    if (
        target != expected
        or partial.parent != source.parent
        or partial not in legacy_partials(source)
    ):
        raise RuntimeError("Recovery paths do not match the recorded source.")
    if (
        source.is_symlink()
        or partial.is_symlink()
        or not journal.matches(source, receipt["source_fingerprint"])
    ):
        raise RuntimeError("Source changed or output is a symlink; recovery blocked.")
    if not receipt.get("handbrake_completed"):
        raise RuntimeError(
            "HandBrake completion is unconfirmed; inspect this attempt before recovery."
        )
    if os.path.lexists(target):
        raise RuntimeError("Final output already exists; nothing overwritten.")
    if receipt.get("partial_fingerprint") and not journal.matches(
        partial, receipt["partial_fingerprint"]
    ):
        raise RuntimeError(
            "Partial file changed since HandBrake completed; inspect before recovery."
        )
    output_info = q.verify_output(partial, job, receipt["target_settings"])
    if not journal.matches(source, receipt["source_fingerprint"]):
        raise RuntimeError("Source changed during verification; recovery blocked.")
    # Reserve exclusively. If publish fails, remove only our empty reservation.
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    reservation = os.fstat(fd)
    os.close(fd)
    try:
        current = target.stat()
        if (current.st_dev, current.st_ino, current.st_size) != (
            reservation.st_dev,
            reservation.st_ino,
            0,
        ):
            raise RuntimeError(
                "Target changed during publication; nothing overwritten."
            )
        os.replace(partial, target)
    except BaseException:
        stat = target.stat()
        if (stat.st_dev, stat.st_ino, stat.st_size) == (
            reservation.st_dev,
            reservation.st_ino,
            0,
        ):
            target.unlink()
        raise
    receipt.update(
        status="done",
        output_info=output_info,
        output_fingerprint=journal.fingerprint(target),
    )
    receipt.pop("error", None)
    save(source, receipt)
    if journal_path:
        journal.append(
            journal_path,
            {
                "kind": "encode",
                "status": "done",
                "job": job,
                "target_settings": receipt["target_settings"],
                "output_info": output_info,
                "source_fingerprint": receipt["source_fingerprint"],
                "output_fingerprint": receipt["output_fingerprint"],
            },
        )
    return output_info


def discover(q, roots, target, protected):
    """Find receipts/older partials independently of a newly rebuilt queue."""
    jobs = {}
    for root in roots:
        root = Path(root).resolve()
        if not root.is_dir():
            raise RuntimeError("Recovery root unavailable: " + str(root))
        for folder, dirs, names in os.walk(root, followlinks=False):
            parent = Path(folder)
            dirs[:] = [
                name
                for name in dirs
                if not protected(name) and not (parent / name).is_symlink()
            ]
            partials = [
                parent / name
                for name in names
                if ".aco-partial-" in name and name.endswith(".mkv")
            ]
            sources = [
                parent / name
                for name in names
                if not protected(name)
                and Path(name).suffix.lower() in q.EXTENSIONS
                and "reencoded" not in Path(name).stem.casefold()
            ]
            for source in sources:
                if source.is_symlink() or not source.is_file():
                    continue
                receipt = load(source)
                if receipt and receipt.get("status") != "done":
                    jobs[str(source)] = receipt["job"]
                elif not receipt and any(
                    p.name.startswith("." + source.stem[:60] + ".aco-partial-")
                    for p in partials
                ):
                    stat = source.stat()
                    jobs[str(source)] = {
                        "source": str(source),
                        "output": str(
                            source.with_name(source.stem + " - reencoded.mkv")
                        ),
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    }
    return {
        "version": 1,
        "root": str(Path(os.path.commonpath([str(Path(r).resolve()) for r in roots]))),
        "roots": [str(Path(r).resolve()) for r in roots],
        "target": target,
        "jobs": list(jobs.values()),
    }


def recover(q, document, journal_path=None):
    """Explicit recovery uses the saved queue, including older untracked partials."""
    recovered, failed = 0, 0
    jobs = document["jobs"]
    for position, job in enumerate(jobs, 1):
        source = Path(job["source"])
        root = Path(document["root"]).resolve()
        if (
            not q.inside(source.resolve(), root)
            or source.is_symlink()
            or not source.is_file()
        ):
            continue
        if document.get("roots") and not any(
            q.inside(source.resolve(), Path(r).resolve()) for r in document["roots"]
        ):
            continue
        try:
            receipt = load(source)
            if receipt and receipt.get("status") == "done":
                continue
            if not receipt:
                candidates = legacy_partials(source)
                if not candidates:
                    continue
                # Old names include only 60 stem characters; require an explicit
                # choice and a unique saved source before adopting a candidate.
                matching = [
                    j
                    for j in jobs
                    if Path(j["source"]).parent == source.parent
                    and Path(j["source"]).stem[:60] == source.stem[:60]
                ]
                if len(matching) != 1:
                    raise RuntimeError(
                        "Ambiguous old partial filename; source assignment needs manual review."
                    )
                stat = source.stat()
                if (stat.st_size, stat.st_mtime_ns) != (job["size"], job["mtime_ns"]):
                    raise RuntimeError(
                        "Saved source changed; old partial needs manual review."
                    )
                print("Source:", source)
                for i, path in enumerate(candidates, 1):
                    print("{}: {}".format(i, path))
                selection = input(
                    "Old untracked partial: enter its number to verify/finalize, or Enter to skip: "
                ).strip()
                if not selection.isdigit() or not 1 <= int(selection) <= len(
                    candidates
                ):
                    continue
                print(
                    "Old outputs have no completion receipt. Checks validate codec, resolution and runtime, not every decoded frame. Original is retained."
                )
                if (
                    input(
                        "Type RECOVER to verify and finalize this selected file: "
                    ).strip()
                    != "RECOVER"
                ):
                    continue
                info = q.details(q.probe(source))
                receipt = {
                    "version": 1,
                    "status": "verification_pending",
                    "job": dict(job, duration=info["duration"]),
                    "target_settings": q.config(document.get("target")),
                    "partial": str(candidates[int(selection) - 1]),
                    "source_fingerprint": journal.fingerprint(source),
                    "handbrake_completed": True,
                    "legacy_adoption": True,
                }
                save(source, receipt)
            q.encoding_status(
                filename=source.name,
                position=position,
                total=len(jobs),
                completed=recovered,
                failed=failed,
                stage="Retrying verification",
                progress="",
                percent=100,
            )
            finalize(q, source, receipt, journal_path)
            recovered += 1
            print("RECOVERED:", job["output"])
        except (RuntimeError, OSError) as error:
            failed += 1
            print("RETAINED for review:", source, str(error))
    return "{} outputs recovered without encoding; {} still need review. Originals kept.".format(
        recovered, failed
    )
