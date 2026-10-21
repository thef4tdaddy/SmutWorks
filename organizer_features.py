"""Additional terminal workflows. All Stash changes use its public API."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import encoding_queue as encoding
import organizer_journal as journal

ITEMS = [('15', 'Preferred quality profile'), ('16', 'Open saved reports'),
         ('17', 'Build encoding list from Stash'), ('18', 'Compare duplicate scene files'),
         ('19', 'Retry Whisparr Sync for one scene'), ('20', 'View move and encode history'),
         ('21', 'Undo a recorded move / primary change'), ('22', 'Review finished encodes'),
         ('23', 'Make reviewed encode primary in Stash')]
DESCRIPTIONS = {
    '15': ('ENCODING / SETTINGS', 'Edit bitrate, resolution limits, source bitrate threshold and hardware/software encoder. New lists use these settings; saved lists retain their own settings.'),
    '16': ('REPORTS / OPEN', 'Open the reports folder in Finder, or select a report to open with the default application.'),
    '17': ('STASH / ENCODING', 'Use Stash metadata to build candidates without a folder walk. Verify each selected source on disk before encoding. HDR is checked before encoding starts for that file.'),
    '18': ('DUPLICATES / REVIEW', 'Compare attached files by resolution, codec, duration and size. Read-only report; no automatic deletion.'),
    '19': ('WHISPARR / RETRY', 'Discover the enabled Whisparr Sync plugin, choose one pending scene, and run only that scene through the plugin. Shows job progress and verifies its new Stash paths.'),
    '20': ('HISTORY', 'Review completed and interrupted organizer actions plus finished encodes. History starts with this version.'),
    '21': ('UNDO', 'Restore one completed move or previous primary file choice. Rechecks current file fingerprints, paths and Stash state; occupied destinations are never overwritten.'),
    '22': ('ENCODING / REVIEW', 'Inspect outputs recorded by this app. Compare sizes and duration, verify HEVC output, and save a review report before choosing a primary file.'),
    '23': ('ENCODING / PRIMARY', 'Choose one reviewed output to use as the primary file of its existing Stash scene. Keep the original attached. A bare scan-created scene can be merged; rich or ambiguous metadata requires manual review.')}


def text_report(o, name, text):
    path = o.REPORTS_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Match existing private report permissions and atomic writes.
    import tempfile
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def settings_menu(o, settings):
    target = encoding.profile(settings)
    for key, label in [('width', 'Maximum width'), ('height', 'Maximum height'),
                       ('video_kbps', 'Average video bitrate (kbps)'), ('min_video_kbps', 'Minimum source video bitrate (kbps)'),
                       ('audio_kbps', 'AAC fallback bitrate (kbps)'),
                       ('min_savings_mib', 'Minimum estimated savings (MiB; 0 disables)'),
                       ('min_savings_percent', 'Minimum estimated reduction (%; 0 disables)')]:
        if key == 'min_video_kbps':
            target[key] = max(target[key], target['video_kbps'] + 1000)
        value = o.prompt(label, str(target[key]))
        try:
            target[key] = int(value)
        except ValueError:
            raise RuntimeError('Use a whole number for ' + label) from None
    print('Encoders: auto, vt_h265_10bit (Apple hardware), x265_10bit (software)')
    target['encoder'] = o.prompt('Encoder', target['encoder'])
    target = encoding.config(target)
    result = dict(settings, preferred_quality=target)
    result.pop('encoding', None)
    o.save_document(o.CONFIG_FILE, result)
    print('Preferred quality saved. Rebuild lists and duplicate recommendations to use it. H.265 10-bit / MKV; originals kept.')
    return result


def open_reports(o):
    o.REPORTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = sorted(o.REPORTS_DIR.glob('*.txt'))
    print('0  Open reports folder in Finder')
    for index, path in enumerate(paths, 1):
        print('{}  {}'.format(index, path.name))
    value = input('Report number [0]: ').strip() or '0'
    if not value.isdigit() or not 0 <= int(value) <= len(paths):
        raise RuntimeError('Choose a report from the list.')
    arguments = ['/usr/bin/open', str(o.REPORTS_DIR)] if value == '0' else ['/usr/bin/open', str(paths[int(value) - 1])]
    subprocess.run(arguments, check=True, capture_output=True)
    print('Opened.')


def scoped_file(o, raw, roots):
    path = o.canonical(raw)
    return path if path.is_absolute() and any(o.beneath(path, root) and not any(
        part == '..' or o.protected_name(part) for part in path.relative_to(root).parts) for root in roots) else None


def stash_candidates(o, api, target, roots):
    if any(not root.is_dir() for root in roots):
        raise RuntimeError('Mount the NAS and verify the selected folders before building a list.')
    # Stash narrows the candidates by recorded bitrate; per-file rules apply below.
    criteria = {'bitrate': {'value': target['min_video_kbps'] * 1000 - 1, 'modifier': 'GREATER_THAN'}}
    jobs, skipped, seen = [], {}, set()
    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1
    for scene in api.filtered_scenes(criteria, technical=True):
        for file in scene.get('files') or []:
            if str(file['id']) in seen:
                continue
            seen.add(str(file['id']))
            source = scoped_file(o, file.get('path') or '', roots)
            if source is None:
                skip('outside selected folders / protected')
                continue
            if 'reencoded' in source.stem.casefold() or source.suffix.lower() not in encoding.EXTENSIONS:
                skip('output or unsupported file type')
                continue
            info = {'width': int(file.get('width') or 0), 'height': int(file.get('height') or 0),
                    'codec': file.get('video_codec') or 'unknown', 'duration': float(file.get('duration') or 0),
                    'video_kbps': round(float(file.get('bit_rate') or 0) / 1000), 'hdr': False}
            why, rejected = encoding.reason(info, target)
            if rejected:
                skip(rejected)
                continue
            output = source.with_name(source.stem + ' - reencoded.mkv')
            try:
                if source.is_symlink() or not source.is_file() or not any(o.beneath(source.resolve(), root.resolve()) for root in roots):
                    skip('source unavailable / symlink')
                    continue
                if os.path.lexists(output):
                    skip('output exists')
                    continue
                st = source.stat()
                if time.time() - st.st_mtime < 600:
                    skip('recently modified')
                    continue
                jobs.append({'scene_id': str(scene['id']), 'stash_file_id': str(file['id']),
                    'source': str(source), 'output': str(output), 'size': st.st_size,
                    'mtime_ns': st.st_mtime_ns, 'reason': why, 'verify_before_encode': True, **info})
            except OSError:
                skip('source unavailable')
    counts = {}
    for job in jobs:
        counts[job['output'].casefold()] = counts.get(job['output'].casefold(), 0) + 1
    clean = []
    for job in jobs:
        if counts[job['output'].casefold()] == 1:
            clean.append(job)
        else:
            skip('output-name collision')
    return {'version': 1, 'root': str(o.SHARE_ROOT.resolve()), 'roots': [str(root.resolve()) for root in roots],
        'target': target, 'jobs': encoding.rank_jobs(clean, target), 'skipped': skipped, 'origin': 'stash',
        'scan_stats': {'cached': 0, 'probed': 0}}


def build_stash_list(o, settings):
    print('1 Incoming   2 Organized media   3 Both')
    scope = input('Scope [3]: ').strip() or '3'
    choices = {'1': [o.INCOMING_ROOT], '2': [o.MEDIA_ROOT], '3': [o.INCOMING_ROOT, o.MEDIA_ROOT]}
    if scope not in choices:
        raise RuntimeError('Choose 1, 2 or 3.')
    document = stash_candidates(o, o.API(settings), encoding.profile(settings), choices[scope])
    document = encoding.apply_savings_limits(document)
    import duplicate_review
    duplicate_review.suppress_redundant_encodes(o, document, settings)
    o.save_document(o.ENCODING_FILE, document)
    encoding.write_csv(o.ENCODING_CSV, document)
    lines = ['STASH ENCODING CANDIDATES', '', 'HDR and current source details will be verified before encoding.',
             'Order: largest estimated GiB saved first (estimates, not guaranteed).',
             'Settings: ' + encoding.summary(document['target']), 'Skipped: ' + json.dumps(document['skipped']), '']
    for job in document['jobs']:
        lines.extend([job['source'], '  Output: ' + job['output'], '  ' + encoding.savings_label(job),
                      '  {}x{} | {} | ~{} kbps'.format(job['width'], job['height'], job['codec'], job['video_kbps'])])
    report = text_report(o, 'encoding-candidates.txt', '\n'.join(lines) + '\n')
    print('{} files queued for review; no video probing or folder walk. Nothing encoded.'.format(len(document['jobs'])))
    print('Report:', report)


def compare_duplicates(o, api):
    scenes = list(api.filtered_scenes({'file_count': {'value': 1, 'modifier': 'GREATER_THAN'}}, technical=True))
    lines = ['MULTIPLE-FILE SCENE COMPARISON', '', 'Read-only comparison. Multiple files are not proof of identical content.', '']
    for scene in scenes:
        lines.extend(['Scene {}: {}'.format(scene['id'], scene.get('title') or '(untitled)'),
                      'File ID | Resolution | Codec | Duration (s) | Size (GiB) | Bitrate (kbps)'])
        files = scene.get('files') or []
        for file in sorted(files, key=lambda f: int(f.get('size') or 0), reverse=True):
            lines.append('{} | {}x{} | {} | {:.2f} | {:.3f} | {}'.format(file['id'], file.get('width', 0), file.get('height', 0),
                file.get('video_codec') or '?', float(file.get('duration') or 0), int(file.get('size') or 0) / 1024**3,
                round(float(file.get('bit_rate') or 0) / 1000)))
            lines.append('  ' + str(o.canonical(file.get('path') or '')))
        lines.append('')
    path = text_report(o, 'duplicate-comparison.txt', '\n'.join(lines))
    print('{} scenes compared. No files changed. Report: {}'.format(len(scenes), path))


def wait_job(api, job_id, timeout=900):
    deadline, previous = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        cancellation = getattr(sys.stdout, 'cancelled', None)
        if cancellation is not None and cancellation.is_set():
            raise KeyboardInterrupt
        job = api.gql('query($id:ID!){findJob(input:{id:$id}){id status progress description error}}', {'id': str(job_id)})['findJob']
        if not job:
            raise RuntimeError('Job is no longer in Stash history. Check its logs before retrying: ' + str(job_id))
        label = '{}: {} ({:.0f}%)'.format(job_id, job['status'], float(job.get('progress') or 0) * 100)
        if label != previous:
            print(label, flush=True)
            previous = label
        if job['status'] == 'FINISHED':
            return
        if job['status'] in ('CANCELLED', 'FAILED'):
            raise RuntimeError('Stash job {}: {}'.format(job_id, job.get('error') or job['status']))
        time.sleep(1)
    raise RuntimeError('Job still running. Check Stash before retrying; job ' + str(job_id))


def retry_sync(o, api):
    pending = o.stashdb_incoming_review(api.scenes(technical=True))
    if not pending:
        print('No StashDB-linked scenes remain in incoming.')
        return
    for scene in pending:
        print('Scene {}: {}'.format(scene['id'], scene.get('title') or '(untitled)'))
    scene_id = input('Scene ID to retry (blank cancels): ').strip()
    if not scene_id:
        return
    if scene_id not in {str(scene['id']) for scene in pending}:
        raise RuntimeError('Choose one of the pending scene IDs.')
    plugins = api.gql('query{plugins{id name enabled hooks{name hooks} tasks{name}}}')['plugins'] or []
    plugins = [plugin for plugin in plugins if plugin.get('enabled') and 'whisparr' in (plugin['name'] + plugin['id']).lower()
               and 'sync' in (plugin['name'] + plugin['id']).lower()]
    if len(plugins) != 1:
        raise RuntimeError('Expected one enabled Whisparr Sync plugin; found ' + str(len(plugins)))
    current = api.scene(scene_id)
    if not o.stashdb_incoming_review([current]):
        raise RuntimeError('Scene no longer needs incoming sync review.')
    print('This runs the installed plugin for scene {} and may move its files.'.format(scene_id))
    if input('Type SYNC to continue: ').strip() != 'SYNC':
        print('Cancelled.')
        return
    history = {'kind': 'plugin_retry', 'status': 'pending', 'scene_id': scene_id, 'plugin_id': plugins[0]['id']}
    history['id'] = journal.append(move_history_path(o), history)
    job_id = api.gql('mutation($id:ID!,$args:Map){runPluginTask(plugin_id:$id,args_map:$args,description:"ACO single-scene sync")} ',
        {'id': plugins[0]['id'], 'args': {'hookContext': {'id': int(scene_id), 'type': 'Scene.Update.Post'}}})['runPluginTask']
    journal.append(move_history_path(o), dict(history, status='running', job_id=str(job_id)))
    print('Started Stash job:', job_id)
    wait_job(api, job_id)
    journal.append(move_history_path(o), dict(history, status='done', job_id=str(job_id)))
    refreshed = api.scene(scene_id)
    remaining = o.stashdb_incoming_review([refreshed]) if refreshed else []
    text = ['WHISPARR SYNC RETRY', 'Scene: ' + scene_id, 'Job: ' + str(job_id),
            'Still in incoming: ' + str(bool(remaining))]
    for file in (refreshed or {}).get('files') or []:
        text.append(str(o.canonical(file['path'])))
    print('Report:', text_report(o, 'whisparr-sync-retry.txt', '\n'.join(text)))
    print('Job finished. ' + ('Some files remain in incoming; inspect the plugin logs.' if remaining else 'Stash no longer records files in incoming.'))


def move_history_path(o):
    return o.REPORTS_DIR / 'move-history.jsonl'


def show_history(o):
    moves = journal.latest(move_history_path(o))
    encodes = journal.latest(o.REPORTS_DIR / 'encode-history.jsonl')
    lines = ['ORGANIZER HISTORY', '', 'History begins with actions performed by this version.', '']
    duplicates = journal.latest(o.REPORTS_DIR / 'duplicate-history.jsonl')
    for event in moves + encodes + duplicates:
        lines.append('{} | {} | {} | {}'.format(event.get('time', ''), event['id'], event['kind'], event['status']))
        job = event.get('job', {})
        lines.append('  {} -> {}'.format(event.get('source', job.get('source', '')), event.get('target', job.get('output', ''))))
    path = text_report(o, 'action-history.txt', '\n'.join(lines) + '\n')
    print('{} recorded actions; {} finished encodes. Report: {}'.format(len(moves) + len(duplicates), len(encodes), path))
    for event in moves[-15:]:
        print('{}  {}  {}'.format(event['id'][:10], event['kind'], event['status']))


def checked_path(o, value):
    path = Path(value)
    roots = [o.INCOMING_ROOT, o.MEDIA_ROOT]
    possible = [root for root in roots if o.beneath(path, root) and path != root]
    if not path.is_absolute() or not possible:
        raise RuntimeError('Recorded path is outside the current library/incoming folders.')
    root = max(possible, key=lambda value: len(value.parts))
    parent = root
    for component in path.relative_to(root).parts:
        if component in ('..', '.'):
            raise RuntimeError('Invalid recorded path.')
        parent = parent / component
        if parent.is_symlink():
            raise RuntimeError('Recorded path contains a symlink.')
    if not o.beneath(path.resolve(), root.resolve()):
        raise RuntimeError('Recorded path resolves outside its folder.')
    return path


def exclusive_move(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    reservation = os.fstat(descriptor)
    os.close(descriptor)
    try:
        os.replace(source, target)
    except BaseException:
        if target.exists():
            current = target.stat()
            if (current.st_dev, current.st_ino, current.st_size) == (reservation.st_dev, reservation.st_ino, 0):
                target.unlink()
        raise


def set_primary(api, scene_id, file_id):
    result = api.gql('mutation($input:SceneUpdateInput!){sceneUpdate(input:$input){id files{id path}}}',
        {'input': {'id': str(scene_id), 'primary_file_id': str(file_id)}}, timeout=600)['sceneUpdate']
    files = (result or {}).get('files') or []
    if not files or str(files[0]['id']) != str(file_id):
        raise RuntimeError('Stash did not confirm the primary file. Inspect the scene before retrying.')
    return files


def undo_record(o, api, event):
    if event.get('status') != 'done':
        raise RuntimeError('Only confirmed completed actions can be undone automatically.')
    if event['kind'] == 'primary_change':
        scene = api.scene(event['scene_id'])
        files = (scene or {}).get('files') or []
        if not files or str(files[0]['id']) != event['new_primary'] or event['old_primary'] not in {str(file['id']) for file in files}:
            raise RuntimeError('Primary/file assignments changed. Review this scene manually.')
        for key in ('source', 'target'):
            if not journal.matches(checked_path(o, event[key]), event[key + '_fingerprint']):
                raise RuntimeError('A reviewed file changed; primary undo blocked.')
        if event.get('old_primary_path') and not journal.matches(checked_path(o, event['old_primary_path']), event['old_primary_fingerprint']):
            raise RuntimeError('Previous primary file changed; undo blocked.')
        journal.append(move_history_path(o), dict(event, status='undo_pending'))
        set_primary(api, event['scene_id'], event['old_primary'])
    elif event['kind'] in ('image_move', 'media_move'):
        current = checked_path(o, event['target'])
        original = checked_path(o, event['source'])
        if os.path.lexists(original):
            raise RuntimeError('Original location is occupied; nothing overwritten.')
        if not journal.matches(current, event['fingerprint']):
            raise RuntimeError('Moved file has changed or is unavailable; undo blocked.')
        if event['kind'] == 'media_move':
            file = api.file(event['file_id'])
            if not file or o.canonical(file['path']) != current or current.name != original.name:
                raise RuntimeError('Stash file path changed; undo blocked.')
            journal.append(move_history_path(o), dict(event, status='undo_pending'))
            if not api.move(event['file_id'], original.parent):
                raise RuntimeError('Undo move was not confirmed. Check Stash before retrying.')
            if o.canonical(api.file(event['file_id'])['path']) != original or not journal.matches(original, event['fingerprint']):
                raise RuntimeError('Undo path or physical file differs; inspect Stash before retrying.')
        else:
            journal.append(move_history_path(o), dict(event, status='undo_pending'))
            exclusive_move(current, original)
    else:
        raise RuntimeError('This action type cannot be undone here.')
    journal.append(move_history_path(o), dict(event, status='undone'))
    print('Restored recorded action:', event['id'])


def undo_menu(o, api):
    records = [event for event in journal.latest(move_history_path(o)) if event['status'] == 'done'
               and event['kind'] in ('image_move', 'media_move', 'primary_change')]
    if not records:
        print('No completed actions available for undo.')
        return
    records = list(reversed(records))
    for index, record in enumerate(records, 1):
        print('{}  {}  {}'.format(index, record['kind'], record.get('target', '')))
    value = input('Action number (blank cancels): ').strip()
    if not value:
        return
    if not value.isdigit() or not 1 <= int(value) <= len(records):
        raise RuntimeError('Choose a listed action number.')
    if input('Type UNDO to restore this action: ').strip() != 'UNDO':
        print('Cancelled.')
        return
    undo_record(o, api, records[int(value) - 1])


def review_entry(o, event):
    source = checked_path(o, event['job']['source'])
    output = checked_path(o, event['job']['output'])
    if not journal.matches(source, event['source_fingerprint']) or not journal.matches(output, event['output_fingerprint']):
        raise RuntimeError('Source/output changed or is unavailable since encoding.')
    source_info, output_info = encoding.details(encoding.probe(source)), encoding.details(encoding.probe(output))
    if source_info['duration'] <= 0 or output_info['codec'] != 'hevc' or abs(source_info['duration'] - output_info['duration']) > max(2, source_info['duration'] * .01):
        raise RuntimeError('Output codec/duration check failed.')
    target = encoding.config(event.get('target_settings'))
    if output_info['width'] > target['width'] or output_info['height'] > target['height']:
        raise RuntimeError('Output exceeds the requested resolution limits.')
    original_size, output_size = source.stat().st_size, output.stat().st_size
    return {'encode_id': event['id'], 'source': str(source), 'output': str(output),
        'source_info': source_info, 'output_info': output_info,
        'source_bytes': original_size, 'output_bytes': output_size,
        'saved_percent': 100 * (1 - output_size / original_size) if original_size else 0,
        'source_fingerprint': event['source_fingerprint'], 'output_fingerprint': event['output_fingerprint'],
        'scene_id': event['job'].get('scene_id'), 'stash_file_id': event['job'].get('stash_file_id')}


def review_encodes(o):
    events = [event for event in journal.latest(o.REPORTS_DIR / 'encode-history.jsonl') if event['status'] == 'done' and event['kind'] == 'encode']
    results, lines = [], ['FINISHED ENCODE REVIEW', '', 'Basic integrity checks do not replace visual quality review. Originals are kept.', '']
    for event in events:
        try:
            row = dict(review_entry(o, event), eligible=True)
            lines.extend([row['output'], '  Source: ' + row['source'],
                '  Size: {:.3f} -> {:.3f} GiB; saving {:.1f}%'.format(row['source_bytes'] / 1024**3, row['output_bytes'] / 1024**3, row['saved_percent']),
                '  Duration: {:.2f} -> {:.2f} seconds; HEVC output verified'.format(row['source_info']['duration'], row['output_info']['duration']), ''])
        except (RuntimeError, OSError, ValueError) as error:
            row = {'encode_id': event['id'], 'output': event['job']['output'], 'eligible': False, 'reason': str(error)}
            lines.extend([row['output'], '  REVIEW BLOCKED: ' + row['reason'], ''])
        results.append(row)
    o.save_document(o.REPORTS_DIR / 'encode-review.json', {'results': results})
    report = text_report(o, 'encode-review.txt', '\n'.join(lines))
    print('{} finished encodes reviewed; {} passed basic checks. Report: {}'.format(len(results), sum(row['eligible'] for row in results), report))


DETAIL_FIELDS = 'id title code director details urls date production_date rating100 organized o_counter play_count resume_time play_duration custom_fields groups{group{id}} galleries{id} studio{id} performers{id name gender} tags{id name} stash_ids{endpoint stash_id} files{id path size}'


def exact_scenes(o, api, path):
    scope = o.path_scope(o.stash_paths(path), 'EQUALS')
    query = 'query($scope:SceneFilterType!){findScenes(filter:{per_page:-1},scene_filter:$scope){scenes{' + DETAIL_FIELDS + '}}}'
    scenes = api.gql(query, {'scope': scope})['findScenes']['scenes']
    return [scene for scene in scenes if any(o.canonical(file['path']) == Path(path) for file in scene.get('files') or [])]


def bare_output_scene(scene, path):
    files = scene.get('files') or []
    return (len(files) == 1 and not scene.get('organized') and not scene.get('details') and not scene.get('date')
        and not scene.get('studio') and not scene.get('performers') and not scene.get('tags') and not scene.get('stash_ids')
        and not any(scene.get(key) for key in ('code', 'director', 'urls', 'production_date', 'rating100', 'custom_fields', 'groups', 'galleries', 'o_counter', 'play_count', 'resume_time', 'play_duration'))
        and (scene.get('title') or '') in ('', Path(path).stem, Path(path).name))


def promote_entry(o, api, event):
    row = review_entry(o, event)  # Recheck immediately, even after a saved review.
    source, output = Path(row['source']), Path(row['output'])
    scenes = exact_scenes(o, api, source)
    if len(scenes) != 1:
        raise RuntimeError('Source must belong to exactly one Stash scene.')
    original = scenes[0]
    original_files = original.get('files') or []
    if not original_files:
        raise RuntimeError('Original scene has no primary file.')
    old_primary = str(original_files[0]['id'])
    if row.get('scene_id') and row['scene_id'] != str(original['id']):
        raise RuntimeError('Source scene assignment changed after encoding.')
    outputs = exact_scenes(o, api, output)
    if not outputs:
        print('Scanning this encoded file into Stash...')
        job_id = api.gql('mutation($input:ScanMetadataInput!){metadataScan(input:$input)}', {'input': {
            'paths': [o.stash_path(output)], 'rescan': False, 'scanGenerateCovers': False, 'scanGeneratePreviews': False,
            'scanGenerateImagePreviews': False, 'scanGenerateSprites': False, 'scanGeneratePhashes': False}})['metadataScan']
        wait_job(api, job_id)
        outputs = exact_scenes(o, api, output)
    if len(outputs) != 1:
        raise RuntimeError('Encoded file does not map to exactly one scene after scan. Review in Stash.')
    output_scene = outputs[0]
    output_file = next(file for file in output_scene['files'] if o.canonical(file['path']) == output)
    output_id = str(output_file['id'])
    if str(original['id']) != str(output_scene['id']) and not bare_output_scene(output_scene, output):
        raise RuntimeError('Encoded output belongs to a scene with metadata/history. Merge manually in Stash, then rerun.')
    if old_primary == output_id:
        print('Encoded output is already primary.')
        return
    old_primary_path = checked_path(o, o.canonical(original_files[0]['path']))
    old_primary_fingerprint = journal.fingerprint(old_primary_path)
    source_file_id = str(next(file['id'] for file in original_files if o.canonical(file['path']) == source))
    history = {'kind': 'primary_change', 'status': 'pending', 'scene_id': str(original['id']),
        'old_primary': old_primary, 'new_primary': output_id, 'source_file_id': source_file_id,
        'old_primary_path': str(old_primary_path), 'old_primary_fingerprint': old_primary_fingerprint, 'source': str(source), 'target': str(output),
        'source_fingerprint': row['source_fingerprint'], 'target_fingerprint': row['output_fingerprint']}
    history['id'] = journal.append(move_history_path(o), history)
    if str(original['id']) != str(output_scene['id']):
        api.gql('mutation($input:SceneMergeInput!){sceneMerge(input:$input){id}}', {'input': {
            'source': [str(output_scene['id'])], 'destination': str(original['id']), 'values': {'id': str(original['id']), 'title': original.get('title') or ''}, 'play_history': False, 'o_history': False}}, timeout=600)
    refreshed = api.scene(original['id'])
    ids = {str(file['id']) for file in (refreshed or {}).get('files') or []}
    if output_id not in ids or old_primary not in ids:
        raise RuntimeError('File assignments differ after merge. Inspect Stash before proceeding.')
    updated_files = set_primary(api, original['id'], output_id)
    by_id = {str(file['id']): checked_path(o, o.canonical(file['path'])) for file in updated_files}
    expected = [(source_file_id, row['source_fingerprint']), (output_id, row['output_fingerprint']), (old_primary, old_primary_fingerprint)]
    if any(key not in by_id or not journal.matches(by_id[key], signature) for key, signature in expected):
        raise RuntimeError('A post-update hook changed file locations/content unexpectedly. Inspect Stash; history remains pending.')
    history.update(source=str(by_id[source_file_id]), target=str(by_id[output_id]), old_primary_path=str(by_id[old_primary]))
    journal.append(move_history_path(o), dict(history, status='done'))
    print('Encoded file is primary; original remains attached. Undo is available in move history.')


def promote_menu(o, api):
    report = o.REPORTS_DIR / 'encode-review.json'
    if not report.exists():
        raise RuntimeError('Run Review finished encodes first.')
    reviewed = json.loads(report.read_text())['results']
    valid_ids = {row['encode_id'] for row in reviewed if row.get('eligible')}
    events = [event for event in journal.latest(o.REPORTS_DIR / 'encode-history.jsonl') if event['id'] in valid_ids]
    if not events:
        print('No reviewed outputs available.')
        return
    for index, event in enumerate(events, 1):
        print('{}  {}'.format(index, event['job']['output']))
    number = input('Output number (blank cancels): ').strip()
    if not number:
        return
    if not number.isdigit() or not 1 <= int(number) <= len(events):
        raise RuntimeError('Choose a listed output.')
    print('This imports the output if needed, merges only a bare output scene, and switches the existing scene primary file.')
    print('Original media stays attached. Installed Stash update hooks may also run.')
    if input('Type PROMOTE to apply: ').strip() != 'PROMOTE':
        print('Cancelled.')
        return
    promote_entry(o, api, events[int(number) - 1])


def handle(choice, o, settings):
    if choice == '15':
        return settings_menu(o, settings)
    actions = {'16': lambda: open_reports(o), '17': lambda: build_stash_list(o, settings),
        '18': lambda: compare_duplicates(o, o.API(settings)), '19': lambda: retry_sync(o, o.API(settings)),
        '20': lambda: show_history(o), '21': lambda: undo_menu(o, o.API(settings)),
        '22': lambda: review_encodes(o), '23': lambda: promote_menu(o, o.API(settings))}
    actions[choice]()
    return settings
