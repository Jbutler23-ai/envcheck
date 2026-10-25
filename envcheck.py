#!/usr/bin/env python3
"""Find environment variable references missing from dotenv files."""

import argparse
import ast
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import sys


NAME = r'([A-Za-z_][A-Za-z0-9_]*)'
QUOTED_NAME = r'''['"]''' + NAME + r'''['"]'''
NAME_RE = re.compile(NAME + r'\Z')

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


CONFIG_NAME = 'envcheck.json'
CONFIG_KEYS = frozenset({'ignore', 'services', 'environments', 'checks'})
IGNORE_KEYS = frozenset({'paths', 'names'})
SERVICE_KEYS = frozenset({'paths', 'environments', 'ignore'})
CHECK_KEYS = frozenset({'empty'})


class ScanError(Exception):
    """An input could not be read, so the scan is incomplete."""


class PathFilter:
    """Match scan-root-relative POSIX paths against glob patterns.

    A pattern without a slash matches a file or directory name anywhere in the
    tree.  A pattern with a slash is anchored at the scan root.  `*` and `?`
    stay within one path segment; `**` crosses segments.  A pattern that matches
    a directory also excludes everything inside it.  Matching is case-sensitive.
    """

    def __init__(self, patterns: list[str]):
        self.patterns = list(patterns)
        self._regexes = [self._compile(pattern) for pattern in self.patterns]

    @staticmethod
    def _compile(pattern: str) -> re.Pattern:
        text = pattern.replace('\\', '/')
        while text.startswith('./'):
            text = text[2:]
        text = text.strip('/')
        anchored = '/' in text
        parts = []
        index = 0
        while index < len(text):
            if text.startswith('**/', index):
                parts.append('(?:.*/)?')
                index += 3
            elif text.startswith('**', index):
                parts.append('.*')
                index += 2
            elif text[index] == '*':
                parts.append('[^/]*')
                index += 1
            elif text[index] == '?':
                parts.append('[^/]')
                index += 1
            else:
                parts.append(re.escape(text[index]))
                index += 1
        body = ''.join(parts)
        prefix = '' if anchored else '(?:.*/)?'
        return re.compile(prefix + body + r'(?:/.*)?\Z')

    def matches(self, relative_posix_path: str) -> bool:
        return any(regex.match(relative_posix_path) for regex in self._regexes)


class Service:
    """One code location with its own dotenv file list for each environment."""

    def __init__(self, name: str, paths: list[str], environments: dict[str, list[str]],
                 ignore_paths: list[str] = (), ignore_names: set[str] = frozenset()):
        self.name = name
        self.paths = list(paths)
        self.environments = dict(environments)
        self.ignore_paths = list(ignore_paths)
        self.ignore_names = set(ignore_names)


class Config:
    def __init__(self, ignore_paths: list[str] = (), ignore_names: set[str] = frozenset(),
                 source: str | None = None, services: dict[str, Service] | None = None,
                 check_empty: bool = False):
        self.ignore_paths = list(ignore_paths)
        self.ignore_names = set(ignore_names)
        self.source = source
        self.services = dict(services or {})
        self.check_empty = check_empty

    def merged(self, extra_paths: list[str], extra_names: list[str]) -> 'Config':
        """Command-line exclusions add to the project configuration."""
        return Config(
            [*self.ignore_paths, *extra_paths],
            self.ignore_names | set(extra_names),
            self.source,
            self.services,
            self.check_empty,
        )

    def ignore_for(self, service: Service | None) -> tuple[PathFilter, set[str]]:
        """A service's own exclusions add to the project-wide ones."""
        if service is None:
            return PathFilter(self.ignore_paths), set(self.ignore_names)
        return (PathFilter([*self.ignore_paths, *service.ignore_paths]),
                self.ignore_names | service.ignore_names)


def _config_error(path: Path, message: str) -> ScanError:
    return ScanError(f'Invalid configuration in {path}: {message}')


