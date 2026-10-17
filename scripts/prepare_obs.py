#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate native, offline OBS source recipes from verified upstream payloads."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from import_bundle import import_bundle, digest, compressed_tar
from upstream import GitHub, ARCHES

ROOT = Path(__file__).resolve().parent.parent

from packages import build_packages, installed_packages, PROFILES

PACKAGES = ('kata-containers-core',) + tuple(name for name in build_packages() if name != 'kata-containers-core')
DESCRIPTIONS = {'kata-containers-core': 'Kata Containers Rust shim and host integration',
                'kata-containers-rootfs-generic': 'Kata Containers generic guest root filesystem',
                'kata-containers-kernel-generic': 'Kata Containers generic guest kernel',
                'kata-containers-virtiofsd': 'Kata Containers virtio-fs daemon',
                'kata-containers-qemu': 'Kata Containers QEMU profile and firmware',
                'kata-containers': 'Kata Containers default QEMU installation',
                'kata-containers-nvidia-cpu': 'Kata Containers NVIDIA CPU QEMU profile',
                'kata-containers-rootfs-nvidia': 'Kata Containers NVIDIA base guest root filesystem',
                'kata-containers-kernel-nvidia': 'Kata Containers NVIDIA guest kernel'}
BUILD = '''set -eu
arch=$(uname -m)
case "$arch" in x86_64) debarch=amd64 ;; aarch64) debarch=arm64 ;; *) exit 1 ;; esac
mkdir inputs
tar --zstd -xf "inputs-${debarch}.tar.zst" -C inputs
'''


def rpm_spec(request):
    version, revision = request['package_version'].split('-', 1)
    lines = ['# SPDX-License-Identifier: MIT', '%global debug_package %{nil}',
             '%global _build_id_links none', '%global __os_install_post %{nil}',
             'Name: kata-containers-core', f'Version: {version}', f'Release: {revision}.el10',
             'Summary: ' + DESCRIPTIONS['kata-containers-core'],
             'License: MIT AND Apache-2.0 AND GPL-2.0-only AND BSD-2-Clause AND BSD-3-Clause',
             f'Source0: kata-containers_{version}.orig.tar.gz',
             'BuildRequires: python3 >= 3.11', 'BuildRequires: zstd', 'BuildRequires: binutils',
             'ExclusiveArch: x86_64 aarch64', 'Requires: kmod', 'Requires: python3 >= 3.11',
             '', '%description', DESCRIPTIONS['kata-containers-core']]
    for name in PACKAGES[1:]:
        lines += ['', '%package -n ' + name, 'Summary: ' + DESCRIPTIONS[name]]
        if name == 'kata-containers':
            lines += ['BuildArch: noarch', 'Requires: kata-containers-qemu = %{version}-%{release}']
        elif name in ('kata-containers-' + profile for profile in PROFILES):
            profile = name.removeprefix('kata-containers-')
            dependencies = ['kata-containers-core (= ${binary:Version})'] + PROFILES[profile]['extra_dependencies']
            for dependency in dependencies:
                if dependency.startswith('kata-containers-'):
                    lines += ['Requires: ' + dependency.split()[0] + ' = %{version}-%{release}']
                else:
                    lines += ['Requires: ' + dependency.replace(' (>= ', ' >= ').replace(')', '')]
        lines += ['', '%description -n ' + name, DESCRIPTIONS[name]]
    lines += ['', '%prep', '%setup -q -n kata-containers-pkgs', '', '%build', BUILD,
              'python3 scripts/assemble.py --format rpm --stage-only --inputs inputs --output stages',
              '', '%install', 'python3 scripts/obs_native.py --stages stages --root "%{buildroot}" --files files']
    post = (ROOT / 'packaging/ubuntu-26.04/debian/postinst').read_text().replace('"$1" = configure', '"$1" -ge 1')
    lines += ['', '%post', post.replace('%', '%%')]
    for profile in PROFILES:
        lines += ['', '%post -n kata-containers-' + profile,
                  'if [ "$1" -ge 1 ]; then',
                  '  /usr/libexec/kata-containers/load-modules --profile ' + profile +
                  ' || echo "Kata: retry host module loading before workloads" >&2', 'fi']
    for name in PACKAGES:
        lines += ['', ('%files' if name == PACKAGES[0] else '%files -n ' + name) + ' -f files/' + name + '.list']
    return '\n'.join(lines) + '\n'


