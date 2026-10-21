#!/usr/bin/env python3
"""Adult Content Organizer: Stash-driven organization and incoming-folder cleanup."""
import collections
import getpass
import organizer_journal
import encoding_queue
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import textwrap
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SHARE_ROOT = Path('/Volumes/Secondary Storage')
MEDIA_ROOT = SHARE_ROOT / 'sex/media/scenes'
INCOMING_ROOT = SHARE_ROOT / 'SeedboxSync/xxx'
APP_VERSION = '0.9.0'
STATE_DIR = Path.home() / 'Library/Application Support/Adult Content Organizer'
CONFIG_FILE = STATE_DIR / 'settings.json'
PROBE_CACHE_FILE = STATE_DIR / 'video-probe-cache.json'
REPORTS_DIR = Path(__file__).resolve().parent / 'reports'
PLAN_FILE = REPORTS_DIR / 'organization-preview.json'
CLEANUP_FILE = REPORTS_DIR / 'cleanup-preview.json'
ENCODING_FILE = REPORTS_DIR / 'encoding-preview.json'
ENCODING_CSV = REPORTS_DIR / 'encoding-list.csv'
MAX_DEPTH = 6
SMALL_FILE_LIMIT = 10 * 1024 * 1024
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.avif', '.heic'}
VIDEO_EXTENSIONS = encoding_queue.EXTENSIONS | {'.mpg', '.mpeg', '.m2ts', '.mts', '.vob', '.flv', '.3gp', '.ogv'}
LEFTOVER_IMAGES = 'leftover images'
PROTECTED = {LEFTOVER_IMAGES, '.sync', '.archive', '.stfolder', '.stignore', '.synclist', '@eadir', '#recycle'}
FLAG_TITLE = 'ACO - Needs title'
FLAG_PERFORMER = 'ACO - Needs female performer'
FLAG_GENERAL = 'ACO - Needs metadata'
MANAGED_FLAGS = {FLAG_TITLE, FLAG_PERFORMER, FLAG_GENERAL}

SCENE_FIELDS = '''id title stash_ids { endpoint stash_id } tags { id name }
 performers { id name gender } files { id path size }'''
SCENES_QUERY = '''query($page:Int!, $scope:SceneFilterType!) {
 findScenes(filter:{page:$page,per_page:100,sort:"id",direction:ASC}, scene_filter:$scope) {
 count scenes { ''' + SCENE_FIELDS + ''' } } }'''
AUDIT_SCENES_QUERY = SCENES_QUERY.replace('files { id path size }', 'files { id path size width height video_codec duration bit_rate frame_rate }')
SINGLE_QUERY = 'query($id:ID!){findScene(id:$id){' + SCENE_FIELDS + '}}'


def log(message):
    print(message, flush=True)


def set_paths(settings):
    global SHARE_ROOT, MEDIA_ROOT, INCOMING_ROOT
    SHARE_ROOT = Path(settings['share_root']).expanduser()
    for field in ('incoming_relative', 'media_relative'):
        relative = Path(settings[field])
        if relative.is_absolute() or '..' in relative.parts or str(relative) in ('', '.'):
            raise RuntimeError('Folder paths inside the share must be nonempty relative paths without ..')
    MEDIA_ROOT = SHARE_ROOT / settings['media_relative']
    INCOMING_ROOT = SHARE_ROOT / settings['incoming_relative']


def stash_path(path):
    return str(Path('/media') / Path(path).relative_to(SHARE_ROOT))


def normalized(text):
    return unicodedata.normalize('NFKC', text).strip().casefold()


def performer_folder(root, name, matches):
    if len(matches) > 1:
        return None
    if matches:
        return matches[0]
    component = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .')
    if not component or component in ('.', '..'):
        return None
    folder = root / component
    # A same-named symlink or non-directory must never become a new destination.
    if os.path.lexists(folder) and (folder.is_symlink() or not folder.is_dir()):
        return None
    return folder


def canonical(path):
    path = Path(path)
    # These two bind mounts point to the same NAS directory in this installation.
    if path == Path('/incoming') or Path('/incoming') in path.parents:
        return SHARE_ROOT / path.relative_to('/incoming')
    if path == Path('/media') or Path('/media') in path.parents:
        return SHARE_ROOT / path.relative_to('/media')
    return path


def beneath(path, root):
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False


def has_stashdb(scene):
    for item in scene.get('stash_ids') or []:
        value = item.get('endpoint') or ''
        try:
            host = urllib.parse.urlsplit(value if '://' in value else '//' + value).hostname or ''
        except ValueError:
            continue
        if host.lower() == 'stashdb.org' or host.lower().endswith('.stashdb.org'):
            return True
    return False


def choose_performer(scene):
    performers = [p for p in scene.get('performers') or []
                  if p.get('gender') in ('FEMALE', 'TRANSGENDER_FEMALE')
                  and (p.get('name') or '').strip()]
    return min(performers, key=lambda p: (normalized(p['name']), str(p['id']))) if performers else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, settings):
        self.url = settings['stash_url'].rstrip('/')
        if not self.url.endswith('/graphql'):
            self.url += '/graphql'
        self.headers = {'Content-Type': 'application/json'}
        if settings.get('api_key'):
            self.headers['ApiKey'] = settings['api_key']
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def gql(self, query, variables=None, timeout=60):
        request = urllib.request.Request(self.url,
            data=json.dumps({'query': query, 'variables': variables or {}}).encode(),
            headers=self.headers, method='POST')
        try:
            with self.opener.open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError('Stash HTTP ' + str(error.code)) from None
        if result.get('errors'):
            messages = '; '.join(str(e.get('message', 'GraphQL error')) for e in result['errors'])
            raise RuntimeError(messages[:500])
        if result.get('data') is None:
            raise RuntimeError('Stash returned no data')
        return result['data']

    def supports_move(self):
        data = self.gql('query{__type(name:"Mutation"){fields{name}}}')
        return any(f['name'] == 'moveFiles' for f in (data.get('__type') or {}).get('fields', []))

    def scenes(self, technical=False):
        incoming = stash_path(INCOMING_ROOT)
        alias = '/incoming/' + str(INCOMING_ROOT.relative_to(SHARE_ROOT))
        scope = {'path': {'value': incoming, 'modifier': 'INCLUDES'},
                 'OR': {'path': {'value': alias, 'modifier': 'INCLUDES'}}}
        yield from self.filtered_scenes(scope, technical=technical)

    def filtered_scenes(self, scope, technical=False):
        page = 1
        while True:
            result = self.gql(AUDIT_SCENES_QUERY if technical else SCENES_QUERY, {'page': page, 'scope': scope})['findScenes']
            scenes = result['scenes']
            yield from scenes
            if len(scenes) < 100:
                break
            page += 1

    def scene(self, scene_id):
        return self.gql(SINGLE_QUERY, {'id': str(scene_id)})['findScene']

    def move(self, file_id, folder):
        query = 'mutation($input:MoveFilesInput!){moveFiles(input:$input)}'
        return self.gql(query, {'input': {'ids': [str(file_id)], 'destination_folder': stash_path(folder)}}, timeout=600)['moveFiles']

    def file(self, file_id):
        return self.gql('query($id:ID!){findFile(id:$id){id path size}}', {'id': str(file_id)})['findFile']

    def ensure_tag(self, name):
        result = self.gql('''query($name:String!){findTags(
          tag_filter:{name:{value:$name,modifier:EQUALS}},filter:{per_page:10}){tags{id name}}}''', {'name': name})
        tags = result['findTags']['tags']
        if len(tags) == 1:
            return str(tags[0]['id'])
        if tags:
            raise RuntimeError('Multiple tags with the same organizer flag name')
        return str(self.gql('mutation($name:String!){tagCreate(input:{name:$name}){id}}', {'name': name})['tagCreate']['id'])

    def change_tags(self, scene_id, ids, mode):
        if ids:
            self.gql('mutation($input:BulkSceneUpdateInput!){bulkSceneUpdate(input:$input){id}}',
                {'input': {'ids': [str(scene_id)], 'tag_ids': {'ids': list(ids), 'mode': mode}}}, timeout=600)