def _string_list(path: Path, value, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _config_error(path, f'"{label}" must be a list of strings.')
    cleaned = [item.strip() for item in value]
    if any(not item for item in cleaned):
        raise _config_error(path, f'"{label}" contains an empty entry.')
    return cleaned


def load_config(path: Path, label: str | None = None) -> Config:
    """Read an envcheck.json file.  Unknown keys and wrong types are errors."""
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise _config_error(path, f'not valid JSON (line {exc.lineno}, column {exc.colno}).') from exc
    if not isinstance(data, dict):
        raise _config_error(path, 'the top level must be a JSON object.')
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise _config_error(path, f'unknown key(s): {", ".join(unknown)}.')
    paths, names = _parse_ignore(path, data.get('ignore', {}), 'ignore')
    defaults = _parse_environments(path, data.get('environments', {}), 'environments')
    services = _parse_services(path, data.get('services', {}), defaults)
    checks = data.get('checks', {})
    if not isinstance(checks, dict):
        raise _config_error(path, '"checks" must be an object.')
    unknown = sorted(set(checks) - CHECK_KEYS)
    if unknown:
        raise _config_error(path, f'unknown key(s) under "checks": {", ".join(unknown)}.')
    check_empty = checks.get('empty', False)
    if not isinstance(check_empty, bool):
        raise _config_error(path, '"checks.empty" must be true or false.')
    return Config(paths, set(names), label or str(path), services, check_empty)


def _parse_ignore(path: Path, ignore, label: str) -> tuple[list[str], list[str]]:
    if not isinstance(ignore, dict):
        raise _config_error(path, f'"{label}" must be an object.')
    unknown = sorted(set(ignore) - IGNORE_KEYS)
    if unknown:
        raise _config_error(path, f'unknown key(s) under "{label}": {", ".join(unknown)}.')
    paths = _string_list(path, ignore.get('paths', []), f'{label}.paths')
    names = _string_list(path, ignore.get('names', []), f'{label}.names')
    for name in names:
        if not NAME_RE.match(name):
            raise _config_error(path, f'"{label}.names" entry is not a valid variable name: {name}')
    return paths, names


def _parse_environments(path: Path, value, label: str) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise _config_error(path, f'"{label}" must be an object mapping an environment name to a list of files.')
    environments = {}
    for name, files in value.items():
        if not name.strip():
            raise _config_error(path, f'"{label}" has an empty environment name.')
        environments[name] = _string_list(path, files, f'{label}.{name}')
    return environments


def _parse_services(path: Path, value, defaults: dict[str, list[str]]) -> dict[str, Service]:
    if not isinstance(value, dict):
        raise _config_error(path, '"services" must be an object mapping a service name to its settings.')
    services = {}
    for name, settings in value.items():
        if not name.strip():
            raise _config_error(path, '"services" has an empty service name.')
        if not isinstance(settings, dict):
            raise _config_error(path, f'"services.{name}" must be an object.')
        unknown = sorted(set(settings) - SERVICE_KEYS)
        if unknown:
            raise _config_error(path, f'unknown key(s) under "services.{name}": {", ".join(unknown)}.')
        if 'paths' not in settings:
            raise _config_error(path, f'"services.{name}" must have a "paths" list.')
        paths = _string_list(path, settings['paths'], f'services.{name}.paths')
        if not paths:
            raise _config_error(path, f'"services.{name}.paths" must list at least one path.')
        # Environment lists are inherited from the top level and replaced, never
        # merged, by a service entry with the same environment name.
        environments = dict(defaults)
        environments.update(
            _parse_environments(path, settings.get('environments', {}), f'services.{name}.environments'))
        if not environments:
            raise _config_error(
                path, f'"services.{name}" has no environments; add them to the service or to the top level.')
        ignore_paths, ignore_names = _parse_ignore(path, settings.get('ignore', {}), f'services.{name}.ignore')
        services[name] = Service(name, paths, environments, ignore_paths, set(ignore_names))
    return services


def validate_names(names: list[str], option: str) -> list[str]:
    for name in names:
        if not NAME_RE.match(name):
            raise ScanError(f'{option} value is not a valid variable name: {name}')
    return names



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


class EnvEntry:
    """One assignment: where it is and whether its value is empty."""

    __slots__ = ('name', 'line', 'empty')

    def __init__(self, name: str, line: int, empty: bool):
        self.name = name
        self.line = line
        self.empty = empty


class ParsedEnv:
    """Assignment names and malformed lines from one dotenv file.

    Values are read only far enough to tell an empty value from a filled one
    and to find where a value ends.  They are never stored or reported.
    """

    def __init__(self, entries: dict[str, EnvEntry] | None = None,
                 problems: list[tuple[int, str]] | None = None):
        self.entries = entries or {}
        self.problems = problems or []

    @property
    def names(self) -> set[str]:
        return set(self.entries)

    def without(self, ignored: set[str]) -> 'ParsedEnv':
        return ParsedEnv({name: entry for name, entry in self.entries.items() if name not in ignored},
                         list(self.problems))


def _unquoted_is_empty(text: str) -> bool:
    """An unquoted value ends at an unescaped ` #` comment.

    Any escaped character is content, including a lone trailing backslash, so
    `NAME=\\` and `NAME=\\ ` are values, not blanks.
    """
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            return False
        if char == '\\':
            escaped = True
            continue
        if char == '#' and (index == 0 or text[index - 1].isspace()):
            break
        if not char.isspace():
            return False
    return not escaped


def _read_quoted(lines: list[str], index: int, column: int, quote: str) -> tuple[bool, int, str]:
    """Consume a quoted value, returning (is_empty, end_index, trailing_text).

    Double quotes honour backslash escapes; single quotes are literal.  A value
    may span lines, in which case the newlines are part of it.
    """
    content = []
    escaped = False
    line_index = index
    position = column
    while line_index < len(lines):
        text = lines[line_index]
        while position < len(text):
            char = text[position]
            if escaped:
                content.append(char)
                escaped = False
            elif quote == '"' and char == '\\':
                escaped = True
            elif char == quote:
                return not ''.join(content), line_index, text[position + 1:]
            else:
                content.append(char)
            position += 1
        content.append('\n')
        escaped = False
        line_index += 1
        position = 0
    raise ScanError('unclosed')


def parse_env_file(path: Path) -> ParsedEnv:
    """Read assignment names, empty-value flags, and malformed line numbers."""
    entries: dict[str, EnvEntry] = {}
    problems: list[tuple[int, str]] = []
    lines = read_text(path).splitlines()
    index = 0
    while index < len(lines):
        line_number = index + 1
        raw = lines[index].strip()
        if not raw or raw.startswith('#'):
            index += 1
            continue
        match = ENV_ASSIGNMENT.match(raw)
        if not match:
            reason = ('name before "=" is not a valid variable name' if '=' in raw
                      else 'no "=" assignment')
            problems.append((line_number, reason))
            index += 1
            continue
        name = match.group(1)
        remainder = raw[match.end():]
        offset = len(remainder) - len(remainder.lstrip())
        value = remainder.lstrip()
        if value[:1] in ('"', "'"):
            column = lines[index].rstrip().rfind(remainder) + offset + 1
            try:
                empty, end_index, trailing = _read_quoted(lines, index, column, value[0])
            except ScanError:
                raise ScanError(
                    f'Cannot read {path}: unclosed quoted value on line {line_number}.') from None
            if trailing.strip() and not trailing.lstrip().startswith('#'):
                problems.append((line_number, 'unexpected text after the closing quote'))
            index = end_index
        else:
            empty = _unquoted_is_empty(value)
        if name in entries:
            problems.append((line_number, f'duplicate of the assignment on line {entries[name].line}'))
        entries[name] = EnvEntry(name, line_number, empty)
        index += 1
    return ParsedEnv(entries, problems)


def find_env_files(root: Path) -> list[Path]:
    return [
        path for path in sorted(root.iterdir())
        if (path.name == '.env' or path.name.startswith('.env.'))
        and not path.is_symlink() and path.is_file()
    ]


# Python scanning ------------------------------------------------------------
#
# Python files are parsed with the ast module so that comments, docstrings and
# ordinary strings never produce matches, and so that lookups split across
# lines or made through import aliases are still found.  Each scope tracks what
# its names refer to: the os module, os.environ, os.getenv, or something else.
# A name that is rebound to anything else (a parameter, an assignment, another
# import) stops being treated as an environment lookup in that scope.

OS_MODULE = 'os'
OS_ENVIRON = 'os.environ'
OS_GETENV = 'os.getenv'
ENV_ROLES = {'environ': OS_ENVIRON, 'getenv': OS_GETENV}
# Unbound names keep their conventional meaning so a file that calls
# getenv("NAME") without a visible import is still checked.
IMPLICIT_ROLES = {'os': OS_MODULE, 'environ': OS_ENVIRON, 'getenv': OS_GETENV}


class _Scope:
    __slots__ = ('kind', 'bindings', 'globals')

    def __init__(self, kind: str):
        self.kind = kind
        self.bindings: dict[str, str | None] = {}
        self.globals: set[str] = set()

    def bind(self, name: str, role: str | None) -> None:
        if name in self.bindings and self.bindings[name] != role:
            role = None
        self.bindings[name] = role


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for item in node.elts for name in _target_names(item)]
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return []


