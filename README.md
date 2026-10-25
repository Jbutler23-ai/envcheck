# envcheck

envcheck compares environment variable references in source code with the names
defined in dotenv files. It reports missing variables and definitions that no
longer appear in the code.

It's for developers keeping local configuration and example files in sync with
an application. It runs locally, uses Python 3.10 or newer, and has no runtime
dependencies. Reports contain variable names and file locations, never values.

## Install

```sh
git clone https://github.com/Jbutler23-ai/envcheck.git
cd envcheck
python -m pip install .
envcheck --help
```

You can also run `python envcheck.py` without installing anything. For development,
use `python -m pip install --editable .`.

## Try it

The included example uses made-up settings and needs no credentials:

```sh
python envcheck.py --path examples/basic --strict --show-refs
python envcheck.py --path examples/basic --format json
```

Both `API_URL` and `REQUEST_TIMEOUT` should be reported as defined. To check your
own application:

```sh
envcheck --path ../my-app --env ../my-app/.env.example --strict
```

Without `--env`, envcheck reads `.env` and `.env.*` files directly inside the scan
root. It combines their keys. That checks coverage across the selected files;
it does **not** prove that each environment has all the settings it needs. For a
deployment check, select its file explicitly. Repeated `--env` arguments also
combine keys. Relative `--env` paths are relative to your current working
directory, even when `--path` points somewhere else.

## Options

| Option | Purpose |
| --- | --- |
| `--path PATH` | Directory to scan recursively; defaults to the current directory |
| `--env FILE` | Dotenv file to compare against; can be repeated |
| `--format plain\|json` | Text or machine-readable JSON output |
| `--strict` | Exit with status 1 when a referenced name is missing |
| `--show-refs` | Include reference locations in text output |
| `--no-unused` | Hide unused-variable details; summary counts remain |
| `--no-color` | Disable terminal colors |
| `--config FILE` | Configuration file to use instead of `envcheck.json` in the scan directory |
| `--no-config` | Ignore any configuration file |
| `--ignore-path GLOB` | Skip matching files or directories; can be repeated |
| `--ignore-name NAME` | Skip a variable name in both code and dotenv files; can be repeated |
| `--service NAME` | Only check this configured service; can be repeated |
| `--environment NAME` | Only check this configured environment; can be repeated |
| `--check-empty` | Also report names that are defined but have no value |

Exit status is 0 after a completed scan, 1 for missing variables with `--strict`,
and 2 for invalid arguments or a file/directory read error. Errors go to stderr.
JSON mode writes only JSON to stdout after a successful scan, including when
`--strict` finds missing names.

## Configuration

Put an `envcheck.json` file in the directory you scan to record what the project
should skip. It is read automatically; `--config FILE` selects a different file
and `--no-config` disables it.

```json
{
  "ignore": {
    "paths": ["generated/**", "tests", "*.test.js"],
    "names": ["PATH", "HOME", "CI"]
  }
}
```

`ignore.paths` holds glob patterns relative to the scan directory, using forward
slashes on every platform. A pattern without a slash, such as `tests`, matches a
file or directory with that name anywhere in the tree. A pattern with a slash is
anchored at the scan directory. `*` and `?` stay within one path segment and
`**` crosses segments. A pattern that matches a directory skips everything inside
it. Matching is case-sensitive and applies only to source scanning, never to
dotenv discovery.

`ignore.names` lists variable names to leave out of the comparison. An ignored
name is not reported as missing when code references it and not reported as
unused when a dotenv file defines it. Only names listed exactly are affected.

`--ignore-path` and `--ignore-name` add to the configuration file rather than
replacing it, so a checked-in file gives the same baseline locally and in CI.
Unknown keys, wrong value types, empty entries, and invalid names are errors with
exit status 2, so a typo cannot silently widen a scan. The built-in skip list for
dependency and build directories always applies.

## Services and environments

A project with more than one service, or more than one deployment target, can
describe them in `envcheck.json`. Each service names its own code folders, and
each environment names its own dotenv files. Every service and environment pair
is then checked **separately**, so a value defined only for development can
never make a production check pass.

```json
{
  "environments": {
    "development": ["config/.env.dev"]
  },
  "services": {
    "api": {
      "paths": ["services/api"],
      "environments": {
        "test": ["config/.env.test"],
        "production": ["config/.env.shared", "config/.env.prod"]
      }
    },
    "worker": {
      "paths": ["services/worker"],
      "ignore": {"names": ["WORKER_DEBUG"]},
      "environments": {
        "production": ["config/.env.shared"]
      }
    }
  }
}
```

- `services.<name>.paths` lists the directories, or individual files, that hold
  that service's code. They are relative to the scan directory and must stay
  inside it. A file covered by two paths is read and counted once.
