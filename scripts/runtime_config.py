# SPDX-License-Identifier: MIT
"""Render missing runtime configuration from the exact upstream source commit."""
import base64
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


def generate_clh_config(client, revision, architecture):
    if not re.fullmatch('[0-9a-f]{40}', revision) or architecture not in ('x86_64', 'aarch64'):
        raise ValueError('configuration generation requires a pinned source and supported architecture')
    paths = ('utils.mk', 'VERSION', 'src/runtime-rs/Makefile',
             'src/runtime-rs/arch/' + architecture + '-options.mk',
             'src/runtime-rs/config/configuration-clh-runtime-rs.toml.in')
    hashes = {}
    with tempfile.TemporaryDirectory(prefix='kata-clh-config-') as directory:
        root = Path(directory)
        for path in paths:
            item = client.api('/contents/' + path + '?ref=' + revision)
            data = base64.b64decode(item['content'])
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            hashes[path] = hashlib.sha256(data).hexdigest()
        runtime = root / 'src/runtime-rs'
        (runtime / 'VERSION').write_bytes((root / 'VERSION').read_bytes())
        # aarch64's options omit CLHCMD even though its bundle ships the VMM
        # and the Rust shim includes the backend. Generate only the config.
        command = ['make', '--no-print-directory', 'config/configuration-clh-runtime-rs.toml',
                   'ARCH=' + architecture, 'PREFIX=/opt/kata', 'CLHCMD=cloud-hypervisor',
                   'HAS_TARPAULIN=unused']
        subprocess.run(command, cwd=runtime, check=True, capture_output=True, text=True, timeout=60)
        text = (runtime / 'config/configuration-clh-runtime-rs.toml').read_text()
    if re.search(r'@[A-Z][A-Z0-9_]*@', text):
        raise ValueError('unresolved upstream Cloud Hypervisor configuration substitutions')
    hypervisor = tomllib.loads(text)['hypervisor']['clh']
    if (hypervisor['path'] != '/opt/kata/bin/cloud-hypervisor'
            or hypervisor['kernel'] != '/opt/kata/share/kata-containers/vmlinux.container'
            or hypervisor['image'] != '/opt/kata/share/kata-containers/kata-containers.img'
            or hypervisor['virtio_fs_daemon'] != '/opt/kata/libexec/virtiofsd'):
        raise ValueError('unexpected generated Cloud Hypervisor asset paths')
    return {'text': text, 'source_revision': revision, 'architecture': architecture,
            'source_files': hashes, 'command': command}
