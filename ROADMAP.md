# Next steps

The basic comparison works, but pattern matching has limits. The next work is
about making the results more useful on real applications. These are estimates
for implementation, tests, and documentation, not hours already spent.

## Python-aware scanning (done)

Done: Python files are parsed with the `ast` module, so comments and strings no
longer match; aliases of `os`, `environ`, and `getenv` are resolved per scope;
rebound names stop matching; multiline lookups report their starting line; and
files that fail to parse fall back to the line-based patterns. Fixtures cover
aliases, shadowing, syntax errors, and fallback values.

Also done: a name whose every lookup carries a fallback is reported as
optional rather than missing, and one lookup without a fallback makes it
required again.

Also done: an explicit `None` default or an argument that expands to nothing,
such as `*[]`, leaves the name required, and lookups with a computed key are
counted per call and located instead of skipped silently.

## Project configuration and exclusions (done)

`envcheck.json` in the scan directory holds `ignore.paths` (glob patterns) and
`ignore.names`. `--config` and `--no-config` select or disable the file, and
`--ignore-path` / `--ignore-name` add to it. Invalid configuration is an error.
Fixtures cover glob semantics, Windows-style separators, invalid files, and the
interaction between the file and the command line. Not covered: `.gitignore`.

## Separate service and environment checks (done)

`envcheck.json` defines `services` with their own `paths` and per-environment
dotenv files, plus a top-level `environments` default that a service replaces by
name. Each service and environment pair is reported separately in both output
formats, and `--service` / `--environment` narrow a run. Fixtures cover two
services that use the same names differently, inheritance and override, filters,
invalid configuration, and missing paths or files.

## Dotenv parsing and empty-value checks (done)

Quoting, escape rules, comment boundaries, and multiline values have fixtures
for each supported case. An empty value is distinguished from a missing key and
reported separately behind `--check-empty` or `checks.empty`. Duplicate keys and
malformed lines are reported with file and line numbers and never their values.
Plain and JSON output carry the same findings.

All four planned items are complete. The test suite passes on Python 3.10,
3.12, and 3.14. Future work will come from trying envcheck on more sample
applications; `.gitignore` support is the most likely next request.