def _annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Annotations are evaluated in the enclosing scope, like defaults."""
    args = node.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    found = [param.annotation for param in params if param is not None and param.annotation is not None]
    if node.returns is not None:
        found.append(node.returns)
    return found


def _argument_names(args: ast.arguments) -> list[str]:
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        params.append(args.vararg)
    if args.kwarg:
        params.append(args.kwarg)
    return [param.arg for param in params]


class _BindingCollector(ast.NodeVisitor):
    """Record every name bound directly in one scope.

    Nested function, lambda and class bodies are separate scopes and are not
    entered.  Comprehensions are entered only for walrus targets, which bind in
    the enclosing scope; their loop targets belong to the comprehension.
    """

    def __init__(self, scope: _Scope):
        self.scope = scope

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.partition('.')[0]
            if alias.asname is None:
                self.scope.bind(top, OS_MODULE if top == 'os' else None)
            else:
                self.scope.bind(alias.asname, OS_MODULE if alias.name == 'os' else None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        from_os = node.module == 'os' and node.level == 0
        for alias in node.names:
            if alias.name == '*':
                continue
            role = ENV_ROLES.get(alias.name) if from_os else None
            self.scope.bind(alias.asname or alias.name, role)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        for name in node.names:
            self.scope.bind(name, None)

    def _bind_targets(self, *targets: ast.AST) -> None:
        for target in targets:
            for name in _target_names(target):
                self.scope.bind(name, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._bind_targets(*node.targets)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind_targets(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind_targets(node.target)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind_targets(node.target)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self._bind_targets(node.target)
        self.visit(node.iter)
        for child in [*node.body, *node.orelse]:
            self.visit(child)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_targets(item.optional_vars)
        for child in node.body:
            self.visit(child)

    visit_AsyncWith = visit_With

    def visit_Delete(self, node: ast.Delete) -> None:
        self._bind_targets(*node.targets)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.scope.bind(node.name, None)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.scope.bind(node.rest, None)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.bind(node.name, None)
        for child in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults, *_annotations(node)]:
            if child is not None:
                self.visit(child)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.bind(node.name, None)
        for child in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(child)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for child in [*node.args.defaults, *node.args.kw_defaults]:
            if child is not None:
                self.visit(child)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)

    def generic_visit(self, node: ast.AST) -> None:
        if type(node).__name__ == 'TypeAlias':  # Python 3.12+
            self._bind_targets(node.name)
            return
        super().generic_visit(node)


class _LookupVisitor(ast.NodeVisitor):
    """Collect literal environment lookups with the line where each starts."""

    def __init__(self):
        self.scopes: list[_Scope] = []
        self.found: dict[str, set[int]] = defaultdict(set)
        # Names with at least one lookup that has no fallback value.
        self.required: set[str] = set()
        # One line entry per lookup whose key could not be read as a literal
        # name; two such lookups on one line are two entries.
        self.computed: list[int] = []

    # -- scope handling -----------------------------------------------------

    def _enter(self, kind: str, body: list[ast.AST], params: list[str] = ()) -> _Scope:
        scope = _Scope(kind)
        for name in params:
            scope.bind(name, None)
        collector = _BindingCollector(scope)
        for node in body:
            collector.visit(node)
        self.scopes.append(scope)
        return scope

    def _leave(self) -> None:
        self.scopes.pop()

    def _resolve(self, name: str) -> str | None:
        innermost = len(self.scopes) - 1
        index = innermost
        while index >= 0:
            scope = self.scopes[index]
            if name in scope.globals:
                index = 0
                scope = self.scopes[0]
            if scope.kind != 'class' or index == innermost:
                if name in scope.bindings:
                    return scope.bindings[name]
            index -= 1
        return IMPLICIT_ROLES.get(name)

    def _role(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._resolve(node.id)
        if isinstance(node, ast.Attribute) and node.attr in ENV_ROLES:
            if self._role(node.value) == OS_MODULE:
                return ENV_ROLES[node.attr]
        return None

    # -- scope-creating nodes ------------------------------------------------

    def visit_Module(self, node: ast.Module) -> None:
        module = self._enter('module', node.body)
        # A function or class body that declares a name global and rebinds it
        # rebinds the module-level name.  The new meaning is merged into the
        # module scope: `global os; import os` keeps os as the os module, while
        # `global os; os = other()` stops os from matching anywhere.
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = _Scope('function')
                collector = _BindingCollector(inner)
                for statement in child.body:
                    collector.visit(statement)
                for name in inner.globals & inner.bindings.keys():
                    module.bind(name, inner.bindings[name])
        self.generic_visit(node)
        self._leave()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for child in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults, *_annotations(node)]:
            if child is not None:
                self.visit(child)
        self._enter('function', node.body, _argument_names(node.args))
        for child in node.body:
            self.visit(child)
        self._leave()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for child in [*node.args.defaults, *node.args.kw_defaults]:
            if child is not None:
                self.visit(child)
        self._enter('function', [node.body], _argument_names(node.args))
        self.visit(node.body)
        self._leave()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for child in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(child)
        self._enter('class', node.body)
        for child in node.body:
            self.visit(child)
        self._leave()

    def _visit_comprehension(self, node: ast.AST) -> None:
        # The first iterable is evaluated in the enclosing scope; everything
        # else sees the comprehension's own loop targets.
        first, *rest = node.generators
        self.visit(first.iter)
        targets = [name for gen in node.generators for name in _target_names(gen.target)]
        self._enter('comprehension', [], targets)
        for condition in first.ifs:
            self.visit(condition)
        for gen in rest:
            self.visit(gen.iter)
            for condition in gen.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self._leave()

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _visit_comprehension

    # -- lookups --------------------------------------------------------------

    def _record(self, key: ast.AST, node: ast.AST, has_fallback: bool) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and NAME_RE.match(key.value):
            self.found[key.value].add(node.lineno)
            if not has_fallback:
                self.required.add(key.value)
        else:
            self.computed.append(node.lineno)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._role(node.value) == OS_ENVIRON:
            self._record(node.slice, node, has_fallback=False)
        self.generic_visit(node)

    @staticmethod
    def _has_fallback(node: ast.Call) -> bool:
        """True when the call passes a fallback value that is not literally None.

        `*[]` and `**{}` expand to nothing, so they are not fallbacks.  A starred
        argument whose contents cannot be read, such as `*args`, is not counted
        either, because it may expand to nothing.
        """
        fallback = None
        positional = [arg for arg in node.args if not isinstance(arg, ast.Starred)]
        if len(positional) > 1:
            fallback = positional[1]
        for arg in node.args:
            if isinstance(arg, ast.Starred) and isinstance(arg.value, (ast.List, ast.Tuple)):
                if arg.value.elts and len(positional) == 1:
                    fallback = arg.value.elts[0]
        for keyword in node.keywords:
            if keyword.arg == 'default':
                fallback = keyword.value
            elif keyword.arg is None and isinstance(keyword.value, ast.Dict):
                for key, value in zip(keyword.value.keys, keyword.value.values):
                    if isinstance(key, ast.Constant) and key.value == 'default':
                        fallback = value
        if fallback is None:
            return False
        return not (isinstance(fallback, ast.Constant) and fallback.value is None)

    @staticmethod
    def _key_node(node: ast.Call) -> ast.AST | None:
        """The key argument, positional or `key=`; None when the call has no key."""
        for arg in node.args:
            return arg
        for keyword in node.keywords:
            if keyword.arg == 'key':
                return keyword.value
        return None

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_lookup = self._role(func) == OS_GETENV or (
            isinstance(func, ast.Attribute) and func.attr == 'get'
            and self._role(func.value) == OS_ENVIRON
        )
        key = self._key_node(node) if is_lookup else None
        if key is not None:
            self._record(key, node, self._has_fallback(node))
        self.generic_visit(node)


def scan_python(text: str) -> dict[str, set[int]] | None:
    """Return literal lookups by line, or None when the file cannot be parsed."""
    result = scan_python_detailed(text)
    return None if result is None else result[0]


def scan_python_detailed(text: str) -> tuple[dict[str, set[int]], set[str], list[int]] | None:
    """Lookups by line, names required (no fallback), and lines with computed keys."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    visitor = _LookupVisitor()
    visitor.visit(tree)
    return visitor.found, visitor.required, visitor.computed


