#!/usr/bin/env python3
"""Find environment variable references missing from dotenv files."""

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import sys


NAME = r'([A-Za-z_][A-Za-z0-9_]*)'
QUOTED_NAME = r'''['"]''' + NAME + r'''['"]'''

PATTERNS: dict[str, list[re.Pattern]] = {
    'python': [
        re.compile(r'\b(?:os\s*\.\s*)?environ\s*\[\s*' + QUOTED_NAME + r'\s*\]'),
        re.compile(r'\b(?:os\s*\.\s*)?environ\s*\.\s*get\s*\(\s*' + QUOTED_NAME),
        re.compile(r'\b(?:os\s*\.\s*)?getenv\s*\(\s*' + QUOTED_NAME),
    ],
    'javascript': [
        re.compile(r'\b(?:process|Bun)\s*\.\s*env\s*\.\s*' + NAME),
        re.compile(r'\b(?:process|Bun)\s*\.\s*env\s*\[\s*' + QUOTED_NAME + r'\s*\]'),
        re.compile(r'\bimport\s*\.\s*meta\s*\.\s*env\s*\.\s*' + NAME),
        re.compile(r'\bimport\s*\.\s*meta\s*\.\s*env\s*\[\s*' + QUOTED_NAME + r'\s*\]'),
        re.compile(r'\bDeno\s*\.\s*env\s*\.\s*get\s*\(\s*' + QUOTED_NAME),
    ],
    'ruby': [
        re.compile(r'\bENV\s*\[\s*' + QUOTED_NAME + r'\s*\]'),
        re.compile(r'\bENV\s*\.\s*(?:fetch|dig)\s*\(\s*' + QUOTED_NAME),
    ],
    'go': [
        re.compile(r'\bos\s*\.\s*(?:Getenv|LookupEnv)\s*\(\s*"' + NAME + r'"'),
    ],
    'php': [
        re.compile(r'\b(?:getenv|env)\s*\(\s*' + QUOTED_NAME),
        re.compile(r'\$_(?:ENV|SERVER)\s*\[\s*' + QUOTED_NAME + r'\s*\]'),
    ],
    'rust': [
        re.compile(r'\b(?:std\s*::\s*)?env\s*::\s*var(?:_os)?\s*\(\s*"' + NAME + r'"'),
    ],
    'shell': [
        re.compile(r'(?<![\\$])\$\{' + NAME + r'(?=\}|:?[=+?\-])'),
        re.compile(r'(?<![\\$])\$' + NAME),
    ],
    'dotnet': [
        re.compile(r'\b(?:Environment\s*\.\s*)?GetEnvironmentVariable\s*\(\s*"' + NAME + r'"'),
    ],
    'java': [
        re.compile(r'\bSystem\s*\.\s*getenv\s*\(\s*"' + NAME + r'"'),
    ],
}

EXT_LANG: dict[str, str] = {
    '.py': 'python', '.pyw': 'python',
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'javascript', '.tsx': 'javascript', '.jsx': 'javascript',
    '.rb': 'ruby', '.go': 'go', '.php': 'php', '.rs': 'rust',
    '.sh': 'shell', '.bash': 'shell', '.zsh': 'shell', '.fish': 'shell',
    '.cs': 'dotnet', '.vb': 'dotnet', '.java': 'java', '.kt': 'java',
}

SKIP_DIRS = frozenset({
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env',
    'dist', 'build', '.next', '.nuxt', 'target', 'vendor', 'out',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'coverage',
    '.turbo', '.cache', 'tmp', 'temp',
})

