"""Build a shareable starter archive from an explicit list, never private outputs."""
import argparse
import re
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
FILES = [
    'README.md', 'QUICKSTART.md', 'assistant-review.txt', 'requirements.txt',
    'env.example', 'config.example.json', '.gitignore',
    'analyze.py', 'runtime.py', 'signals.py', 'recommendations.py', 'review.py',
    'scheduling.py', 'presentation.py', 'preview_recap.py', 'demo.py', 'probe.py',
    'build_bundle.py', 'snapshot.py', 'LICENSE', 'CONTRIBUTING.md',
    'context_links.py', 'commitments.py', 'followup_state.py', 'community_demo.py',
    'docs/guidelines.md', 'docs/snapshot-contract.md',
    'docs/validation-recommandations.md',
    'docs/community-foundations.md', 'docs/business-case-template.md',
    '.github/ISSUE_TEMPLATE/business-case.yml',
]


def build(destination):
    files = [ROOT / name for name in FILES]
    files += sorted((ROOT / 'tests').glob('test_*.py'))
    files += sorted((ROOT / 'examples').glob('*.json'))
    # Current contributor docs only. Historical commercial launch notes stay local.
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing file or symlink in bundle: ' + path.name)
        content = path.read_text()
        if re.search(r'\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}|\bpat-[A-Za-z0-9_-]{25,}', content):
            raise ValueError('Possible secret detected: ' + path.name)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('Choose a new archive name to preserve the existing bundle')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, 'x', ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, 'open-pipeline-agent/' + path.relative_to(ROOT).as_posix())
    print(f'{len(files)} files packaged: {destination}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'open-pipeline-agent-starter.zip')
    build(parser.parse_args().output)
