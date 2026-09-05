"""Classify only reviewed, modified prose files; uncertainty keeps full CI."""
import os
import re
import subprocess
from pathlib import Path

# No glob: executable documentation and unreviewed paths keep full coverage.
DOCS = frozenset(['AGENTS.md', 'README.md', 'STATUS.md', 'docs/AGENTS.md', 'docs/architecture.md', 'docs/operations.md', 'docs/system_state_matrix.md'])


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL)


def docs_only(base, head):
    if not all(re.fullmatch(r'[0-9a-f]{40}', sha or '') for sha in (base, head)):
        return False
    try:
        git('merge-base', '--is-ancestor', base, head)
        entries = git('diff', '--raw', '-z', '--no-renames', '--no-abbrev', base, head).split(b'\0')
        if entries == [b''] or entries[-1] != b'' or len(entries) % 2 != 1:
            return False
        for index in range(0, len(entries) - 1, 2):
            fields = entries[index].split()
            path = entries[index + 1].decode('utf-8')
            if len(fields) != 5 or fields[:2] != [b':100644', b'100644']:
                return False
            if fields[4] != b'M' or path not in DOCS:
                return False
        return True
    except (subprocess.CalledProcessError, UnicodeError, OSError):
        return False


def main():
    event = os.environ.get('GITHUB_EVENT_NAME', '')
    base = os.environ.get('BASE_SHA', '')
    head = os.environ.get('SOURCE_SHA', '')
    enabled = event in {'pull_request', 'push'}
    if event == 'workflow_dispatch' and os.environ.get('DOCS_FAST_PATH') == 'true':
        enabled = True
        try:
            base = git('rev-parse', f'{head}^1').decode().strip()
        except subprocess.CalledProcessError:
            base = ''
    result = enabled and docs_only(base, head)
    if result:
        # Cheap content validation, without dependency installation or code execution.
        git('diff', '--check', base, head)
        paths = git('diff', '--name-only', '-z', base, head).split(b'\0')[:-1]
        for path in paths:
            content = git('show', f'{head}:{path.decode()}').decode('utf-8')
            if not content.strip() or '\0' in content:
                raise ValueError('Documentation must contain nonempty UTF-8 text without NUL')
    value = 'true' if result else 'false'
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'docs_only={value}\n')
    with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
        summary.write(f'Documentation fast path: {value}. Source: {head}; base: {base}.\n')
        if result:
            summary.write('Prose checks only; no runtime test or deployable release evidence.\n')


if __name__ == '__main__':
    main()
