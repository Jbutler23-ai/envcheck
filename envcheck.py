#!/usr/bin/env python3
"""
envcheck - Audit environment variable usage across your codebase.
Finds referenced-but-missing vars and defined-but-unused vars.
Zero dependencies. Pure Python.
"""

import re
import sys
import os
import json
import argparse
from pathlib import Path
from collections import defaultdict


# ── Language patterns ──────────────────────────────────────────────────────────

PATTERNS: dict[str, list[re.Pattern]] = {
    'python': [
        re.compile(r'os\.environ\[[\'"]([\w]+)[\'"]\]'),
        re.compile(r'os\.environ\.get\([\'"]([\w]+)[\'"]'),
        re.compile(r'os\.getenv\([\'"]([\w]+)[\'"]'),
        re.compile(r'environ\.get\([\'"]([\w]+)[\'"]'),
        re.compile(r'getenv\([\'"]([\w]+)[\'"]'),
    ],
    'javascript': [
        re.compile(r'process\.env\.([A-Z_][A-Z0-9_]{1,})(?!\w)'),
        re.compile(r'process\.env\[[\'"]([\w]+)[\'"]\]'),
        re.compile(r'import\.meta\.env\.([A-Z_][A-Z0-9_]*)(?!\w)'),
        re.compile(r'Deno\.env\.get\([\'"]([\w]+)[\'"]'),
        re.compile(r'Bun\.env\.([A-Z_][A-Z0-9_]*)(?!\w)'),
    ],
    'ruby': [
        re.compile(r'ENV\[[\'"]([\w]+)[\'"]\]'),
        re.compile(r'ENV\.fetch\([\'"]([\w]+)[\'"]'),
        re.compile(r'ENV\.dig\([\'"]([\w]+)[\'"]'),
    ],
    'go': [
        re.compile(r'os\.Getenv\("([\w]+)"\)'),
        re.compile(r'os\.LookupEnv\("([\w]+)"\)'),
    ],
    'php': [
        re.compile(r'getenv\([\'"]([\w]+)[\'"]'),
        re.compile(r'\$_ENV\[[\'"]([\w]+)[\'"]\]'),
        re.compile(r'\$_SERVER\[[\'"]([\w]+)[\'"]\]'),
        re.compile(r'env\([\'"]([\w]+)[\'"]'),
    ],
    'rust': [
        re.compile(r'env::var\("([\w]+)"\)'),
        re.compile(r'std::env::var\("([\w]+)"\)'),
        re.compile(r'env::var_os\("([\w]+)"\)'),
    ],
    'shell': [
        re.compile(r'\$\{([A-Z_][A-Z0-9_]+)\}'),
        re.compile(r'(?<!\$)\$([A-Z_][A-Z0-9_]{2,})(?!\w)'),
    ],
    'dotnet': [
        re.compile(r'Environment\.GetEnvironmentVariable\("([\w]+)"\)'),
        re.compile(r'GetEnvironmentVariable\("([\w]+)"\)'),
    ],
    'java': [
        re.compile(r'System\.getenv\("([\w]+)"\)'),
        re.compile(r'System\.getenv\([\'"]([\w]+)[\'"]\)'),
    ],
}

EXT_LANG: dict[str, str] = {
    '.py': 'python', '.pyw': 'python',
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'javascript', '.tsx': 'javascript', '.jsx': 'javascript',
    '.rb': 'ruby',
    '.go': 'go',
    '.php': 'php',
    '.rs': 'rust',
    '.sh': 'shell', '.bash': 'shell', '.zsh': 'shell', '.fish': 'shell',
    '.cs': 'dotnet', '.vb': 'dotnet',
    '.java': 'java', '.kt': 'java',
}

SKIP_DIRS = frozenset({
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env',
    'dist', 'build', '.next', '.nuxt', 'target', 'vendor', 'out',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'coverage',
    '.turbo', '.cache', 'tmp', 'temp',
})

