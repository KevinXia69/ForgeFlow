#!/usr/bin/env python3
"""Package only the exact native-tested images, anonymously readable by downloaders."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def public_index(name: str, revision: str, expected: dict[str, str]) -> str:
    repository = f'kevinxia69/{name}'
    query = urllib.parse.urlencode({'service': 'ghcr.io', 'scope': f'repository:{repository}:pull'})
    # Deliberately no GH_TOKEN or Docker credential: recipients must have access.
    with urllib.request.urlopen('https://ghcr.io/token?' + query, timeout=30) as response:
        token = json.load(response)['token']
    request = urllib.request.Request(f'https://ghcr.io/v2/{repository}/manifests/sha-{revision}', headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(1024 * 1024 + 1)
        assert len(raw) <= 1024 * 1024
        digest = response.headers['Docker-Content-Digest']
    assert digest == 'sha256:' + hashlib.sha256(raw).hexdigest()
    manifests = json.loads(raw)['manifests']
    assert len(manifests) == 2
    assert {m['platform']['architecture'] for m in manifests} == {'amd64', 'arm64'}
    assert all(m['platform']['os'] == 'linux' for m in manifests)
    assert {m['platform']['architecture']: m['digest'] for m in manifests} == expected
    return f'ghcr.io/{repository}@{digest}'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    assert revision == os.environ['GITHUB_SHA']
    receipts = [json.loads(p.read_text()) for p in args.receipts.rglob('image-receipt.json')]
    assert len(receipts) == 2 and {r['architecture'] for r in receipts} == {'amd64', 'arm64'}
    assert all(r['commit'] == revision for r in receipts)
    results = [json.loads(p.read_text()) for p in args.receipts.rglob('result.json')]
    assert len(results) == 2 and all(r['ok'] is True and bool(r['checks']) and all(r['checks'].values()) for r in results)
    refs = {}
    for role, name in (('app', 'forgeflow-workbench'), ('odoo', 'forgeflow-workbench-odoo')):
        for receipt in receipts:
            image = receipt['images'][role]
            assert image['tested_id'] == image['published_id']
            assert image['reference'].startswith(f'ghcr.io/kevinxia69/{name}@sha256:')
        expected = {r['architecture']: r['images'][role]['reference'].split('@')[1] for r in receipts}
        refs[role] = public_index(name, revision, expected)
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_installers.py'), '--version', version,
                    '--output', str(args.output), '--app-image', refs['app'], '--odoo-image', refs['odoo']], check=True, cwd=ROOT)


if __name__ == '__main__':
    main()
