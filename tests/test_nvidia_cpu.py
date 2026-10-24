# SPDX-License-Identifier: MIT
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import ci
import import_bundle
import packages
import prepare_obs

spec = importlib.util.spec_from_file_location('nvidia_setup', ROOT / 'scripts/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class NvidiaAssets(unittest.TestCase):
    def index(self):
        base = 'opt/kata/share/kata-containers/'
        names = ['opt/kata/runtime-rs/bin/containerd-shim-kata-v2',
                 'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-runtime-rs.toml',
                 'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-nvidia-cpu-runtime-rs.toml',
                 'opt/kata/bin/qemu-system-aarch64', 'opt/kata/bin/qemu-system-aarch64-no-shared-fs',
                 'opt/kata/libexec/virtiofsd', 'opt/kata/share/aavmf/AAVMF_CODE.fd',
                 'opt/kata/share/aavmf/AAVMF_VARS.fd', base + 'vmlinux.container',
                 base + 'kata-containers.img', base + 'root_hash_base.txt', base + 'root_hash_nvidia.txt',
                 base + 'nvidia.image', base + 'nvidia.kernel', base + 'gpu-extension.image',
                 'opt/kata/share/kata-qemu-no-shared-fs/qemu/bios.bin']
        index = {name: tarfile.TarInfo(name) for name in names}
        for name, target in [('kata-containers-nvidia.img', 'nvidia.image'),
                             ('vmlinuz-nvidia-gpu.container', 'nvidia.kernel')]:
            member = tarfile.TarInfo(base + name)
            member.type, member.linkname = tarfile.SYMTYPE, target
            index[member.name] = member
        return index

    def test_import_includes_cpu_assets_and_resolved_links_without_gpu_driver_extension(self):
        selected = import_bundle.component_members(self.index(), 'aarch64', ('qemu', 'nvidia-cpu'))
        self.assertIn('opt/kata/share/kata-containers/nvidia.image', selected['rootfs-image-nvidia'])
        self.assertIn('opt/kata/share/kata-containers/nvidia.kernel', selected['kernel-nvidia-gpu'])
        self.assertIn('opt/kata/bin/qemu-system-aarch64-no-shared-fs', selected['qemu-no-shared-fs'])
        self.assertNotIn('opt/kata/share/kata-containers/gpu-extension.image', set().union(*selected.values()))

    def test_incomplete_cpu_bundle_fails_instead_of_silently_shipping_generic_only(self):
        index = self.index()
        del index['opt/kata/bin/qemu-system-aarch64-no-shared-fs']
        with self.assertRaisesRegex(ValueError, 'missing required qemu-no-shared-fs'):
            import_bundle.component_members(index, 'aarch64', ('qemu', 'nvidia-cpu'))

    def test_import_includes_uncompressed_cpu_and_compressed_gpu_kernels(self):
        base = 'opt/kata/share/kata-containers/'
        for architecture in ('x86_64', 'aarch64'):
            with self.subTest(architecture=architecture):
                index = self.index()
                if architecture == 'x86_64':
                    for name in ('opt/kata/bin/qemu-system-x86_64',
                                 'opt/kata/bin/qemu-system-x86_64-no-shared-fs',
                                 'opt/kata/share/ovmf/OVMF.fd'):
                        index[name] = tarfile.TarInfo(name)
                alias = tarfile.TarInfo(base + 'vmlinux-nvidia-gpu.container')
                alias.type, alias.linkname = tarfile.SYMTYPE, 'nvidia-uncompressed.kernel'
                index[alias.name] = alias
                index[base + alias.linkname] = tarfile.TarInfo(base + alias.linkname)
                selected = import_bundle.component_members(index, architecture, ('qemu', 'nvidia-cpu'))
                self.assertEqual(selected['kernel-nvidia-gpu'], {
                    base + 'vmlinux-nvidia-gpu.container', base + 'nvidia-uncompressed.kernel',
                    base + 'vmlinuz-nvidia-gpu.container', base + 'nvidia.kernel'})

    def test_uncompressed_cpu_kernel_requires_its_symlink_target(self):
        index = self.index()
        alias = tarfile.TarInfo('opt/kata/share/kata-containers/vmlinux-nvidia-gpu.container')
        alias.type, alias.linkname = tarfile.SYMTYPE, 'missing-uncompressed.kernel'
        index[alias.name] = alias
        with self.assertRaisesRegex(ValueError, 'missing required kernel-nvidia-gpu payload: .*missing-uncompressed.kernel'):
            import_bundle.component_members(index, 'aarch64', ('qemu', 'nvidia-cpu'))

    def test_cpu_dependency_closure_does_not_pull_generic_guest_or_virtiofsd(self):
        selected = packages.installed_packages('nvidia-cpu')
        self.assertEqual(set(selected), {'kata-containers-core', 'kata-containers-nvidia-cpu',
                                        'kata-containers-rootfs-nvidia', 'kata-containers-kernel-nvidia'})
        self.assertEqual(len(packages.build_packages()), 17)
        self.assertEqual(set(packages.build_packages(['qemu'])), set(packages.GENERIC_PACKAGES))

    def test_rpm_declares_cpu_dependencies_and_module_loading(self):
        text = prepare_obs.rpm_spec({'package_version': '4.2.0-2'})
        section = text.split('%package -n kata-containers-nvidia-cpu\n')[1].split('%description')[0]
        for dependency in ('kata-containers-core', 'kata-containers-rootfs-nvidia', 'kata-containers-kernel-nvidia'):
            self.assertIn('Requires: ' + dependency + ' = %{version}-%{release}', section)
        self.assertIn('Requires: erofs-utils >= 1.8.2', section)
        self.assertNotIn('virtiofsd', section)
        self.assertIn('load-modules --profile nvidia-cpu', text)

    def test_revision_two_keeps_previous_stable_container_and_future_releases_start_at_one(self):
        version = ci.package_version('release', 'refs/tags/4.2.0', '0.0.0-0.1', '123', '1', {'4.2.0': 2})
        self.assertEqual(version, '4.2.0-2')
        settings = {'channels': {'release': {'project': 'home:test', 'package': 'kata-containers'}}}
        self.assertEqual(ci.destination_for(settings, {'channel': 'release', 'package_version': version})['package'],
                         'kata-containers-4.2.0-r2')
        self.assertEqual(ci.package_version('release', 'refs/tags/4.3.0', '0.0.0-0.1', '123', '1', {'4.2.0': 2}),
                         '4.3.0-1')

    def test_cpu_ci_pins_latest_stable_and_preserves_other_upstream_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, output, tools = root / 'upstream', root / 'logs', root / 'bin'
            for path in (upstream / 'ci', upstream / 'tests', output, tools):
                path.mkdir(parents=True)
            (upstream / 'ci/install_yq.sh').write_text('true\n')
            (upstream / 'tests/gha-run-k8s-common.sh').write_text(
                'get_latest_patch_release_from_a_github_project() { echo "upstream-$1"; }\n'
                'die() { echo "$*" >&2; exit 1; }\n'
                'deploy_k8s() {\n'
                '  echo "selected:$CONTAINER_ENGINE_VERSION"\n'
                '  get_latest_patch_release_from_a_github_project containerd/containerd v2.4 true\n'
                '  get_latest_patch_release_from_a_github_project kubernetes-sigs/cri-tools v1.35\n'
                '}\n')
            fixture = root / 'release.json'
            fixture.write_text(json.dumps({'tag_name': 'v2.4.2', 'prerelease': False, 'draft': False}))
            curl = tools / 'curl'
            curl.write_text('#!/bin/bash\nwhile [[ "$1" != -o ]]; do shift; done\ncp "$RELEASE_FIXTURE" "$2"\n')
            curl.chmod(0o755)
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                       RELEASE_FIXTURE=str(fixture), KUBERNETES='vanilla', KATA_PACKAGE_PROFILE='nvidia-cpu')
            command = ['bash', str(ROOT / 'scripts/setup-kubernetes.sh'), str(upstream), str(output)]
            result = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.splitlines(),
                             ['selected:v2.4', 'v2.4.2', 'upstream-kubernetes-sigs/cri-tools'])
            fixture.write_text(json.dumps({'tag_name': 'v2.5.0-rc.1', 'prerelease': True}))
            result = subprocess.run(command, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Expected a stable containerd release', result.stderr)

    def test_cpu_workload_has_a_separate_kubeadm_host(self):
        targets = json.loads((ROOT / 'obs/ci.json').read_text())['targets']
        jobs = ci.smoke_matrix(targets)['include']
        cpu = [job for job in jobs if job['profile'] == 'nvidia-cpu']
        self.assertEqual(len(cpu), 1)
        self.assertEqual(cpu[0]['k8s_distro'], 'kubeadm')
        self.assertEqual(cpu[0]['arch'], 'x86_64')
        self.assertEqual(len([job for job in jobs if job['profile'] == 'qemu']), 5)


class NvidiaConfiguration(unittest.TestCase):
    @contextlib.contextmanager
    def installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def mapped(value):
                path = Path(value)
                return path if str(path).startswith(str(root)) else root / str(path).lstrip('/')
            marker = mapped('/usr/share/kata-containers/installed-profiles/nvidia-cpu.json')
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps(packages.PROFILES['nvidia-cpu']))
            vendor = mapped('/etc/containerd/conf.d/50-kata-nvidia-cpu.toml')
            vendor.parent.mkdir(parents=True)
            vendor.write_text((ROOT / 'packaging/ubuntu-26.04/profiles/nvidia-cpu.toml').read_text())
            with patch.object(setup, 'Path', side_effect=mapped):
                yield

    def test_erofs_options_match_upstream_cpu_contract_and_reject_schema_two(self):
        import tomllib
        with self.installed():
            config = tomllib.loads(setup.profile_tables(3))['plugins']
            images = config['io.containerd.cri.v1.images']
            self.assertEqual(images['runtime_platforms']['kata-nvidia-cpu']['snapshotter'], 'erofs')
            self.assertFalse(images['disable_snapshot_annotations'])
            snapshotter = config['io.containerd.snapshotter.v1.erofs']
            self.assertEqual(snapshotter['default_size'], '0')
            self.assertEqual(snapshotter['dmverity_mode'], 'on')
            self.assertFalse(snapshotter['enable_fsverity'])
            self.assertNotIn('max_unmerged_layers', snapshotter)
            self.assertTrue(config['io.containerd.differ.v1.erofs']['enable_dmverity'])
            with self.assertRaisesRegex(ValueError, 'requires containerd >= 2.3.0'):
                setup.profile_tables(2)

    def test_old_runtime_is_rejected_before_mkfs_or_configuration_changes(self):
        with self.installed(), patch.object(setup, 'containerd_binary', return_value='/test/containerd'), \
                patch.object(setup, 'command', return_value='containerd github.com/containerd/containerd/v2 v2.1.9'), \
                patch.object(setup.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'requires kubeadm containerd >= 2.3.0'):
                setup.validate_profile_requirements('kubeadm')
            run.assert_not_called()

    def test_supported_runtime_requires_actual_erofs_mkfs_options(self):
        with self.installed(), patch.object(setup, 'containerd_binary', return_value='/test/containerd'), \
                patch.object(setup, 'command', return_value='containerd github.com/containerd/containerd/v2 v2.3.0'), \
                patch.object(setup.shutil, 'which', return_value='/usr/bin/mkfs.erofs'), \
                patch.object(setup.subprocess, 'run', return_value=Mock(stdout='--mkfs-time=TIME --sort=METHOD')) as run:
            setup.validate_profile_requirements('kubeadm')
            run.return_value.stdout = 'old mkfs help'
            with self.assertRaisesRegex(ValueError, 'lacks required'):
                setup.validate_profile_requirements('kubeadm')


if __name__ == '__main__':
    unittest.main()
