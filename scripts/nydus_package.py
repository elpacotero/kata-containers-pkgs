# SPDX-License-Identifier: MIT
"""Import the release-pinned nydus guest-pull tools and Kata deployment templates."""
import json
from pathlib import Path
import tarfile
import tempfile

from import_bundle import compressed_tar, digest
from upstream import contents

BASE = 'opt/kata/nydus-for-kata-tee/'
CONFIG = 'etc/kata-containers/nydus-snapshotter.toml'
UNIT = 'usr/lib/systemd/system/nydus-for-kata-tee.service'


def render(config, service):
    replacements = {
        '@SNAPSHOTTER_ROOT_DIR@': '/var/lib/nydus-for-kata-tee',
        '@SNAPSHOTTER_GRPC_SOCKET_ADDRESS@': '/run/nydus-for-kata-tee/containerd-nydus-grpc.sock',
        '@NYDUS_OVERLAYFS_PATH@': '/' + BASE + 'nydus-overlayfs',
        '@CONTAINERD_NYDUS_GRPC_BINARY@': '/' + BASE + 'containerd-nydus-grpc',
        '@CONFIG_GUEST_PULLING@': '/' + CONFIG,
        '@CRI_SERVICE@': 'containerd.service',
    }
    for key, value in replacements.items():
        config, service = config.replace(key, value), service.replace(key, value)
    if '@' in config or '@' in service:
        raise ValueError('unresolved nydus deployment template substitution')
    service = service.replace('[Service]\n', '[Service]\nRuntimeDirectory=nydus-for-kata-tee\nStateDirectory=nydus-for-kata-tee\n')
    return config, service


def import_snapshotter(inputs, request, client):
    descriptor = request['upstream']['auxiliary']['nydus_snapshotter']
    archive = inputs / 'nydus-download.tar.gz'
    client.download(descriptor, archive)
    revision = request['source_revision']
    config = contents(client, 'tools/packaging/kata-deploy/nydus-snapshotter/config-guest-pulling.toml', revision).decode()
    service = contents(client, 'tools/packaging/kata-deploy/nydus-snapshotter/nydus-snapshotter.service', revision).decode()
    config, service = render(config, service)
    with tempfile.TemporaryDirectory(prefix='kata-nydus-') as directory:
        root = Path(directory)
        names = []
        with tarfile.open(archive) as source:
            for binary in ('containerd-nydus-grpc', 'nydus-overlayfs'):
                member = source.getmember('bin/' + binary)
                if not member.isfile():
                    raise ValueError('nydus binary is not a regular file')
                target = root / BASE / binary
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.extractfile(member).read())
                target.chmod(0o755)
                names.append(str(target.relative_to(root)))
        for name, text in ((CONFIG, config), (UNIT, service),
                           ('usr/share/doc/kata-containers-nydus-snapshotter/LICENSE.nydus',
                            contents(client, 'LICENSE', descriptor['version'], repository=descriptor['repository']).decode())):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            names.append(name)
        output = inputs / 'kata-static-nydus-snapshotter.tar.zst'
        # compressed_tar accepts explicitly selected paths only.
        compressed_tar(root, names, output)
    archive.unlink()
    status_file = inputs / 'source-build.json'
    status = json.loads(status_file.read_text())
    status['components']['nydus-snapshotter'] = {'state': 'imported',
        'artifacts': {output.name: digest(output)}, 'upstream': descriptor,
        'configuration_source': revision}
    status_file.write_text(json.dumps(status, indent=2) + '\n')