def existing_folders(root, names):
    index = collections.defaultdict(list)
    if not root.is_dir():
        raise RuntimeError('Media root is not accessible: ' + str(root))
    def fail(error):
        raise error
    for directory, subdirs, _ in os.walk(root, followlinks=False, onerror=fail):
        path = Path(directory)
        depth = len(path.relative_to(root).parts)
        if path != root and normalized(path.name) in names:
            if not path.is_symlink() and beneath(path.resolve(), root.resolve()):
                index[normalized(path.name)].append(path)
            subdirs[:] = []
            continue
        if depth >= MAX_DEPTH:
            subdirs[:] = []
        else:
            subdirs[:] = [name for name in subdirs if not protected_name(name)
                          and not (path / name).is_symlink()]
    return index


def eligible(scene):
    if not (scene.get('title') or '').strip():
        return None, 'no-title'
    if has_stashdb(scene):
        return None, 'has-stashdb'
    performer = choose_performer(scene)
    return (performer, None) if performer else (None, 'no-female-performer')


def desired_flags(scene):
    flags = set()
    if not (scene.get('title') or '').strip():
        flags.add(FLAG_TITLE)
    if choose_performer(scene) is None:
        flags.add(FLAG_PERFORMER)
    if flags:
        flags.add(FLAG_GENERAL)
    return flags


def metadata_flags_task(api, apply=False):
    changes, flagged = 0, 0
    tag_cache = {}
    # Take a stable read snapshot before applying tag updates to avoid pagination changes.
    scenes = list(api.scenes())
    for original in scenes:
        scene = api.scene(original['id']) if apply else original
        if not scene or has_stashdb(scene):
            continue
        if not any(beneath(canonical(f.get('path') or ''), INCOMING_ROOT)
                   for f in scene.get('files') or []):
            continue
        wanted = desired_flags(scene)
        current = {t['name']: str(t['id']) for t in scene.get('tags') or []}
        additions = wanted - set(current)
        removals = (set(current) & MANAGED_FLAGS) - wanted
        if wanted:
            flagged += 1
        if not additions and not removals:
            continue
        changes += 1
        log('{} FLAGS scene {}: add [{}]; remove [{}]'.format(
            'UPDATE' if apply else 'PREVIEW', scene['id'], ', '.join(sorted(additions)), ', '.join(sorted(removals))))
        if apply:
            for name in additions:
                if name not in tag_cache:
                    tag_cache[name] = api.ensure_tag(name)
            api.change_tags(scene['id'], [current[name] for name in removals], 'REMOVE')
            api.change_tags(scene['id'], [tag_cache[name] for name in additions], 'ADD')
    return '{} incoming scenes need metadata; {} scene flag updates {}.'.format(
        flagged, changes, 'applied' if apply else 'previewed, nothing changed')


def build_plan(scenes, root, incoming, details=None):
    candidates = []
    skipped = collections.Counter()
    def record(scene, item, reason, missing=None):
        skipped[reason] += 1
        if details is not None:
            details.append({'scene_id': str(scene['id']), 'file_id': str(item.get('id', '')),
                'title': scene.get('title') or '', 'source': item.get('path') or '',
                'reason': reason, 'missing': missing or []})
    for scene in scenes:
        performer, metadata_reason = eligible(scene)
        for item in scene.get('files') or []:
            raw = item.get('path') or ''
            path = canonical(raw)
            if not path.is_absolute() or not beneath(path, incoming) or not beneath(path.resolve(), incoming.resolve()):
                record(scene, item, 'outside-incoming')
                continue
            if any(protected_name(part) for part in path.relative_to(incoming).parts):
                record(scene, item, 'protected-incoming-path')
                continue
            if has_stashdb(scene):
                record(scene, item, 'has-stashdb')
                continue
            if Path(raw).is_symlink() or path.is_symlink() or not path.is_file():
                record(scene, item, 'missing-or-symlink')
                continue
            if metadata_reason:
                missing = []
                if not (scene.get('title') or '').strip():
                    missing.append('title')
                if choose_performer(scene) is None:
                    missing.append('female performer')
                record(scene, item, metadata_reason, missing)
                continue
            candidates.append((scene, performer, item, path))
    names = {normalized(p['name']) for _, p, _, _ in candidates}
    folders = existing_folders(root, names) if names else {}
    plan = []
    for scene, performer, item, path in candidates:
        matches = folders.get(normalized(performer['name']), [])
        folder = performer_folder(root, performer['name'], matches)
        if folder is None:
            record(scene, item, 'unsafe-performer-folder' if len(matches) < 2 else 'ambiguous-performer-folder')
            continue
        target = folder / 'not on stash' / path.name
        if not beneath(target.parent.resolve(), root.resolve()):
            record(scene, item, 'unsafe-target')
            continue
        if os.path.lexists(str(target)):
            record(scene, item, 'destination-exists')
            continue
        plan.append({'scene_id': str(scene['id']), 'file_id': str(item['id']),
                     'performer_id': str(performer['id']), 'performer': performer['name'],
                     'source': item['path'], 'target': str(target),
                     'size': path.stat().st_size})
    counts = collections.Counter(normalized(p['target']) for p in plan)
    ids = collections.Counter(p['file_id'] for p in plan)
    clean = []
    for item in plan:
        if counts[normalized(item['target'])] > 1 or ids[item['file_id']] > 1:
            skipped['plan-collision'] += 1
            if details is not None:
                details.append(dict(item, reason='plan-collision', missing=[]))
        else:
            clean.append(item)
    return clean, dict(skipped)


