# SPDX-License-Identifier: MIT
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ci
import upstream
import import_bundle
import prepare_obs

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def run(identifier=20, conclusion='success', event='push'):
    return {'id': identifier, 'event': event, 'head_branch': 'main', 'status': 'completed',
            'conclusion': conclusion, 'head_sha': SHA, 'head_repository': {'full_name': upstream.REPOSITORY},
            'html_url': 'https://github.com/kata-containers/kata-containers/actions/runs/' + str(identifier)}


def artifact(architecture, expired=False):
    return {'id': 1 if architecture == 'amd64' else 2, 'name': 'kata-static-tarball-' + architecture,
            'expired': expired, 'size_in_bytes': 10, 'digest': DIGEST,
            'workflow_run': {'id': 20, 'head_sha': SHA}}


class Client:
    def __init__(self):
        self.runs = [run(10), run(20), run(30, conclusion='failure'), run(40, event='pull_request')]
        self.artifacts = [artifact('amd64'), artifact('arm64')]
    def api(self, path):
        return {'workflow_runs': self.runs}
    def pages(self, path, key):
        self.path = path
        return self.artifacts


class SuccessfulPayloadSelection(unittest.TestCase):
    def test_selects_latest_successful_push_and_both_architectures_from_one_run(self):
        client = Client()
        selected = upstream.development(client, ['x86_64', 'aarch64'])
        self.assertEqual(selected['run_id'], 20)
        self.assertEqual(selected['source_revision'], SHA)
        self.assertEqual(set(selected['bundles']), {'x86_64', 'aarch64'})
        self.assertIn('/20/artifacts', client.path)

    def test_incomplete_expired_or_ambiguous_bundle_does_not_fall_back_to_an_older_run(self):
        for artifacts in ([artifact('amd64')], [artifact('amd64'), artifact('arm64', True)],
                          [artifact('amd64'), artifact('amd64'), artifact('arm64')]):
            client = Client()
            client.artifacts = artifacts
            with self.subTest(artifacts=artifacts), self.assertRaises(upstream.NotReady):
                upstream.development(client, ['x86_64', 'aarch64'])

    def test_wrong_artifact_commit_is_rejected(self):
        client = Client()
        client.artifacts[0]['workflow_run']['head_sha'] = 'c' * 40
        with self.assertRaisesRegex(ValueError, 'different run or commit'):
            upstream.development(client, ['x86_64', 'aarch64'])

    def test_artifact_digest_is_required(self):
        client = Client()
        client.artifacts[0]['digest'] = None
        with self.assertRaises(ValueError):
            upstream.development(client, ['x86_64'])

    def test_deduplication_includes_packaging_revision_and_payload_identity(self):
        request = {'upstream': upstream.development(Client(), ['x86_64']), 'packaging_revision': SHA, 'targets': []}
        self.assertTrue(ci.same_inputs(request, copy.deepcopy(request)))
        changed = copy.deepcopy(request)
        changed['packaging_revision'] = 'c' * 40
        self.assertFalse(ci.same_inputs(request, changed))
        changed = copy.deepcopy(request)
        changed['upstream']['bundles']['x86_64']['digest'] = 'sha256:' + 'c' * 64
        self.assertFalse(ci.same_inputs(request, changed))

    def test_packaging_commit_change_with_identical_package_inputs_reuses_obs(self):
        request = {'upstream': upstream.development(Client(), ['x86_64']), 'packaging_revision': SHA,
                   'packaging_fingerprint': 'b' * 64, 'targets': []}
        changed = dict(request, packaging_revision='c' * 40)
        self.assertTrue(ci.same_inputs(request, changed))
        changed['packaging_fingerprint'] = 'c' * 64
        self.assertFalse(ci.same_inputs(request, changed))


