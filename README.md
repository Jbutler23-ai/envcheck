# envcheck ✦

**Audit environment variable usage across your entire codebase. Zero dependencies. Pure Python.**

Finds vars your code needs but forgot to define — before they crash in production. Also flags stale vars sitting in your `.env` that nothing uses anymore.

---

## The problem

You add `os.environ["STRIPE_WEBHOOK_SECRET"]` in your code, deploy, and get a `KeyError` at runtime because nobody added it to `.env`. Or you have 30 vars in `.env` and half of them are from a feature you deleted six months ago. **envcheck** catches both.

---

## Features

- Scans source files for every env var reference
- Cross-checks against all `.env*` files in your project
- Reports: defined · missing · unused
- Supports **10 languages** out of the box
- JSON output for CI pipelines (`--format json`)
- `--strict` flag for fail-fast CI enforcement
- `--show-refs` to see exactly where each var is used
- Zero external dependencies — pure Python stdlib

---

## Supported Languages

| Language | Patterns detected |
|---|---|
| Python | `os.environ["X"]`, `os.getenv("X")`, `environ.get("X")` |
| JavaScript / TypeScript | `process.env.X`, `import.meta.env.X`, `Deno.env.get("X")` |
| Ruby | `ENV["X"]`, `ENV.fetch("X")` |
| Go | `os.Getenv("X")`, `os.LookupEnv("X")` |
| Rust | `env::var("X")`, `std::env::var("X")` |
| PHP | `getenv("X")`, `$_ENV["X"]`, `$_SERVER["X"]` |
| Java / Kotlin | `System.getenv("X")` |
| C# / VB | `Environment.GetEnvironmentVariable("X")` |
| Shell | `$VAR`, `${VAR}` |

---

## Install

```bash
git clone https://github.com/Jbutler23-ai/envcheck.git
cd envcheck

# Run directly
python envcheck.py

# Or install globally
pip install --editable .
envcheck
```

---

## Usage

```
envcheck [options]

  --path PATH       Directory to scan  (default: .)
  --env FILE        Specific .env file to check against (repeatable)
  --format FORMAT   plain | json  (default: plain)
  --strict          Exit 1 if any vars are missing  (great for CI)
  --show-refs       Show file:line for each var reference
  --no-unused       Hide unused-var warnings
  --no-color        Disable colored output
```

---

## Examples

```bash
# Scan current project
envcheck

# Scan a subdirectory, check against a specific env file
envcheck --path ./backend --env .env.production

# Use in CI — fail the build if anything is missing
envcheck --strict

# See exactly where every var is referenced
envcheck --show-refs

# JSON output for scripts
envcheck --format json | jq '.missing'

# Check multiple env files at once
envcheck --env .env --env .env.example
```

---

## Sample Output

```
┌──────────────────────────────────┐
│       envcheck  ✦  v1.0          │
└──────────────────────────────────┘

Scanning: /home/user/myapp

  📄  .env                            12 var(s)
  📄  .env.example                     9 var(s)

Scanning source files…
Scanned 47 source file(s) · 15 unique var reference(s)

──────────────────────────────────────────────────────

  ✓  DATABASE_URL                defined  (.env, .env.example)
  ✓  SECRET_KEY                  defined  (.env, .env.example)
  ✓  REDIS_URL                   defined  (.env)
  ✗  STRIPE_WEBHOOK_SECRET       MISSING   ← payments.py:34, webhooks.py:12
  ✗  SENDGRID_API_KEY            MISSING   ← email.py:8
  ⚠  OLD_SENTRY_DSN              unused    (in .env)
  ⚠  LEGACY_DB_HOST              unused    (in .env, .env.example)

──────────────────────────────────────────────────────

  5 ok  ·  2 missing  ·  2 unused

  Add missing vars to your .env (and .env.example)
```

---

## CI Integration

Add to your GitHub Actions workflow:

```yaml
- name: Check environment variables
  run: python envcheck.py --strict
```

Or with the installed command:

```yaml
- run: envcheck --strict --format json | tee envcheck-report.json
```

---

## License

MIT
