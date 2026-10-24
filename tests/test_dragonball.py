# SPDX-License-Identifier: MIT
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import assemble
import ci
import import_bundle
import packages
import prepare_obs
import smoke_debug
import test_nvidia_cpu


class Dragonball(unittest.TestCase):
    def test_import_keeps_dedicated_kernel_and_resolves_its_link(self):
        base = 'opt/kata/share/kata-containers/'
        index = test_nvidia_cpu.NvidiaAssets().index()
        config = 'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-dragonball.toml'
        index[config] = tarfile.TarInfo(config)
        kernel = tarfile.TarInfo(base + 'vmlinux-dragonball-experimental.container')
        kernel.type, kernel.linkname = tarfile.SYMTYPE, 'dragonball.kernel'
        index[kernel.name] = kernel
        index[base + kernel.linkname] = tarfile.TarInfo(base + kernel.linkname)
        selected = import_bundle.component_members(index, 'aarch64', ['qemu', 'dragonball'])
        self.assertIn(config, selected['shim-v2-rust'])
        self.assertEqual(selected['kernel-dragonball-experimental'], {kernel.name, base + kernel.linkname})
        self.assertNotIn('kernel-dragonball-experimental', import_bundle.component_members(index, 'aarch64', ['qemu']))
        del index[base + kernel.linkname]
        with self.assertRaisesRegex(ValueError, 'missing required kernel-dragonball-experimental'):
            import_bundle.component_members(index, 'aarch64', ['dragonball'])

    def test_dependencies_and_native_recipes_use_integrated_vmm(self):
        self.assertEqual(set(packages.installed_packages('dragonball')), {
            'kata-containers-core', 'kata-containers-dragonball',
            'kata-containers-rootfs-generic', 'kata-containers-kernel-dragonball'})
        self.assertEqual(assemble.PAYLOADS['dragonball'], ())
        for arch in ('amd64', 'arm64'):
            self.assertIn('dragonball', packages.profiles_for_arch(arch))
        spec = prepare_obs.rpm_spec({'package_version': '4.3.1-2'})
        section = spec.split('%package -n kata-containers-dragonball\n')[1].split('%description')[0]
        self.assertIn('Requires: kata-containers-kernel-dragonball = %{version}-%{release}', section)
        self.assertNotIn('Requires: kata-containers-qemu', section)
        self.assertNotIn('Requires: kata-containers-virtiofsd', section)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'debian'
            prepare_obs.debian_files({'package_version': '4.3.1-2'}, destination)
            self.assertIn('Package: kata-containers-dragonball\nArchitecture: any',
                          (destination / 'control').read_text())

    def test_staged_profile_registers_its_own_config_and_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.toml'
            source.write_text('[hypervisor.dragonball]\nshared_fs = "inline-virtio-fs"\n')
            config = assemble.stage_runtime_config(source, root, 'dragonball')
            subprocess.run([sys.executable, str(ROOT / 'scripts/stage_profile.py'), 'dragonball',
                            '--format', 'rpm', '--architecture', 'arm64', '--version', '4.3.1-2',
                            '--root', str(root)], check=True, capture_output=True)
            self.assertTrue(config.is_symlink())
            self.assertEqual(config.readlink().as_posix(), 'runtimes/dragonball/configuration-dragonball.toml')
            self.assertTrue((config.resolve().parent / 'config.d').is_dir())
            registration = tomllib.loads((root / 'usr/share/kata-containers/containerd/dragonball.toml').read_text())
            runtime = registration['plugins']['io.containerd.cri.v1.runtime']['containerd']['runtimes']['kata-dragonball']
            self.assertEqual(runtime['options']['ConfigPath'], '/' + str(config.relative_to(root)))
            self.assertEqual(runtime['runtime_path'], '/opt/kata/bin/containerd-shim-kata-v2')
            control = (root / 'DEBIAN/control').read_text()
            self.assertIn('kata-containers-kernel-dragonball (= 4.3.1-2)', control)
            self.assertNotIn('kata-containers-virtiofsd', control)

    def test_debug_configures_dragonball_without_qemu_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def mapped(value):
                path = Path(value)
                return path if path.is_relative_to(root) else root / str(path).lstrip('/')
            config = mapped('/opt/kata/share/defaults/kata-containers/configuration-dragonball.toml')
            config.parent.mkdir(parents=True)
            config.write_text('[hypervisor.dragonball]\npath = ""\nkernel_params = "console=ttyS1"\n')
            (config.parent / 'config.d').mkdir()
            containerd = mapped('/etc/containerd/config.toml')
            containerd.parent.mkdir(parents=True)
            containerd.write_text('version = 3\n')
            for debug in (False, True, False):
                output = root / ('debug' if debug else 'normal')
                output.mkdir(exist_ok=True)
                with patch.object(smoke_debug, 'Path', side_effect=mapped), patch('smoke_debug.subprocess.run'):
                    smoke_debug.configure('kubeadm', output, debug, profile='dragonball')
                effective = json.loads((output / 'kata-effective-config.json').read_text())
                self.assertEqual(set(effective['hypervisor']), {'dragonball'})
                hypervisor = effective['hypervisor']['dragonball']
                self.assertEqual(hypervisor['enable_debug'], debug)
                self.assertEqual(hypervisor['path'], '')
                self.assertIn('console=ttyS1', hypervisor['kernel_params'])
                self.assertFalse((config.parent / 'config.d/40-early-console.toml').exists())

    def test_generic_verity_is_bound_to_the_selected_image(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'configuration.toml'
            config.write_text('[hypervisor.dragonball]\nshared_fs = "inline-virtio-fs"\n[runtime]\n')
            assemble.stage_guest_verity(config, 'dragonball', 'root_hash=correct')
            text = config.read_text()
            self.assertEqual(tomllib.loads(text)['hypervisor']['dragonball']['kernel_verity_params'], 'root_hash=correct')
            assemble.stage_guest_verity(config, 'dragonball', 'root_hash=correct')
            self.assertEqual(config.read_text(), text)
            with self.assertRaisesRegex(ValueError, 'verity measurement mismatch'):
                assemble.stage_guest_verity(config, 'dragonball', 'root_hash=changed')

    def test_smoke_matrix_adds_one_kubeadm_workload_and_preserves_legacy_requests(self):
        targets = json.loads((ROOT / 'obs/ci.json').read_text())['targets']
        jobs = [job for job in ci.smoke_matrix(targets)['include'] if job['profile'] == 'dragonball']
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]['k8s_distro'], jobs[0]['test_kind']), ('kubeadm', 'workload'))
        self.assertFalse(any(job['profile'] == 'dragonball' for job in ci.smoke_matrix(targets, profiles=['qemu'])['include']))
