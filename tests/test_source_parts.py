# SPDX-License-Identifier: MIT
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prepare_obs


class SourceParts(unittest.TestCase):
    def test_round_trip_bounded_architecture_parts_shared_by_both_recipes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'inputs.tar.zst'
            contents = bytes(range(256)) * 17
            original.write_bytes(contents)
            parts = prepare_obs.source_parts(original, '4.2.0', 'arm64', root, chunk_size=999)
            self.assertEqual(len(parts), 5)
            unpacked = root / 'unpacked'
            unpacked.mkdir()
            for part in parts:
                with tarfile.open(part) as archive:
                    members = archive.getmembers()
                    self.assertEqual(len(members), 1)
                    self.assertLessEqual(members[0].size, 999)
                    archive.extractall(unpacked, filter='data')
            joined = b''.join(path.read_bytes() for path in sorted(unpacked.glob('inputs-arm64-*/payload.part')))
            self.assertEqual(joined, contents)
            spec = prepare_obs.rpm_spec({'package_version': '4.2.0-3'}, parts)
            for index, path in enumerate(parts, 1):
                self.assertIn(f'Source{index}: {path.name}', spec)
                self.assertIn(f' -a{index}', spec)
            self.assertIn('cat inputs-${debarch}-*/payload.part', prepare_obs.BUILD)

    def test_empty_payload_and_invalid_architecture_fail_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'empty'
            source.touch()
            with self.assertRaisesRegex(ValueError, 'empty architecture'):
                prepare_obs.source_parts(source, '4.2.0', 'amd64', root)
            with self.assertRaisesRegex(ValueError, 'invalid input'):
                prepare_obs.source_parts(source, '4.2.0', 'riscv64', root)
