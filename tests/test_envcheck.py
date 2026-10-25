import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import envcheck


SCRIPT = Path(envcheck.__file__).resolve()


class EnvcheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, filename, text):
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def run_cli(self, *args, encoding='utf-8', env=None):
        child_env = os.environ.copy()
        child_env['PYTHONIOENCODING'] = encoding
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=self.root,
            env=child_env, capture_output=True, text=True, encoding=encoding,
            timeout=20,
        )

    def test_supported_language_patterns(self):
        samples = {
            '.py': [
                'os.environ[ "key" ]', 'os.environ . get ( "key", "fallback")',
                'os.getenv ( "key" )', 'environ.get("key")',
                'getenv("key")', 'environ["key"]',
            ],
            '.js': [
                'process.env.key', 'process . env [ "key" ]',
                'import.meta.env.key', 'import.meta.env["key"]',
                'Deno.env.get ( "key" )', 'Bun.env.key', 'Bun.env["key"]',
            ],
            '.rb': ['ENV[ "key" ]', 'ENV.fetch ( "key" )', 'ENV.dig("key")'],
            '.go': ['os.Getenv ( "key" )', 'os.LookupEnv("key")'],
            '.php': ['getenv ( "key" )', '$_ENV[ "key" ]', '$_SERVER["key"]', 'env("key")'],
            '.rs': ['env::var ( "key" )', 'std::env::var("key")', 'env::var_os("key")'],
            '.sh': ['$key', '${key}', '${key:-fallback}', '${key?missing}'],
            '.cs': ['Environment.GetEnvironmentVariable ( "key" )', 'GetEnvironmentVariable("key")'],
            '.java': ['System.getenv ( "key" )'],
        }
        for suffix, expressions in samples.items():
            for expression in expressions:
                with self.subTest(suffix=suffix, expression=expression):
                    path = self.write('sample' + suffix, expression)
                    self.assertEqual(envcheck.scan_file(path, self.root), {'key': [('sample' + suffix, 1)]})

    def test_all_supported_extensions_are_scanned(self):
        samples = {
            'python': 'os.getenv("A")', 'javascript': 'process.env.A',
            'ruby': 'ENV["A"]', 'go': 'os.Getenv("A")', 'php': 'getenv("A")',
            'rust': 'env::var("A")', 'shell': '$A',
            'dotnet': 'Environment.GetEnvironmentVariable("A")', 'java': 'System.getenv("A")',
        }
        for suffix, language in envcheck.EXT_LANG.items():
            self.write('sample' + suffix, samples[language])
        refs, count = envcheck.scan_directory(self.root)
        self.assertEqual(count, len(envcheck.EXT_LANG))
        self.assertEqual(len(refs['A']), len(envcheck.EXT_LANG))

    def test_case_single_letter_and_system_names_are_preserved(self):
        self.write('main.js', 'process.env.A; process.env.api_key; process.env.API_KEY;\n'
                   'process.env.DEBUG; process.env.LOG_LEVEL; process.env.GITHUB_TOKEN; process.env.PATH;')
        self.write('.env', 'api_key=local\n')
        result = self.run_cli('--format', 'json')
        report = json.loads(result.stdout)
        self.assertEqual(report['ok'], ['api_key'])
        self.assertEqual(set(report['missing']), {'A', 'API_KEY', 'DEBUG', 'LOG_LEVEL', 'GITHUB_TOKEN', 'PATH'})

    def test_references_are_unique_per_line_and_sorted(self):
        self.write('z.py', 'os.getenv("A"); getenv("A")\nos.getenv("A")\n')
        self.write('nested/b.py', 'os.environ.get("A")\n')
        self.write('a.rs', 'std::env::var("A"); env::var("A")\n')
        refs, count = envcheck.scan_directory(self.root)
        self.assertEqual(count, 3)
        self.assertEqual(refs, {'A': [('a.rs', 1), ('nested/b.py', 1), ('z.py', 1), ('z.py', 2)]})

    def test_full_line_comments_and_excluded_directories(self):
        self.write('main.py', '# os.getenv("COMMENT")\n  # os.getenv("COMMENT")\nos.getenv("USED")\n')
        self.write('main.js', '// process.env.COMMENT\n')
        self.write('node_modules/dependency.js', 'process.env.DEPENDENCY')
        self.write('.git/hook.sh', '$HOOK')
        self.write('notes.txt', 'os.getenv("NOT_SOURCE")')
        refs, count = envcheck.scan_directory(self.root)
        self.assertEqual(refs, {'USED': [('main.py', 3)]})
        self.assertEqual(count, 2)

    def test_dotenv_bom_export_blank_values_and_case(self):
        path = self.write('.env', '\ufeffFIRST=value\n export mixedCase = "text"\nEMPTY=\n'
                          '# COMMENT=x\nINVALID-KEY=x\nNO_ASSIGNMENT\nexported=value\n')
        parsed = envcheck.parse_env_file(path)
        self.assertEqual(parsed.names, {'FIRST', 'mixedCase', 'EMPTY', 'exported'})
        self.assertEqual(parsed.problems, [
            (5, 'name before "=" is not a valid variable name'), (6, 'no "=" assignment')])
        self.assertTrue(parsed.entries['EMPTY'].empty)
        self.assertFalse(parsed.entries['FIRST'].empty)

    def test_dotenv_discovery_matches_only_root_dotenv_names(self):
        for filename in ('.env', '.env.local', '.env.example', '.environment', '.envrc', 'child/.env'):
            self.write(filename, 'KEY=value')
        (self.root / '.env.directory').mkdir()
        self.assertEqual([path.name for path in envcheck.find_env_files(self.root)], ['.env', '.env.example', '.env.local'])

    def test_multiline_values_are_not_mistaken_for_assignment_names(self):
        self.write('.env', 'CERT="first line\nVALUE_CONTENT=private\nlast line"\n'
                   "OTHER='first\nMORE_CONTENT=private\nlast'\nREAL=value\n")
        result = self.run_cli('--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['unused'], ['CERT', 'OTHER', 'REAL'])
        self.assertNotIn('VALUE_CONTENT', result.stdout)
        self.assertNotIn('MORE_CONTENT', result.stdout)
        self.assertNotIn('private', result.stdout)

    def test_unclosed_dotenv_quotes_report_location_without_value(self):
        self.write('.env', 'TOKEN="private-content\n')
        result = self.run_cli('--format', 'json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('unclosed quoted value on line 1', result.stderr)
        self.assertNotIn('private-content', result.stderr)

    def test_json_is_parseable_and_values_never_appear(self):
        self.write('main.py', 'os.getenv("TOKEN")\nos.getenv("MISSING")\n')
        self.write('.env', 'TOKEN=sample-private-value-789\nUNUSED=another-private-value-456\n')
        for output_format in ('plain', 'json'):
            with self.subTest(format=output_format):
                result = self.run_cli('--format', output_format, '--no-color')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertNotIn('sample-private-value-789', result.stdout)
                self.assertNotIn('another-private-value-456', result.stdout)
                self.assertNotIn('\033', result.stdout)
                if output_format == 'json':
                    self.assertEqual(json.loads(result.stdout), {
                        'summary': {'ok': 1, 'missing': 1, 'optional': 0, 'unused': 1, 'invalid': 0, 'computed': 0},
                        'ok': ['TOKEN'], 'missing': {'MISSING': [{'file': 'main.py', 'line': 2}]},
                        'optional': {}, 'unused': ['UNUSED'], 'invalid': [], 'computed': [],
                    })

    def test_repeated_env_files_with_same_basename_keep_both_definitions(self):
        self.write('main.py', 'os.getenv("FIRST")\nos.getenv("SECOND")\n')
        self.write('first/.env', 'FIRST=value\n')
        self.write('second/.env', 'SECOND=value\n')
        result = self.run_cli('--env', 'first/.env', '--env', 'second/.env', '--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['ok'], ['FIRST', 'SECOND'])
        plain = self.run_cli('--env', 'first/.env', '--env', 'second/.env')
        self.assertIn('first/.env', plain.stdout)
        self.assertIn('second/.env', plain.stdout)

    def test_explicit_env_paths_are_relative_to_working_directory(self):
        self.write('backend/main.py', 'os.getenv("SELECTED")')
        self.write('backend/.env', 'AUTOMATIC=value\n')
        self.write('config/.env', 'SELECTED=value\n')
        result = self.run_cli('--path', 'backend', '--env', 'config/.env', '--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['ok'], ['SELECTED'])
        self.assertEqual(json.loads(result.stdout)['unused'], [])

    def test_exit_status_with_and_without_strict(self):
        self.write('main.py', 'os.getenv("REQUIRED")')
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(self.run_cli('--strict').returncode, 1)
        self.assertEqual(self.run_cli('--strict', '--format', 'json').returncode, 1)
        self.write('.env', 'REQUIRED=value\n')
        self.assertEqual(self.run_cli('--strict').returncode, 0)

    def test_process_environment_does_not_satisfy_dotenv_references(self):
        self.write('main.py', 'os.getenv("OUTSIDE")')
        result = self.run_cli('--strict', '--format', 'json', env={'OUTSIDE': 'value'})
        self.assertEqual(result.returncode, 1)
        self.assertIn('OUTSIDE', json.loads(result.stdout)['missing'])

    def test_empty_project_has_a_valid_report(self):
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['summary'],
                         {'ok': 0, 'missing': 0, 'optional': 0, 'unused': 0, 'invalid': 0, 'computed': 0})

    def test_no_unused_hides_details_but_preserves_summary(self):
        self.write('.env', 'UNUSED_NAME=value\n')
        result = self.run_cli('--no-unused', '--format', 'json')
        report = json.loads(result.stdout)
        self.assertEqual(report['unused'], [])
        self.assertEqual(report['summary']['unused'], 1)
        plain = self.run_cli('--no-unused')
        self.assertNotIn('UNUSED_NAME', plain.stdout)
        self.assertIn('1 unused', plain.stdout)

    def test_show_refs_lists_every_location(self):
        self.write('main.py', 'os.getenv("A")\n' * 6)
        self.write('.env', 'A=value\n')
        result = self.run_cli('--show-refs')
        for line in range(1, 7):
            self.assertIn(f'main.py:{line}', result.stdout)

    def test_windows_pipe_encoding_handles_unicode_paths(self):
        self.write('check_\u03bb.py', 'os.getenv("A")')
        for output_format in ('plain', 'json'):
            with self.subTest(format=output_format):
                result = self.run_cli('--format', output_format, encoding='cp1252')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                if output_format == 'json':
                    self.assertEqual(json.loads(result.stdout)['missing']['A'][0]['file'], 'check_\u03bb.py')

    def test_bad_paths_return_two_without_partial_stdout(self):
        self.write('file.txt', 'text')
        for args in (('--path', 'missing'), ('--path', 'file.txt'), ('--env', 'missing.env'), ('--env', '.')):
            with self.subTest(args=args):
                result = self.run_cli(*args, '--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('envcheck:', result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_invalid_utf8_source_or_env_returns_two(self):
        for filename in ('invalid.py', '.env'):
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_bytes(b'\xff\xfe')
                result = self.run_cli('--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('expected UTF-8', result.stderr)
                path.unlink()

    def test_unreadable_file_is_an_error(self):
        path = self.write('main.py', 'os.getenv("A")')
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError(13, 'Permission denied', str(path))):
            with self.assertRaisesRegex(envcheck.ScanError, 'Permission denied'):
                envcheck.scan_file(path, self.root)

    def test_walk_permission_error_is_not_a_successful_partial_scan(self):
        def denied_walk(root, onerror, followlinks):
            onerror(PermissionError(13, 'Permission denied', str(root / 'restricted')))
            return iter(())

        stderr = io.StringIO()
        with mock.patch.object(envcheck.os, 'walk', side_effect=denied_walk):
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()) as stdout:
                status = envcheck.main(['--path', str(self.root), '--format', 'json'])
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('Permission denied', stderr.getvalue())

    def test_symlink_entries_are_filtered_before_reading(self):
        self.write('main.py', 'os.getenv("LOCAL")')
        self.write('linked.py', 'os.getenv("OUTSIDE_FILE")')
        self.write('linked_dir/other.py', 'os.getenv("OUTSIDE_DIRECTORY")')
        original_is_symlink = Path.is_symlink

        def marked_link(path):
            return path.name in {'linked.py', 'linked_dir'} or original_is_symlink(path)

        with mock.patch.object(Path, 'is_symlink', marked_link):
            refs, count = envcheck.scan_directory(self.root)
        self.assertEqual(refs, {'LOCAL': [('main.py', 1)]})
        self.assertEqual(count, 1)

    def test_source_symlinks_are_skipped(self):
        target_dir = self.root / 'external'
        target_dir.mkdir()
        target_file = self.write('external/secret.py', 'os.getenv("OUTSIDE")')
        scan_root = self.root / 'project'
        scan_root.mkdir()
        try:
            (scan_root / 'linked.py').symlink_to(target_file)
            (scan_root / 'linked_dir').symlink_to(target_dir, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Creating symlinks is unavailable: {exc}')
        self.assertEqual(envcheck.scan_directory(scan_root), ({}, 0))


    def test_python_multiline_lookups_report_the_starting_line(self):
        path = self.write('settings.py', 'import os\n\nflag = os.getenv(\n    "FEATURE_FLAG",\n    "off",\n)\n'
                          'cache = os.environ[\n    "CACHE_URL"\n]\ntimeout = os.environ.get(\n    "REQUEST_TIMEOUT")\n')
        self.assertEqual(envcheck.scan_file(path, self.root), {
            'CACHE_URL': [('settings.py', 7)],
            'FEATURE_FLAG': [('settings.py', 3)],
            'REQUEST_TIMEOUT': [('settings.py', 10)],
        })

    def test_python_import_aliases_are_resolved(self):
        path = self.write('aliases.py', 'import os as system\nfrom os import environ as env_map, getenv as read\n'
                          'import os.path\n\nsystem.getenv("ALIAS_MODULE")\nenv_map["ALIAS_ENVIRON"]\n'
                          'read("ALIAS_GETENV")\nenv_map.get("ALIAS_GET", "x")\nos.environ["DOTTED_IMPORT"]\n')
        self.assertEqual(set(envcheck.scan_file(path, self.root)),
                         {'ALIAS_MODULE', 'ALIAS_ENVIRON', 'ALIAS_GETENV', 'ALIAS_GET', 'DOTTED_IMPORT'})

    def test_python_strings_docstrings_and_trailing_comments_do_not_match(self):
        path = self.write('docs.py', '"""Set os.getenv("IN_DOCSTRING") before start."""\nimport os\n\n'
                          'help_text = "use os.environ[\'IN_STRING\']"\nvalue = 1  # os.getenv("IN_COMMENT")\n'
                          'template = f"{os.getenv(\'IN_FSTRING\')}"\nreal = os.getenv("REAL_NAME")\n')
        self.assertEqual(envcheck.scan_file(path, self.root),
                         {'IN_FSTRING': [('docs.py', 6)], 'REAL_NAME': [('docs.py', 7)]})

    def test_python_lookups_without_imports_still_match(self):
        path = self.write('bare.py', 'os.getenv("BARE_OS")\ngetenv("BARE_GETENV")\nenviron["BARE_ENVIRON"]\n'
                          'environ.get("BARE_GET")\n')
        self.assertEqual(set(envcheck.scan_file(path, self.root)),
                         {'BARE_OS', 'BARE_GETENV', 'BARE_ENVIRON', 'BARE_GET'})

    def test_python_rebound_names_do_not_match(self):
        samples = {
            'parameter': 'import os\ndef handler(os):\n    return os.getenv("SHADOWED")\n',
            'lambda_parameter': 'import os\nread = lambda getenv: getenv("SHADOWED")\n',
            'assignment': 'environ = {}\nenviron["SHADOWED"]\n',
            'later_assignment': 'import os\nos.getenv("SHADOWED")\nos = None\n',
            'other_import': 'from mypkg import environ, getenv\nenviron["SHADOWED"]\ngetenv("SHADOWED")\n',
            'other_module_alias': 'import mypkg.os as os\nos.getenv("SHADOWED")\n',
            'from_other_as_os': 'from mypkg import client as os\nos.environ["SHADOWED"]\n',
            'for_target': 'import os\nfor os in []:\n    os.getenv("SHADOWED")\n',
            'with_target': 'import os\nwith open("f") as environ:\n    environ["SHADOWED"]\n',
            'except_target': 'import os\ntry:\n    pass\nexcept Exception as os:\n    os.getenv("SHADOWED")\n',
            'function_name': 'import os\ndef getenv(k): return k\ngetenv("SHADOWED")\n',
            'class_body': 'import os\nclass Settings:\n    os = object()\n    value = os.getenv("SHADOWED")\n',
            'enclosing_function': 'import os\ndef outer():\n    os = None\n    def inner():\n        return os.getenv("SHADOWED")\n',
            'comprehension_target': 'import os\nnames = [os.getenv("SHADOWED") for os in []]\n',
            'walrus': 'import os\n[(getenv := 1) for x in []]\ngetenv("SHADOWED")\n',
            'match_capture': 'import os\nmatch 1:\n    case os:\n        os.getenv("SHADOWED")\n',
            'global_rebind_in_function': 'import os\nos.getenv("SHADOWED")\ndef setup():\n    global os\n'
                                         '    os = fake_module()\n    os.getenv("SHADOWED")\nos.getenv("SHADOWED")\n',
            'global_rebind_in_nested_function': 'def outer():\n    def inner():\n        global getenv\n'
                                                '        getenv = 1\ngetenv("SHADOWED")\n',
            'annotation_shadows_body': 'import os\ndef f(os: int):\n    return os.getenv("SHADOWED")\n',
            'global_rebind_in_class_body': 'import os\nos.getenv("SHADOWED")\nclass Config:\n    global os\n'
                                           '    os = fake_module()\n    value = os.getenv("SHADOWED")\n'
                                           'def load():\n    return os.getenv("SHADOWED")\n',
            'global_rebind_in_method': 'import os\nclass Config:\n    def load(self):\n        global os\n'
                                       '        os = fake_module()\nos.getenv("SHADOWED")\n',
            'global_reimport_then_rebind': 'import os\ndef load():\n    global os\n    import os\n    os = None\n'
                                           'os.getenv("SHADOWED")\n',
            'global_import_of_other_module_as_os': 'import os\ndef load():\n    global os\n    import pathlib as os\n'
                                                   'os.getenv("SHADOWED")\n',
        }
        for label, text in samples.items():
            with self.subTest(case=label):
                path = self.write(f'{label}.py', text)
                self.assertNotIn('SHADOWED', envcheck.scan_file(path, self.root))

    def test_python_scope_rules_keep_valid_lookups(self):
        path = self.write('scopes.py', 'import os\n\nclass Settings:\n    os = object()\n\n    def load(self):\n'
                          '        return os.getenv("METHOD_SEES_MODULE")\n\ndef reader():\n    global os\n'
                          '    return os.getenv("GLOBAL_STATEMENT")\n\nnames = [os.getenv("LOOP_OTHER_NAME") for item in []]\n'
                          'from os import *\nvalue = environ["AFTER_STAR_IMPORT"]\n')
        self.assertEqual(set(envcheck.scan_file(path, self.root)),
                         {'METHOD_SEES_MODULE', 'GLOBAL_STATEMENT', 'LOOP_OTHER_NAME', 'AFTER_STAR_IMPORT'})

    def test_python_non_literal_and_invalid_keys_are_ignored(self):
        path = self.write('computed.py', 'import os\nos.getenv(name)\nos.getenv(prefix + "_URL")\n'
                          'os.getenv(f"{prefix}_URL")\nos.getenv("not-a-name")\nos.environ[key]\nos.getenv()\n'
                          'os.getenv("LITERAL_KEY")\n')
        self.assertEqual(envcheck.scan_file(path, self.root), {'LITERAL_KEY': [('computed.py', 8)]})

    def test_python_fallback_values_still_count(self):
        path = self.write('defaults.py', 'import os\nos.getenv("WITH_DEFAULT", "10")\nos.environ.get("ALSO_DEFAULT", None)\n')
        self.assertEqual(set(envcheck.scan_file(path, self.root)), {'WITH_DEFAULT', 'ALSO_DEFAULT'})

    def test_python_syntax_error_falls_back_to_patterns(self):
        path = self.write('broken.py', 'import os\ndef broken(:\n    pass\n# os.getenv("COMMENTED")\n'
                          'value = os.getenv("FALLBACK")  # os.getenv("TRAILING")\n')
        self.assertIsNone(envcheck.scan_python(path.read_text()))
        self.assertEqual(envcheck.scan_file(path, self.root),
                         {'FALLBACK': [('broken.py', 5)], 'TRAILING': [('broken.py', 5)]})
        refs, count = envcheck.scan_directory(self.root)
        self.assertEqual(count, 1)
        self.assertIn('FALLBACK', refs)

    def test_python_null_byte_file_falls_back_without_error(self):
        path = self.write('nul.py', 'os.getenv("BEFORE_NUL")\x00\n')
        self.assertEqual(envcheck.scan_file(path, self.root), {'BEFORE_NUL': [('nul.py', 1)]})

    def test_python_comprehension_first_iterable_uses_enclosing_scope(self):
        path = self.write('comps.py', 'import os\n\nparts = [os for os in os.getenv("FIRST_ITERABLE")]\n'
                          'pairs = {os: 1 for os in os.environ["DICT_ITERABLE"]}\n'
                          'flat = [item for os in [] for item in os.getenv("LATER_ITERABLE")]\n'
                          'conds = [item for os in [] if os.getenv("CONDITION")]\n'
                          'elems = [os.getenv("ELEMENT") for item in []]\n'
                          'gen = (item for item in os.getenv("GENERATOR"))\n')
        self.assertEqual(envcheck.scan_file(path, self.root), {
            'DICT_ITERABLE': [('comps.py', 4)],
            'ELEMENT': [('comps.py', 7)],
            'FIRST_ITERABLE': [('comps.py', 3)],
            'GENERATOR': [('comps.py', 8)],
        })

    def test_python_function_annotations_are_scanned_in_enclosing_scope(self):
        path = self.write('annotated.py', 'import os\n\ndef configure(\n    flag: os.getenv("ARG_ANNOTATION") = "x",\n'
                          '    *rest: os.getenv("VARARG_ANNOTATION"),\n    key: os.environ["KWONLY_ANNOTATION"],\n'
                          '    **extra: os.getenv("KWARG_ANNOTATION"),\n) -> os.getenv("RETURN_ANNOTATION"):\n    pass\n\n'
                          'async def fetch(os: os.getenv("ANNOTATION_SEES_OUTER")) -> None:\n    return os.getenv("SHADOWED")\n\n'
                          'setting: os.getenv("VARIABLE_ANNOTATION") = 1\n')
        self.assertEqual(envcheck.scan_file(path, self.root), {
            'ANNOTATION_SEES_OUTER': [('annotated.py', 11)],
            'ARG_ANNOTATION': [('annotated.py', 4)],
            'KWARG_ANNOTATION': [('annotated.py', 7)],
            'KWONLY_ANNOTATION': [('annotated.py', 6)],
            'RETURN_ANNOTATION': [('annotated.py', 8)],
            'VARARG_ANNOTATION': [('annotated.py', 5)],
            'VARIABLE_ANNOTATION': [('annotated.py', 14)],
        })

    def test_python_global_declaration_without_rebinding_keeps_lookups(self):
        path = self.write('globals.py', 'import os\n\ndef reader():\n    global os\n    return os.getenv("DECLARED_ONLY")\n\n'
                          'def writer():\n    global cache\n    cache = {}\n    return os.getenv("OTHER_GLOBAL_REBOUND")\n')
        self.assertEqual(set(envcheck.scan_file(path, self.root)), {'DECLARED_ONLY', 'OTHER_GLOBAL_REBOUND'})

    def test_python_global_reimport_keeps_lookups(self):
        path = self.write('reimport.py', 'def load():\n    global os\n    import os\n    return os.getenv("FUNCTION_REIMPORT")\n\n'
                          'class Config:\n    global environ\n    from os import environ\n    value = environ["CLASS_REIMPORT"]\n\n'
                          'top = os.getenv("MODULE_AFTER_REIMPORT")\nother = environ.get("MODULE_ALIAS_AFTER_REIMPORT")\n')
        self.assertEqual(envcheck.scan_file(path, self.root), {
            'CLASS_REIMPORT': [('reimport.py', 9)],
            'FUNCTION_REIMPORT': [('reimport.py', 4)],
            'MODULE_AFTER_REIMPORT': [('reimport.py', 11)],
            'MODULE_ALIAS_AFTER_REIMPORT': [('reimport.py', 12)],
        })

    def test_path_filter_glob_semantics(self):
        cases = [
            ('tests', 'tests/unit/a.py', True), ('tests', 'src/tests/a.py', True), ('tests', 'src/mytests/a.py', False),
            ('tests/**', 'tests/unit/a.py', True), ('tests/**', 'src/tests/a.py', False),
            ('*.test.js', 'src/app.test.js', True), ('*.test.js', 'src/app.js', False),
            ('generated/*.py', 'generated/a.py', True), ('generated/*.py', 'generated/sub/a.py', False),
            ('**/fixtures', 'a/b/fixtures/x.py', True), ('src/**/legacy', 'src/legacy/x.py', True),
            ('src/**/legacy', 'src/a/b/legacy/x.py', True), ('./scripts', 'scripts/run.sh', True),
            ('scripts/', 'scripts/run.sh', True), ('scripts\\build', 'scripts/build/x.py', True),
            ('a?c', 'abc/x.py', True), ('a?c', 'abbc/x.py', False), ('Tests', 'tests/a.py', False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertIs(envcheck.PathFilter([pattern]).matches(path), expected)

    def test_config_file_in_scan_root_skips_paths_and_names(self):
        self.write('envcheck.json', '{"ignore": {"paths": ["generated/**", "tests"], "names": ["PATH", "LEGACY_KEY"]}}')
        self.write('app/main.py', 'import os\nos.getenv("APP_URL")\nos.getenv("PATH")\n')
        self.write('generated/schema.py', 'import os\nos.getenv("GENERATED_NAME")\n')
        self.write('app/tests/test_x.py', 'import os\nos.getenv("TEST_ONLY")\n')
        self.write('.env', 'APP_URL=fake\nLEGACY_KEY=fake\nSTALE=fake\n')
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'summary': {'ok': 1, 'missing': 0, 'optional': 0, 'unused': 1, 'invalid': 0, 'computed': 0},
            'ok': ['APP_URL'], 'missing': {}, 'optional': {}, 'unused': ['STALE'], 'invalid': [],
            'computed': [],
        })
        plain = self.run_cli('--no-color')
        self.assertIn('Configuration: envcheck.json', plain.stdout)
        self.assertIn('Ignoring 2 path pattern(s) and 2 name(s)', plain.stdout)
        self.assertIn('Scanned 1 source file(s)', plain.stdout)

    def test_no_config_and_explicit_config_override_discovery(self):
        self.write('envcheck.json', '{"ignore": {"names": ["FROM_ROOT"]}}')
        self.write('ci/strict.json', '{"ignore": {"names": ["FROM_CI"]}}')
        self.write('main.py', 'import os\nos.getenv("FROM_ROOT")\nos.getenv("FROM_CI")\n')
        with self.subTest(mode='default'):
            report = json.loads(self.run_cli('--format', 'json').stdout)
            self.assertEqual(sorted(report['missing']), ['FROM_CI'])
        with self.subTest(mode='no-config'):
            report = json.loads(self.run_cli('--no-config', '--format', 'json').stdout)
            self.assertEqual(sorted(report['missing']), ['FROM_CI', 'FROM_ROOT'])
        with self.subTest(mode='explicit'):
            report = json.loads(self.run_cli('--config', 'ci/strict.json', '--format', 'json').stdout)
            self.assertEqual(sorted(report['missing']), ['FROM_ROOT'])
        with self.subTest(mode='conflict'):
            result = self.run_cli('--config', 'ci/strict.json', '--no-config')
            self.assertEqual(result.returncode, 2)
            self.assertIn('cannot be used together', result.stderr)

    def test_command_line_exclusions_add_to_configuration(self):
        self.write('envcheck.json', '{"ignore": {"paths": ["generated"], "names": ["CONFIGURED"]}}')
        self.write('generated/g.py', 'import os\nos.getenv("GENERATED")\n')
        self.write('scripts/s.py', 'import os\nos.getenv("SCRIPTED")\n')
        self.write('main.py', 'import os\nos.getenv("CONFIGURED")\nos.getenv("FLAGGED")\nos.getenv("KEPT")\n')
        result = self.run_cli('--ignore-path', 'scripts/**', '--ignore-name', 'FLAGGED', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(json.loads(result.stdout)['missing']), ['KEPT'])

    def test_ignored_names_do_not_hide_unlisted_names(self):
        self.write('envcheck.json', '{"ignore": {"names": ["HOME"]}}')
        self.write('main.py', 'import os\nos.getenv("HOME")\nos.getenv("HOMEPAGE")\nos.getenv("home")\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(sorted(report['missing']), ['HOMEPAGE', 'home'])

    def test_ignored_paths_do_not_affect_dotenv_discovery(self):
        self.write('envcheck.json', '{"ignore": {"paths": ["**"]}}')
        self.write('main.py', 'import os\nos.getenv("ANY")\n')
        self.write('.env', 'DEFINED=fake\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(report, {'summary': {'ok': 0, 'missing': 0, 'optional': 0, 'unused': 1, 'invalid': 0, 'computed': 0},
                                  'ok': [], 'missing': {}, 'optional': {}, 'unused': ['DEFINED'],
                                  'invalid': [], 'computed': []})

    def test_invalid_configuration_exits_two_without_stdout(self):
        cases = {
            '{"ignore": {"paths": [1]}': 'not valid JSON (line',
            '[]': 'top level must be a JSON object',
            '{"ignroe": {}}': 'unknown key(s): ignroe',
            '{"ignore": []}': '"ignore" must be an object',
            '{"ignore": {"nmaes": []}}': 'unknown key(s) under "ignore": nmaes',
            '{"ignore": {"paths": [1]}}': '"ignore.paths" must be a list of strings',
            '{"ignore": {"paths": "src"}}': '"ignore.paths" must be a list of strings',
            '{"ignore": {"paths": [" "]}}': '"ignore.paths" contains an empty entry',
            '{"ignore": {"names": ["BAD-NAME"]}}': 'not a valid variable name: BAD-NAME',
        }
        self.write('main.py', 'import os\nos.getenv("A")\n')
        for text, message in cases.items():
            with self.subTest(config=text):
                self.write('envcheck.json', text)
                result = self.run_cli('--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('Invalid configuration in', result.stderr)
                self.assertIn(message, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        with self.subTest(option='--ignore-name'):
            (self.root / 'envcheck.json').unlink()
            result = self.run_cli('--ignore-name', 'not valid')
            self.assertEqual(result.returncode, 2)
            self.assertIn('--ignore-name value is not a valid variable name', result.stderr)
        with self.subTest(option='--config missing'):
            result = self.run_cli('--config', 'missing.json')
            self.assertEqual(result.returncode, 2)
            self.assertIn('Cannot read', result.stderr)

    def test_ignored_directories_are_pruned_before_reading(self):
        self.write('skipped/unreadable.py', 'os.getenv("A")')
        self.write('kept.py', 'os.getenv("B")')
        original = Path.read_text

        def guarded(path, *args, **kwargs):
            if path.name == 'unreadable.py':
                raise AssertionError('ignored file was read')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', guarded):
            refs, count = envcheck.scan_directory(self.root, envcheck.PathFilter(['skipped']))
        self.assertEqual((refs, count), ({'B': [('kept.py', 1)]}, 1))

    SERVICE_CONFIG = json.dumps({
        'environments': {'development': ['config/.env.dev']},
        'services': {
            'api': {'paths': ['services/api'], 'environments': {
                'test': ['config/.env.test'],
                'production': ['config/.env.shared', 'config/.env.prod']}},
            'worker': {'paths': ['services/worker'], 'ignore': {'names': ['WORKER_DEBUG']},
                       'environments': {'production': ['config/.env.shared']}},
        },
    })

    def write_service_project(self):
        self.write('envcheck.json', self.SERVICE_CONFIG)
        self.write('services/api/main.py', 'import os\nos.getenv("API_URL")\nos.getenv("API_TOKEN")\n')
        self.write('services/worker/run.py', 'import os\nos.getenv("QUEUE_URL")\nos.getenv("WORKER_DEBUG")\n')
        self.write('config/.env.dev', 'API_URL=fake\nAPI_TOKEN=fake\nQUEUE_URL=fake\n')
        self.write('config/.env.test', 'API_URL=fake\nAPI_TOKEN=fake\n')
        self.write('config/.env.shared', 'SHARED_FLAG=fake\n')
        self.write('config/.env.prod', 'API_URL=fake\n')

    def test_services_are_checked_separately_per_environment(self):
        self.write_service_project()
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report['summary'],
                         {'services': 2, 'checks': 5, 'ok': 6, 'missing': 2, 'optional': 0, 'unused': 5, 'invalid': 0, 'computed': 0})
        api = report['services']['api']['environments']
        self.assertEqual(sorted(api), ['development', 'production', 'test'])
        self.assertEqual(api['development']['summary'],
                         {'ok': 2, 'missing': 0, 'optional': 0, 'unused': 1, 'invalid': 0, 'computed': 0})
        self.assertEqual(api['test']['ok'], ['API_TOKEN', 'API_URL'])
        self.assertEqual(api['production']['files'], ['config/.env.shared', 'config/.env.prod'])
        self.assertEqual(api['production']['missing'],
                         {'API_TOKEN': [{'file': 'services/api/main.py', 'line': 3}]})
        self.assertEqual(api['production']['unused'], ['SHARED_FLAG'])

    def test_development_value_cannot_satisfy_production(self):
        self.write_service_project()
        report = json.loads(self.run_cli('--format', 'json').stdout)
        api = report['services']['api']['environments']
        self.assertIn('API_TOKEN', api['development']['ok'])
        self.assertIn('API_TOKEN', api['production']['missing'])

    def test_service_sees_only_its_own_code(self):
        self.write_service_project()
        report = json.loads(self.run_cli('--format', 'json').stdout)
        worker = report['services']['worker']['environments']
        self.assertEqual(report['services']['worker']['files_scanned'], 1)
        self.assertEqual(sorted(worker['production']['missing']), ['QUEUE_URL'])
        self.assertEqual(worker['development']['unused'], ['API_TOKEN', 'API_URL'])
        self.assertNotIn('WORKER_DEBUG', worker['production']['missing'])
        self.assertNotIn('API_URL', worker['production']['missing'])

    def test_top_level_environments_are_inherited_and_overridden(self):
        self.write('envcheck.json', json.dumps({
            'environments': {'development': ['.env.dev'], 'production': ['.env.prod']},
            'services': {'api': {'paths': ['api'], 'environments': {'production': ['.env.api-prod']}}},
        }))
        self.write('api/main.py', 'import os\nos.getenv("NAME")\n')
        self.write('.env.dev', 'NAME=fake\n')
        self.write('.env.prod', 'WRONG=fake\n')
        self.write('.env.api-prod', 'NAME=fake\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        api = report['services']['api']['environments']
        self.assertEqual(api['development']['files'], ['.env.dev'])
        self.assertEqual(api['production']['files'], ['.env.api-prod'])

    def test_service_and_environment_filters(self):
        self.write_service_project()
        report = json.loads(self.run_cli('--service', 'api', '--environment', 'production', '--format', 'json').stdout)
        self.assertEqual(list(report['services']), ['api'])
        self.assertEqual(list(report['services']['api']['environments']), ['production'])
        self.assertEqual(report['summary']['checks'], 1)
        for args, message in (
            (('--service', 'nope'), 'Unknown service(s): nope'),
            (('--environment', 'staging'), 'Unknown environment(s): staging'),
            (('--env', 'config/.env.dev'), '--env cannot be combined with configured services'),
        ):
            with self.subTest(args=args):
                result = self.run_cli(*args, '--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)
        with self.subTest(args='no services configured'):
            (self.root / 'envcheck.json').unlink()
            result = self.run_cli('--service', 'api')
            self.assertEqual(result.returncode, 2)
            self.assertIn('need services in the configuration file', result.stderr)

    def test_no_config_falls_back_to_a_single_combined_check(self):
        self.write_service_project()
        report = json.loads(self.run_cli('--no-config', '--format', 'json').stdout)
        self.assertNotIn('services', report)
        self.assertEqual(sorted(report['missing']), ['API_TOKEN', 'API_URL', 'QUEUE_URL', 'WORKER_DEBUG'])

    def test_plain_service_output_names_each_service_and_environment(self):
        self.write_service_project()
        result = self.run_cli('--no-color', '--show-refs')
        self.assertIn('Each service and environment is checked separately.', result.stdout)
        self.assertIn('Service: api', result.stdout)
        self.assertIn('Service: worker', result.stdout)
        self.assertIn('production (config/.env.shared, config/.env.prod)', result.stdout)
        self.assertIn('services/api/main.py:3', result.stdout)
        self.assertNotIn('fake', result.stdout)

    def test_invalid_service_configuration_exits_two(self):
        self.write('main.py', 'import os\nos.getenv("A")\n')
        cases = {
            '{"services": []}': '"services" must be an object',
            '{"services": {"api": []}}': '"services.api" must be an object',
            '{"services": {"api": {"environments": {}}}}': '"services.api" must have a "paths" list',
            '{"services": {"api": {"paths": []}}}': 'must list at least one path',
            '{"services": {"api": {"paths": ["."], "extra": 1}}}': 'unknown key(s) under "services.api": extra',
            '{"services": {"api": {"paths": ["."]}}}': '"services.api" has no environments',
            '{"services": {"api": {"paths": ["."], "environments": []}}}': '"services.api.environments" must be an object',
            '{"services": {"api": {"paths": ["."], "environments": {"dev": "x"}}}}':
                '"services.api.environments.dev" must be a list of strings',
            '{"services": {"api": {"paths": ["."], "environments": {"dev": [".env"]},'
            ' "ignore": {"names": ["BAD-NAME"]}}}}': 'not a valid variable name',
            '{"environments": []}': '"environments" must be an object',
        }
        for text, message in cases.items():
            with self.subTest(config=text):
                self.write('envcheck.json', text)
                result = self.run_cli('--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_missing_service_paths_and_dotenv_files_are_errors(self):
        self.write('api/main.py', 'import os\nos.getenv("A")\n')
        self.write('.env.dev', 'A=fake\n')
        cases = {
            'gone': ('{"services": {"api": {"paths": ["gone"], "environments": {"dev": [".env.dev"]}}}}',
                     'Path for service "api" does not exist: gone'),
            'outside': ('{"services": {"api": {"paths": ["../outside"], "environments": {"dev": [".env.dev"]}}}}',
                        'is outside the scan directory'),
            'missing_env': ('{"services": {"api": {"paths": ["api"], "environments": {"dev": ["gone.env"]}}}}',
                            'environment "dev" does not exist: gone.env'),
            'directory_env': ('{"services": {"api": {"paths": ["api"], "environments": {"dev": ["api"]}}}}',
                              'environment "dev" is not a file: api'),
        }
        for label, (text, message) in cases.items():
            with self.subTest(case=label):
                self.write('envcheck.json', text)
                result = self.run_cli('--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)

    def test_service_file_path_and_overlapping_paths_count_once(self):
        self.write('envcheck.json', json.dumps({'services': {'api': {
            'paths': ['api', 'api/main.py'], 'environments': {'dev': ['.env.dev']}}}}))
        self.write('api/main.py', 'import os\nos.getenv("A")\n')
        self.write('.env.dev', 'A=fake\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(report['services']['api']['files_scanned'], 1)
        self.assertEqual(report['services']['api']['environments']['dev']['summary'],
                         {'ok': 1, 'missing': 0, 'optional': 0, 'unused': 0, 'invalid': 0, 'computed': 0})

    def test_dotenv_value_shapes_and_empty_detection(self):
        path = self.write('.env', 'FILLED=value\nEMPTY=\nSPACES=   \nQUOTED_EMPTY=""\nSINGLE_EMPTY=\'\'\n'
                          'QUOTED_SPACE=" "\nTRAILING_COMMENT=value # note\nONLY_COMMENT= # note\n'
                          'HASH_IN_VALUE=a#b\nESCAPED_HASH=a\\ #b\nCERT="first\nNOT_A_KEY=x\nlast"\n'
                          'SINGLE=\'first\nlast\'\nAFTER_QUOTE="v" junk\nOK_AFTER_QUOTE="v" # fine\n')
        parsed = envcheck.parse_env_file(path)
        empty = {name for name, entry in parsed.entries.items() if entry.empty}
        self.assertEqual(empty, {'EMPTY', 'SPACES', 'QUOTED_EMPTY', 'SINGLE_EMPTY', 'ONLY_COMMENT'})
        self.assertNotIn('NOT_A_KEY', parsed.entries)
        self.assertEqual(parsed.entries['CERT'].line, 11)
        self.assertEqual(parsed.entries['SINGLE'].line, 14)
        self.assertEqual(parsed.problems, [(16, 'unexpected text after the closing quote')])

    def test_duplicate_assignment_is_reported_and_last_one_wins(self):
        path = self.write('.env', 'NAME=first\nOTHER=value\nNAME=\n')
        parsed = envcheck.parse_env_file(path)
        self.assertEqual(parsed.problems, [(3, 'duplicate of the assignment on line 1')])
        self.assertTrue(parsed.entries['NAME'].empty)
        self.assertEqual(parsed.entries['NAME'].line, 3)

    def test_empty_value_is_reported_separately_from_missing(self):
        self.write('main.py', 'import os\nos.getenv("FILLED")\nos.getenv("BLANK")\nos.getenv("ABSENT")\n')
        self.write('.env', 'FILLED=value\nBLANK=\n')
        with self.subTest(mode='off'):
            report = json.loads(self.run_cli('--format', 'json').stdout)
            self.assertEqual(sorted(report['ok']), ['BLANK', 'FILLED'])
            self.assertEqual(sorted(report['missing']), ['ABSENT'])
            self.assertNotIn('empty', report)
        with self.subTest(mode='on'):
            result = self.run_cli('--check-empty', '--strict', '--format', 'json')
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report['ok'], ['FILLED'])
            self.assertEqual(sorted(report['missing']), ['ABSENT'])
            self.assertEqual(report['empty'], {'BLANK': {'file': '.env', 'line': 2}})
            self.assertEqual(report['summary'],
                             {'ok': 1, 'missing': 1, 'optional': 0, 'unused': 0, 'empty': 1, 'invalid': 0, 'computed': 0})
        with self.subTest(mode='config'):
            self.write('envcheck.json', '{"checks": {"empty": true}}')
            report = json.loads(self.run_cli('--format', 'json').stdout)
            self.assertEqual(report['empty'], {'BLANK': {'file': '.env', 'line': 2}})

    def test_later_environment_file_overrides_an_empty_value(self):
        self.write('envcheck.json', json.dumps({'checks': {'empty': True}, 'services': {'api': {
            'paths': ['api'], 'environments': {'production': ['.env.shared', '.env.prod']}}}}))
        self.write('api/main.py', 'import os\nos.getenv("FILLED_LATER")\nos.getenv("BLANKED_LATER")\n')
        self.write('.env.shared', 'FILLED_LATER=\nBLANKED_LATER=value\n')
        self.write('.env.prod', 'FILLED_LATER=value\nBLANKED_LATER=\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        environment = report['services']['api']['environments']['production']
        self.assertEqual(environment['ok'], ['FILLED_LATER'])
        self.assertEqual(environment['empty'], {'BLANKED_LATER': {'file': '.env.prod', 'line': 2}})

    def test_invalid_lines_report_file_and_line_without_values(self):
        self.write('main.py', 'import os\nos.getenv("GOOD_NAME")\n')
        self.write('.env', 'GOOD_NAME=secret-value-000\nBAD-NAME=secret-value-111\nNO_EQUALS_HERE\n'
                           'QUOTED="v" secret-value-222\nGOOD_NAME=secret-value-333\n')
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report['invalid'], [
            {'file': '.env', 'line': 2, 'reason': 'name before "=" is not a valid variable name'},
            {'file': '.env', 'line': 3, 'reason': 'no "=" assignment'},
            {'file': '.env', 'line': 4, 'reason': 'unexpected text after the closing quote'},
            {'file': '.env', 'line': 5, 'reason': 'duplicate of the assignment on line 1'},
        ])
        self.assertEqual(report['summary']['invalid'], 4)
        plain = self.run_cli('--no-color', '--strict')
        self.assertIn('[invalid] .env:2 name before "=" is not a valid variable name', plain.stdout)
        self.assertIn('4 invalid line(s)', plain.stdout)
        for output in (result.stdout, plain.stdout):
            for value in ('secret-value-000', 'secret-value-111', 'secret-value-222', 'secret-value-333'):
                self.assertNotIn(value, output)

    def test_services_read_only_their_configured_dotenv_files(self):
        self.write('envcheck.json', json.dumps({'services': {'api': {
            'paths': ['api'], 'environments': {'production': ['config/.env.prod']}}}}))
        self.write('api/main.py', 'import os\nos.getenv("API_URL")\n')
        self.write('config/.env.prod', 'API_URL=fake\n')
        self.write('.env', 'DISCOVERED=fake\n')
        self.write('.env.bak', 'STRAY="unclosed\n')
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        environment = json.loads(result.stdout)['services']['api']['environments']['production']
        self.assertEqual(environment['files'], ['config/.env.prod'])
        self.assertEqual(environment['ok'], ['API_URL'])
        self.assertEqual(environment['unused'], [])
        self.assertNotIn('DISCOVERED', result.stdout)
        self.assertNotIn('STRAY', result.stdout)

    def test_empty_and_invalid_results_match_between_formats(self):
        self.write('envcheck.json', '{"checks": {"empty": true}}')
        self.write('main.py', 'import os\nos.getenv("BLANK")\nos.getenv("ABSENT")\n')
        self.write('.env', 'BLANK=\nSTALE=value\nNO_EQUALS_HERE\n')
        plain = self.run_cli('--no-color', '--strict')
        report = json.loads(self.run_cli('--strict', '--format', 'json').stdout)
        self.assertEqual(report['summary'],
                         {'ok': 0, 'missing': 1, 'optional': 0, 'unused': 1, 'empty': 1, 'invalid': 1, 'computed': 0})
        self.assertIn('0 ok / 1 missing / 1 empty / 1 unused / 1 invalid line(s)', plain.stdout)
        self.assertIn('[empty]   BLANK', plain.stdout)
        self.assertIn('defined but not filled in (.env:1)', plain.stdout)
        self.assertIn('[missing] ABSENT', plain.stdout)
        self.assertIn('[invalid] .env:3', plain.stdout)

    def test_invalid_checks_configuration_exits_two(self):
        self.write('main.py', 'import os\nos.getenv("A")\n')
        for text, message in (('{"checks": []}', '"checks" must be an object'),
                              ('{"checks": {"emtpy": true}}', 'unknown key(s) under "checks": emtpy'),
                              ('{"checks": {"empty": "yes"}}', '"checks.empty" must be true or false')):
            with self.subTest(config=text):
                self.write('envcheck.json', text)
                result = self.run_cli('--format', 'json')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn(message, result.stderr)

    def test_backslash_values_are_not_empty(self):
        path = self.write('.env', 'LONE_SLASH=\\\nESCAPED_SPACE=\\ \nDOUBLE_SLASH=\\\\\nESCAPED_HASH=\\#\n'
                          'SPACED_SLASH=  \\  \nQUOTED_SLASH=\'\\\'\nTRULY_BLANK=\nBLANK_THEN_COMMENT= # note\n')
        parsed = envcheck.parse_env_file(path)
        empty = {name for name, entry in parsed.entries.items() if entry.empty}
        self.assertEqual(empty, {'TRULY_BLANK', 'BLANK_THEN_COMMENT'})
        self.assertEqual(parsed.problems, [])

    def test_python_fallback_only_names_are_optional(self):
        path = self.write('settings.py', 'import os\nos.getenv("POSITIONAL", "10")\n'
                          'os.environ.get("KEYWORD", default="x")\nos.getenv(\n    "MULTILINE",\n    "d",\n)\n'
                          'os.getenv("NONE_DEFAULT", None)\nos.environ.get("GET_ONLY")\nos.environ["SUBSCRIPT"]\n'
                          'os.getenv("BARE")\n')
        refs, required, computed = envcheck.scan_file_detailed(path, self.root)
        self.assertEqual(set(refs), {'POSITIONAL', 'KEYWORD', 'MULTILINE', 'NONE_DEFAULT', 'GET_ONLY', 'SUBSCRIPT', 'BARE'})
        self.assertEqual(required, {'NONE_DEFAULT', 'GET_ONLY', 'SUBSCRIPT', 'BARE'})

    def test_one_reference_without_fallback_makes_a_name_required(self):
        self.write('a.py', 'import os\nos.getenv("MIXED", "d")\nos.getenv("ONLY_FALLBACK", "d")\n')
        self.write('b.py', 'import os\nos.environ["MIXED"]\n')
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report['missing'], {'MIXED': [{'file': 'a.py', 'line': 2}, {'file': 'b.py', 'line': 2}]})
        self.assertEqual(report['optional'], {'ONLY_FALLBACK': [{'file': 'a.py', 'line': 3}]})
        self.assertEqual(report['summary']['missing'], 1)
        self.assertEqual(report['summary']['optional'], 1)

    def test_optional_names_do_not_fail_strict_and_are_ok_when_defined(self):
        self.write('main.py', 'import os\nos.getenv("UNDEFINED_WITH_FALLBACK", "d")\nos.getenv("DEFINED_WITH_FALLBACK", "d")\n')
        self.write('.env', 'DEFINED_WITH_FALLBACK=fake\n')
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['ok'], ['DEFINED_WITH_FALLBACK'])
        self.assertEqual(report['missing'], {})
        self.assertEqual(list(report['optional']), ['UNDEFINED_WITH_FALLBACK'])
        plain = self.run_cli('--no-color', '--strict')
        self.assertEqual(plain.returncode, 0)
        self.assertIn('[optional] UNDEFINED_WITH_FALLBACK', plain.stdout)
        self.assertIn('not defined; code has a fallback (main.py:2)', plain.stdout)
        self.assertIn('1 ok / 0 missing / 1 optional / 0 unused', plain.stdout)

    def test_pattern_languages_treat_every_reference_as_required(self):
        self.write('app.js', 'const port = process.env.PORT || "3000";\n')
        self.write('broken.py', 'import os\ndef broken(:\n    pass\nos.getenv("FALLBACK_IN_BROKEN_FILE", "d")\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(sorted(report['missing']), ['FALLBACK_IN_BROKEN_FILE', 'PORT'])
        self.assertEqual(report['optional'], {})

    def test_optional_names_are_reported_per_service_and_environment(self):
        self.write('envcheck.json', json.dumps({'services': {'api': {
            'paths': ['api'], 'environments': {'dev': ['.env.dev'], 'prod': ['.env.prod']}}}}))
        self.write('api/main.py', 'import os\nos.getenv("TIMEOUT", "10")\nos.environ["TOKEN"]\n')
        self.write('.env.dev', 'TIMEOUT=fake\nTOKEN=fake\n')
        self.write('.env.prod', 'TOKEN=fake\n')
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        environments = json.loads(result.stdout)['services']['api']['environments']
        self.assertEqual(environments['dev']['ok'], ['TIMEOUT', 'TOKEN'])
        self.assertEqual(environments['prod']['ok'], ['TOKEN'])
        self.assertEqual(list(environments['prod']['optional']), ['TIMEOUT'])
        self.assertEqual(json.loads(result.stdout)['summary']['optional'], 1)

    def test_ignored_names_are_not_reported_as_optional(self):
        self.write('envcheck.json', '{"ignore": {"names": ["IGNORED_FALLBACK"]}}')
        self.write('main.py', 'import os\nos.getenv("IGNORED_FALLBACK", "d")\nos.getenv("KEPT_FALLBACK", "d")\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(list(report['optional']), ['KEPT_FALLBACK'])

    def test_only_none_and_empty_unpacking_are_not_fallbacks(self):
        path = self.write('fallbacks.py', 'import os\nos.getenv("EMPTY_STRING", "")\nos.getenv("BRACKET_STRING", "[]")\n'
                          'os.getenv("EMPTY_LIST", [])\nos.getenv("ZERO", 0)\nos.getenv("FALSE", False)\n'
                          'os.getenv("FROM_VARIABLE", other)\nos.getenv("STAR_FILLED", *["d"])\n'
                          'os.getenv("DOUBLE_STAR_FILLED", **{"default": "d"})\nos.getenv(key="KEYWORD_KEY", default="d")\n'
                          'os.getenv("NONE_VALUE", None)\nos.getenv("STAR_EMPTY", *[])\nos.getenv("STAR_EMPTY_TUPLE", *())\n'
                          'os.getenv("STAR_NONE", *[None])\nos.getenv("STAR_UNKNOWN", *args)\n'
                          'os.getenv("DOUBLE_STAR_EMPTY", **{})\nos.getenv("DOUBLE_STAR_UNKNOWN", **options)\n'
                          'os.environ.get(key="GET_KEYWORD_KEY")\n')
        refs, required, computed = envcheck.scan_file_detailed(path, self.root)
        self.assertEqual(set(refs) - required, {
            'EMPTY_STRING', 'BRACKET_STRING', 'EMPTY_LIST', 'ZERO', 'FALSE', 'FROM_VARIABLE',
            'STAR_FILLED', 'DOUBLE_STAR_FILLED', 'KEYWORD_KEY'})
        self.assertEqual(required, {
            'NONE_VALUE', 'STAR_EMPTY', 'STAR_EMPTY_TUPLE', 'STAR_NONE', 'STAR_UNKNOWN',
            'DOUBLE_STAR_EMPTY', 'DOUBLE_STAR_UNKNOWN', 'GET_KEYWORD_KEY'})
        self.assertEqual(computed, [])

    def test_computed_keys_are_counted_and_located(self):
        path = self.write('computed.py', 'import os\nos.getenv(name)\nos.environ[key]\n'
                          'os.getenv(f"{prefix}_URL")\nos.getenv("prefix" + suffix)\nos.getenv("not-a-valid-name")\n'
                          'os.environ.get(other, "d")\nos.getenv("LITERAL_NAME")\nos.getenv()\n'
                          'os.getenv(key=name)\nos.environ.get(key=name, default="d")\n')
        refs, required, computed = envcheck.scan_file_detailed(path, self.root)
        self.assertEqual(set(refs), {'LITERAL_NAME'})
        self.assertEqual(computed, [('computed.py', line) for line in (2, 3, 4, 5, 6, 7, 10, 11)])

    def test_two_computed_lookups_on_one_line_count_separately(self):
        path = self.write('same_line.py', 'import os\nos.getenv(a); os.getenv(b)\n'
                          'value = os.getenv(f"{p}_A") + os.environ[q]\nos.getenv("LITERAL"); os.getenv("LITERAL")\n')
        refs, required, computed = envcheck.scan_file_detailed(path, self.root)
        self.assertEqual(computed, [('same_line.py', 2), ('same_line.py', 2), ('same_line.py', 3), ('same_line.py', 3)])
        self.assertEqual(refs, {'LITERAL': [('same_line.py', 4)]})
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(report['summary']['computed'], 4)
        plain = self.run_cli('--no-color', '--show-refs')
        self.assertIn('4 lookup(s) use a computed key and were not checked '
                      '(same_line.py:2, same_line.py:2, same_line.py:3, same_line.py:3).', plain.stdout)

    def test_computed_keys_appear_in_both_output_formats(self):
        self.write('main.py', 'import os\nos.getenv(name)\nos.environ[key]\nos.getenv("LITERAL")\n')
        self.write('.env', 'LITERAL=fake\n')
        result = self.run_cli('--strict', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['computed'],
                         [{'file': 'main.py', 'line': 2}, {'file': 'main.py', 'line': 3}])
        self.assertEqual(report['summary']['computed'], 2)
        plain = self.run_cli('--no-color', '--strict')
        self.assertEqual(plain.returncode, 0)
        self.assertIn('2 lookup(s) use a computed key and were not checked (main.py:2, main.py:3).', plain.stdout)

    def test_a_project_with_only_computed_keys_is_not_silently_clean(self):
        self.write('main.py', 'import os\nos.getenv(build_name())\n')
        result = self.run_cli('--no-color', '--strict')
        self.assertEqual(result.returncode, 0)
        self.assertIn('1 lookup(s) use a computed key and were not checked (main.py:2).', result.stdout)
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(report['summary'], {'ok': 0, 'missing': 0, 'optional': 0, 'unused': 0,
                                             'invalid': 0, 'computed': 1})

    def test_computed_keys_are_reported_per_service(self):
        self.write('envcheck.json', json.dumps({'services': {'api': {
            'paths': ['api'], 'environments': {'dev': ['.env.dev']}}}}))
        self.write('api/main.py', 'import os\nos.getenv(name)\nos.getenv("LITERAL")\n')
        self.write('.env.dev', 'LITERAL=fake\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(report['services']['api']['environments']['dev']['computed'],
                         [{'file': 'api/main.py', 'line': 2}])
        self.assertEqual(report['summary']['computed'], 1)

    def test_pattern_languages_report_no_computed_keys(self):
        self.write('app.js', 'process.env[name];\nprocess.env.LITERAL;\n')
        self.write('broken.py', 'import os\ndef broken(:\n    pass\nos.getenv(name)\n')
        report = json.loads(self.run_cli('--format', 'json').stdout)
        self.assertEqual(report['computed'], [])
        self.assertEqual(report['summary']['computed'], 0)

    def test_no_dotenv_message_accounts_for_fallbacks(self):
        self.write('main.py', 'import os\nos.getenv("REQUIRED")\nos.getenv("OPTIONAL", "10")\n')
        result = self.run_cli('--no-color', '--strict')
        self.assertEqual(result.returncode, 1)
        self.assertIn('No dotenv files found; every name without a fallback is reported as missing.',
                      result.stdout)
        self.assertNotIn('all detected names are reported as missing', result.stdout)
        self.assertIn('[missing] REQUIRED', result.stdout)
        self.assertIn('[optional] OPTIONAL', result.stdout)

if __name__ == '__main__':
    unittest.main()
