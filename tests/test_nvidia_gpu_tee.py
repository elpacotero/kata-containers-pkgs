# SPDX-License-Identifier: MIT
import base64
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import assemble
import ci
import import_bundle
import nydus_package
import packages
import prepare_obs
import upstream
import test_nvidia_cpu
# setup is not named setup.py on the installed host.
import importlib.util
spec = importlib.util.spec_from_file_location('tee_setup', ROOT / 'scripts/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class NvidiaGPUTEE(unittest.TestCase):
    def test_dependency_closure_and_architecture_exclusion(self):
        closure = set(packages.installed_packages('nvidia-gpu-tee'))
        self.assertEqual(closure, set(packages.installed_packages('nvidia-gpu')) | {
            'kata-containers-nvidia-gpu-tee', 'kata-containers-rootfs-coco', 'kata-containers-nydus-snapshotter'})
        self.assertEqual(len(closure), 9)
        self.assertEqual(len(packages.build_packages(architecture='amd64')), 17)
        self.assertEqual(len(packages.build_packages(architecture='arm64')), 14)
        self.assertNotIn('nvidia-gpu-tee', packages.profiles_for_arch('aarch64'))
        self.assertEqual(packages.profile_handlers('nvidia-gpu-tee'), ['kata-nvidia-gpu-snp', 'kata-nvidia-gpu-tdx'])

    def test_import_uses_only_extensions_and_two_experimental_vmms(self):
        index = test_nvidia_cpu.NvidiaAssets().index()
        base = 'opt/kata/share/kata-containers/'
        required = [base + 'root_hash_nvidia-gpu-extension.txt', base + 'kata-containers-nvidia-gpu-extension.img',
                    base + 'root_hash_coco-extension.txt', base + 'kata-containers-coco-extension.img',
                    'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-nvidia-gpu-runtime-rs.toml',
                    'opt/kata/share/ovmf/AMDSEV.fd', 'opt/kata/share/ovmf/OVMF.inteltdx.fd']
        # The base fixture is ARM, so supply its x86 equivalents explicitly.
        required += ['opt/kata/bin/qemu-system-x86_64', 'opt/kata/bin/qemu-system-x86_64-no-shared-fs',
                     'opt/kata/share/ovmf/OVMF.fd']
        for variant in ('snp', 'tdx'):
            required += ['opt/kata/bin/qemu-system-x86_64-' + variant + '-experimental',
                'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-nvidia-gpu-' + variant + '-runtime-rs.toml']
        for name in required + [base + 'kata-containers-nvidia-gpu.img', base + 'kata-containers-nvidia-gpu-confidential.img']:
            index[name] = tarfile.TarInfo(name)
        selected = import_bundle.component_members(index, 'x86_64', ['nvidia-gpu-tee'])
        self.assertIn('qemu-snp-experimental', selected)
        self.assertIn('qemu-tdx-experimental', selected)
        self.assertIn('rootfs-image-nvidia', selected)
        self.assertIn('rootfs-image-coco-extension', selected)
        self.assertFalse(any('confidential.img' in name or name.endswith('kata-containers-nvidia-gpu.img')
                             for members in selected.values() for name in members))
        arm = import_bundle.component_members(index, 'aarch64', ['qemu', 'nvidia-cpu', 'nvidia-gpu', 'nvidia-gpu-tee'])
        self.assertNotIn('rootfs-image-coco-extension', arm)
        del index['opt/kata/share/ovmf/AMDSEV.fd']
        with self.assertRaisesRegex(ValueError, 'missing required ovmf-sev'):
            import_bundle.component_members(index, 'x86_64', ['nvidia-gpu-tee'])

    def test_both_measurements_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for name, suffix in (('gpu', 'nvidia-gpu-extension'), ('coco', 'coco-extension')):
                base = root / 'opt/kata/share/kata-containers'
                base.mkdir(parents=True, exist_ok=True)
                path = '/opt/kata/share/kata-containers/kata-containers-' + suffix + '.img'
                (root / path.lstrip('/')).write_bytes(b'image')
                (base / ('root_hash_' + suffix + '.txt')).write_text(name)
                images.append({'name': name, 'path': path, 'verity_params': name})
            assemble.validate_gpu_extension({'guest_extension_images': images}, root, root)
            images[1]['verity_params'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'measurement mismatch'):
                assemble.validate_gpu_extension({'guest_extension_images': images}, root, root)

    def test_native_recipes_limit_new_packages_to_x86(self):
        request = {'package_version': '4.2.0-4'}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'debian'
            prepare_obs.debian_files(request, output)
            sections = (output / 'control').read_text().split('\n\n')[1:]
            for section in sections:
                if any('Package: ' + name + '\n' in section for name in packages.X86_PACKAGES):
                    self.assertIn('Architecture: amd64', section)
        rpm = prepare_obs.rpm_spec(request)
        for name in packages.X86_PACKAGES:
            self.assertIn('%ifarch x86_64\n\n%package -n ' + name, rpm)
            self.assertIn('%ifarch x86_64\n\n%files -n ' + name, rpm)

    def test_proxy_config_matches_both_handlers(self):
        data = tomllib.loads((ROOT / 'packaging/ubuntu-26.04/profiles/nvidia-gpu-tee.toml').read_text())
        runtimes = data['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes']
        self.assertEqual(set(runtimes), set(packages.profile_handlers('nvidia-gpu-tee')))
        self.assertTrue(all(runtime['snapshotter'] == 'nydus-for-kata-tee' for runtime in runtimes.values()))
        self.assertFalse(data['plugins']['io.containerd.cri.v1.images']['disable_snapshot_annotations'])
        self.assertEqual(data['proxy_plugins']['nydus-for-kata-tee']['type'], 'snapshot')
        jobs = ci.smoke_matrix(json.loads((ROOT / 'obs/ci.json').read_text())['targets'])['include']
        tee = [job for job in jobs if job['profile'] == 'nvidia-gpu-tee']
        self.assertEqual(len(tee), 1)
        self.assertEqual(tee[0]['test_kind'], 'installation')


class NydusDeployment(unittest.TestCase):
    def test_pinned_release_resolution_checks_digest_and_source(self):
        client = Mock()
        version = 'v0.15.15'
        text = 'externals:\n  nydus-snapshotter:\n    url: https://github.com/containerd/nydus-snapshotter\n    version: ' + version + '\n'
        asset = {'id': 1, 'name': 'nydus-snapshotter-' + version + '-linux-amd64.tar.gz',
                 'state': 'uploaded', 'digest': 'sha256:' + 'a' * 64, 'size': 5,
                 'browser_download_url': 'https://example.org/nydus'}
        def api(path, repository=upstream.REPOSITORY):
            if path.startswith('/contents/'):
                self.assertEqual(repository, upstream.REPOSITORY)
                return {'content': base64.b64encode(text.encode()).decode()}
            self.assertEqual(repository, 'containerd/nydus-snapshotter')
            return {'assets': [asset]}
        client.api.side_effect = api
        descriptor = upstream.nydus_snapshotter(client, 'a' * 40)
        self.assertEqual(descriptor['version'], version)
        self.assertEqual(descriptor['digest'], asset['digest'])
        asset['digest'] = None
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            upstream.nydus_snapshotter(client, 'a' * 40)

    def test_binding_is_idempotent_non_mutating_preview_and_preserves_state(self):
        for distro, units in setup.CRI_UNITS.items():
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                unit = root / 'usr/lib/systemd/system' / setup.NYDUS_UNIT
                unit.parent.mkdir(parents=True)
                unit.write_text('[Service]\n')
                state = root / 'var/lib/nydus-for-kata-tee/persistent'
                state.parent.mkdir(parents=True)
                state.write_text('keep')
                self.assertFalse(setup.nydus_setup(distro, root=root)['configured'])
                self.assertFalse((root / 'etc').exists())
                setup.nydus_setup(distro, apply=True, root=root)
                self.assertTrue(setup.nydus_setup(distro, root=root)['configured'])
                before = sorted(str(p.relative_to(root)) for p in root.rglob('*'))
                setup.nydus_setup(distro, apply=True, root=root)
                self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob('*')))
                self.assertEqual(state.read_text(), 'keep')
                for service in units:
                    self.assertTrue((root / 'etc/systemd/system' / (service + '.wants') / setup.NYDUS_UNIT).is_symlink())
                alternate = 'k3s' if distro != 'k3s' else 'rke2'
                setup.nydus_setup(alternate, apply=True, root=root)
                self.assertTrue(setup.nydus_setup(alternate, root=root)['configured'])
                self.assertFalse(setup.nydus_setup(distro, root=root)['configured'])

    def test_binding_preserves_administrator_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = root / 'usr/lib/systemd/system' / setup.NYDUS_UNIT
            unit.parent.mkdir(parents=True)
            unit.touch()
            link = root / 'etc/systemd/system/containerd.service.wants' / setup.NYDUS_UNIT
            link.parent.mkdir(parents=True)
            link.symlink_to('/custom/nydus.service')
            with self.assertRaisesRegex(ValueError, 'administrator'):
                setup.nydus_setup('kubeadm', apply=True, root=root)
            self.assertEqual(str(link.readlink()), '/custom/nydus.service')
