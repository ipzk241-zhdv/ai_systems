"""Logical storage paths. None of these paths are paths on the VM filesystem."""
from pathlib import PurePosixPath
import re

from file_store import FileError


def components(store, path):
    if not isinstance(path, str) or not path.startswith('/') or '\\' in path or len(path) > 6000:
        raise FileError('Шлях має починатися з /, наприклад /Навчання/звіт.docx.')
    names = path.strip('/').split('/') if path.strip('/') else []
    if len(names) > 33:
        raise FileError('Завелика глибина шляху.')
    return [store.valid_name(name) for name in names]


def folder_id(store, user_id, path, create=False):
    store.require_user(user_id)
    names = components(store, path)
    if len(names) > 32:
        raise FileError('Максимальна глибина папок — 32 рівні.')
    db = store.connect()
    try:
        with db:
            if create:
                db.execute('BEGIN IMMEDIATE')
            parent = None
            for name in names:
                row = db.execute('SELECT id FROM folders WHERE parent_id IS ? AND name_key=?', (parent, name.casefold())).fetchone()
                if row:
                    parent = row[0]
                elif create:
                    import time
                    parent = db.execute('INSERT INTO folders(parent_id,name,name_key,created_at) VALUES (?,?,?,?)',
                                        (parent, name, name.casefold(), time.time())).lastrowid
                else:
                    raise FileError('Папку не знайдено: ' + path)
            return parent
    finally:
        db.close()


def catalog(store, user_id):
    folders = {node['id']:node for node in store.list_folders(user_id)}
    paths = {None:'/'}
    def parent_path(folder, seen=None):
        if folder in paths:
            return paths[folder]
        seen = set() if seen is None else seen
        if folder in seen or len(seen) >= 32 or folder not in folders:
            raise FileError('Пошкоджено структуру папок.')
        seen.add(folder)
        node = folders[folder]
        paths[folder] = parent_path(node['parent_id'], seen) + node['name'] + '/'
        return paths[folder]
    return [dict(item, path=parent_path(item['folder_id']) + item['name']) for item in store.list_files(user_id)]


def resolve(store, user_id, reference):
    store.require_user(user_id)
    reference = reference.strip().strip('"«»')
    match = re.fullmatch(r'#([1-9][0-9]{0,17})', reference)
    if match:
        file_id = int(match[1]); store.get_file(user_id, file_id)
        return [item for item in catalog(store, user_id) if item['id'] == file_id]
    if reference.startswith('/'):
        names = components(store, reference)
        if not names:
            raise FileError('Вкажи шлях до файла або #номер.')
        parent = folder_id(store, user_id, '/' + '/'.join(names[:-1]))
        return [item for item in catalog(store, user_id) if item['folder_id'] == parent and item['name'].casefold() == names[-1].casefold()]
    return [item for item in catalog(store, user_id) if item['name'].casefold() == reference.casefold()]


def destination(store, target, original_name, rename=False):
    target = target.strip().strip('"«»')
    names = components(store, target)
    as_file = rename or (not target.endswith('/') and names and bool(PurePosixPath(names[-1]).suffix))
    if as_file:
        if not names:
            raise FileError('Вкажи назву нового файла.')
        return '/' + '/'.join(names[:-1]), names[-1]
    return '/' + '/'.join(names), store.valid_name(original_name)