def debian_files(request, destination):
    version = request['package_version']
    destination.mkdir()
    control = ['Source: kata-containers', 'Section: admin', 'Priority: optional',
               'Maintainer: Kata Containers community <kata-dev@lists.katacontainers.io>',
               'Build-Depends: dpkg-dev, python3 (>= 3.11), zstd, binutils', 'Standards-Version: 4.7.0']
    for name in PACKAGES:
        control += ['', 'Package: ' + name, 'Architecture: ' + ('all' if name == 'kata-containers' else 'any'),
                    'Description: ' + DESCRIPTIONS[name]]
    (destination / 'control').write_text('\n'.join(control) + '\n')
    (destination / 'changelog').write_text(f'kata-containers ({version}) unstable; urgency=low\n\n'
        '  * Package verified upstream Kata binaries.\n\n'
        ' -- Kata Containers community <kata-dev@lists.katacontainers.io>  ' +
        datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000') + '\n')
    (destination / 'rules').write_text('''#!/usr/bin/make -f
.PHONY: clean build build-arch build-indep binary binary-arch binary-indep
clean:
\trm -rf inputs packages build debian/files
build build-arch build-indep:
binary binary-arch binary-indep: build/stamp
build/stamp:
\tmkdir -p build
\tsh debian/unpack-inputs.sh
\tpython3 scripts/assemble.py --inputs inputs --output packages
\tfor file in packages/*.deb; do cp "$$file" ../; basename "$$file" | sed 's/$$/ admin optional/' >> debian/files; done
\ttouch $@
''')
    (destination / 'rules').chmod(0o755)
    (destination / 'unpack-inputs.sh').write_text(BUILD)
    (destination / 'source').mkdir()
    (destination / 'source/format').write_text('3.0 (quilt)\n')


def prepare(request, output, client=None, root=ROOT):
    client = client or GitHub()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='kata-obs-source-') as directory:
        work = Path(directory)
        tree = work / 'kata-containers-pkgs'
        tree.mkdir()
        for name in ('scripts', 'packaging', 'LICENSES'):
            shutil.copytree(root / name, tree / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('LICENSE', 'config.json'):
            shutil.copyfile(root / name, tree / name)
        config = json.loads((tree / 'config.json').read_text())
        config.update(source_revision=request['source_revision'], package_version=request['package_version'])
        (tree / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
        (tree / 'build-request.json').write_text(json.dumps(request, indent=2) + '\n')
        # Preserve the license at the exact upstream source commit.
        import base64
        license_item = client.api('/contents/LICENSE?ref=' + request['source_revision'])
        license_text = base64.b64decode(license_item['content']).decode()
        sizes = {}
        for architecture, descriptor in request['upstream']['bundles'].items():
            print('Downloading upstream bundle for ' + architecture, flush=True)
            bundle = work / ('bundle-' + architecture + '.tar.zst')
            sha = client.download(descriptor, bundle)
            inputs = work / ('inputs-' + architecture)
            import_bundle(bundle, architecture, request, inputs, sha, license_text)
            name = 'inputs-' + ARCHES[architecture] + '.tar.zst'
            compressed_tar(inputs, [p.name for p in inputs.iterdir()], tree / name)
            sizes[architecture] = {'downloaded_bytes': descriptor['size'], 'obs_input_bytes': (tree / name).stat().st_size,
                                   'bundle_sha256': sha}
            bundle.unlink()
            shutil.rmtree(inputs)
        version = request['package_version'].split('-', 1)[0]
        orig = output / f'kata-containers_{version}.orig.tar.gz'
        with tarfile.open(orig, 'w:gz') as archive:
            archive.add(tree, arcname='kata-containers-pkgs')
        debian_files(request, tree / 'debian')
        deb = output / f'kata-containers_{request["package_version"]}.debian.tar.xz'
        with tarfile.open(deb, 'w:xz') as archive:
            archive.add(tree / 'debian', arcname='debian')
        files = [orig, deb]
        lines = ['Format: 3.0 (quilt)', 'Source: kata-containers', 'Binary: ' + ', '.join(PACKAGES),
                 'Architecture: any all', 'Version: ' + request['package_version'],
                 'Maintainer: Kata Containers community <kata-dev@lists.katacontainers.io>',
                 'Build-Depends: dpkg-dev, python3 (>= 3.11), zstd, binutils', 'Files:']
        def hash_file(path, algorithm):
            with path.open('rb') as stream:
                return hashlib.file_digest(stream, algorithm).hexdigest()
        lines += [f' {hash_file(p,"md5")} {p.stat().st_size} {p.name}' for p in files]
        lines += ['Checksums-Sha256:'] + [f' {digest(p)} {p.stat().st_size} {p.name}' for p in files]
        (output / 'kata-containers.dsc').write_text('\n'.join(lines) + '\n')
        (output / 'kata-containers.spec').write_text(rpm_spec(request))
        (output / 'build-request.json').write_text(json.dumps(request, indent=2) + '\n')
        (output / 'payload-sizes.json').write_text(json.dumps(sizes, indent=2) + '\n')
    return sizes
