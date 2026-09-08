"""Relocate path-bearing records in an offline COPY; never use on a live database."""
from pathlib import Path
import argparse
import json
import sqlite3


def migrate(state: Path, old: str):
    old = old.replace('\\', '/').rstrip('/')
    def relocate(value):
        if isinstance(value, dict):
            return {k: relocate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relocate(v) for v in value]
        if isinstance(value, str):
            try:
                nested = json.loads(value)
            except ValueError:
                nested = None
            if isinstance(nested, (dict, list)):
                updated = relocate(nested)
                return json.dumps(updated, ensure_ascii=False) if updated != nested else value
            normalized = value.replace('\\', '/')
            if normalized.startswith(old + '/') or normalized == old:
                return state.as_posix() + normalized[len(old):]
        return value
    count = 0
    for path in state.rglob('*.sqlite3'):
        if 'accounts' in path.relative_to(state).parts:
            continue
        with sqlite3.connect(path) as db:
            if db.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database integrity failed before migration')
            tables = list(db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"))
            for (name,) in tables:
                table = '"' + name.replace('"', '""') + '"'
                for column in list(db.execute(f'pragma table_info({table})')):
                    col = '"' + column[1].replace('"', '""') + '"'
                    for (value,) in list(db.execute(f"select distinct {col} from {table} where typeof({col})='text'")):
                        updated = relocate(value)
                        if updated != value:
                            db.execute(f'update {table} set {col}=? where {col}=?', (updated, value))
            db.commit()
            if db.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database integrity failed after migration')
        count += 1
    for path in state.rglob('*.json'):
        if any(p in path.relative_to(state).parts for p in ('accounts', 'releases', 'restic')):
            continue
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        updated = relocate(value)
        if updated != value:
            path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-prefix', required=True)
    parser.add_argument('--offline-copy', action='store_true', required=True)
    args = parser.parse_args()
    state = Path(__file__).resolve().parents[1] / '.cbrs/runtime'
    print(json.dumps({'databases_checked': migrate(state, args.source_prefix)}))
