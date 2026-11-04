"""Project provider data to the user-approved persistent fields."""
import json
from pathlib import Path

SPEC = json.loads((Path(__file__).resolve().parents[1] / 'approved_fields.json').read_text(encoding='utf-8'))


def short_id(value):
    return value.removeprefix('https://openalex.org/') if isinstance(value, str) else value


def short_doi(value):
    return value.removeprefix('https://doi.org/') if isinstance(value, str) else value


def selected_work(work):
    tree = {}
    for path in SPEC['campos_aprobados']:
        if not path.startswith('raw_json.'):
            continue
        node = tree
        for part in path.removeprefix('raw_json.').split('.'):
            node = node.setdefault(part, {})

    normalizers = {}
    for name, rule in SPEC['normalizacion'].items():
        if isinstance(rule, dict) and 'eliminar_prefijo' in rule:
            for path in rule['aplicar_a']:
                normalizers[path] = rule['eliminar_prefijo']

    def project(value, shape, path):
        if value is None:
            return None
        if not shape:
            prefix = normalizers.get(path)
            return value.removeprefix(prefix) if prefix and isinstance(value, str) else value
        if not isinstance(value, dict):
            raise ValueError('Expected object at ' + path)
        result = {}
        for key, children in shape.items():
            is_list = key.endswith('[]')
            field = key[:-2] if is_list else key
            if field not in value:
                continue
            item = value[field]
            next_path = path + '.' + key
            if is_list and item is not None:
                if not isinstance(item, list):
                    raise ValueError('Expected list at ' + next_path)
                result[field] = [project(entry, children, next_path) for entry in item]
            else:
                result[field] = project(item, children, next_path)
        return result

    return project(work, tree, 'raw_json')


def migrate_papers(db):
    if db.execute('PRAGMA user_version').fetchone()[0] >= 3:
        return
    rows = db.execute('SELECT id,doi,source_id,raw_json FROM papers').fetchall()
    with db:
        for old_id, doi, source_id, raw in rows:
            db.execute('UPDATE papers SET id=?,doi=?,source_id=?,raw_json=? WHERE id=?',
                       (short_id(old_id), short_doi(doi), short_id(source_id),
                        json.dumps(selected_work(json.loads(raw)), ensure_ascii=False), old_id))
        db.execute('PRAGMA user_version=3')