def save_document(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(document, output, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


REASON_LABELS = {
    'has-stashdb': 'Managed by Whisparr Sync (StashDB linked)',
    'outside-incoming': 'File is outside incoming',
    'protected-incoming-path': 'Protected or excluded path',
    'missing-or-symlink': 'File is unavailable on the mounted share, or is a symlink',
    'unsafe-performer-folder': 'Performer folder name/path needs review',
    'ambiguous-performer-folder': 'More than one matching performer folder; resolve ambiguity',
    'unsafe-target': 'Destination resolves outside the organized root',
    'destination-exists': 'Destination file already exists; resolve the conflict',
    'plan-collision': 'More than one proposed file uses the same destination or file ID'}


def organization_report(plan, details, supported):
    missing = [entry for entry in details if entry.get('missing')]
    other = [entry for entry in details if not entry.get('missing') and entry['reason'] != 'has-stashdb']
    delegated = [entry for entry in details if entry['reason'] == 'has-stashdb']
    report = PLAN_FILE.with_suffix('.txt')
    lines = ['ADULT CONTENT ORGANIZER - ORGANIZATION PREVIEW', '',
        'Incoming: ' + str(INCOMING_ROOT), 'Organized root: ' + str(MEDIA_ROOT), '',
        '{} files can be organized.'.format(len(plan)),
        '{} files need metadata edits before they can be moved.'.format(len(missing)),
        '{} files have other issues; {} files are managed by Whisparr Sync.'.format(len(other), len(delegated)),
        'Nothing moved. This report covers files indexed in Stash.',
        'Native Stash move API: ' + ('available' if supported else 'UNAVAILABLE - moving is disabled'), '',
        'FILES NEEDING METADATA EDITS', '============================']
    if not missing:
        lines.append('None.')
    for index, entry in enumerate(missing, 1):
        lines.extend(['', '{}. {}'.format(index, canonical(entry['source'])),
            '   Stash scene ID: ' + entry['scene_id'], '   Current title: ' + (entry['title'] or '(blank)'),
            '   Add/edit in Stash: ' + ', '.join(entry['missing'])])
    lines.extend(['', 'PROPOSED MOVES', '=============='])
    if not plan:
        lines.append('None.')
    for index, entry in enumerate(plan, 1):
        lines.extend(['', '{}. {}'.format(index, canonical(entry['source'])),
            '   Stash scene ID: ' + entry['scene_id'], '   Performer: ' + entry['performer'],
            '   Destination: ' + entry['target']])
    for title, entries in [('OTHER ISSUES', other), ('LEFT TO WHISPARR SYNC', delegated)]:
        lines.extend(['', title, '=' * len(title)])
        if not entries:
            lines.append('None.')
        for index, entry in enumerate(entries, 1):
            lines.extend(['', '{}. {}'.format(index, canonical(entry['source'])),
                '   Stash scene ID: ' + entry['scene_id'],
                '   Reason: ' + REASON_LABELS.get(entry['reason'], entry['reason'])])
    report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=str(report.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write('\n'.join(lines) + '\n')
        os.replace(temporary, report)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    summary = ['{} files can be organized.'.format(len(plan)),
        '{} files need metadata edits before they can be moved.'.format(len(missing))]
    if other:
        summary.append('{} files have other issues listed in the report.'.format(len(other)))
    if delegated:
        summary.append('{} files left to Whisparr Sync.'.format(len(delegated)))
    if not supported:
        summary.append('Stash move API unavailable; moving is disabled.')
    summary.extend(['See report for the full list and missing information:', str(report), 'Nothing moved.'])
    return '\n'.join(summary)


STASH_CLEANUP_FILTERS = [
    ('multiple_files', 'File count greater than 1', {'file_count': {'value': 1, 'modifier': 'GREATER_THAN'}}),
    ('above_1080p', 'Resolution greater than 1080p', {'resolution': {'value': 'FULL_HD', 'modifier': 'GREATER_THAN'}}),
    ('not_hevc', 'Video codec is not HEVC', {'video_codec': {'value': 'hevc', 'modifier': 'NOT_EQUALS'}})]


def stashdb_incoming_review(scenes):
    matches = []
    for scene in scenes:
        stashdb_ids = [item['stash_id'] for item in scene.get('stash_ids') or []
            if item.get('stash_id') and has_stashdb({'stash_ids': [item]})]
        if not stashdb_ids:
            continue
        incoming_files = []
        for file in scene.get('files') or []:
            path = canonical(file.get('path') or '')
            if not path.is_absolute() or not beneath(path, INCOMING_ROOT):
                continue
            relative = path.relative_to(INCOMING_ROOT)
            if '..' in relative.parts or any(protected_name(part) for part in relative.parts):
                continue
            incoming_files.append(file)
        if not incoming_files:
            continue
        review = dict(scene, files=incoming_files)
        issues = []
        if not (scene.get('title') or '').strip():
            issues.append('Add a scene title in Stash')
        if choose_performer(scene) is None:
            issues.append('Review female performer assignment in Stash')
        issues.append('File still in incoming/sync; review or run Whisparr Sync')
        review['review_notes'] = issues
        review['stashdb_ids'] = stashdb_ids
        matches.append(review)
    return matches


def stash_cleanup_report(api):
    sections = []
    for key, title, criteria in STASH_CLEANUP_FILTERS:
        log('Checking Stash: ' + title)
        # Ask Stash to apply its own filter rules rather than approximating them.
        scenes = list(api.filtered_scenes(criteria, technical=True))
        sections.append({'key': key, 'title': title, 'criteria': criteria, 'scenes': scenes})
    log('Checking StashDB-linked scenes still in incoming/sync')
    pending_sync = stashdb_incoming_review(api.scenes(technical=True))
    sections.append({'key': 'stashdb_incoming', 'title': 'StashDB linked and still in incoming - needs sync review',
        'criteria': {'stashdb_endpoint': 'stashdb.org', 'incoming': str(INCOMING_ROOT)}, 'scenes': pending_sync})
    counts = {section['key']: len(section['scenes']) for section in sections}
    summary = [
        '{} scenes have more than one file.'.format(counts['multiple_files']),
        '{} scenes match resolution greater than 1080p.'.format(counts['above_1080p']),
        '{} scenes match video codec is not HEVC.'.format(counts['not_hevc']),
        '{} StashDB-linked scenes still in incoming/sync need review.'.format(counts['stashdb_incoming'])]
    lines = ['ADULT CONTENT ORGANIZER - STASH CLEANUP CHECKS', '',
        'Scope: first three checks use native Stash filters across the library.',
        'StashDB sync review is limited to incoming. A pending sync is not a confirmed error.',
        'Scenes can appear in more than one section. All attached files are listed.',
        'Multiple-file scenes are review candidates. No files changed.', '', *summary]
    for section in sections:
        lines.extend(['', section['title'].upper(), '=' * len(section['title'])])
        if not section['scenes']:
            lines.append('None.')
        for scene in section['scenes']:
            files = scene.get('files') or []
            lines.extend(['', 'Scene {}: {}'.format(scene['id'], scene.get('title') or '(untitled)'),
                'Attached files: ' + str(len(files))])
            for note in scene.get('review_notes', []):
                lines.append('Review: ' + note)
            if scene.get('stashdb_ids'):
                lines.append('StashDB IDs: ' + ', '.join(scene['stashdb_ids']))
            for file in files:
                lines.extend(['  File {}: {}'.format(file['id'], canonical(file.get('path') or '')),
                    '    {}x{} | codec: {} | {} bytes'.format(file.get('width', '?'), file.get('height', '?'),
                        file.get('video_codec') or '(unknown)', file.get('size', '?'))])
    report = REPORTS_DIR / 'stash-cleanup-checks.txt'
    save_document(report.with_suffix('.json'), {'sections': sections, 'counts': counts})
    descriptor, temporary = tempfile.mkstemp(dir=str(report.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write('\n'.join(lines) + '\n')
        os.replace(temporary, report)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return '\n'.join(summary + ['See report for full scene and file lists:', str(report), 'Nothing changed.'])


def save_plan(plan, skipped):
    save_document(PLAN_FILE, {'version': 1, 'root': str(MEDIA_ROOT),
        'incoming': str(INCOMING_ROOT), 'plan': plan, 'skipped': skipped})


def protected_name(name):
    return name.startswith('.') or name.casefold() in PROTECTED


def leftover_file(path):
    if path.is_symlink() or not path.is_file() or protected_name(path.name):
        return False
    extension = path.suffix.casefold()
    if extension in VIDEO_EXTENSIONS:
        return False
    return extension in IMAGE_EXTENSIONS or path.stat().st_size <= SMALL_FILE_LIMIT


def cleanup_preview(root):
    empty, images, visited = [], [], []
    if not root.is_dir():
        raise RuntimeError('Cleanup root is not accessible: ' + str(root))
    resolved_root = root.resolve()
    pending = [root]
    while pending:
        path = pending.pop()
        if path != root and (path.is_symlink() or not beneath(path.resolve(), resolved_root)):
            continue
        # One directory listing; DirEntry caches type/stat results for this preview.
        with os.scandir(path) as iterator:
            entries = list(iterator)
        child_dirs = [entry for entry in entries if not protected_name(entry.name)
                      and entry.is_dir(follow_symlinks=False)]
        pending.extend(Path(entry.path) for entry in child_dirs)
        if path == root:
            continue
        visited.append((str(path), [entry.path for entry in entries]))
        if not entries:
            empty.append(str(path))
            continue
        file_details = []
        for entry in entries:
            extension = Path(entry.name).suffix.casefold()
            if protected_name(entry.name) or not entry.is_file(follow_symlinks=False) or extension in VIDEO_EXTENSIONS:
                break
            stat = entry.stat(follow_symlinks=False)
            is_image = extension in IMAGE_EXTENSIONS
            if not is_image and stat.st_size > SMALL_FILE_LIMIT:
                break
            file_details.append({'name': entry.name, 'bytes': stat.st_size,
                'mtime_ns': stat.st_mtime_ns, 'kind': 'image' if is_image else 'small file'})
        else:
            images.append({'folder': str(path), 'files': [item['name'] for item in file_details],
                           'bytes': sum(item['bytes'] for item in file_details), 'file_details': file_details})
    removable = set(empty)
    for path, entries in reversed(visited):
        if entries and all(entry in removable for entry in entries):
            removable.add(path)
    return {'version': 1, 'incoming': str(root), 'empty': sorted(removable), 'leftover_review': images}


def cleanup_task(mode):
    roots = [INCOMING_ROOT, MEDIA_ROOT]
    if mode == 'preview-cleanup':
        previews = []
        for root in roots:
            log('CHECKING: ' + str(root))
            previews.append(cleanup_preview(root))
        document = {'version': 3, 'small_file_limit_bytes': SMALL_FILE_LIMIT, 'roots': [str(root) for root in roots],
                    'empty': sorted({p for preview in previews for p in preview['empty']}),
                    'leftover_review': [dict(item, root=preview['incoming']) for preview in previews for item in preview['leftover_review']]}
        save_document(CLEANUP_FILE, document)
        for path in document['empty'][:100]:
            log('EMPTY FOLDER: ' + path)
        for item in document['leftover_review'][:100]:
            log('LEFTOVERS: {} ({} files)'.format(item['folder'], len(item['files'])))
            for file in item['file_details']:
                if file['kind'] == 'image':
                    destination = Path(item['root']) / LEFTOVER_IMAGES / Path(item['folder']).relative_to(item['root']) / file['name']
                    log('  IMAGE MOVE PREVIEW: {} -> {}'.format(file['name'], destination))
        log('Complete report: ' + str(CLEANUP_FILE))
        return '{} empty folders across both roots; {} image/small-file-only folders for review. Nothing removed.'.format(
            len(document['empty']), len(document['leftover_review']))
    document = json.loads(CLEANUP_FILE.read_text())
    if document.get('version') != 3 or document.get('roots') != [str(root) for root in roots]:
        raise RuntimeError('Cleanup preview changed. Run cleanup preview again.')
    removed, kept = 0, 0
    outcomes = []
    for value in sorted(document['empty'], key=lambda p: len(Path(p).parts), reverse=True):
        path = Path(value)
        containing = [root for root in roots if beneath(path, root)]
        # Never remove either configured root, including overlapping root layouts.
        if path in roots or not containing:
            kept += 1
            outcomes.append({'folder': value, 'status': 'kept: outside scope or root'})
            continue
        root = max(containing, key=lambda p: len(p.parts))
        rel = path.relative_to(root)
        if any(protected_name(part) for part in rel.parts) or path.is_symlink() or not beneath(path.resolve(), root.resolve()):
            kept += 1
            outcomes.append({'folder': value, 'status': 'kept: protected or symlink'})
            continue
        try:
            path.rmdir()  # Empty-only removal. New files, including leftover files, prevent removal.
            removed += 1
            outcomes.append({'folder': value, 'status': 'removed'})
            log('REMOVED EMPTY FOLDER: ' + str(path))
        except OSError:
            kept += 1
            outcomes.append({'folder': value, 'status': 'kept: nonempty or unavailable'})
    save_document(CLEANUP_FILE.with_name('cleanup-results.json'), {'roots': document['roots'], 'outcomes': outcomes})
    return '{} empty folders removed; {} changed/unavailable folders kept. All leftover files untouched.'.format(removed, kept)


def move_leftover_images():
    roots = [INCOMING_ROOT, MEDIA_ROOT]
    document = json.loads(CLEANUP_FILE.read_text())
    if document.get('version') != 3 or document.get('roots') != [str(root) for root in roots]:
        raise RuntimeError('Run option 7 again to create a current image-move preview.')
    outcomes = []
    moved, skipped = 0, 0
    report = CLEANUP_FILE.with_name('image-move-results.json')
    for item in document['leftover_review']:
        root = Path(item['root'])
        folder = Path(item['folder'])
        if root not in roots or folder == root or not beneath(folder, root):
            raise RuntimeError('Image preview contains a folder outside the configured roots.')
        relative = folder.relative_to(root)
        if any(protected_name(part) for part in relative.parts) or folder.is_symlink() or not beneath(folder.resolve(), root.resolve()):
            raise RuntimeError('Image source folder changed or is protected. Run option 7 again.')
        for file in item['file_details']:
            name = file['name']
            if file['kind'] != 'image' or Path(name).suffix.casefold() not in IMAGE_EXTENSIONS:
                continue
            if Path(name).name != name or protected_name(name):
                raise RuntimeError('Invalid image name in saved preview.')
            source = folder / name
            target = root / LEFTOVER_IMAGES / relative / name
            outcome = {'source': str(source), 'target': str(target), 'status': 'skipped'}
            try:
                # Recheck whole source folder: newly arrived video/download files prevent moves.
                if not folder.is_dir() or not all(leftover_file(p) for p in folder.iterdir()):
                    raise FileExistsError('folder contents changed')
                if source.is_symlink() or not source.is_file() or not beneath(source.resolve(), root.resolve()):
                    raise FileNotFoundError('source unavailable')
                current = source.stat()
                if (current.st_size, current.st_mtime_ns) != (file['bytes'], file['mtime_ns']):
                    raise FileExistsError('source changed since preview')
                # Reject destination symlinks even if they happen to resolve inside the root.
                parent = root
                for part in target.parent.relative_to(root).parts:
                    parent = parent / part
                    if parent.is_symlink():
                        raise FileExistsError('destination contains symlink')
                if not beneath(target.parent.resolve(), root.resolve()):
                    raise FileExistsError('destination outside root')
                target.parent.mkdir(parents=True, exist_ok=True)
                # Reserve destination exclusively; never replace an existing image.
                history = dict(kind='image_move', status='pending', source=str(source), target=str(target), fingerprint=organizer_journal.fingerprint(source))
                history['id'] = organizer_journal.append(CLEANUP_FILE.with_name('move-history.jsonl'), history)
                descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                reserved = os.fstat(descriptor)
                os.close(descriptor)
                try:
                    os.replace(source, target)
                except BaseException:
                    # Remove only our own empty reservation if the move failed.
                    if target.exists():
                        now = target.stat()
                        if (now.st_dev, now.st_ino, now.st_size) == (reserved.st_dev, reserved.st_ino, 0):
                            target.unlink()
                    raise
                organizer_journal.append(CLEANUP_FILE.with_name('move-history.jsonl'), dict(history, status='done', fingerprint=organizer_journal.fingerprint(target)))
                outcome['status'] = 'moved'
                moved += 1
                log('MOVED IMAGE: {} -> {}'.format(source, target))
            except OSError as error:
                skipped += 1
                outcome['reason'] = str(error)
                log('SKIP IMAGE: ' + str(source))
            finally:
                outcomes.append(outcome)
                save_document(report, {'roots': document['roots'], 'outcomes': outcomes})
    return '{} images moved; {} changed/existing/unavailable images skipped. Other files kept. Report: {}'.format(moved, skipped, report)


def revalidate(api, item):
    scene = api.scene(item['scene_id'])
    if not scene:
        return False
    performer, reason = eligible(scene)
    if reason or str(performer['id']) != item['performer_id'] or performer['name'] != item['performer']:
        return False
    files = [f for f in scene.get('files') or [] if str(f['id']) == item['file_id']]
    if len(files) != 1 or files[0]['path'] != item['source']:
        return False
    source = canonical(item['source'])
    if not source.is_file() or source.is_symlink() or not beneath(source.resolve(), INCOMING_ROOT.resolve()):
        return False
    if source.stat().st_size != item['size']:
        return False
    folder = Path(item['target']).parent
    if not beneath(folder.resolve(), MEDIA_ROOT.resolve()) or os.path.lexists(item['target']):
        return False
    # Recheck the existing performer folder and naming before executing a saved plan.
    matches = existing_folders(MEDIA_ROOT, {normalized(performer['name'])}).get(normalized(performer['name']), [])
    performer_root = performer_folder(MEDIA_ROOT, performer['name'], matches)
    expected = performer_root / 'not on stash' / source.name if performer_root is not None else None
    return expected is not None and str(expected) == item['target']


def run(mode, settings):
    if mode in ('preview-cleanup', 'apply-cleanup'):
        return cleanup_task(mode)
    api = API(settings)
    if mode in ('preview-flags', 'apply-flags'):
        return metadata_flags_task(api, apply=(mode == 'apply-flags'))
    if mode not in ('preview', 'apply'):
        raise RuntimeError('Unknown task mode')
    supported = api.supports_move()
    if mode == 'preview':
        details = []
        plan, skipped = build_plan(api.scenes(), MEDIA_ROOT, INCOMING_ROOT, details)
        save_plan(plan, skipped)
        return organization_report(plan, details, supported)
    if not supported:
        raise RuntimeError('This Stash version does not expose moveFiles. No files moved.')
    document = json.loads(PLAN_FILE.read_text())
    if document.get('version') != 1 or document.get('root') != str(MEDIA_ROOT) or document.get('incoming') != str(INCOMING_ROOT):
        raise RuntimeError('Preview configuration changed. Run preview again.')
    moved, skipped = 0, 0
    for item in document['plan']:
        if not revalidate(api, item):
            skipped += 1
            log('SKIP changed/conflicting scene ' + item['scene_id'])
            continue
        log('MOVING scene {}: {} -> {}'.format(item['scene_id'], item['source'], item['target']))
        # A timeout may mean a server-side move is still running. Stop; never retry blindly.
        history = dict(kind='media_move', status='pending', scene_id=item['scene_id'], file_id=item['file_id'],
            source=str(canonical(item['source'])), target=item['target'], fingerprint=organizer_journal.fingerprint(canonical(item['source'])))
        history['id'] = organizer_journal.append(PLAN_FILE.with_name('move-history.jsonl'), history)
        if not api.move(item['file_id'], Path(item['target']).parent):
            raise RuntimeError('Stash did not confirm the move. Check file locations before retrying.')
        updated = api.file(item['file_id'])
        if canonical(updated['path']) != Path(item['target']):
            raise RuntimeError('Move returned success but recorded path differs. Check this scene before continuing.')
        organizer_journal.append(PLAN_FILE.with_name('move-history.jsonl'), dict(history, status='done', fingerprint=organizer_journal.fingerprint(item['target'])))
        moved += 1
        log('MOVED scene ' + item['scene_id'] + '; Stash file path verified.')
    return '{} files moved and verified; {} stale/conflicting entries skipped.'.format(moved, skipped)


def prompt(label, default):
    value = input('{} [{}]: '.format(label, default)).strip()
    return value or default


def configure(old=None):
    old = old or {}
    print('\nStash connection and NAS folders')
    print('Use the folder mounted in Finder, not the NAS /volume2 path.')
    url = prompt('Stash URL', old.get('stash_url', 'http://192.168.1.221:9999'))
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise RuntimeError('Use an http(s) URL without embedded credentials, query or fragment.')
    key = getpass.getpass('Stash API key (hidden; Enter keeps the saved key): ')
    settings = {'stash_url': url, 'api_key': key or old.get('api_key', ''),
        'share_root': prompt('Mounted NAS share', old.get('share_root', '/Volumes/Secondary Storage')),
        'incoming_relative': prompt('Incoming folder inside share', old.get('incoming_relative', 'SeedboxSync/xxx')),
        'media_relative': prompt('Organized root inside share', old.get('media_relative', 'sex/media/scenes'))}
    if old.get('encoding'):
        settings['encoding'] = old['encoding']
    set_paths(settings)
    save_document(CONFIG_FILE, settings)
    print('Settings saved privately. No media files changed.')
    print('Reports folder:', REPORTS_DIR)
    return settings


def connection_test(settings):
    api = API(settings)
    data = api.gql('query{version{version}}')
    print('Stash version:', (data.get('version') or {}).get('version', 'connected'))
    print('Native file-move API:', 'available' if api.supports_move() else 'not available; moving is disabled')
    print('NAS share:', 'mounted' if SHARE_ROOT.is_dir() else 'NOT FOUND — mount it in Finder')
    print('Incoming folder:', 'found' if INCOMING_ROOT.is_dir() else 'NOT FOUND')
    print('Organized root:', 'found' if MEDIA_ROOT.is_dir() else 'NOT FOUND')


MENU = '''
Adult Content Organizer

  1  Connection and folder settings
  2  Test Stash connection and NAS folders
  3  Preview file organization
  4  Move files from the saved preview
  5  Preview missing-metadata flags
  6  Update missing-metadata flags in Stash
  7  Preview empty and image/small-file-only folders in BOTH roots
  8  Remove previewed empty folders
  9  View the last saved report
 10  Scan videos and export encoding list
 11  Encode the saved list with HandBrakeCLI
 12  Show copy-and-paste encoding command
 13  Move previewed images into each root’s leftover images folder
 14  Run Stash cleanup filters
  0  Exit
'''


MENU_ITEMS = [
    ('1', 'Connection and folder settings'), ('2', 'Test connection and folders'),
    ('3', 'Preview organization'), ('4', 'Move previewed media'),
    ('5', 'Preview metadata flags'), ('6', 'Update metadata flags'),
    ('7', 'Scan empty folders and leftovers'), ('13', 'Collect previewed images'),
    ('8', 'Remove previewed empty folders'), ('14', 'Run Stash cleanup filters'), ('9', 'View saved reports'),
    ('10', 'Build encoding list'), ('12', 'Copy encoding command'),
    ('11', 'Start HandBrake encoding'), ('0', 'Exit')]
MENU_DESCRIPTIONS = {
    '14': ('STASH / CLEANUP REVIEW', 'Use your Stash filters: file count greater than 1, resolution greater than 1080p, video codec not HEVC. Also find StashDB-linked files still in incoming/sync for review. Save full lists to a text report. No files changed.'),
    '1': ('SETUP', 'Edit the Stash connection, API key, mounted NAS share and folder paths. Saved connection settings are retained between launches.'),
    '2': ('SETUP', 'Check Stash connectivity, native file-move support and mounted folders. This check does not change media.'),
    '3': ('ORGANIZE / PREVIEW', 'Find eligible incoming scenes using Stash metadata. Shows basic counts and saves all proposed destinations and missing metadata to reports/organization-preview.txt. Nothing moves yet.'),
    '4': ('ORGANIZE / APPLY', 'Move media from the saved organization preview using the Stash API. Rechecks metadata, source files and destination conflicts.'),
    '5': ('METADATA / PREVIEW', 'Show scenes that need a title or female performer before organization. Preview only.'),
    '6': ('METADATA / APPLY', 'Add or clear organizer metadata flags in Stash. Existing user tags are preserved.'),
    '7': ('CLEANUP / PREVIEW', 'Check BOTH incoming and organized media. Report empty folders and image/small-file-only leftovers. Preview image destinations; nothing moves or gets removed.'),
    '13': ('CLEANUP / COLLECT', 'Move previewed images into leftover images under their respective root. Preserve relative subfolders. Leave small non-image files in place. Run Scan empty folders and leftovers first.'),
    '8': ('CLEANUP / APPLY', 'Remove only previewed folders that are still empty. Both library roots are preserved. Run a fresh cleanup scan after collecting images.'),
    '9': ('REPORTS', 'Read saved organization, cleanup, image collection and encoding reports. Reports live in reports/ beside this app.'),
    '10': ('HANDBRAKE / PREVIEW', 'Choose incoming or organized media and build a reviewed encoding list. Target: up to 1080p H.265 10-bit at 5000 kbps. No encoding starts.'),
    '12': ('HANDBRAKE / COMMAND', 'Print one copy-and-paste command for the saved encoding list. Run it in another Terminal window to see HandBrake progress.'),
    '11': ('HANDBRAKE / ENCODE', 'Encode the saved list on this Mac. Requires typing ENCODE before starting. Outputs stay beside their originals as - reencoded.mkv. Originals are kept.'),
    '0': ('EXIT', 'Close Adult Content Organizer. Saved settings and reports remain available next time.')}


import organizer_features
MENU_ITEMS[-1:-1] = organizer_features.ITEMS
MENU_DESCRIPTIONS.update(organizer_features.DESCRIPTIONS)
MENU = '\nAdult Content Organizer\n\n' + '\n'.join(' {:>2}  {}'.format(*item) for item in MENU_ITEMS) + '\n'


def menu_selection(current, key, typed=''):
    """Handle selection without executing actions; Enter alone commits a choice."""
    if key in ('UP', 'DOWN', 'HOME', 'END'):
        if key == 'UP':
            current = (current - 1) % len(MENU_ITEMS)
        elif key == 'DOWN':
            current = (current + 1) % len(MENU_ITEMS)
        else:
            current = 0 if key == 'HOME' else len(MENU_ITEMS) - 1
        return current, '', None
    if key == 'QUIT':
        return current, typed, '0'
    if key == 'ENTER':
        if typed:
            return current, typed, typed if typed in dict(MENU_ITEMS) else None
        return current, typed, MENU_ITEMS[current][0]
    if key == 'BACKSPACE':
        return current, typed[:-1], None
    if key.isdigit() and len(key) == 1:
        return current, (typed + key)[-2:], None
    return current, typed, None


def choose_menu(settings, previous='1'):
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb':
        print(MENU)
        return input('Choose: ').strip()
    import curses
    import termios
    terminal_settings = termios.tcgetattr(sys.stdin.fileno())
    current = next((i for i, item in enumerate(MENU_ITEMS) if item[0] == previous), 0)
    def screen(window):
        nonlocal current
        typed = ''
        window.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        color = False
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            for number, fg, bg in [(1, curses.COLOR_CYAN, background),
                    (2, curses.COLOR_WHITE, curses.COLOR_BLUE),
                    (3, curses.COLOR_GREEN, background), (4, curses.COLOR_YELLOW, background)]:
                curses.init_pair(number, fg, bg)
            color = True
        accent = curses.color_pair(1) | curses.A_BOLD if color else curses.A_BOLD
        selected = curses.color_pair(2) | curses.A_BOLD if color else curses.A_REVERSE
        good = curses.color_pair(3) if color else curses.A_NORMAL
        while True:
            window.erase()
            height, width = window.getmaxyx()
            def write(row, col, text, style=0, limit=None):
                available = max(0, width - col - 1)
                if limit is not None:
                    available = min(available, limit)
                if 0 <= row < height - 1 and available:
                    try:
                        window.addnstr(row, col, text, available, style)
                    except curses.error:
                        pass
            if height < 14 or width < 45:
                write(0, 0, 'Adult Content Organizer', accent)
                write(2, 0, 'Enlarge Terminal for the full menu.')
                write(4, 0, MENU_ITEMS[current][1], selected)
                write(6, 0, 'Up/Down: move | Enter: select | Q: exit')
            else:
                write(1, 2, 'ADULT CONTENT ORGANIZER  ' + APP_VERSION, accent)
                write(2, 2, 'Stash library tools  /  Cleanup  /  HandBrake', curses.A_DIM)
                configured = settings is not None
                mounted = configured and SHARE_ROOT.is_dir()
                status = 'SETUP NEEDED' if not configured else ('NAS MOUNTED' if mounted else 'NAS NOT FOUND')
                write(1, max(29, width - len(status) - 3), status, good if mounted else accent)
                write(4, 2, 'Stash: ' + settings['stash_url'] if configured else 'Start with Connection and folder settings.', curses.A_DIM)
                rows = max(1, height - 11)
                start = max(0, min(current - rows + 1, len(MENU_ITEMS) - rows))
                split = width >= 80
                panel = min(52, width // 2) if split else width - 3
                write(6, 2, 'ACTIONS', accent)
                for offset, (number, label) in enumerate(MENU_ITEMS[start:start + rows]):
                    index = start + offset
                    text = '{} {:>2}  {}'.format('>' if index == current else ' ', number, label)
                    write(7 + offset, 2, text.ljust(panel - 2), selected if index == current else 0, panel - 2)
                category, description = MENU_DESCRIPTIONS[MENU_ITEMS[current][0]]
                if split:
                    x = panel + 3
                    write(6, x, category, accent)
                    for offset, line in enumerate(textwrap.wrap(description, width=max(20, width - x - 4))):
                        if 8 + offset < height - 4:
                            write(8 + offset, x, line)
                    write(height - 6, x, 'Reports: ./reports/', curses.A_DIM)
                else:
                    write(height - 4, 2, category + ' | ' + description, curses.A_DIM)
                write(height - 3, 2, 'NUMBER: ' + typed if typed else 'UP / DOWN  Move    ENTER  Select    Q  Exit', accent)
                write(height - 2, 2, 'Type a number + Enter for a shortcut.  More actions below' if start + rows < len(MENU_ITEMS) else 'Type a number + Enter for a shortcut.', curses.A_DIM)
            window.refresh()
            key = window.getch()
            mapped = {curses.KEY_UP: 'UP', curses.KEY_DOWN: 'DOWN', curses.KEY_HOME: 'HOME',
                      curses.KEY_END: 'END', curses.KEY_ENTER: 'ENTER', 10: 'ENTER', 13: 'ENTER',
                      curses.KEY_BACKSPACE: 'BACKSPACE', 127: 'BACKSPACE', 8: 'BACKSPACE',
                      ord('q'): 'QUIT', ord('Q'): 'QUIT', 27: 'QUIT'}
            if key == 3:
                raise KeyboardInterrupt
            action = mapped.get(key, chr(key) if 0 <= key < 256 else '')
            current, typed, choice = menu_selection(current, action, typed)
            if choice is not None:
                return choice
    try:
        return curses.wrapper(screen)
    except curses.error:
        print(MENU)
        return input('Choose: ').strip()
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal_settings)


def perform_choice(choice, settings):
    actions = {'3': 'preview', '4': 'apply', '5': 'preview-flags',
               '6': 'apply-flags', '7': 'preview-cleanup', '8': 'apply-cleanup'}
    if choice == '1':
        return configure(settings)
    elif choice == '9':
        for path in (REPORTS_DIR / 'stash-cleanup-checks.txt', PLAN_FILE.with_suffix('.txt'), PLAN_FILE, CLEANUP_FILE, CLEANUP_FILE.with_name('cleanup-results.json'), CLEANUP_FILE.with_name('image-move-results.json'), ENCODING_FILE):
            if path.exists():
                print('\n' + path.name + '\n' + path.read_text())
    elif not settings:
        print('Choose 1 and save your connection settings first.')
    elif choice in {str(number) for number in range(15, 24)}:
        import organizer_features
        return organizer_features.handle(choice, sys.modules[__name__], settings)
    elif choice == '14':
        print(stash_cleanup_report(API(settings)))
    elif choice == '13':
        print(move_leftover_images())
    elif choice == '12':
        if not ENCODING_FILE.exists():
            raise RuntimeError('Choose 10 to scan and save an encoding list first.')
        print('Copy this command into Terminal. It starts encoding the saved list:')
        command_text = encoding_queue.queue_command(ENCODING_FILE)
        print('\n' + command_text + '\n')
        import subprocess
        try:
            subprocess.run(['/usr/bin/pbcopy'], input=command_text, text=True, check=True, capture_output=True)
            print('Encoding command copied to the clipboard.')
        except (OSError, subprocess.CalledProcessError):
            print('Copy the command shown above.')
        print('Completed outputs are skipped on reruns. Originals are kept.')
        print('Keep the NAS mounted and avoid moving these files during encoding.')
    elif choice == '10':
        print('Scan: 1 Incoming folder   2 Organized media')
        scope = input('Choose scope [1]: ').strip() or '1'
        if scope not in ('1', '2'):
            raise RuntimeError('Choose 1 or 2 for the scan scope.')
        cache = {}
        if PROBE_CACHE_FILE.exists():
            try:
                saved_cache = json.loads(PROBE_CACHE_FILE.read_text())
                if saved_cache.get('version') == 1 and isinstance(saved_cache.get('files'), dict):
                    cache = saved_cache['files']
            except (OSError, ValueError, AttributeError):
                pass  # A missing/corrupt cache simply triggers fresh probes.
        def checkpoint(files):
            save_document(PROBE_CACHE_FILE, {'version': 1, 'files': files})
        try:
            document = encoding_queue.scan(INCOMING_ROOT if scope == '1' else MEDIA_ROOT, protected_name, cache, checkpoint, encoding_queue.config(settings.get('encoding')))
        finally:
            checkpoint(cache)
        print('Video metadata: {} cached; {} freshly inspected.'.format(document['scan_stats']['cached'], document['scan_stats']['probed']))
        save_document(ENCODING_FILE, document)
        encoding_queue.write_csv(ENCODING_CSV, document)
        for job in document['jobs'][:100]:
            print('CANDIDATE: {} ({}x{}, {}, ~{} kbps)'.format(job['source'], job['width'], job['height'], job['codec'], job['video_kbps']))
        print('{} candidates; skipped: {}'.format(len(document['jobs']), document['skipped']))
        print('List:', ENCODING_CSV)
        print('Target settings:', encoding_queue.summary(document['target']))
        print('No encoding started. Originals will be kept.')
    elif choice == '11':
        document = json.loads(ENCODING_FILE.read_text())
        allowed_roots = [str(INCOMING_ROOT.resolve()), str(MEDIA_ROOT.resolve())]
        valid_stash_scope = document.get('origin') == 'stash' and document.get('root') == str(SHARE_ROOT.resolve()) and document.get('roots') and all(root in allowed_roots for root in document['roots'])
        if document.get('root') not in allowed_roots and not valid_stash_scope:
            raise RuntimeError('Scan configuration changed. Scan again first.')
        print('{} saved jobs. Settings: {}'.format(len(document['jobs']), encoding_queue.summary(document['target'])))
        print('Output: beside each source, with " - reencoded.mkv". Originals kept.')
        if input('Type ENCODE to start now: ').strip() == 'ENCODE':
            print(encoding_queue.run(document, ENCODING_FILE.with_suffix('.lock'), REPORTS_DIR / 'encode-history.jsonl'))
        else:
            print('Encoding cancelled.')
    elif choice == '2':
        connection_test(settings)
    elif choice in actions:
        print('\nWorking… This may take time over the NAS connection.')
        print(run(actions[choice], settings))
    else:
        print('Choose one of the numbered options.')
    return settings


def main():
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.chmod(0o700)
    import fcntl
    with (STATE_DIR / '.app.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Adult Content Organizer is already open in another terminal.')
            return 1
        settings = None
        if CONFIG_FILE.exists():
            settings = json.loads(CONFIG_FILE.read_text())
            set_paths(settings)
        previous = '1'
        while True:
            try:
                choice = choose_menu(settings, previous)
                previous = choice if choice in dict(MENU_ITEMS) else previous
                if choice == '0':
                    return 0
                if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM') != 'dumb':
                    import terminal_dashboard
                    updated = terminal_dashboard.run(lambda: perform_choice(choice, settings),
                        MENU_ITEMS, choice, 'Stash: ' + settings['stash_url'] if settings else 'Connection setup')
                    if updated is not None:
                        settings = updated
                else:
                    settings = perform_choice(choice, settings)
                    input('\nPress Enter to return to the menu…')
            except (EOFError, KeyboardInterrupt):
                print('\nExiting. If a move was interrupted, check Stash and file locations before retrying.')
                return 0
            except Exception as error:
                message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                print('\nAction stopped:', message)
                try:
                    input('Press Enter to return to the menu…')
                except (EOFError, KeyboardInterrupt):
                    return 0


if __name__ == '__main__':
    sys.exit(main())
