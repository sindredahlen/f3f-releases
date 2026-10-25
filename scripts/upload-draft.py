import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile

REPO = 'sindredahlen/f3f-releases'
TAG = 'v0.1.35'
TRANSFER = 'build-transfer-v0.1.35.json'
MANIFEST_HASH = 'b679fa5b7ada5e2014c906cfd776da71854306d1dd926d3d325aeb054a20069d'
FILES = {'F3F-Standplass.exe', 'F3F-Standplass.zip',
         'F3F-Standplass-Setup-v0.1.35.exe', 'F3F-Standplass-v0.1.35.zip',
         'SHA256SUMS.txt'}

def gh(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('GitHub operation failed; output withheld to protect temporary download credentials')
    return result.stdout

def release():
    rows = json.loads(gh('api', f'repos/{REPO}/releases'))
    matches = [row for row in rows if row['tag_name'] == TAG]
    if len(matches) != 1 or not matches[0]['draft']:
        raise RuntimeError('Expected exactly one unpublished release draft')
    return matches[0]

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

transfer_id = None
try:
    current = release()
    transfer = [a for a in current['assets'] if a['name'] == TRANSFER]
    if len(transfer) != 1:
        raise RuntimeError('Missing unique draft-only transfer metadata')
    transfer_id = transfer[0]['id']
    with tempfile.TemporaryDirectory(prefix='f3f-draft-verify-') as temp:
        root = Path(temp)
        meta = root / 'metadata'
        gh('release', 'download', TAG, '--repo', REPO, '--pattern', TRANSFER, '--dir', str(meta))
        config = json.loads((meta / TRANSFER).read_text())
        url = config['archive_url']
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'productionresultssa7.blob.core.windows.net':
            raise RuntimeError('Unexpected private artifact destination')
        if config['manifest_sha256'] != MANIFEST_HASH or config['source_run_id'] != 40207468585:
            raise RuntimeError('Unexpected build provenance')
        archive = root / 'build.zip'
        try:
            with urllib.request.urlopen(url, timeout=180) as response, archive.open('wb') as target:
                shutil.copyfileobj(response, target)
        except Exception:
            raise RuntimeError('Private artifact download failed; URL withheld') from None
        # Never print or save the temporary download capability in reports.
        del url, config
        package = root / 'package'
        package.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            if set(bundle.namelist()) != FILES | {'RELEASE-NOTES.md'}:
                raise RuntimeError('Unexpected build artifact contents')
            if sum(info.file_size for info in bundle.infolist()) > 200_000_000:
                raise RuntimeError('Build artifact exceeds expected size')
            for name in bundle.namelist():
                with bundle.open(name) as source, (package / name).open('wb') as target:
                    shutil.copyfileobj(source, target)
        if digest(package / 'SHA256SUMS.txt') != MANIFEST_HASH:
            raise RuntimeError('Build checksum manifest does not match approved local files')
        listed = {}
        for line in (package / 'SHA256SUMS.txt').read_text(encoding='utf-8-sig').splitlines():
            expected, name = line.split(None, 1)
            name = name.strip()
            if name not in FILES - {'SHA256SUMS.txt'} or name in listed:
                raise RuntimeError('Unexpected checksum entry')
            if digest(package / name) != expected.lower():
                raise RuntimeError('Build checksum mismatch')
            listed[name] = expected.lower()
        if set(listed) != FILES - {'SHA256SUMS.txt'}:
            raise RuntimeError('Incomplete checksum manifest')
        listed['SHA256SUMS.txt'] = MANIFEST_HASH
        current = release()
        existing = {a['name'] for a in current['assets']} - {TRANSFER}
        if not existing <= FILES:
            raise RuntimeError('Unexpected existing release asset; refusing to replace it')
        if existing:
            old = root / 'existing'
            old.mkdir()
            for name in existing:
                gh('release', 'download', TAG, '--repo', REPO, '--pattern', name, '--dir', str(old))
                if digest(old / name) != listed[name]:
                    raise RuntimeError('Existing draft asset differs from verified build')
        missing = sorted(FILES - existing)
        if missing:
            release()  # Recheck draft status immediately before mutation.
            gh('release', 'upload', TAG, '--repo', REPO, *[str(package / name) for name in missing])
        downloaded = root / 'readback'
        downloaded.mkdir()
        for name in sorted(FILES):
            gh('release', 'download', TAG, '--repo', REPO, '--pattern', name, '--dir', str(downloaded))
            if digest(downloaded / name) != listed[name]:
                raise RuntimeError('Uploaded asset readback checksum mismatch')
        release()
        report = {
            'repository': REPO, 'tag': TAG, 'draft': True,
            'source_run_id': 40207468585,
            'source_commit': 'e11a24e66addff93f29f736085c7e25dbaf13740',
            'assets': [{'name': name, 'size': (package / name).stat().st_size,
                        'sha256': listed[name], 'readback_verified': True}
                       for name in sorted(FILES)]}
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('### Windows 0.1.35 draft assets verified\n\n')
            summary.write('All five draft assets match the verified Windows build after upload and download. Release remains unpublished.\n\n')
            for asset in report['assets']:
                summary.write(f"- {asset['name']}: {asset['size']} bytes, SHA-256 `{asset['sha256']}`\n")
        print(json.dumps(report, indent=2))
finally:
    if transfer_id is not None:
        gh('api', '--method', 'DELETE', f'repos/{REPO}/releases/assets/{transfer_id}')
        print('Temporary private transfer metadata removed.')
