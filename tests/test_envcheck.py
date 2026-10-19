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
        self.assertEqual(envcheck.parse_env_file(path), {'FIRST', 'mixedCase', 'EMPTY', 'exported'})

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
                        'summary': {'ok': 1, 'missing': 1, 'unused': 1},
                        'ok': ['TOKEN'], 'missing': {'MISSING': [{'file': 'main.py', 'line': 2}]},
                        'unused': ['UNUSED'],
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
        self.assertEqual(json.loads(result.stdout)['summary'], {'ok': 0, 'missing': 0, 'unused': 0})

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


if __name__ == '__main__':
    unittest.main()
