"""Append-only local action history; pending entries survive interrupted operations."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import uuid


def append(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = dict(event)
    record.setdefault('id', uuid.uuid4().hex)
    record['time'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'a', encoding='utf-8') as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(record) + '\n')
        output.flush()
        os.fsync(output.fileno())
    return record['id']


def read(path):
    path = Path(path)
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            raise RuntimeError('History has an incomplete line {}. Keep a copy and inspect before continuing.'.format(number)) from None
    return events


def latest(path):
    result = {}
    for event in read(path):
        result[event['id']] = event
    return list(result.values())


def fingerprint(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('File is unavailable or is a symlink: ' + str(path))
    info = path.stat()
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def matches(path, signature):
    try:
        return fingerprint(path) == signature
    except (OSError, RuntimeError):
        return False