def scan_with_patterns(text: str, lang: str) -> dict[str, set[int]]:
    found: dict[str, set[int]] = defaultdict(set)
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(('#', '//')):
            continue
        for pattern in PATTERNS[lang]:
            for match in pattern.finditer(line):
                found[match.group(1)].add(line_number)
    return found


def scan_file(path: Path, root: Path) -> dict[str, list[Ref]]:
    return scan_file_detailed(path, root)[0]


def scan_file_detailed(path: Path, root: Path) -> tuple[dict[str, list[Ref]], set[str], list[Ref]]:
    """References, required names, and locations of lookups with a computed key.

    Only the Python scanner can see fallbacks or computed keys; pattern-matched
    languages treat every reference as required and report nothing computed.
    """
    lang = EXT_LANG.get(path.suffix.lower())
    if not lang or path.is_symlink():
        return {}, set(), []
    relative_path = path.relative_to(root).as_posix()
    text = read_text(path)
    detailed = scan_python_detailed(text) if lang == 'python' else None
    if detailed is None:
        found = scan_with_patterns(text, lang)
        required, computed_lines = set(found), []
    else:
        found, required, computed_lines = detailed
    refs = {
        name: sorted((relative_path, line) for line in found[name])
        for name in sorted(found)
    }
    return refs, required, sorted((relative_path, line) for line in computed_lines)