# Common system/CI vars — skip these to reduce noise
SYSTEM_VARS = frozenset({
    'PATH', 'HOME', 'USER', 'SHELL', 'PWD', 'LANG', 'LC_ALL', 'LC_CTYPE',
    'TERM', 'TMPDIR', 'TMP', 'TEMP', 'EDITOR', 'VISUAL', 'PAGER', 'DISPLAY',
    'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR', 'XDG_CACHE_HOME',
    'CI', 'CD', 'GITHUB_ACTIONS', 'GITHUB_TOKEN', 'GITHUB_SHA',
    'TRAVIS', 'CIRCLECI', 'GITLAB_CI', 'BITBUCKET_BUILD_NUMBER',
    'NODE_ENV', 'RAILS_ENV', 'RACK_ENV', 'APP_ENV', 'FLASK_ENV',
    'DEBUG', 'VERBOSE', 'LOG_LEVEL',
})


# ── ANSI colors ────────────────────────────────────────────────────────────────

class C:
    RESET   = '\033[0m'
    BOLD    = '\033[1m'
    DIM     = '\033[2m'
    RED     = '\033[91m'
    GREEN   = '\033[92m'
    YELLOW  = '\033[93m'
    BLUE    = '\033[94m'
    CYAN    = '\033[96m'


def disable_color():
    for a in vars(C):
        if not a.startswith('_'):
            setattr(C, a, '')


# ── Core logic ─────────────────────────────────────────────────────────────────

def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, val = line.partition('=')
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if re.match(r'^[A-Za-z_]\w*$', key):
                    result[key] = val
    except Exception:
        pass
    return result


def find_env_files(root: Path) -> list[Path]:
    found = []
    try:
        for p in sorted(root.iterdir()):
            if p.is_file() and p.name.startswith('.env'):
                found.append(p)
    except PermissionError:
        pass
    return found


Ref = tuple[str, int]  # (filepath, line_number)


def scan_file(path: Path, root: Path) -> dict[str, list[Ref]]:
    lang = EXT_LANG.get(path.suffix.lower())
    if not lang:
        return {}
    patterns = PATTERNS[lang]
    found: dict[str, list[Ref]] = defaultdict(list)
    try:
        for i, line in enumerate(
            path.read_text(encoding='utf-8', errors='replace').splitlines(), 1
        ):
            if line.strip().startswith('#') or line.strip().startswith('//'):
                continue
            for pat in patterns:
                for m in pat.finditer(line):
                    var = m.group(1).upper()
                    if var and var not in SYSTEM_VARS and len(var) >= 2:
                        found[var].append((str(path.relative_to(root)), i))
    except Exception:
        pass
    return dict(found)


def scan_directory(root: Path) -> dict[str, list[Ref]]:
    all_refs: dict[str, list[Ref]] = defaultdict(list)
    file_count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if fp.suffix.lower() in EXT_LANG:
                refs = scan_file(fp, root)
                file_count += 1
                for var, locs in refs.items():
                    all_refs[var].extend(locs)
    return dict(all_refs), file_count


# ── Output ─────────────────────────────────────────────────────────────────────

def print_header():
    print(f"\n{C.BOLD}{C.CYAN}┌──────────────────────────────────┐{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}│       envcheck  ✦  v1.0          │{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}└──────────────────────────────────┘{C.RESET}\n")


def print_results(
    refs: dict[str, list[Ref]],
    env_data: dict[str, dict[str, str]],
    show_refs: bool,
    hide_unused: bool,
    root: Path,
) -> int:
    defined  = set(k for d in env_data.values() for k in d)
    missing  = set(refs) - defined
    unused   = defined - set(refs)
    ok_vars  = set(refs) & defined

    width = max((len(v) for v in (set(refs) | defined)), default=20) + 2
    width = max(width, 26)

    for var in sorted(ok_vars):
        sources = [n for n, d in env_data.items() if var in d]
        print(f"  {C.GREEN}✓{C.RESET}  {C.BOLD}{var:<{width}}{C.RESET}  defined  {C.DIM}({', '.join(sources)}){C.RESET}")
        if show_refs:
            for fpath, line in refs[var][:4]:
                print(f"      {C.DIM}↳  {fpath}:{line}{C.RESET}")

    for var in sorted(missing):
        locs    = refs[var]
        excerpt = ', '.join(f"{r[0]}:{r[1]}" for r in locs[:3])
        more    = f"  +{len(locs)-3} more" if len(locs) > 3 else ''
        print(f"  {C.RED}✗{C.RESET}  {C.BOLD}{var:<{width}}{C.RESET}  {C.RED}MISSING{C.RESET}   ← {C.DIM}{excerpt}{more}{C.RESET}")

    if not hide_unused:
        for var in sorted(unused):
            sources = [n for n, d in env_data.items() if var in d]
            print(f"  {C.YELLOW}⚠{C.RESET}  {C.BOLD}{var:<{width}}{C.RESET}  {C.YELLOW}unused{C.RESET}    (in {', '.join(sources)})")

    print(f"\n{C.DIM}{'─' * 54}{C.RESET}")

    ok_s     = f"{C.GREEN}{len(ok_vars)} ok{C.RESET}"
    miss_s   = f"{C.RED}{len(missing)} missing{C.RESET}"     if missing else f"{C.DIM}0 missing{C.RESET}"
    unused_s = f"{C.YELLOW}{len(unused)} unused{C.RESET}"    if unused  else f"{C.DIM}0 unused{C.RESET}"
    print(f"\n  {ok_s}  ·  {miss_s}  ·  {unused_s}\n")

    if missing:
        print(f"  {C.RED}Add missing vars to your .env (and .env.example){C.RESET}\n")

    return 1 if missing else 0


