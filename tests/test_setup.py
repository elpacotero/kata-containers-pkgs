# SPDX-License-Identifier: MIT
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('kata_setup', Path(__file__).resolve().parents[1] / 'scripts/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class ManagedTemplates(unittest.TestCase):
    def test_extends_base_and_preserves_admin_configuration_on_reapply(self):
        original = '{{ template "base" . }}\n# administrator settings\n'
        once = setup.managed_block(original, '[table]\nvalue = 1\n')
        self.assertEqual(once, setup.managed_block(once, '[table]\nvalue = 1\n'))
        changed = setup.managed_block(once, '[table]\nvalue = 2\n')
        self.assertIn('# administrator settings', changed)
        self.assertIn('value = 2', changed)
        self.assertNotIn('value = 1', changed)

    def test_malformed_blocks_are_not_overwritten(self):
        for original in (setup.BEGIN, setup.END, setup.BEGIN + setup.END + setup.BEGIN + setup.END):
            with self.subTest(original=original), self.assertRaises(ValueError):
                setup.managed_block(original, 'test')


class DistroAdapters(unittest.TestCase):
    def test_bundled_distro_handlers_survive_repeat_apply_without_losing_admin_settings(self):
        import argparse
        import contextlib
        import io
        import tempfile
        import tomllib
        from unittest.mock import patch
        vendor = (Path(__file__).resolve().parents[1] / 'packaging/ubuntu-26.04/profiles/qemu.toml').read_text()
        for distro in ('k0s', 'k3s', 'microk8s', 'rke2'):
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                def mapped(value):
                    path = Path(value)
                    return path if str(path).startswith(str(root)) else root / str(path).lstrip('/')
                def write(path, text):
                    path = mapped(path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                    return path
                write('/usr/share/kata-containers/installed-profiles/qemu.json', '{}')
                write('/etc/containerd/conf.d/50-kata-qemu.toml', vendor)
                if distro in ('k3s', 'rke2'):
                    write(f'/var/lib/rancher/{distro}/agent/etc/containerd/config.toml', 'version = 3\n')
                    dest = write(f'/var/lib/rancher/{distro}/agent/etc/containerd/config-v3.toml.tmpl',
                                 '{{ template "base" . }}\n# administrator settings\n')
                    version = 3
                elif distro == 'microk8s':
                    dest = write('/var/snap/microk8s/current/args/containerd-template.toml',
                                 'version = 2\n# administrator settings\n')
                    version = 2
                else:
                    write('/etc/k0s/containerd.toml', '# k0s_managed=true\nversion = 2\n')
                    dest = mapped('/etc/k0s/containerd.d/50-kata-containers.toml')
                    version = 2
                args = argparse.Namespace(k8s_distro=distro, apply=True, dry_run=False, status=False)
                with patch.object(setup, 'Path', side_effect=mapped), contextlib.redirect_stdout(io.StringIO()):
                    setup.bundled_setup(args)
                    first = dest.read_text()
                    backups = list(dest.parent.glob('*.kata-backup-*'))
                    setup.bundled_setup(args)
                    self.assertEqual(first, dest.read_text())
                    self.assertEqual(backups, list(dest.parent.glob('*.kata-backup-*')))
                parsed = tomllib.loads(first.replace('{{ template "base" . }}', ''))
                plugin = 'io.containerd.cri.v1.runtime' if version == 3 else 'io.containerd.grpc.v1.cri'
                runtimes = parsed['plugins'][plugin]['containerd']['runtimes']
                self.assertEqual(set(runtimes), {'kata-qemu'})
                self.assertEqual(runtimes['kata-qemu']['runtime_type'], 'io.containerd.kata.v2')
                self.assertEqual(runtimes['kata-qemu']['options']['ConfigPath'],
                                 '/opt/kata/share/defaults/kata-containers/configuration-qemu.toml')
                if distro != 'k0s':
                    self.assertIn('# administrator settings', first)


class KubeadmSchemas(unittest.TestCase):
    def test_schema_four_migration_and_reapply_preserve_imports(self):
        import contextlib
        import io
        import json
        import sys
        import tempfile
        import tomllib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            config.write_text('version = 3\nimports = ["/etc/containerd/admin/*.toml"]\n')
            def mapped(value):
                path = Path(value)
                return path if str(path).startswith(str(root)) else root / str(path).lstrip('/')
            marker = mapped('/usr/share/kata-containers/installed-profiles/qemu.json')
            marker.parent.mkdir(parents=True)
            marker.write_text('{"handler": "kata-qemu"}')
            def command(*args):
                if args[-1] == 'migrate':
                    return 'version = 4\nimports = ["/etc/containerd/admin/*.toml"]\n[debug]\nlevel = "info"\n'
                self.assertEqual(args[-1], 'dump')
                return 'version = 4\n[plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu]\n'
            with patch.object(setup, 'Path', side_effect=mapped), patch.object(setup, 'CONFIG', config), \
                    patch.object(setup, 'command', side_effect=command) as invoke, \
                    patch.object(setup.os, 'geteuid', return_value=0), \
                    patch.object(sys, 'argv', ['setup', '--k8s-distro', 'kubeadm', '--apply']), \
                    contextlib.redirect_stdout(io.StringIO()):
                setup.main()
                written = config.read_text()
                self.assertEqual(tomllib.loads(written)['version'], 4)
                self.assertEqual(tomllib.loads(written)['imports'], ['/etc/containerd/admin/*.toml', setup.IMPORT])
                self.assertEqual(tomllib.loads(written)['debug']['level'], 'info')
                backups = list(root.glob('*.kata-backup-*'))
                invoke.reset_mock()
                setup.main()
                invoke.assert_not_called()
                self.assertEqual(config.read_text(), written)
                self.assertEqual(list(root.glob('*.kata-backup-*')), backups)
                sys.argv[-1] = '--status'
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    setup.main()
                self.assertTrue(json.loads(output.getvalue())['imports_configured'])