def scan_directory(root: Path, ignore: PathFilter | None = None, start: Path | None = None,
                   seen: set[Path] | None = None) -> tuple[dict[str, list[Ref]], int]:
    refs, file_count, _, _ = scan_directory_detailed(root, ignore, start, seen)
    return refs, file_count


def scan_directory_detailed(root: Path, ignore: PathFilter | None = None, start: Path | None = None,
                            seen: set[Path] | None = None
                            ) -> tuple[dict[str, list[Ref]], int, set[str], list[Ref]]:
    """Scan `start` (default: the whole root), labelling paths relative to root.

    When `seen` is given, files already in it are skipped and new ones added, so
    overlapping scan paths read and count each file once.  The third result is
    the set of names required by at least one file, and the fourth is where
    lookups with a computed key were found.
    """
    all_refs: dict[str, list[Ref]] = defaultdict(list)
    required: set[str] = set()
    computed: list[Ref] = []
    file_count = 0
    ignore = ignore or PathFilter([])
    start = start or root

    def walk_error(error: OSError):
        raise ScanError(f'Cannot scan {error.filename}: {error.strerror}.') from error

    def excluded(path: Path) -> bool:
        return ignore.matches(path.relative_to(root).as_posix())

    for dirpath, dirnames, filenames in os.walk(start, onerror=walk_error, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not (Path(dirpath) / name).is_symlink()
            and not excluded(Path(dirpath) / name)
        )
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() in EXT_LANG and not path.is_symlink() and not excluded(path):
                if seen is not None:
                    if path.resolve() in seen:
                        continue
                    seen.add(path.resolve())
                refs, file_required, file_computed = scan_file_detailed(path, root)
                required |= file_required
                computed.extend(file_computed)
                file_count += 1
                for name, locations in refs.items():
                    all_refs[name].extend(locations)
    return ({name: sorted(all_refs[name]) for name in sorted(all_refs)},
            file_count, required, sorted(computed))


def resolve_inside(root: Path, relative: str, label: str) -> Path:
    """Resolve a configured path under the scan root, rejecting escapes."""
    target = root / relative
    try:
        resolved = target.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError) as exc:
        raise ScanError(f'{label} is outside the scan directory: {relative}') from exc
    if target.is_symlink():
        raise ScanError(f'{label} is a symlink and is not followed: {relative}')
    if not target.exists():
        raise ScanError(f'{label} does not exist: {relative}')
    return target