def print_json(refs: dict[str, list[Ref]], env_data: dict[str, dict[str, str]]) -> int:
    defined  = set(k for d in env_data.values() for k in d)
    missing  = set(refs) - defined
    unused   = defined - set(refs)
    ok_vars  = set(refs) & defined

    out = {
        'summary': {'ok': len(ok_vars), 'missing': len(missing), 'unused': len(unused)},
        'ok':      sorted(ok_vars),
        'missing': {v: [{'file': r[0], 'line': r[1]} for r in refs[v]] for v in sorted(missing)},
        'unused':  sorted(unused),
    }
    print(json.dumps(out, indent=2))
    return 1 if missing else 0


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        prog='envcheck',
        description='Audit environment variable usage across your codebase.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
supported languages:
  Python · JavaScript/TypeScript · Ruby · Go · Rust · PHP · Java/Kotlin · C#/VB · Shell

examples:
  envcheck                        scan current directory
  envcheck --path ./backend       scan a specific path
  envcheck --env .env.production  check against a specific .env file
  envcheck --strict               exit 1 on any missing vars (great for CI)
  envcheck --format json          JSON output for scripts/CI
  envcheck --show-refs            show file:line for each reference
  envcheck --no-unused            hide unused-variable warnings
        ''',
    )
    ap.add_argument('--path',       default='.',  help='directory to scan  (default: .)')
    ap.add_argument('--env',        action='append', metavar='FILE', help='.env file to check against (repeatable)')
    ap.add_argument('--format',     default='plain', choices=['plain', 'json'])
    ap.add_argument('--strict',     action='store_true', help='exit 1 if any vars are missing')
    ap.add_argument('--show-refs',  action='store_true', help='print file:line for each var reference')
    ap.add_argument('--no-unused',  action='store_true', help='hide unused-var warnings')
    ap.add_argument('--no-color',   action='store_true')
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        disable_color()

    print_header()

    root = Path(args.path).resolve()
    print(f"{C.DIM}Scanning: {root}{C.RESET}\n")

    # Load env files
    env_paths = [Path(e) for e in args.env] if args.env else find_env_files(root)
    env_data: dict[str, dict[str, str]] = {}
    for ep in env_paths:
        if ep.exists():
            data = parse_env_file(ep)
            env_data[ep.name] = data
            print(f"  {C.BLUE}📄{C.RESET}  {ep.name:<30}  {C.DIM}{len(data)} var(s){C.RESET}")
        else:
            print(f"  {C.YELLOW}⚠{C.RESET}   {ep} — not found")

    if not env_data:
        print(f"  {C.YELLOW}No .env files found. Showing all referenced vars.{C.RESET}")

    # Scan source
    print(f"\n{C.DIM}Scanning source files…{C.RESET}")
    refs, file_count = scan_directory(root)
    print(f"{C.DIM}Scanned {file_count} source file(s) · {len(refs)} unique var reference(s){C.RESET}\n")
    print(f"{C.DIM}{'─' * 54}{C.RESET}\n")

    if args.format == 'json':
        exit_code = print_json(refs, env_data)
    else:
        exit_code = print_results(refs, env_data, args.show_refs, args.no_unused, root)

    if args.strict:
        sys.exit(exit_code)


if __name__ == '__main__':
    main()
