# SPDX-License-Identifier: MIT
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import assemble
import ci
import import_bundle
import packages
import prepare_obs
import test_nvidia_cpu


class NvidiaGPU(unittest.TestCase):
    def test_gpu_import_resolves_driver_extension_and_keeps_cpu_base(self):
        index = test_nvidia_cpu.NvidiaAssets().index()
        base = 'opt/kata/share/kata-containers/'
        for name in (base + 'root_hash_nvidia-gpu-extension.txt',
                     'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-nvidia-gpu-runtime-rs.toml'):
            index[name] = tarfile.TarInfo(name)
        alias = tarfile.TarInfo(base + 'kata-containers-nvidia-gpu-extension.img')
        alias.type, alias.linkname = tarfile.SYMTYPE, 'gpu-extension.image'
        index[alias.name] = alias
        selected = import_bundle.component_members(index, 'aarch64')
        self.assertIn(base + 'gpu-extension.image', selected['rootfs-image-nvidia-gpu-extension'])
        self.assertIn('rootfs-image-nvidia', selected)
        self.assertIn('qemu-no-shared-fs', selected)
        del index[base + 'root_hash_nvidia-gpu-extension.txt']
        with self.assertRaisesRegex(ValueError, 'missing required rootfs-image-nvidia-gpu-extension'):
            import_bundle.component_members(index, 'aarch64')

    def test_gpu_reuses_cpu_base_with_six_packages_and_no_generic_guest(self):
        self.assertEqual(set(packages.installed_packages('nvidia-gpu')), {
            'kata-containers-core', 'kata-containers-nvidia-cpu', 'kata-containers-nvidia-gpu',
            'kata-containers-rootfs-nvidia', 'kata-containers-kernel-nvidia',
            'kata-containers-rootfs-nvidia-gpu'})
        self.assertEqual(len(packages.build_packages()), 17)
        text = prepare_obs.rpm_spec({'package_version': '4.2.0-3'})
        section = text.split('%package -n kata-containers-nvidia-gpu\n')[1].split('%description')[0]
        self.assertIn('Requires: kata-containers-nvidia-cpu = %{version}-%{release}', section)
        self.assertIn('Requires: kata-containers-rootfs-nvidia-gpu = %{version}-%{release}', section)
        self.assertEqual(packages.PROFILES['nvidia-gpu']['modules'], ['vfio', 'vfio_pci', 'iommufd'])

    def test_gpu_uses_private_firmware_without_changing_generic_qemu(self):
        for name, firmware in (('ovmf', 'OVMF.fd'), ('aavmf', 'AAVMF_CODE.fd')):
            with self.subTest(architecture=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                gpu, generic = root / 'gpu', root / 'generic'
                relative = Path('opt/kata/share') / name / firmware
                for stage in (gpu, generic):
                    path = stage / relative
                    path.parent.mkdir(parents=True)
                    path.write_bytes(b'firmware')
                config = gpu / 'config.toml'
                config.write_text(f'firmware = "/{relative}"\nfirmware_volume = "@FIRMWAREVOLUMEPATH_NV@"\n')
                assemble.stage_gpu_firmware(gpu, config)
                self.assertTrue((generic / relative).is_file())
                self.assertFalse((gpu / relative).exists())
                private = Path('opt/kata/share/kata-nvidia-gpu/firmware') / name / firmware
                self.assertEqual((gpu / private).read_bytes(), b'firmware')
                self.assertIn('/' + str(private), config.read_text())
                self.assertIn('firmware_volume = ""', config.read_text())
                self.assertNotIn('@', config.read_text())

    def test_gpu_extension_measurement_must_match_its_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / 'opt/kata/share/kata-containers'
            base.mkdir(parents=True)
            (base / 'kata-containers-nvidia-gpu-extension.img').write_bytes(b'image')
            (base / 'root_hash_nvidia-gpu-extension.txt').write_text('root_hash=correct\n')
            config = {'guest_extension_images': [{'name': 'gpu',
                'path': '/opt/kata/share/kata-containers/kata-containers-nvidia-gpu-extension.img',
                'verity_params': 'root_hash=correct'}]}
            assemble.validate_gpu_extension(config, root)
            config['guest_extension_images'][0]['verity_params'] = 'root_hash=wrong'
            with self.assertRaisesRegex(ValueError, 'measurement mismatch'):
                assemble.validate_gpu_extension(config, root)
            config['guest_extension_images'] = []
            with self.assertRaisesRegex(ValueError, 'exactly one GPU guest extension'):
                assemble.validate_gpu_extension(config, root)

    def test_gpu_ci_is_installation_only_and_legacy_requests_do_not_gain_a_gpu_job(self):
        targets = json.loads((ROOT / 'obs/ci.json').read_text())['targets']
        jobs = ci.smoke_matrix(targets)['include']
        gpu = [job for job in jobs if job['profile'] == 'nvidia-gpu']
        self.assertEqual(len(gpu), 1)
        self.assertEqual(gpu[0]['test_kind'], 'installation')
        self.assertEqual(gpu[0]['arch'], 'x86_64')
        self.assertTrue(all(job['test_kind'] == 'workload' for job in jobs if job['profile'] not in ('nvidia-gpu', 'nvidia-gpu-tee')))
        self.assertEqual(len(ci.smoke_matrix(targets, profiles=['qemu', 'nvidia-cpu'])['include']), 6)