def scan_service(root: Path, service: Service, ignore: PathFilter
                 ) -> tuple[dict[str, list[Ref]], int, set[str], list[Ref]]:
    """Scan every path of one service, with each file counted once."""
    all_refs: dict[str, list[Ref]] = defaultdict(list)
    required: set[str] = set()
    computed: list[Ref] = []
    seen: set[Path] = set()
    file_count = 0
    for relative in service.paths:
        label = f'Path for service "{service.name}"'
        target = resolve_inside(root, relative, label)
        if target.is_dir():
            refs, count, path_required, path_computed = scan_directory_detailed(
                root, ignore, start=target, seen=seen)
            file_count += count
        elif target.resolve() in seen or ignore.matches(target.relative_to(root).as_posix()):
            continue
        else:
            seen.add(target.resolve())
            refs, path_required, path_computed = scan_file_detailed(target, root)
            file_count += 1 if refs or target.suffix.lower() in EXT_LANG else 0
        required |= path_required
        computed.extend(path_computed)
        for name, locations in refs.items():
            all_refs[name].extend(locations)
    deduped = {name: sorted(set(all_refs[name])) for name in sorted(all_refs)}
    return deduped, file_count, required, sorted(computed)


def load_environment(root: Path, service: Service, environment: str) -> tuple[dict[str, ParsedEnv], list[str]]:
    """Read one environment's dotenv files; names are never merged across environments."""
    env_data: dict[str, ParsedEnv] = {}
    for relative in service.environments[environment]:
        label = f'Dotenv file for service "{service.name}" environment "{environment}"'
        path = resolve_inside(root, relative, label)
        if not path.is_file():
            raise ScanError(f'{label} is not a file: {relative}')
        env_data[path.relative_to(root).as_posix()] = parse_env_file(path)
    return env_data, list(env_data)


def check_services(root: Path, config: Config, services: list[Service],
                   environments: list[str] | None) -> list[dict]:
    """Build one independent result per service and environment."""
    results = []
    for service in services:
        ignore_filter, ignore_names = config.ignore_for(service)
        refs, file_count, required, computed = scan_service(root, service, ignore_filter)
        refs = {name: refs[name] for name in refs if name not in ignore_names}
        required -= ignore_names
        selected = [name for name in service.environments if environments is None or name in environments]
        for environment in sorted(selected):
            env_data, files = load_environment(root, service, environment)
            env_data = {label: parsed.without(ignore_names) for label, parsed in env_data.items()}
            results.append({
                'service': service.name, 'environment': environment, 'files': files,
                'refs': refs, 'required': required, 'computed': computed,
                'env_data': env_data, 'file_count': file_count,
            })
    return results


def effective_entries(env_data: dict[str, ParsedEnv]) -> dict[str, tuple[str, EnvEntry]]:
    """Later files in an environment override earlier ones, so the last wins."""
    effective: dict[str, tuple[str, EnvEntry]] = {}
    for label, parsed in env_data.items():
        for name, entry in parsed.entries.items():
            effective[name] = (label, entry)
    return effective


def result_groups(refs: dict[str, list[Ref]], env_data: dict[str, ParsedEnv], check_empty: bool = False,
                  required: set[str] | None = None):
    """Split names into ok, missing, unused, optional, and (optionally) empty.

    An empty name is defined, so it is never reported as missing.  An undefined
    name is optional only when every reference to it carries a fallback value;
    one reference without a fallback makes it missing.  When `required` is not
    given every reference is treated as required.
    """
    effective = effective_entries(env_data)
    defined = set(effective)
    referenced = set(refs)
    required = referenced if required is None else required
    empty = {name for name in referenced & defined if effective[name][1].empty} if check_empty else set()
    undefined = referenced - defined
    missing = undefined & required
    optional = undefined - required
    return (referenced & defined) - empty, missing, defined - referenced, empty, optional


def env_problems(env_data: dict[str, ParsedEnv]) -> list[tuple[str, int, str]]:
    return [(label, line, reason)
            for label, parsed in env_data.items() for line, reason in parsed.problems]


