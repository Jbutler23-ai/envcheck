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

Exit status is 0 after a completed scan, 1 for missing variables with `--strict`,
and 2 for invalid arguments or a file/directory read error. Errors go to stderr.
JSON mode writes only JSON to stdout after a successful scan, including when
`--strict` finds missing names.

## Supported references

The scanner recognizes common literal forms, including:

| Source | Examples |
| --- | --- |
| Python | `os.environ["NAME"]`, `os.environ.get("NAME")`, `os.getenv("NAME")` |
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

Dotenv parsing accepts `NAME=value` and `export NAME=value`, blank lines,
comments, and a UTF-8 byte-order mark. It checks for a key's presence, not whether
its value is valid or nonempty. Quoted multiline values are skipped so their
contents do not become variable names; an unclosed quote is an error. Other
malformed lines are ignored. Source and dotenv files must be UTF-8.

## Limits

This is a line-based pattern scanner, not a language parser. It can miss computed
names, aliases, and calls split across lines. Strings, inline comments, and block
comments can produce false positives. A fallback such as
`os.getenv("TIMEOUT", "10")` still counts as a reference. Shell references may be
local variables rather than exported environment variables.

The scanner skips common dependency, build, cache, and virtual environment
directories, including `.git`, `node_modules`, `.venv`, `vendor`, `dist`, and `build`.
It skips source symlinks and does not follow directory symlinks. It does not yet
apply `.gitignore` or custom exclusion rules. Dotenv discovery is limited to the
scan root; nested service directories need their own scan.

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

JSON output has `summary`, `ok`, `missing`, and `unused` fields. Missing entries
include their source file and line. To save a report while keeping the exit code,
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
