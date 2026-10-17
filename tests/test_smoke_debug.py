# SPDX-License-Identifier: MIT
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import smoke
import smoke_debug
import smoke_qemu


class BothModes(unittest.TestCase):
    def test_debug_always_runs_and_both_must_succeed(self):
        for normal_ok, debug_ok in itertools.product((False, True), repeat=2):
            with self.subTest(normal=normal_ok, debug=debug_ok), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def workload(distro, folder):
                    if not {'normal': normal_ok, 'debug': debug_ok}[folder.name]:
                        raise ValueError('pod failed')
                with patch('smoke_debug.configure') as configure, patch('smoke_debug.verify_containerd'), \
                        patch('smoke_debug.collect') as collect, patch('kubernetes.wait_ready'), \
                        patch('smoke.subprocess.run'), patch('smoke.workload', side_effect=workload) as run:
                    if normal_ok and debug_ok:
                        smoke.workload_modes('kubeadm', root)
                    else:
                        with self.assertRaisesRegex(ValueError, 'smoke modes failed'):
                            smoke.workload_modes('kubeadm', root)
                    self.assertEqual([call.args[2] for call in configure.call_args_list], [False, True])
                    self.assertEqual(run.call_count, 2)
                    self.assertEqual(collect.call_count, 2)
                results = json.loads((root / 'mode-results.json').read_text())
                self.assertEqual(results['normal']['result'], 'passed' if normal_ok else 'failed')
                self.assertEqual(results['debug']['result'], 'passed' if debug_ok else 'failed')

    def test_failed_normal_configuration_still_attempts_debug(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('smoke_debug.configure', side_effect=[ValueError('config failed'), None]) as configure, \
                patch('smoke_debug.verify_containerd'), patch('smoke_debug.collect'), \
                patch('kubernetes.wait_ready'), patch('smoke.subprocess.run'), patch('smoke.workload') as run:
            with self.assertRaisesRegex(ValueError, 'normal'):
                smoke.workload_modes('kubeadm', Path(directory))
            self.assertEqual(configure.call_count, 2)
            self.assertEqual(run.call_count, 1)


class LoggingConfiguration(unittest.TestCase):
    def test_containerd_identification_ignores_multicall_server_parent(self):
        self.assertFalse(smoke_debug.containerd_command(['/usr/local/bin/k3s', 'server']))
        self.assertFalse(smoke_debug.containerd_command(['/var/lib/rancher/k3s/data/hash/bin/k3s-server']))
        self.assertTrue(smoke_debug.containerd_command(['containerd', '-c', '/config', '--log-level', 'debug']))
        self.assertTrue(smoke_debug.containerd_command(['/usr/bin/k3s', 'containerd', '-c', '/config']))
        self.assertTrue(smoke_debug.containerd_command(['containerd ']))

    def test_latest_startup_record_recovers_hidden_containerd_arguments(self):
        text = ('time="old" level=info msg="Running containerd -c /old --log-level info"\n'
                'time="new" level=info msg="Running containerd -c /current --log-level debug"\n')
        self.assertEqual(smoke_debug.containerd_startup_args(text),
                         ['containerd', '-c', '/current', '--log-level', 'debug'])
        with self.assertRaisesRegex(ValueError, 'no startup'):
            smoke_debug.containerd_startup_args('no containerd startup here')

    def test_qemu_helper_ignores_snap_python_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            qemu = Path(directory) / 'qemu'
            qemu.write_text('#!/bin/sh\nprintf "QEMU_PROBE_OK\\n"\n')
            qemu.chmod(0o755)
            helper = Path(smoke_qemu.__file__)
            env = dict(os.environ, PYTHONHOME='/nonexistent/snap-python', PYTHONPATH='/nonexistent/snap-site')
            result = subprocess.check_output([sys.executable, '-I', '-S', str(helper), '--qemu', str(qemu),
                                              '--output', directory, '--', '--version'], env=env, text=True)
            self.assertEqual(result.strip(), 'QEMU_PROBE_OK')

    def test_level_changes_preserve_other_tables_and_are_idempotent(self):
        original = 'version = 3\n[debug]\n  level = "info"\n  address = "socket"\n[plugins.test]\nvalue = true\n'
        debug = smoke_debug.set_debug_level(original, 'debug')
        self.assertEqual(tomllib.loads(debug), {'version': 3, 'debug': {'level': 'debug', 'address': 'socket'},
                                              'plugins': {'test': {'value': True}}})
        self.assertEqual(smoke_debug.set_debug_level(debug, 'debug'), debug)
        self.assertEqual(tomllib.loads(smoke_debug.set_debug_level(debug, 'info'))['debug']['level'], 'info')

    def test_every_distro_configures_kata_and_containerd_for_both_modes(self):
        for distro in ('kubeadm', 'k0s', 'k3s', 'microk8s', 'rke2'):
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def mapped(value):
                    path = Path(value)
                    return path if path.is_relative_to(root) else root / str(path).lstrip('/')
                base = mapped('/opt/kata/share/defaults/kata-containers/configuration-qemu.toml')
                base.parent.mkdir(parents=True)
                base.write_text('[hypervisor.qemu]\nkernel_params = \"cgroup_no_v1=all\"\n'
                                'path = "/opt/kata/bin/qemu-system-x86_64"\n')
                (base.parent / 'config.d').mkdir()
                for filename in ('/etc/containerd/config.toml', '/etc/k0s/containerd.toml',
                                 '/var/snap/microk8s/current/args/containerd-template.toml'):
                    path = mapped(filename)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('version = 3\n[debug]\nlevel = "info"\n')
                unit = mapped('/etc/systemd/system/k0scontroller.service')
                unit.parent.mkdir(parents=True)
                unit.write_text('[Service]\nExecStart=/usr/local/bin/k0s controller --single '
                                '--logging=containerd=debug,etcd=info\n')
                for debug in (False, True, False):
                    output = root / ('debug' if debug else 'normal')
                    output.mkdir(exist_ok=True)
                    with patch.object(smoke_debug, 'Path', side_effect=mapped), patch('smoke_debug.subprocess.run'):
                        smoke_debug.configure(distro, output, debug)
                    kata = tomllib.loads((output / 'kata-debug.toml').read_text())
                    level = 'debug' if debug else 'info'
                    for section in (kata['hypervisor']['qemu'], kata['runtime'], kata['agent']['kata']):
                        self.assertEqual(section['enable_debug'], debug)
                        self.assertEqual(section['log_level'], level)
                    self.assertEqual(kata['agent']['kata']['debug_console_enabled'], debug)
                    effective = json.loads((output / 'kata-effective-config.json').read_text())
                    params = effective['hypervisor']['qemu']['kernel_params'].split()
                    self.assertIn('cgroup_no_v1=all', params)
                    self.assertEqual('agent.log=debug' in params, debug)
                    self.assertEqual('initcall_debug' in params, debug)
                    hypervisor = effective['hypervisor']['qemu']
                    self.assertEqual(hypervisor['path'].endswith('/qemu-debug'), debug)
                    if debug:
                        self.assertIn(hypervisor['path'], hypervisor['valid_hypervisor_paths'])
                    if distro in ('k3s', 'rke2'):
                        self.assertIn('CONTAINERD_LOG_LEVEL=' + level, (output / 'containerd-logging.conf').read_text())
                    else:
                        self.assertEqual(tomllib.loads((output / 'containerd-config.toml').read_text())['debug']['level'], level)
                    if distro == 'k0s':
                        self.assertIn('containerd=' + level + ',etcd=info', unit.read_text())

    def test_later_dropin_disabling_component_debug_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'configuration-qemu.toml'
            config.write_text('[hypervisor.qemu]\nenable_debug = true\nlog_level = "debug"\n'
                              'kernel_params = "agent.log=debug initcall_debug"\n'
                              '[runtime]\nenable_debug = true\nlog_level = "debug"\n'
                              '[agent.kata]\nenable_debug = true\nlog_level = "debug"\ndebug_console_enabled = true\n')
            folder = root / 'config.d';folder.mkdir()
            (folder / '50-override.toml').write_text('[runtime]\nenable_debug = false\n')
            with self.assertRaisesRegex(ValueError, 'component debug'):
                smoke_debug.verify_kata_config(config, root, True)

    def test_early_console_preserves_qemu_arguments_and_version_probes(self):
        args = ['-kernel', '/kernel', '-append', 'root=/dev/pmem0p1 console=hvc0',
                '-qmp', 'unix:fd=16,server=on,wait=off', '-nodefaults']
        result = smoke_qemu.command('/qemu', args, Path('/diagnostics'), 42)
        self.assertEqual(args[3], 'root=/dev/pmem0p1 console=hvc0')
        self.assertIn('root=/dev/pmem0p1 console=hvc0 console=ttyS0,115200', result[4])
        self.assertIn('earlycon=uart8250,io,0x3f8,115200', result[4])
        self.assertIn('unix:fd=16,server=on,wait=off', result)
        self.assertIn('file:/diagnostics/guest-kernel-42.log', result)
        self.assertEqual(smoke_qemu.command('/qemu', ['--version'], Path('/unused'), 42),
                         ['/qemu', '--version'])