ENV_ASSIGNMENT = re.compile(r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')
Ref = tuple[str, int]


class ScanError(Exception):
    """An input could not be read, so the scan is incomplete."""


class Colors:
    def __init__(self, enabled: bool):
        self.reset = '\033[0m' if enabled else ''
        self.bold = '\033[1m' if enabled else ''
        self.dim = '\033[2m' if enabled else ''
        self.red = '\033[91m' if enabled else ''
        self.green = '\033[92m' if enabled else ''
        self.yellow = '\033[93m' if enabled else ''


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8-sig')
    except UnicodeError as exc:
        raise ScanError(f'Cannot read {path}: expected UTF-8 text.') from exc
    except OSError as exc:
        raise ScanError(f'Cannot read {path}: {exc.strerror or type(exc).__name__}.') from exc


def parse_env_file(path: Path) -> set[str]:
    """Read assignment names; values are not needed for this check."""
    names = set()
    quote = None
    quote_line = 0
    for line_number, line in enumerate(read_text(path).splitlines(), 1):
        if quote:
            if has_closing_quote(line, quote):
                quote = None
            continue
        line = line.strip()
        match = ENV_ASSIGNMENT.match(line)
        if match:
            names.add(match.group(1))
            value = line[match.end():].lstrip()
            if value.startswith(('"', "'")) and not has_closing_quote(value[1:], value[0]):
                quote = value[0]
                quote_line = line_number
    if quote:
        raise ScanError(f'Cannot read {path}: unclosed quoted value on line {quote_line}.')
    return names


def has_closing_quote(text: str, quote: str) -> bool:
    escaped = False
    for char in text:
        if char == quote and not escaped:
            return True
        escaped = char == '\\' and not escaped
    return False


def find_env_files(root: Path) -> list[Path]:
    return [
        path for path in sorted(root.iterdir())
        if (path.name == '.env' or path.name.startswith('.env.'))
        and not path.is_symlink() and path.is_file()
    ]


def scan_file(path: Path, root: Path) -> dict[str, list[Ref]]:
    lang = EXT_LANG.get(path.suffix.lower())
    if not lang or path.is_symlink():
        return {}
    found: dict[str, set[Ref]] = defaultdict(set)
    relative_path = path.relative_to(root).as_posix()
    for line_number, line in enumerate(read_text(path).splitlines(), 1):
        if line.lstrip().startswith(('#', '//')):
            continue
        for pattern in PATTERNS[lang]:
            for match in pattern.finditer(line):
                found[match.group(1)].add((relative_path, line_number))
    return {name: sorted(found[name]) for name in sorted(found)}


def scan_directory(root: Path) -> tuple[dict[str, list[Ref]], int]:
    all_refs: dict[str, list[Ref]] = defaultdict(list)
    file_count = 0

    def walk_error(error: OSError):
        raise ScanError(f'Cannot scan {error.filename}: {error.strerror}.') from error

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not (Path(dirpath) / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in EXT_LANG and not path.is_symlink():
                refs = scan_file(path, root)
                file_count += 1
                for name, locations in refs.items():
                    all_refs[name].extend(locations)
    return {name: sorted(all_refs[name]) for name in sorted(all_refs)}, file_count


def result_groups(refs: dict[str, list[Ref]], env_data: dict[str, set[str]]):
    defined = set().union(*env_data.values())
    return set(refs) & defined, set(refs) - defined, defined - set(refs)


def print_results(
    refs: dict[str, list[Ref]],
    env_data: dict[str, set[str]],
    show_refs: bool,
    hide_unused: bool,
    colors: Colors,
) -> int:
    ok_vars, missing, unused = result_groups(refs, env_data)
    width = max((len(name) for name in set(refs) | unused), default=20) + 2
    c = colors
    for name in sorted(ok_vars):
        sources = ', '.join(path for path, names in env_data.items() if name in names)
        print(f'  {c.green}[ok]{c.reset}      {name:<{width}} defined ({sources})')
        if show_refs:
            for path, line in refs[name]:
                print(f'      {c.dim}{path}:{line}{c.reset}')
    for name in sorted(missing):
        locations = refs[name] if show_refs else refs[name][:3]
        excerpt = ', '.join(f'{path}:{line}' for path, line in locations)
        remaining = len(refs[name]) - len(locations)
        more = f' (+{remaining} more)' if remaining else ''
        print(f'  {c.red}[missing]{c.reset} {name:<{width}} {excerpt}{more}')
    if not hide_unused:
        for name in sorted(unused):
            sources = ', '.join(path for path, names in env_data.items() if name in names)
            print(f'  {c.yellow}[unused]{c.reset}  {name:<{width}} in {sources}')
    print(f'\n  {len(ok_vars)} ok / {len(missing)} missing / {len(unused)} unused\n')
    if missing:
        print('  Check missing names against the dotenv files for this environment.\n')
    return 1 if missing else 0


def print_json(
    refs: dict[str, list[Ref]], env_data: dict[str, set[str]], hide_unused: bool = False,
) -> int:
    ok_vars, missing, unused = result_groups(refs, env_data)
    report = {
        'summary': {'ok': len(ok_vars), 'missing': len(missing), 'unused': len(unused)},
        'ok': sorted(ok_vars),
        'missing': {
            name: [{'file': path, 'line': line} for path, line in refs[name]]
            for name in sorted(missing)
        },
        'unused': [] if hide_unused else sorted(unused),
    }
    print(json.dumps(report, indent=2))
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='envcheck',
        description='Compare environment variable references with dotenv assignment names.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
supported languages:
  Python, JavaScript/TypeScript, Ruby, Go, Rust, PHP, Java/Kotlin, C#/VB, Shell

examples:
  envcheck                         scan the current directory
  envcheck --path ./backend         scan another directory
  envcheck --env .env.production    select a dotenv file (relative to the current directory)
  envcheck --strict --format json   emit JSON and exit 1 for missing names

exit codes: 0 = scan complete, 1 = missing names with --strict, 2 = input/read error
''',
    )
    parser.add_argument('--path', default='.', help='directory to scan (default: .)')
    parser.add_argument('--env', action='append', metavar='FILE', help='dotenv file to compare (repeatable)')
    parser.add_argument('--format', default='plain', choices=['plain', 'json'])
    parser.add_argument('--strict', action='store_true', help='exit 1 if any names are missing')
    parser.add_argument('--show-refs', action='store_true', help='show all detected file:line references in plain output')
    parser.add_argument('--no-unused', action='store_true', help='hide unused-name details; keep the summary count')
    parser.add_argument('--no-color', action='store_true', help='disable terminal colors')
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')

    colors = Colors(not args.no_color and sys.stdout.isatty() and 'NO_COLOR' not in os.environ)
    try:
        root = Path(args.path).resolve()
        if not root.is_dir():
            raise ScanError(f'Scan path is not a directory: {root}')
        env_paths = [Path(name).resolve() for name in args.env] if args.env else find_env_files(root)
        env_data: dict[str, set[str]] = {}
        for path in env_paths:
            try:
                label = path.relative_to(root).as_posix()
            except ValueError:
                label = str(path)
            env_data[label] = parse_env_file(path)
        refs, file_count = scan_directory(root)
    except (ScanError, OSError, RuntimeError) as exc:
        print(f'envcheck: {exc}', file=sys.stderr)
        return 2

    if args.format == 'json':
        result = print_json(refs, env_data, args.no_unused)
    else:
        print(f'\n{colors.bold}envcheck{colors.reset}\n\nScanning: {root}\n')
        for label, names in env_data.items():
            print(f'  {label}: {len(names)} name(s)')
        if not env_data:
            print('  No dotenv files found; all detected names are reported as missing.')
        print(f'\nScanned {file_count} source file(s); {len(refs)} unique name(s).\n')
        result = print_results(refs, env_data, args.show_refs, args.no_unused, colors)
    return result if args.strict else 0


if __name__ == '__main__':
    raise SystemExit(main())
