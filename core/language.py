"""Shared, presentation-only language preferences. No Shop/runtime/model mutations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

import locking

LANGUAGES = ('auto', 'zh-CN', 'en')
CATALOG_ROOT = Path(__file__).resolve().parent.parent / 'locales'
CATALOGS = {name: json.loads((CATALOG_ROOT / (name + '.json')).read_text()) for name in ('en', 'zh-CN')}


def detect_language(env=None):
    env = os.environ if env is None else env
    value = env.get('LC_ALL') or env.get('LC_MESSAGES') or env.get('LANG') or ''
    return 'zh-CN' if re.match(r'^zh(?:[-_.@]|$)', value, re.I) else 'en'


def preference_path():
    config = Path(os.environ.get('SHOP_CONFIG_DIR') or str(Path.home() / '.config/shop-workstation')).expanduser()
    return config / 'language.json'


def read_preference(path=None):
    path = preference_path() if path is None else Path(path)
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise RuntimeError('Language preference must be a regular file')
        with path.open('rb') as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return {'language': 'auto', 'revision': 'missing', 'path': str(path)}
    if len(raw) > 4096:
        raise RuntimeError('Language preference exceeds 4 KiB')
    document = json.loads(raw)
    if (not isinstance(document, dict) or document.get('version') != 1
            or isinstance(document.get('version'), bool) or document.get('language') not in LANGUAGES
            or set(document) != {'version', 'language'}):
        raise RuntimeError('Invalid language preference; inspect language.json before saving')
    return {'language': document['language'], 'revision': hashlib.sha256(raw).hexdigest(), 'path': str(path)}


def resolve_language():
    try:
        preference = read_preference()['language']
        if preference != 'auto':
            return preference
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        pass  # Invalid display preferences never rewrite data or change authorization.
    return detect_language()


def t(key, *values, language=None):
    language = language or resolve_language()
    text = CATALOGS.get(language, {}).get(key) or CATALOGS['en'].get(key) or key
    return re.sub(r'\{(\d+)\}', lambda m: str(values[int(m[1])]) if int(m[1]) < len(values) else m[0], text)


class ArgumentParser(argparse.ArgumentParser):
    """Translate help labels only; option names, parsed values and errors stay stable."""
    def __init__(self, *args, **kwargs):
        add_help = kwargs.pop('add_help', True)
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = t('Positional arguments')
        self._optionals.title = t('Options')
        if add_help:
            self.add_argument('-h', '--help', action='help', help=t('Show this help message and exit'))

    def format_help(self):
        return super().format_help().replace('usage: ', t('Usage:') + ' ', 1)

    def format_usage(self):
        return super().format_usage().replace('usage: ', t('Usage:') + ' ', 1)


def save_preference(language, expected, expected_path, path=None):
    if language not in LANGUAGES:
        raise RuntimeError('Expected auto, zh-CN or en')
    path = preference_path() if path is None else Path(path)
    if str(path) != expected_path:
        raise RuntimeError('Language preference location changed; reopen the language selector')
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / 'language.lock').open('a') as lock:
        try:
            locking.acquire(lock)
        except RuntimeError as error:
            raise RuntimeError('Another language update is active; reopen the language selector') from error
        if path.is_symlink():
            raise RuntimeError('Refusing to replace a symlink language preference')
        current = read_preference(path)
        if current['revision'] != expected:
            raise RuntimeError('Language preference changed; reopen the language selector')
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.language-', dir=path.parent)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'language': language}, stream)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
    return read_preference(path)


def main(argv=None):
    parser = ArgumentParser(description=t('Shop display language; does not change tasks or models'))
    parser.add_argument('language', nargs='?', choices=LANGUAGES, help=t('Save a personal language preference'))
    parser.add_argument('--expected', help=argparse.SUPPRESS)
    parser.add_argument('--expected-path', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    current = read_preference()
    if args.language:
        current = save_preference(args.language, args.expected or current['revision'], args.expected_path or current['path'])
    print(json.dumps({**current, 'effective': resolve_language()}, ensure_ascii=False))


def cli(argv=None):
    try:
        main(argv)
    except Exception as error:
        print('shop-language: ' + str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    cli()