class VerifiedDownloads(unittest.TestCase):
    def test_checksum_failure_leaves_no_accepted_payload(self):
        client = upstream.GitHub()
        with tempfile.TemporaryDirectory() as directory, patch.object(client.opener, 'open', return_value=io.BytesIO(b'wrong')):
            output = Path(directory) / 'bundle'
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                client.download({'url': 'https://api.github.com/test', 'digest': DIGEST, 'kind': 'release-asset'}, output)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix('.download').exists())

    def test_cross_host_redirect_drops_authorization(self):
        import urllib.request
        request = urllib.request.Request('https://api.github.com/test', headers={'Authorization': 'Bearer secret'})
        redirected = upstream.Redirect().redirect_request(request, None, 302, 'redirect', {}, 'https://example.com/archive')
        self.assertIsNone(redirected.get_header('Authorization'))

    def test_verified_artifact_unwraps_one_expected_bundle(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('kata-static.tar.zst', b'payload')
        contents = data.getvalue()
        client = upstream.GitHub()
        with tempfile.TemporaryDirectory() as directory, patch.object(client.opener, 'open', return_value=io.BytesIO(contents)):
            path = Path(directory) / 'bundle'
            result = client.download({'url': 'https://api.github.com/test', 'digest': 'sha256:' + hashlib.sha256(contents).hexdigest(), 'kind': 'actions-artifact'}, path)
            self.assertEqual(path.read_bytes(), b'payload')
            self.assertEqual(result, hashlib.sha256(b'payload').hexdigest())


class BundleSelection(unittest.TestCase):
    def test_qemu_subset_keeps_resolved_guest_links_and_excludes_other_profiles(self):
        names = ['opt/kata/runtime-rs/bin/containerd-shim-kata-v2',
                 'opt/kata/share/defaults/kata-containers/runtime-rs/configuration-qemu-runtime-rs.toml',
                 'opt/kata/bin/qemu-system-aarch64', 'opt/kata/libexec/virtiofsd',
                 'opt/kata/share/aavmf/AAVMF_CODE.fd', 'opt/kata/share/aavmf/AAVMF_VARS.fd',
                 'opt/kata/share/kata-containers/root_hash_base.txt',
                 'opt/kata/share/kata-containers/base.image', 'opt/kata/share/kata-containers/kernel',
                 'opt/kata/bin/cloud-hypervisor', 'opt/kata/share/kata-containers/nvidia.image']
        index = {name: tarfile.TarInfo(name) for name in names}
        for alias, target in (('kata-containers.img', 'base.image'), ('vmlinux.container', 'kernel')):
            name = 'opt/kata/share/kata-containers/' + alias
            member = tarfile.TarInfo(name)
            member.type, member.linkname = tarfile.SYMTYPE, target
            index[name] = member
        selected = import_bundle.component_members(index, 'aarch64', ('qemu',))
        self.assertIn('opt/kata/share/kata-containers/base.image', selected['rootfs-image'])
        self.assertIn('opt/kata/share/kata-containers/kernel', selected['kernel'])
        self.assertNotIn('opt/kata/bin/cloud-hypervisor', set().union(*selected.values()))
        index['opt/kata/share/kata-containers/kata-containers.img'].linkname = '../../../../etc/passwd'
        with self.assertRaisesRegex(ValueError, 'escapes'):
            import_bundle.component_members(index, 'aarch64', ('qemu',))

    def test_unsafe_archive_paths_are_rejected(self):
        for name in ('/etc/passwd', '../file', 'opt/../../file'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                import_bundle.name_of(tarfile.TarInfo(name))


class NativeRecipes(unittest.TestCase):
    def test_rpm_declares_all_packages_and_uses_offline_payload_staging(self):
        text = prepare_obs.rpm_spec({'package_version': '0.0.0-0.123.1'})
        self.assertIn('Release: 0.123.1.el10', text)
        self.assertNotIn('docker', text)
        self.assertNotIn('curl', text)
        self.assertEqual(text.count('%files'), 17)
        self.assertIn('kata-containers-virtiofsd = %{version}-%{release}', text)
        self.assertIn('--stage-only', text)


class Retention(unittest.TestCase):
    def test_retains_current_development_and_all_release_or_unrelated_artifacts(self):
        artifacts = [
            {'id': 1, 'name': 'development-build-1-1', 'workflow_run': {'id': 1}},
            {'id': 2, 'name': 'development-smoke-ubuntu-k3s-1-1', 'workflow_run': {'id': 1}},
            {'id': 3, 'name': 'development-build-2-1', 'workflow_run': {'id': 2}},
            {'id': 4, 'name': 'release-build-request-1-1', 'workflow_run': {'id': 1}},
            {'id': 5, 'name': 'development-build-unknown', 'workflow_run': None},
            {'id': 6, 'name': 'unrelated-artifact', 'workflow_run': {'id': 1}},
        ]
        self.assertEqual(ci.obsolete_development_artifacts(artifacts, '2'), [1, 2])