def print_results(
    refs: dict[str, list[Ref]],
    env_data: dict[str, ParsedEnv],
    show_refs: bool,
    hide_unused: bool,
    colors: Colors,
    check_empty: bool = False,
    required: set[str] | None = None,
    computed: list[Ref] | None = None,
) -> int:
    ok_vars, missing, unused, empty, optional = result_groups(refs, env_data, check_empty, required)
    effective = effective_entries(env_data)
    problems = env_problems(env_data)
    width = max((len(name) for name in set(refs) | unused), default=20) + 2
    c = colors

    def sources(name: str) -> str:
        return ', '.join(label for label, parsed in env_data.items() if name in parsed.entries)

    for name in sorted(ok_vars):
        print(f'  {c.green}[ok]{c.reset}      {name:<{width}} defined ({sources(name)})')
        if show_refs:
            for path, line in refs[name]:
                print(f'      {c.dim}{path}:{line}{c.reset}')
    for name in sorted(missing):
        locations = refs[name] if show_refs else refs[name][:3]
        excerpt = ', '.join(f'{path}:{line}' for path, line in locations)
        remaining = len(refs[name]) - len(locations)
        more = f' (+{remaining} more)' if remaining else ''
        print(f'  {c.red}[missing]{c.reset} {name:<{width}} {excerpt}{more}')
    for name in sorted(optional):
        locations = refs[name] if show_refs else refs[name][:3]
        excerpt = ', '.join(f'{path}:{line}' for path, line in locations)
        remaining = len(refs[name]) - len(locations)
        more = f' (+{remaining} more)' if remaining else ''
        print(f'  {c.dim}[optional]{c.reset} {name:<{width - 1}} not defined; code has a fallback '
              f'({excerpt}{more})')
    for name in sorted(empty):
        label, entry = effective[name]
        print(f'  {c.yellow}[empty]{c.reset}   {name:<{width}} defined but not filled in '
              f'({label}:{entry.line})')
    if not hide_unused:
        for name in sorted(unused):
            print(f'  {c.yellow}[unused]{c.reset}  {name:<{width}} in {sources(name)}')
    for label, line, reason in problems:
        print(f'  {c.red}[invalid]{c.reset} {label}:{line} {reason}')
    computed = computed or []
    counts = f'{len(ok_vars)} ok / {len(missing)} missing'
    if optional:
        counts += f' / {len(optional)} optional'
    if check_empty:
        counts += f' / {len(empty)} empty'
    counts += f' / {len(unused)} unused'
    if problems:
        counts += f' / {len(problems)} invalid line(s)'
    print(f'\n  {counts}\n')
    if computed:
        shown = computed if show_refs else computed[:3]
        excerpt = ', '.join(f'{path}:{line}' for path, line in shown)
        remaining = len(computed) - len(shown)
        more = f' (+{remaining} more)' if remaining else ''
        print(f'  {len(computed)} lookup(s) use a computed key and were not checked '
              f'({excerpt}{more}).\n')
    if missing:
        print('  Check missing names against the dotenv files for this environment.\n')
    return 1 if (missing or empty or problems) else 0


def check_json(refs: dict[str, list[Ref]], env_data: dict[str, ParsedEnv],
               hide_unused: bool, check_empty: bool, required: set[str] | None = None,
               computed: list[Ref] | None = None) -> tuple[dict, int]:
    """One check's report; the same data the plain output prints."""
    ok_vars, missing, unused, empty, optional = result_groups(refs, env_data, check_empty, required)
    effective = effective_entries(env_data)
    problems = env_problems(env_data)
    computed = computed or []

    def locations(name: str) -> list[dict]:
        return [{'file': path, 'line': line} for path, line in refs[name]]

    report = {
        'summary': {'ok': len(ok_vars), 'missing': len(missing), 'optional': len(optional),
                    'unused': len(unused), 'computed': len(computed)},
        'ok': sorted(ok_vars),
        'missing': {name: locations(name) for name in sorted(missing)},
        'optional': {name: locations(name) for name in sorted(optional)},
        'unused': [] if hide_unused else sorted(unused),
        'invalid': [{'file': label, 'line': line, 'reason': reason} for label, line, reason in problems],
        'computed': [{'file': path, 'line': line} for path, line in computed],
    }
    if check_empty:
        report['summary']['empty'] = len(empty)
        report['empty'] = {
            name: {'file': effective[name][0], 'line': effective[name][1].line}
            for name in sorted(empty)
        }
    report['summary']['invalid'] = len(problems)
    return report, 1 if (missing or empty or problems) else 0


def print_json(refs: dict[str, list[Ref]], env_data: dict[str, ParsedEnv],
               hide_unused: bool = False, check_empty: bool = False,
               required: set[str] | None = None, computed: list[Ref] | None = None) -> int:
    report, status = check_json(refs, env_data, hide_unused, check_empty, required, computed)
    print(json.dumps(report, indent=2))
    return status


def print_service_results(results: list[dict], show_refs: bool, hide_unused: bool,
                          colors: Colors, check_empty: bool = False) -> int:
    status = 0
    current_service = None
    for result in results:
        if result['service'] != current_service:
            current_service = result['service']
            print(f'{colors.bold}Service: {current_service}{colors.reset}'
                  f'  ({result["file_count"]} source file(s))')
        files = ', '.join(result['files']) or 'no dotenv files'
        print(f'\n  {colors.bold}{result["environment"]}{colors.reset} ({files})\n')
        status |= print_results(result['refs'], result['env_data'], show_refs,
                                hide_unused, colors, check_empty, result['required'],
                                result['computed'])
    return status


def service_json(results: list[dict], hide_unused: bool, check_empty: bool = False) -> tuple[dict, int]:
    services: dict[str, dict] = {}
    totals = {'ok': 0, 'missing': 0, 'optional': 0, 'unused': 0, 'invalid': 0, 'computed': 0}
    if check_empty:
        totals['empty'] = 0
    status = 0
    for result in results:
        report, result_status = check_json(result['refs'], result['env_data'], hide_unused,
                                           check_empty, result['required'], result['computed'])
        status |= result_status
        for key in totals:
            totals[key] += report['summary'][key]
        entry = services.setdefault(result['service'],
                                    {'files_scanned': result['file_count'], 'environments': {}})
        entry['environments'][result['environment']] = {'files': result['files'], **report}
    summary = {'services': len(services), 'checks': len(results), **totals}
    return {'summary': summary, 'services': services}, status


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
  envcheck --ignore-path "tests/**" --ignore-name PATH
                                    add exclusions on top of envcheck.json

  envcheck --service api --environment production
                                    check one configured service and environment
  envcheck --check-empty            also report settings that are not filled in

