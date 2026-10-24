# SPDX-License-Identifier: MIT
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import assemble
import ci
import import_bundle
import packages
import prepare_obs
import runtime_config
import smoke_debug
import test_nvidia_cpu

CLH_CONFIG = 'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-clh-runtime-rs.toml'


class CloudHypervisor(unittest.TestCase):
    def index(self, architecture, configuration=True):
        index = test_nvidia_cpu.NvidiaAssets().index()
        names = ['opt/kata/bin/cloud-hypervisor', 'opt/kata/versions.yaml']
        if architecture == 'x86_64':
            names += ['opt/kata/bin/qemu-system-x86_64', 'opt/kata/share/ovmf/OVMF.fd']
        if configuration:
            names.append(CLH_CONFIG)
        index.update({name: tarfile.TarInfo(name) for name in names})
        return index

    def test_import_selects_vmm_config_and_shared_guest_on_both_architectures(self):
        for architecture in ('x86_64', 'aarch64'):
            with self.subTest(architecture=architecture):
                index = self.index(architecture)
                selected = import_bundle.component_members(index, architecture, ['qemu', 'cloud-hypervisor'])
                self.assertEqual(selected['cloud-hypervisor'], {'opt/kata/bin/cloud-hypervisor'})
                self.assertIn(CLH_CONFIG, selected['shim-v2-rust'])
                self.assertNotIn('kernel-dragonball-experimental', selected)
                del index['opt/kata/bin/cloud-hypervisor']
                with self.assertRaisesRegex(ValueError, 'missing required cloud-hypervisor'):
                    import_bundle.component_members(index, architecture, ['cloud-hypervisor'])

    @unittest.skipUnless(shutil.which('zstd'), 'requires zstd')
    def test_missing_arm_config_is_generated_with_pinned_provenance(self):
        for bundled_config in (False, True):
            with self.subTest(bundled_config=bundled_config), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'source'
                source.mkdir()
                for name, member in self.index('aarch64', configuration=bundled_config).items():
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if member.issym():
                        path.symlink_to(member.linkname)
                    else:
                        path.write_text('bundled payload')
                bundle = root / 'bundle.tar.zst'
                import_bundle.compressed_tar(source, self.index('aarch64', configuration=bundled_config), bundle)
                request = {'schema_version': 1, 'channel': 'release', 'source_revision': 'a' * 40,
                           'source_ref': 'refs/tags/4.3.1', 'packaging_revision': 'b' * 40,
                           'package_version': '4.3.1-2', 'profiles': ['qemu', 'cloud-hypervisor'],
                           'upstream': {'kind': 'release', 'source_revision': 'a' * 40,
                                        'source_ref': 'refs/tags/4.3.1'}}
                record = {'text': '[hypervisor.clh]\npath = "/opt/kata/bin/cloud-hypervisor"\n',
                          'source_revision': 'a' * 40, 'architecture': 'aarch64',
                          'source_files': {'source': 'c' * 64}, 'command': ['make']}
                provider = Mock(return_value=record)
                output = root / 'inputs'
                import_bundle.import_bundle(bundle, 'aarch64', request, output, import_bundle.digest(bundle),
                                            'license', configuration_provider=provider)
                status = json.loads((output / 'source-build.json').read_text())
                with subprocess.Popen(['zstd', '-dc', str(output / 'kata-static-shim-v2-rust.tar.zst')],
                                      stdout=subprocess.PIPE) as proc:
                    with tarfile.open(fileobj=proc.stdout, mode='r|') as archive:
                        data = next(archive.extractfile(member).read().decode() for member in archive
                                    if member.name == CLH_CONFIG)
                    self.assertEqual(proc.wait(), 0)
                if bundled_config:
                    provider.assert_not_called()
                    self.assertEqual(data, 'bundled payload')
                    self.assertNotIn('generated_configurations', status['inputs'])
                else:
                    provider.assert_called_once_with('aarch64')
                    self.assertEqual(data, record['text'])
                    provenance = status['inputs']['generated_configurations'][CLH_CONFIG]
                    self.assertEqual(provenance['source_revision'], request['source_revision'])
                    self.assertEqual(provenance['source_files'], record['source_files'])
                    self.assertEqual(provenance['sha256'], hashlib.sha256(data.encode()).hexdigest())
                    provider.return_value = dict(record, source_revision='c' * 40)
                    with self.assertRaisesRegex(ValueError, 'provenance does not match'):
                        import_bundle.import_bundle(bundle, 'aarch64', request, root / 'wrong',
                                                    import_bundle.digest(bundle), 'license', provider)

    def test_dependency_closure_and_recipes_share_generic_assets(self):
        self.assertEqual(set(packages.installed_packages('cloud-hypervisor')), {
            'kata-containers-core', 'kata-containers-cloud-hypervisor', 'kata-containers-rootfs-generic',
            'kata-containers-kernel-generic', 'kata-containers-virtiofsd'})
        for arch in ('amd64', 'arm64'):
            self.assertIn('cloud-hypervisor', packages.profiles_for_arch(arch))
        spec = prepare_obs.rpm_spec({'package_version': '4.3.1-2'})
        section = spec.split('%package -n kata-containers-cloud-hypervisor\n')[1].split('%description')[0]
        self.assertIn('Requires: kata-containers-virtiofsd = %{version}-%{release}', section)
        self.assertNotIn('Requires: kata-containers-qemu', section)

    def test_staged_profile_uses_clh_handler_and_public_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.toml'
            source.write_text('[hypervisor.clh]\npath = "/opt/kata/bin/cloud-hypervisor"\n')
            config = assemble.stage_runtime_config(source, root, 'cloud-hypervisor')
            subprocess.run([sys.executable, str(ROOT / 'scripts/stage_profile.py'), 'cloud-hypervisor',
                            '--format', 'rpm', '--architecture', 'arm64', '--version', '4.3.1-2',
                            '--root', str(root)], check=True, capture_output=True)
            runtime = tomllib.loads((root / 'usr/share/kata-containers/containerd/cloud-hypervisor.toml').read_text())
            registration = runtime['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes']['kata-clh']
            self.assertEqual(registration['options']['ConfigPath'], '/' + str(config.relative_to(root)))
            self.assertEqual(tomllib.loads(config.read_text())['hypervisor']['clh']['path'], '/opt/kata/bin/cloud-hypervisor')

    def test_debug_configures_clh_without_qemu_console_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def mapped(value):
                path = Path(value)
                return path if path.is_relative_to(root) else root / str(path).lstrip('/')
            config = mapped('/opt/kata/share/defaults/kata-containers/configuration-cloud-hypervisor.toml')
            config.parent.mkdir(parents=True)
            config.write_text('[hypervisor.clh]\npath = "/opt/kata/bin/cloud-hypervisor"\nkernel_params = "cgroup_no_v1=all"\n')
            (config.parent / 'config.d').mkdir()
            containerd = mapped('/etc/containerd/config.toml')
            containerd.parent.mkdir(parents=True)
            containerd.write_text('version = 3\n')
            for debug in (False, True, False):
                output = root / ('debug' if debug else 'normal')
                output.mkdir(exist_ok=True)
                with patch.object(smoke_debug, 'Path', side_effect=mapped), patch('smoke_debug.subprocess.run'):
                    smoke_debug.configure('kubeadm', output, debug, profile='cloud-hypervisor')
                effective = json.loads((output / 'kata-effective-config.json').read_text())
                self.assertEqual(set(effective['hypervisor']), {'clh'})
                self.assertEqual(effective['hypervisor']['clh']['enable_debug'], debug)
                self.assertEqual(effective['hypervisor']['clh']['path'], '/opt/kata/bin/cloud-hypervisor')
                self.assertFalse((config.parent / 'config.d/40-early-console.toml').exists())

    def test_generation_rejects_mutable_source_or_unsupported_architecture(self):
        for revision, arch in (('main', 'aarch64'), ('a' * 40, 's390x')):
            with self.assertRaisesRegex(ValueError, 'pinned source and supported architecture'):
                runtime_config.generate_clh_config(Mock(), revision, arch)

    def test_smoke_matrix_includes_one_clh_kubeadm_workload(self):
        targets = json.loads((ROOT / 'obs/ci.json').read_text())['targets']
        jobs = [job for job in ci.smoke_matrix(targets)['include'] if job['profile'] == 'cloud-hypervisor']
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['k8s_distro'], jobs[0]['test_kind']), ('kubeadm', 'workload'))
