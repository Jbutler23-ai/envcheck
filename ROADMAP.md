# Next steps

The basic comparison works, but pattern matching has limits. The next work is
about making the results more useful on real applications. These are estimates
for implementation, tests, and documentation, not hours already spent.

## Python-aware scanning (8-12 hours)

- Use Python's AST to distinguish environment lookups from strings and comments.
- Resolve common imports and aliases for os, environ, and getenv.
- Recognize literal keys across multiple lines and mark calls with fallback values.
- Add fixtures for aliases, shadowed names, syntax errors, and optional settings.

Done when supported literal lookups retain accurate file/line locations, comments
and ordinary strings stop producing Python matches, and unsupported cases have a
documented behavior.

## Project configuration and exclusions (4-6 hours)

- Add a project configuration file for ignored paths and variable names.
- Define how command-line options override project settings.
- Support user-selected exclusions for generated code, tests, and system variables.
- Test invalid configuration, glob matching, and paths on Windows and Unix.

Done when the same checked-in configuration produces repeatable results locally
and in CI without hiding unlisted names.

## Separate service and environment checks (7-10 hours)

- Let a project define source roots and dotenv files for each service.
- Report development, test, and production coverage separately instead of merging
  their keys into one set.
- Make shared defaults and overrides explicit.
- Add fixtures for two services that use the same variable names differently.

Done when a value defined only for development cannot make a production check
pass, and each report identifies the service and environment it covers.

## Dotenv parsing and empty-value checks (5-7 hours)

- Extend the basic quoted-value handling to cover escape rules and comment
  boundaries, with fixtures for each supported case.
- Distinguish a missing key from a present but empty value.
- Define duplicate-key and malformed-line behavior, with useful line numbers.
- Add an optional empty-value check without printing values in errors or reports.

Done when these cases have fixtures, documented semantics, and identical results
from text and JSON output.

The remaining estimate is **24-35 hours**. The order may change as these features
are tried on small sample applications. Packaging repairs, the initial regression
suite, and the existing CI workflow are already done and are not part of this
estimate.
