#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Import the supported generic QEMU and NVIDIA CPU/GPU profiles from an immutable upstream Rust bundle."""
import argparse
import hashlib
import json
import posixpath
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile

from build_request import validate_request


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def entries(archive):
    with subprocess.Popen(['zstd', '-dc', str(archive)], stdout=subprocess.PIPE) as proc:
        with tarfile.open(fileobj=proc.stdout, mode='r|') as tar:
            for member in tar:
                yield tar, member
        if proc.wait():
            raise ValueError('cannot decompress upstream bundle')


def name_of(member):
    name = PurePosixPath(member.name)
    if name.is_absolute() or '..' in name.parts:
        raise ValueError('unsafe upstream archive path')
    return name.as_posix()


def component_members(index, architecture, profiles=("qemu", "nvidia-cpu", "nvidia-gpu")):
    base = 'opt/kata/share/kata-containers/'
    components = {
        'shim-v2-rust': {'opt/kata/runtime-rs/bin/containerd-shim-kata-v2',
                        'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-runtime-rs.toml'},
        'qemu': {'opt/kata/bin/qemu-system-' + architecture},
        'kernel': {base + 'vmlinux.container'},
        'rootfs-image': {base + 'kata-containers.img', base + 'root_hash_base.txt'},
        'virtiofsd': {'opt/kata/libexec/virtiofsd'},
        'ovmf': {'opt/kata/share/ovmf/OVMF.fd'} if architecture == 'x86_64' else
                {'opt/kata/share/aavmf/AAVMF_CODE.fd', 'opt/kata/share/aavmf/AAVMF_VARS.fd'},
    }
    if any(profile in profiles for profile in ('nvidia-cpu', 'nvidia-gpu')):
        components['shim-v2-rust'].add('opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-nvidia-cpu-runtime-rs.toml')
        components.update({
            'qemu-no-shared-fs': {'opt/kata/bin/qemu-system-' + architecture + '-no-shared-fs'},
            'kernel-nvidia-gpu': {base + 'vmlinuz-nvidia-gpu.container'},
            'rootfs-image-nvidia': {base + 'kata-containers-nvidia.img', base + 'root_hash_nvidia.txt'},
        })
        components['qemu-no-shared-fs'].update(name for name in index if name.startswith(
            ('opt/kata/share/kata-qemu-no-shared-fs/', 'opt/kata/lib/kata-qemu-no-shared-fs/')))
    if 'nvidia-gpu' in profiles:
        components['shim-v2-rust'].add('opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-nvidia-gpu-runtime-rs.toml')
        components['rootfs-image-nvidia-gpu-extension'] = {
            base + 'kata-containers-nvidia-gpu-extension.img', base + 'root_hash_nvidia-gpu-extension.txt'}
    components['qemu'].update(name for name in index if name.startswith(
        ('opt/kata/share/kata-qemu/', 'opt/kata/lib/kata-qemu/')))
    for component, names in components.items():
        pending = list(names)
        while pending:
            name = pending.pop()
            if name not in index:
                raise ValueError(f'upstream bundle is missing required {component} payload: {name}')
            member = index[name]
            if member.issym() or member.islnk():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname)) if member.issym() else PurePosixPath(member.linkname).as_posix()
                if not target.startswith('opt/kata/'):
                    raise ValueError('upstream payload link escapes /opt/kata')
                if target not in names:
                    names.add(target)
                    pending.append(target)
    return components


def compressed_tar(root, names, destination):
    with destination.open('xb') as stream:
        with subprocess.Popen(['zstd', '-T2', '-3', '-c'], stdin=subprocess.PIPE, stdout=stream) as proc:
            with tarfile.open(fileobj=proc.stdin, mode='w|', dereference=False) as tar:
                for name in sorted(names):
                    tar.add(root / name, arcname=name, recursive=False)
            proc.stdin.close()
            if proc.wait():
                raise ValueError('cannot compress selected upstream payload')


def import_bundle(bundle, architecture, request, output, bundle_sha256, license_text):
    validate_request(request)
    if architecture not in ('x86_64', 'aarch64') or digest(bundle) != bundle_sha256:
        raise ValueError('upstream bundle architecture or checksum mismatch')
    output.mkdir(parents=True, exist_ok=False)
    index = {}
    for _, member in entries(bundle):
        name = name_of(member)
        if name in index:
            raise ValueError('duplicate upstream archive entry: ' + name)
        index[name] = member
    components = component_members(index, architecture, request.get("profiles", ("qemu", "nvidia-cpu", "nvidia-gpu")))
    selected = set().union(*components.values(), {'opt/kata/versions.yaml'})
    with tempfile.TemporaryDirectory(prefix='kata-import-') as directory:
        root = Path(directory)
        for tar, member in entries(bundle):
            if name_of(member) in selected:
                tar.extract(member, root, filter='data')
        status = {'kind': 'upstream-binary-import', 'upstream': request['upstream'],
                  'inputs': {'source_revision': request['source_revision'], 'architecture': architecture,
                             'source_revision_kind': 'upstream_workflow_head' if request['channel'] == 'development' else 'upstream_release_tag',
                             'bundle_sha256': bundle_sha256, 'source_patch_sha256': hashlib.sha256(b'').hexdigest()},
                  'components': {}}
        for component, names in components.items():
            archive = output / f'kata-static-{component}.tar.zst'
            compressed_tar(root, names, archive)
            status['components'][component] = {'state': 'imported', 'artifacts': {archive.name: digest(archive)}}
        shutil.copyfile(root / 'opt/kata/versions.yaml', output / 'versions.yaml')
    (output / 'LICENSE.upstream').write_text(license_text)
    (output / 'source-patch.diff').write_bytes(b'')
    (output / 'build-request.json').write_text(json.dumps(request, indent=2) + '\n')
    (output / 'source-build.json').write_text(json.dumps(status, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--architecture', required=True, choices=('x86_64', 'aarch64'))
    parser.add_argument('--request', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--license', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    import_bundle(args.bundle, args.architecture, json.loads(args.request.read_text()), args.output,
                  args.sha256, args.license.read_text())


if __name__ == '__main__':
    main()
