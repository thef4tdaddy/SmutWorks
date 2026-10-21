"""Read-only encoding discovery and an explicit, sequential HandBrakeCLI runner."""
import collections
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import organizer_journal

EXTENSIONS = {'.mp4', '.mkv', '.mov', '.m4v', '.avi', '.wmv', '.ts', '.webm'}
TARGET = {'width': 1920, 'height': 1080, 'video_kbps': 5000, 'audio_kbps': 160, 'audio_mode': 'copy-first-with-aac-fallback'}


DEFAULT_CONFIG = dict(TARGET, encoder='auto', min_video_kbps=6500, min_savings_mib=0, min_savings_percent=0)


def config(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG):
        raise RuntimeError('Unsupported encoding settings; rebuild the encoding list.')
    target = dict(DEFAULT_CONFIG, **value)
    limits = {'width': (320, 7680), 'height': (180, 4320), 'video_kbps': (500, 100000),
              'audio_kbps': (64, 512), 'min_video_kbps': (500, 200000),
              'min_savings_mib': (0, 1000000), 'min_savings_percent': (0, 99)}
    for key, (minimum, maximum) in limits.items():
        if type(target[key]) is not int or not minimum <= target[key] <= maximum:
            raise RuntimeError('Invalid encoding setting: ' + key)
    if target['min_video_kbps'] <= target['video_kbps']:
        raise RuntimeError('Minimum source bitrate must exceed the target video bitrate.')
    if target['encoder'] not in ('auto', 'vt_h265_10bit', 'x265_10bit') or target['audio_mode'] != 'copy-first-with-aac-fallback':
        raise RuntimeError('Unsupported encoder/audio mode.')
    return target


def profile(settings):
    """One profile for discovery, encoding, and duplicate recommendations."""
    return config(settings.get('preferred_quality', settings.get('encoding')))


def apply_savings_limits(document):
    target = config(document['target'])
    jobs, excluded = [], 0
    enabled = target['min_savings_mib'] or target['min_savings_percent']
    for job in rank_jobs(document['jobs'], target):
        if enabled and (job['estimated_savings_bytes'] is None or
            job['estimated_savings_bytes'] < target['min_savings_mib'] * 1024**2 or
            job['estimated_savings_percent'] < target['min_savings_percent']):
            excluded += 1
        else:
            jobs.append(job)
    document['jobs'] = jobs
    document['skipped']['below minimum estimated savings'] = excluded
    return document


def summary(target=None):
    value = config(target)
    return 'H.265 10-bit | max {}x{} | {} kbps video | {} encoder | AAC fallback {} kbps'.format(
        value['width'], value['height'], value['video_kbps'], value['encoder'], value['audio_kbps'])


def rank_jobs(jobs, target=None):
    """Estimate bytes saved, retaining non-video bytes as a conservative audio allowance."""
    target = config(target)
    ranked = []
    for original in jobs:
        job = dict(original)
        duration = float(job.get('duration') or 0)
        size = int(job.get('size') or 0)
        rate = float(job.get('video_kbps') or 0)
        if duration > 0 and math.isfinite(duration) and size > 0 and rate > 0:
            # Audio is copied when supported. Keep the source's entire non-video
            # allowance, at least the configured fallback audio, plus 1% overhead.
            audio_bytes = max(size - duration * rate * 125,
                              duration * target['audio_kbps'] * 125)
            estimated = int((duration * target['video_kbps'] * 125 + audio_bytes) * 1.01)
            saved = max(0, size - estimated)
            job.update(estimated_output_bytes=estimated, estimated_savings_bytes=saved,
                       estimated_savings_percent=round(100 * saved / size, 1))
        else:
            job.update(estimated_output_bytes=None, estimated_savings_bytes=None,
                       estimated_savings_percent=None)
        ranked.append(job)
    return sorted(ranked, key=lambda j: (-(j['estimated_savings_bytes']
                  if j['estimated_savings_bytes'] is not None else -1), j.get('source', '').casefold()))


def savings_label(job):
    saved = job.get('estimated_savings_bytes')
    return ('estimated savings unknown' if saved is None else
            '~{:.2f} GiB ({:.1f}%) smaller'.format(saved / 1024**3, job['estimated_savings_percent']))


