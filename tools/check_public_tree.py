"""Check the tracked public source tree before release. Never print secret values."""
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = {'models', 'inputs', 'prepared-models', 'campaigns', 'coverage', 'archive',
                 'recovery-workspace', 'runs', 'build', 'output', 'drafts'}
GENERATED = {'.plan', '.swg', '.luks', '.hash', '.sqlite', '.zip', '.target', '.pyc', '.pdf'}
PATTERNS = {
    'private-key': rb'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    'cloud-key': rb'(?:AKIA|ASIA)[A-Z0-9]{16}',
    'github-token': rb'(?:ghp_|github_pat_)[A-Za-z0-9_]{24,}',
    'local-home-path': rb'/Users/[A-Za-z0-9_-]+/',
}


def main():
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    failures = []
    count = 0
    for name in filter(None, names):
        p = Path(name)
        reasons = []
        if p.parts[0] in PRIVATE_ROOTS or p.suffix in GENERATED:
            reasons.append('private or generated artifact')
        if name.endswith(('.desk.json', '.memories.json', '.notes.json')) or p.name == 'workspace.json':
            reasons.append('private workspace state')
        file = ROOT / p
        if file.is_symlink() or not file.is_file():
            reasons.append('unsupported file type')
        else:
            data = file.read_bytes()
            if len(data) > 2*1024**2:
                reasons.append('oversized source')
            reasons += [kind for kind, pattern in PATTERNS.items() if re.search(pattern, data)]
        count += 1
        if reasons:
            failures.append(name + ': ' + ', '.join(reasons))
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print(f'Public tree: {count} tracked source files; no flagged artifacts or secret patterns.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