configuration (envcheck.json in the scan directory):
  {"ignore": {"paths": ["generated/**"], "names": ["PATH"]},
   "environments": {"development": [".env"]},
   "services": {"api": {"paths": ["services/api"],
                        "environments": {"production": [".env.production"]}}}}

  Each service and environment is checked separately, so a name defined only
  for development cannot make a production check pass.

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
    parser.add_argument('--config', metavar='FILE', help=f'configuration file (default: {CONFIG_NAME} in the scan directory)')
    parser.add_argument('--no-config', action='store_true', help='ignore any configuration file')
    parser.add_argument('--ignore-path', action='append', default=[], metavar='GLOB', help='skip matching files or directories (repeatable; adds to the configuration)')
    parser.add_argument('--ignore-name', action='append', default=[], metavar='NAME', help='skip a variable name (repeatable; adds to the configuration)')
    parser.add_argument('--service', action='append', default=[], metavar='NAME', help='only check this configured service (repeatable)')
    parser.add_argument('--environment', action='append', default=[], metavar='NAME', help='only check this configured environment (repeatable)')
    parser.add_argument('--check-empty', action='store_true', help='also report names that are defined but have no value')
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')

    colors = Colors(not args.no_color and sys.stdout.isatty() and 'NO_COLOR' not in os.environ)
    check_empty = args.check_empty
    try:
        root = Path(args.path).resolve()
        if not root.is_dir():
            raise ScanError(f'Scan path is not a directory: {root}')
        config = Config()
        if args.config and args.no_config:
            raise ScanError('--config and --no-config cannot be used together.')
        if args.config:
            config = load_config(Path(args.config).resolve(), args.config)
        elif not args.no_config and (root / CONFIG_NAME).is_file():
            config = load_config(root / CONFIG_NAME, CONFIG_NAME)
        config = config.merged(args.ignore_path, validate_names(args.ignore_name, '--ignore-name'))
        check_empty = args.check_empty or config.check_empty
        service_results = None
        if (args.service or args.environment) and not config.services:
            raise ScanError('--service and --environment need services in the configuration file.')
        if config.services and args.env:
            raise ScanError('--env cannot be combined with configured services; use --service or --no-config.')
        if config.services:
            unknown = sorted(set(args.service) - set(config.services))
            if unknown:
                raise ScanError(f'Unknown service(s): {", ".join(unknown)}. '
                                f'Configured: {", ".join(sorted(config.services))}.')
            chosen = [config.services[name] for name in sorted(args.service or config.services)]
            known_environments = {name for service in chosen for name in service.environments}
            unknown = sorted(set(args.environment) - known_environments)
            if unknown:
                raise ScanError(f'Unknown environment(s): {", ".join(unknown)}. '
                                f'Configured: {", ".join(sorted(known_environments))}.')
            service_results = check_services(root, config, chosen, args.environment or None)
        # With services configured, only the files named for each service are
        # read; nothing is discovered from the scan directory.
        env_data: dict[str, ParsedEnv] = {}
        if service_results is None:
            env_paths = [Path(name).resolve() for name in args.env] if args.env else find_env_files(root)
            for path in env_paths:
                try:
                    label = path.relative_to(root).as_posix()
                except ValueError:
                    label = str(path)
                env_data[label] = parse_env_file(path)
        if service_results is None:
            refs, file_count, required, computed = scan_directory_detailed(
                root, PathFilter(config.ignore_paths))
        else:
            refs, file_count, required, computed = {}, 0, set(), []
    except (ScanError, OSError, RuntimeError) as exc:
        print(f'envcheck: {exc}', file=sys.stderr)
        return 2

    refs = {name: refs[name] for name in refs if name not in config.ignore_names}
    required -= config.ignore_names
    env_data = {label: parsed.without(config.ignore_names) for label, parsed in env_data.items()}

    if args.format == 'json':
        if service_results is not None:
            report, result = service_json(service_results, args.no_unused, check_empty)
            print(json.dumps(report, indent=2))
        else:
            result = print_json(refs, env_data, args.no_unused, check_empty, required, computed)
    else:
        print(f'\n{colors.bold}envcheck{colors.reset}\n\nScanning: {root}\n')
        if config.source:
            print(f'  Configuration: {config.source}')
        if config.ignore_paths or config.ignore_names:
            print(f'  Ignoring {len(config.ignore_paths)} path pattern(s) and {len(config.ignore_names)} name(s)')
        if service_results is not None:
            print(f'\nEach service and environment is checked separately.\n')
            result = print_service_results(service_results, args.show_refs, args.no_unused,
                                           colors, check_empty)
        else:
            for label, parsed in env_data.items():
                print(f'  {label}: {len(parsed.entries)} name(s)')
            if not env_data:
                print('  No dotenv files found; every name without a fallback is reported'
                      ' as missing.')
            print(f'\nScanned {file_count} source file(s); {len(refs)} unique name(s).\n')
            result = print_results(refs, env_data, args.show_refs, args.no_unused, colors,
                                   check_empty, required, computed)
    return result if args.strict else 0


if __name__ == '__main__':
    raise SystemExit(main())
