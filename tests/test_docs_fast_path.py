"""Exercise classification against real Git trees, including unsafe Markdown changes."""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('docs_fast_path', ROOT / '.github/scripts/docs_fast_path.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DocsFastPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = Path.cwd()
        os.chdir(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        Path('README.md').write_text('Original\n')
        Path('runtime.py').write_text('x = 1\n')
        Path('docs').mkdir()
        Path('docs/AGENT_INTEGRATIONS.md').write_text('Runtime template\n')
        self.base = self.commit()

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def test_existing_prose_modification(self):
        Path('README.md').write_text('Updated\n')
        self.assertTrue(MODULE.docs_only(self.base, self.commit()))

    def test_mixed_code_is_full(self):
        Path('README.md').write_text('Updated\n')
        Path('runtime.py').write_text('x = 2\n')
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_compiled_markdown_is_full(self):
        Path('docs/AGENT_INTEGRATIONS.md').write_text('Changed template\n')
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_added_allowlisted_document_is_full(self):
        Path('STATUS.md').write_text('New\n')
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_deleted_document_is_full(self):
        Path('README.md').unlink()
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_renamed_document_is_full(self):
        Path('README.md').rename('STATUS.md')
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_symlink_is_full(self):
        Path('README.md').unlink()
        Path('README.md').symlink_to('runtime.py')
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_executable_mode_is_full(self):
        Path('README.md').chmod(0o755)
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_unknown_workflow_is_full(self):
        Path('.github').mkdir()
        Path('.github/workflow.yml').write_text('name: changed\n')
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))

    def test_missing_empty_and_reverse_range_are_full(self):
        self.assertFalse(MODULE.docs_only(self.base, self.base))
        self.assertFalse(MODULE.docs_only('0' * 40, self.base))
        self.assertFalse(MODULE.docs_only('', self.base))
        Path('README.md').write_text('Updated\n')
        head = self.commit()
        self.assertFalse(MODULE.docs_only(head, self.base))

    def run_event(self, event, opt_in='false', content='Updated\n'):
        Path('README.md').write_text(content)
        head = self.commit()
        output = Path(self.tmp.name) / 'output'
        summary = Path(self.tmp.name) / 'summary'
        with patch.dict(os.environ, {
            'GITHUB_EVENT_NAME': event, 'BASE_SHA': self.base,
            'SOURCE_SHA': head, 'DOCS_FAST_PATH': opt_in,
            'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary),
        }):
            MODULE.main()
        return output.read_text()

    def test_push_and_pr_fast_path(self):
        self.assertIn('docs_only=true', self.run_event('pull_request'))

    def test_manual_default_is_full(self):
        self.assertIn('docs_only=false', self.run_event('workflow_dispatch'))

    def test_explicit_post_merge_dispatch(self):
        self.assertIn('docs_only=true', self.run_event('workflow_dispatch', 'true'))

    def test_schedule_is_full_even_with_opt_in(self):
        self.assertIn('docs_only=false', self.run_event('schedule', 'true'))

    def test_invalid_document_does_not_report_success(self):
        with self.assertRaises(ValueError):
            self.run_event('push', content='')

    def test_multi_commit_push_includes_earlier_code(self):
        Path('runtime.py').write_text('x = 2\n')
        self.commit()
        Path('README.md').write_text('Updated\n')
        self.assertFalse(MODULE.docs_only(self.base, self.commit()))


if __name__ == '__main__':
    unittest.main()