def encoding_status(**values):
    import sys
    update = getattr(sys.stdout, 'set_encoding_status', None)
    if update:
        update(**values)


def inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def executable(name):
    path = shutil.which(name)
    if not path:
        for base in ('/opt/homebrew/bin', '/usr/local/bin'):
            candidate = Path(base) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        raise RuntimeError(name + ' was not found. Install it or add it to PATH.')
    return path


def probe(path):
    result = subprocess.run([executable('ffprobe'), '-v', 'error', '-show_streams',
        '-show_format', '-of', 'json', str(path)], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError('ffprobe could not read this file')
    return json.loads(result.stdout)


def details(data):
    video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    if not video:
        raise RuntimeError('No video track')
    fmt = data.get('format', {})
    duration = float(fmt.get('duration') or video.get('duration') or 0)
    rate = float(video.get('bit_rate') or 0)
    if not rate:
        # Container bitrate includes audio; retain an explicitly approximate estimate.
        rate = max(0, float(fmt.get('bit_rate') or 0) - sum(
            float(s.get('bit_rate') or 0) for s in data.get('streams', [])
            if s.get('codec_type') == 'audio'))
    pixel = video.get('pix_fmt') or ''
    import re
    match = re.search(r'(?:p|gray)(10|12|14|16)(?:le|be)?$', pixel)
    depth = 0
    for field in ('bits_per_raw_sample', 'bits_per_sample'):
        try:
            depth = int(video.get(field) or 0)
        except (ValueError, TypeError):
            depth = 0
        if depth > 0:
            break
    if not depth and match:
        depth = int(match.group(1))
    return {'bit_depth': depth, 'width': int(video.get('width') or 0), 'height': int(video.get('height') or 0),
        'codec': video.get('codec_name', 'unknown'), 'video_kbps': round(rate / 1000),
        'duration': duration, 'hdr': video.get('color_transfer') in ('smpte2084', 'arib-std-b67')}


def reason(info, target=None):
    target = config(target)
    if not info['width'] or not info['height'] or not math.isfinite(info['duration']) or info['duration'] <= 0:
        return None, 'unknown dimensions/duration'
    if info['hdr']:
        return None, 'HDR: review separately before converting to SDR'
    if info['video_kbps'] <= 0:
        return None, 'unknown bitrate'
    if info['video_kbps'] < target['min_video_kbps']:
        return None, 'already near/below target bitrate; limited expected savings'
    if info['width'] > target['width'] or info['height'] > target['height']:
        return 'reduce resolution and bitrate', None
    return 'reduce bitrate', None


def valid_cached_info(info):
    return (isinstance(info, dict) and set(info) in ({'width', 'height', 'codec', 'video_kbps', 'duration', 'hdr'},
            {'width', 'height', 'codec', 'video_kbps', 'duration', 'hdr', 'bit_depth'})
        and all(isinstance(info[key], int) and info[key] >= 0 for key in ('width', 'height', 'video_kbps'))
        and (not 'bit_depth' in info or type(info['bit_depth']) is int and info['bit_depth'] >= 0)
        and isinstance(info['duration'], (int, float)) and math.isfinite(info['duration'])
        and isinstance(info['codec'], str) and isinstance(info['hdr'], bool))


def scan(root, protected, cache=None, checkpoint=None, target=None):
    target_config = config(target)
    root = Path(root).resolve()
    if not root.is_dir():
        raise RuntimeError('Scan folder is not accessible')
    jobs, skipped = [], collections.Counter()
    cache = {} if cache is None else cache
    reused, probed = 0, 0
    def fail(error):
        raise error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        parent = Path(folder)
        dirs[:] = sorted(d for d in dirs if not protected(d) and not (parent / d).is_symlink())
        for name in sorted(files):
            source = parent / name
            if protected(name) or source.suffix.lower() not in EXTENSIONS:
                continue
            if source.is_symlink() or not source.is_file():
                skipped['symlink/unavailable'] += 1
                continue
            if 'reencoded' in source.stem.casefold() or '.aco-partial-' in name:
                skipped['reencoded/partial output'] += 1
                continue
            target = source.with_name(source.stem + ' - reencoded.mkv')
            if os.path.lexists(target):
                skipped['output already exists'] += 1
                continue
            stat = source.stat()
            if time.time() - stat.st_mtime < 600:
                skipped['modified in last 10 minutes'] += 1
                continue
            try:
                signature = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
                cached = cache.get(str(source))
                if isinstance(cached, dict) and cached.get('signature') == signature and valid_cached_info(cached.get('info')):
                    info = cached['info']
                    reused += 1
                else:
                    print('Inspecting:', source, flush=True)
                    info = details(probe(source))
                    after_probe = source.stat()
                    if [after_probe.st_size, after_probe.st_mtime_ns, after_probe.st_ctime_ns] != signature:
                        skipped['changed while scanning'] += 1
                        continue
                    cache[str(source)] = {'signature': signature, 'info': info}
                    probed += 1
                    if checkpoint and probed % 25 == 0:
                        checkpoint(cache)
                why, skip = reason(info, target_config)
                if skip:
                    skipped[skip] += 1
                    continue
                if (source.stat().st_size, source.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                    skipped['changed while scanning'] += 1
                    continue
                jobs.append({'source': str(source), 'output': str(target), 'size': stat.st_size,
                    'mtime_ns': stat.st_mtime_ns, 'reason': why, **info})
            except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError):
                skipped['unreadable/probe failed'] += 1
    # Same stem with different source extensions would produce the same destination.
    counts = collections.Counter(j['output'].casefold() for j in jobs)
    clean = [j for j in jobs if counts[j['output'].casefold()] == 1]
    skipped['output-name collision'] += len(jobs) - len(clean)
    return {'version': 1, 'root': str(root), 'target': target_config, 'jobs': rank_jobs(clean, target_config), 'skipped': dict(skipped), 'scan_stats': {'cached': reused, 'probed': probed}}


def write_csv(path, document):
    with Path(path).open('w', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(['Source', 'Output', 'Resolution', 'Codec', 'Approx video kbps', 'Reason', 'Estimated output GiB', 'Estimated savings GiB', 'Estimated reduction %'])
        for job in document['jobs']:
            writer.writerow([job['source'], job['output'], '{}x{}'.format(job['width'], job['height']),
                             job['codec'], job['video_kbps'], job['reason'],
                             job.get('estimated_output_bytes', 0) / 1024**3 if job.get('estimated_output_bytes') is not None else '',
                             job.get('estimated_savings_bytes', 0) / 1024**3 if job.get('estimated_savings_bytes') is not None else '',
                             job.get('estimated_savings_percent', '')])
    Path(path).chmod(0o600)


def encoder_for(handbrake, requested='auto'):
    result = subprocess.run([handbrake, '--help'], capture_output=True, text=True, timeout=30)
    # Hardware availability varies by HandBrake build and Mac.
    if requested != 'auto':
        if requested not in result.stdout:
            raise RuntimeError('Requested encoder is unavailable: ' + requested)
        return requested
    return 'vt_h265_10bit' if 'vt_h265_10bit' in result.stdout else 'x265_10bit'


def command(handbrake, encoder, source, output, target=None):
    target = config(target)
    return [handbrake, '-i', str(source), '-o', str(output), '-f', 'av_mkv',
        '-e', encoder, '-b', str(target['video_kbps']), '--vfr', '-X', str(target['width']), '-Y', str(target['height']),
        '--non-anamorphic', '--crop', '0:0:0:0', '--no-comb-detect', '--no-decomb',
        '-a', '1', '-E', 'copy', '--audio-fallback', 'ca_aac', '-B', str(target['audio_kbps']), '-6', 'stereo']


def execute_encode(arguments):
    import sys
    from types import SimpleNamespace
    if not getattr(sys.stdout, 'aco_capture_subprocess', False):
        return subprocess.run(arguments)
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, errors='replace', bufsize=1)
    try:
        with process.stdout:
            for line in process.stdout:
                print(line.rstrip(), flush=True)
        return SimpleNamespace(returncode=process.wait())
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise


def run(document, lock_path=None, journal_path=None):
    if lock_path is None:
        return _run(document, journal_path)
    import fcntl
    with Path(lock_path).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('This encoding list is already running.') from None
        return _run(document, journal_path)


def _run(document, journal_path=None):
    if document.get('version') != 1:
        raise RuntimeError('Encoding settings changed. Create a new scan first.')
    target_settings = config(document.get('target'))
    root = Path(document['root'])
    if not root.is_dir():
        raise RuntimeError('Scan root is unavailable')
    handbrake = executable('HandBrakeCLI')
    encoder = encoder_for(handbrake, target_settings['encoder'])
    print('Encoder:', encoder, '(software encoding)' if encoder == 'x265_10bit' else '(Apple hardware)')
    completed, skipped = 0, 0
    for position, job in enumerate(rank_jobs(document['jobs'], target_settings), 1):
        encoding_status(filename=Path(job['source']).name, position=position, total=len(document['jobs']),
                        stage='Checking source', progress='')
        source, target = Path(job['source']), Path(job['output'])
        try:
            source.resolve().relative_to(root.resolve())
        except ValueError:
            raise RuntimeError('Saved source is outside the scan root')
        if document.get('roots') and not any(inside(source.resolve(), Path(value).resolve()) for value in document['roots']):
            raise RuntimeError('Source is outside the selected scan folders.')
        expected = source.with_name(source.stem + ' - reencoded.mkv')
        if target != expected or source.is_symlink() or not source.is_file() or os.path.lexists(target):
            print('SKIP changed/unavailable/existing output:', source)
            skipped += 1
            continue
        before = source.stat()
        if (before.st_size, before.st_mtime_ns) != (job['size'], job['mtime_ns']):
            print('SKIP source changed since preview:', source)
            skipped += 1
            continue
        source_info = None
        if job.get('verify_before_encode'):
            source_info = details(probe(source))
            _, skip_reason = reason(source_info, target_settings)
            if skip_reason:
                print('SKIP fresh source inspection:', skip_reason, source)
                skipped += 1
                continue
            job = dict(job, duration=source_info['duration'])
        fd, partial = tempfile.mkstemp(prefix='.' + source.stem[:60] + '.aco-partial-', suffix='.mkv', dir=str(source.parent))
        os.close(fd)
        print('ENCODING:', source, flush=True)
        encoding_status(stage='Encoding')
        try:
            result = execute_encode(command(handbrake, encoder, source, partial, target_settings))
            if result.returncode:
                raise RuntimeError('HandBrake stopped; partial output retained at ' + partial)
            encoding_status(stage='Verifying output', progress='')
            output_info = details(probe(partial))
            after = source.stat()
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise RuntimeError('Source changed during encoding; partial output retained at ' + partial)
            if output_info['codec'] != 'hevc' or abs(output_info['duration'] - job['duration']) > max(2, job['duration'] * .01):
                raise RuntimeError('Encoded output did not pass codec/duration checks; retained at ' + partial)
            # Reserve final name without overwriting any existing file.
            reserve = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(reserve)
            os.replace(partial, target)
            if journal_path:
                organizer_journal.append(journal_path, {'kind': 'encode', 'status': 'done', 'job': job,
                    'target_settings': target_settings, 'output_info': output_info,
                    'source_fingerprint': organizer_journal.fingerprint(source),
                    'output_fingerprint': organizer_journal.fingerprint(target)})
            completed += 1
            encoding_status(stage='Finished', completed=completed)
            print('FINISHED:', target)
        except KeyboardInterrupt:
            raise RuntimeError('Encoding interrupted; original untouched, partial output at ' + partial) from None
    encoding_status(stage='Queue finished', progress='', completed=completed)
    return '{} encoded; {} stale/conflicting entries skipped. Originals kept.'.format(completed, skipped)


def queue_command(queue_path):
    import shlex
    import sys
    return ' '.join(shlex.quote(v) for v in [sys.executable, str(Path(__file__).resolve()), '--queue', str(queue_path)])


if __name__ == '__main__':
    import argparse
    import fcntl
    parser = argparse.ArgumentParser(description='Run a reviewed SmutWorks encoding list.')
    parser.add_argument('--queue', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        with args.queue.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(run(json.loads(args.queue.read_text()), journal_path=args.queue.with_name('encode-history.jsonl')))
    except BlockingIOError:
        parser.exit(1, 'This encoding list is already running.\n')
    except (RuntimeError, OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