- `services.<name>.environments` maps an environment name to the dotenv files
  that define it. Several files in one list are combined, which is how a shared
  file plus an overlay works. Names are never combined *across* environments.
- The top-level `environments` block is the default for every service. A service
  that lists the same environment name replaces that list outright rather than
  adding to it, so an override is always visible in one place. In the example,
  `worker` inherits `development` and defines its own `production`.
- `services.<name>.ignore` takes the same `paths` and `names` as the global
  `ignore` block and adds to it for that service only.
- All paths in this block are relative to the scan directory, unlike `--env`,
  which is relative to your current working directory.

Use `--service NAME` and `--environment NAME` to check part of the project; both
are repeatable and an unknown name is an error. `--env` cannot be combined with
configured services, since that would merge environments back together; use
`--no-config` for a single combined check instead.

Plain output groups results under each service and environment:

```
Service: api  (1 source file(s))

  production (config/.env.shared, config/.env.prod)

  [ok]      API_URL       defined (config/.env.prod)
  [missing] API_TOKEN     services/api/main.py:3
  [unused]  SHARED_FLAG   in config/.env.shared
```

Only the files listed for a service and environment are read. Nothing is
discovered from the scan directory, so an unrelated `.env.bak` sitting next to
your code cannot affect, or break, a service run.

In JSON mode the report has a top-level `summary` with `services` and `checks`
counts, then `services.<name>.environments.<name>` holding the same `summary`,
`ok`, `missing`, `optional`, `unused`, `invalid`, and `computed` fields used by
a single check, plus the `files` that defined that environment. With `--strict`, a missing name in any service or
environment exits 1.

## Supported references

The scanner recognizes common literal forms, including:

| Source | Examples |
| --- | --- |
| Python | `os.environ["NAME"]`, `os.environ.get("NAME")`, `os.getenv("NAME")`, plus import aliases such as `from os import environ as env` |
| JavaScript / TypeScript | `process.env.NAME`, `process.env["NAME"]`, `import.meta.env.NAME`, `Deno.env.get("NAME")`, `Bun.env.NAME` |
| Ruby | `ENV["NAME"]`, `ENV.fetch("NAME")`, `ENV.dig("NAME")` |
| Go | `os.Getenv("NAME")`, `os.LookupEnv("NAME")` |
| Rust | `env::var("NAME")`, `std::env::var("NAME")`, `env::var_os("NAME")` |
| PHP | `getenv("NAME")`, `$_ENV["NAME"]`, `$_SERVER["NAME"]`, `env("NAME")` |
| Java / Kotlin | `System.getenv("NAME")` |
| C# / VB | `Environment.GetEnvironmentVariable("NAME")` |
| Shell | `$NAME`, `${NAME}`, `${NAME:-fallback}` |

Names are case-sensitive, including single-letter names. Every detected name is
compared with the selected dotenv files. The current process environment is not
used, and system/CI names are not silently exempted.

## Dotenv parsing

Parsing accepts `NAME=value` and `export NAME=value`, blank lines, comments, and
a UTF-8 byte-order mark. Values are read only far enough to find where they end
and whether they are empty. They are never stored, printed, or included in JSON.

- **Quoting.** A double-quoted value honours `\` escapes; a single-quoted value
  is literal. Either may span lines, and names inside such a value are not
  treated as assignments. An unclosed quote is an error with its line number.
- **Comments.** Outside quotes, a `#` that follows whitespace or starts the line
  begins a comment. `NAME=a#b` keeps `a#b` as the value.
- **Empty values.** `NAME=`, a value of only whitespace, `NAME=""`, and `NAME=''`
  all count as empty. A quoted space, `NAME=" "`, is a real value, and so is any
  escaped character, including a value that is only a backslash.
- **Duplicates.** The last assignment of a name wins, matching how dotenv loaders
  behave, and the repeat is reported with both line numbers.

### Optional names

An undefined name whose every reference carries a fallback is reported as
`[optional]` rather than `[missing]`, with the locations that reference it. It
does not fail `--strict`, because the code will run without it. Define it in a
dotenv file and it becomes `ok` like any other name. In JSON these appear under
`optional`, in the same shape as `missing`.

### Computed keys

A Python lookup whose key is not a literal name cannot be compared with anything,
so envcheck counts it and reports where it is:

```
  2 lookup(s) use a computed key and were not checked (main.py:2, main.py:3).
```

This covers `os.getenv(name)`, `os.getenv(key=name)`, `os.environ[key]`,
f-strings, concatenation, and a literal that is not a valid variable name. Each
lookup is counted on its own, so two on one line count twice. It means a project
whose lookups are all computed is never reported as clean with nothing to say. The count is
informational and does not affect the exit status. In JSON it appears as
`computed`, a list of `file` and `line` entries, with a matching summary count.

