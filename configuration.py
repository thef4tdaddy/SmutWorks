"""Validated, installation-independent settings with legacy compatibility."""
from pathlib import Path, PurePosixPath

GENDERS = {'MALE', 'FEMALE', 'TRANSGENDER_MALE', 'TRANSGENDER_FEMALE',
           'INTERSEX', 'NON_BINARY'}


def remote_root(value):
    if not isinstance(value, str) or not value.startswith('/') or '..' in PurePosixPath(value).parts or '\\' in value:
        raise RuntimeError('Stash paths must be absolute POSIX paths without .. or backslashes.')
    return str(PurePosixPath(value))


def relative_folder(value, label):
    if not isinstance(value, str):
        raise RuntimeError(label + ' must be a relative folder path.')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or str(path) in ('', '.'):
        raise RuntimeError(label + ' must be nonempty and relative, without ..')
    return str(path)


def normalize(settings):
    result = dict(settings)
    legacy = 'settings_version' not in settings
    if not legacy and (type(settings['settings_version']) is not int or settings['settings_version'] != 1):
        raise RuntimeError('Unsupported settings version; use a compatible SmutWorks release.')
    result['settings_version'] = 1
    root = Path(settings['share_root']).expanduser()
    if not root.is_absolute():
        raise RuntimeError('Local media root must be an absolute path.')
    result['share_root'] = str(root)
    for key in ('incoming_relative', 'media_relative'):
        result[key] = relative_folder(settings[key], key)
    result['stash_root'] = remote_root(settings.get('stash_root', '/media'))
    aliases = settings.get('stash_aliases', ['/incoming'] if legacy else [])
    if not isinstance(aliases, list) or len(aliases) > 10:
        raise RuntimeError('Stash path aliases must be a list with at most 10 roots.')
    result['stash_aliases'] = [value for value in dict.fromkeys(remote_root(item) for item in aliases)
                               if value != result['stash_root']]
    roots = [PurePosixPath(result['stash_root'])] + [PurePosixPath(value) for value in result['stash_aliases']]
    for index, root in enumerate(roots):
        for other in roots[index + 1:]:
            if root in other.parents or other in root.parents:
                raise RuntimeError('Stash roots and aliases must not be nested; their mappings would be ambiguous.')
    result['unlinked_subfolder'] = relative_folder(settings.get('unlinked_subfolder',
        'not on stash' if legacy else 'unlinked'), 'Organization subfolder')
    genders = settings.get('performer_genders', ['FEMALE', 'TRANSGENDER_FEMALE'] if legacy else ['*'])
    if not isinstance(genders, list) or not genders or not all(value in GENDERS | {'*'} for value in genders):
        raise RuntimeError('Choose valid Stash performer genders, or * for any gender.')
    result['performer_genders'] = list(dict.fromkeys(genders))
    prefix = settings.get('tag_prefix', 'ACO' if legacy else 'SmutWorks')
    if not isinstance(prefix, str) or not prefix.strip() or any(ord(c) < 32 for c in prefix):
        raise RuntimeError('Managed tag prefix must be nonempty text without control characters.')
    result['tag_prefix'] = prefix.strip()
    result['legacy_performer_tag'] = settings.get('legacy_performer_tag', legacy)
    depth = settings.get('performer_search_depth', 6)
    if type(depth) is not int or not 1 <= depth <= 30:
        raise RuntimeError('Performer folder search depth must be from 1 to 30.')
    delegate = settings.get('delegate_stashdb', True)
    if type(delegate) is not bool:
        raise RuntimeError('delegate_stashdb must be true or false.')
    result['delegate_stashdb'] = delegate
    result['performer_search_depth'] = depth
    return result