### Empty values

An empty value is **not** the same as a missing one: the setting exists, it just
has nothing filled in. By default such a name is treated as defined and reported
as `ok`. Pass `--check-empty`, or set `{"checks": {"empty": true}}` in
`envcheck.json`, to list it separately as `[empty]` with the file and line that
defines it. It is never reported as missing.

### Invalid lines

A line that cannot be parsed is reported as `[invalid]` with its file, line
number, and the reason, and never its contents. The reasons are a name that is
not a valid variable name, a line with no `=`, text after a closing quote, and a
duplicate assignment. In JSON these appear under `invalid`, each with `file`,
`line`, and `reason`.

With `--strict`, missing names, empty values when that check is on, and invalid
lines all exit 1. Optional names and computed keys never do. Plain and JSON output report exactly the same findings.

Source and dotenv files must be UTF-8.

## Limits

Python files are parsed with the standard `ast` module. Comments, docstrings,
and ordinary strings never count as references, lookups split across lines are
found and reported on the line where they start, and `import os as x`,
`from os import environ as e`, and `from os import getenv` are followed. A name
that is rebound to anything else in the same scope, such as a parameter called
`os` or a module-level `environ = {}`, is not treated as a lookup anywhere in
that scope, even before the rebinding. A function or class body that declares a
name `global` and rebinds it changes the module-level name too: `global os`
followed by `os = other()` hides it everywhere, while `global os` followed by
`import os` keeps it as the os module. Annotations and the first
iterable of a comprehension are checked in the enclosing scope, following
Python's own rules. Bare `environ[...]`, `environ.get(...)`,
and `getenv(...)` are recognized without a visible import. Computed keys such as
`os.getenv(name)` or `os.getenv(f"{prefix}_URL")` cannot be checked, but they
are counted and located rather than skipped silently. A
Python file that does not parse under the running interpreter falls back to the
line-based patterns described next.

A lookup with a fallback, such as `os.getenv("TIMEOUT", "10")` or
`os.environ.get("TIMEOUT", default="10")`, is recorded as optional. A name is
optional only when **every** reference to it has a fallback; a single
`os.environ["TIMEOUT"]` or `os.getenv("TIMEOUT")` anywhere in the same check
makes it required.

Any fallback value counts, including `""`, `[]`, `0`, and `False`, with one
exception: an explicit `None` is treated as no fallback, since it behaves the
same as omitting it. An argument that expands to nothing is not a fallback
either: `os.getenv("X", *[])` and `os.getenv("X", **{})` pass no default, and a
starred argument whose contents cannot be read, such as `*args`, is not counted
because it may be empty. `*["10"]` and `**{"default": "10"}` are read as
fallbacks. The key may be given as `key="X"` as well as positionally. Only the
Python scanner can see fallbacks; every reference found by pattern matching,
including one in a Python file that did not parse, is treated as required.

Other languages use line-based pattern matching, not a language parser. That
can miss computed names, aliases, and calls split across lines, and strings,
inline comments, and block comments can produce false positives. Shell
references may be local variables rather than exported environment variables.

The scanner skips common dependency, build, cache, and virtual environment
directories, including `.git`, `node_modules`, `.venv`, `vendor`, `dist`, and `build`.
It skips source symlinks and does not follow directory symlinks. Project
exclusions come from `envcheck.json`; `.gitignore` is not read. Automatic dotenv
discovery is limited to the scan root; name the files per environment under
`services` to check nested directories.

envcheck does not execute source code, load settings into the process environment,
or send files over the network. It is a configuration check, not a secret scanner
or a replacement for testing the application.

## CI

Install from this repository, then select the application and configuration file
you want to check. For example, in a checkout of this repository:

```sh
python -m pip install .
envcheck --path examples/basic --env examples/basic/.env.example --strict --format json
```

For your own workflow, replace those example paths with your application paths.
Keep real dotenv files out of Git. Commit only examples containing made-up values.

JSON output has `summary`, `ok`, `missing`, `optional`, `unused`, `invalid`, and
`computed` fields, plus `empty` when that check is on; with configured services it has `summary` and
`services` instead. Missing entries include their
source file and line. To save a report while keeping the exit code,
redirect it directly:

```sh
envcheck --strict --format json > envcheck-report.json
```

## Development

```sh
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

The GitHub Actions workflow runs tests and installation checks on Windows, Linux,
and macOS with Python 3.10, 3.12, and 3.14. See [ROADMAP.md](ROADMAP.md) for the next
steps, including more accurate parsing and separate checks for each environment.

## License

[MIT](LICENSE).
