# SPDX-License-Identifier: MIT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci
import smoke

A, B, C = "a" * 40, "b" * 40, "c" * 40
TARGETS = [{"id": "ubuntu", "repository": "Ubuntu_26.04", "arch": "x86_64", "format": "deb"},
           {"id": "alma", "repository": "AlmaLinux_10.2", "arch": "aarch64", "format": "rpm"}]


def request(channel="release"):
    return {"schema_version": 1, "channel": channel, "source_ref": "refs/tags/4.2.0",
            "source_repository": "https://github.com/kata-containers/kata-containers.git",
            "source_revision": A, "packaging_revision": B, "packaging_repository": "example/packages",
            "package_version": "0.0.0-0.123.1", "request_id": "test-123"}


class SourceSelection(unittest.TestCase):
    def test_latest_stable_across_series_and_peeled_annotated_tag(self):
        refs = f"{A} refs/tags/4.9.9\n{B} refs/tags/v4.10.0\n{C} refs/tags/v4.10.0^{{}}\n{A} refs/tags/5.0.0-rc1\n"
        self.assertEqual(ci.select_source("release", refs), ("refs/tags/v4.10.0", C))

    def test_only_stable_tags_are_selected(self):
        with self.assertRaisesRegex(ValueError, "no stable"):
            ci.select_source("release", f"{A} refs/tags/v5.0.0-rc1\n{B} refs/tags/4.2.0-dev\n")

    def test_development_explicitly_resolves_upstream_main(self):
        self.assertEqual(ci.select_source("development", f"{A} refs/heads/main\n{B} refs/heads/stable-4.2"),
                         ("refs/heads/main", A))

    def test_ambiguous_latest_tags_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            ci.select_source("release", f"{A} refs/tags/4.2.0\n{B} refs/tags/v4.2.0")

    def test_mutable_revisions_and_invalid_versions_are_rejected(self):
        for field, value in (("source_revision", "main"), ("packaging_revision", "latest"),
                             ("channel", "invalid"), ("package_version", "0.0.0;echo bad")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ci.validate_request(dict(request(), **{field: value}))


class FakeOBS(ci.OBS):
    def __init__(self):
        self.project, self.package = "home:test", "kata-containers"
        self.current = request()
        self.md5 = "1234"
        self.jobs = {target["id"]: {"srcmd5": self.md5, "code": "succeeded"} for target in TARGETS}

    def source(self):
        return self.current, self.md5

    def ensure_package(self):
        pass

    def history(self, target, source_md5):
        return self.jobs[target["id"]]


class BuildMonitoring(unittest.TestCase):
    def test_latest_release_skips_only_after_every_target_succeeds(self):
        client = FakeOBS()
        self.assertTrue(ci.release_built(request(), client, TARGETS))
        client.jobs["alma"] = None
        self.assertFalse(ci.release_built(request(), client, TARGETS))
        client.jobs["alma"] = {"code": "failed"}
        self.assertFalse(ci.release_built(request(), client, TARGETS))

    def test_alias_of_an_already_built_tag_is_skipped(self):
        client = FakeOBS()
        client.current = dict(request(), source_ref="refs/tags/v4.2.0")
        self.assertTrue(ci.release_built(request(), client, TARGETS))

    def test_new_release_is_not_hidden_by_old_success(self):
        client = FakeOBS()
        client.current = dict(request(), source_ref="refs/tags/4.1.0")
        self.assertFalse(ci.release_built(request(), client, TARGETS))

    def test_retagged_stable_release_requires_investigation(self):
        client = FakeOBS()
        client.current = dict(request(), source_revision=C)
        with self.assertRaisesRegex(ValueError, "tag moved"):
            ci.release_built(request(), client, TARGETS)

    def test_wait_returns_receipt_for_the_exact_request(self):
        receipt = FakeOBS().wait(request(), TARGETS, timeout=1, interval=0)
        self.assertEqual(receipt["request_id"], "test-123")
        self.assertEqual(len(receipt["targets"]), 2)

    def test_failed_target_fails_monitoring(self):
        client = FakeOBS()
        client.jobs["alma"] = {"code": "failed"}
        with self.assertRaisesRegex(RuntimeError, "alma failed"):
            client.wait(request(), TARGETS, timeout=1, interval=0)

    def test_superseded_request_does_not_report_stale_success(self):
        client = FakeOBS()
        client.jobs["alma"] = None
        with patch.object(client, "source", side_effect=[(request(), "1234"), (dict(request(), request_id="other"), "5678")]), patch("ci.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                client.wait(request(), TARGETS, timeout=1, interval=0)

    def test_job_history_matches_source_revision_and_latest_attempt(self):
        client = object.__new__(ci.OBS)
        client.project, client.package = "test", "kata"
        xml = b'<jobhistlist><jobhist srcmd5="old" code="succeeded" endtime="30"/><jobhist srcmd5="new" code="succeeded" endtime="10"/><jobhist srcmd5="new" code="failed" endtime="20"/></jobhistlist>'
        with patch.object(client, "api", return_value=xml):
            self.assertEqual(client.history(TARGETS[0], "new")["code"], "failed")
            self.assertIsNone(client.history(TARGETS[0], "missing"))


class StableVersionRetention(unittest.TestCase):
    def test_stable_version_matches_tag_and_does_not_depend_on_workflow_run(self):
        for tag in ('refs/tags/4.2.0', 'refs/tags/v4.2.0'):
            self.assertEqual(ci.package_version('release', tag, '0.0.0-0.1', '123', '2'), '4.2.0-1')
        self.assertEqual(ci.package_version('development', 'refs/heads/main', '0.0.0-0.1', '123', '2'),
                         '0.0.0-0.123.2')
        with self.assertRaises(ValueError):
            ci.package_version('release', 'refs/tags/4.2.0-rc1', '0.0.0-0.1', '123', '1')

    def test_stable_versions_have_distinct_containers_and_development_reuses_one(self):
        settings = {'channels': {channel: {'project': 'home:test', 'package': 'kata-containers'}
                                for channel in ('release', 'development')}}
        first = ci.destination_for(settings, dict(request(), package_version='4.2.0-1'))
        second = ci.destination_for(settings, dict(request(), package_version='4.3.0-1'))
        self.assertEqual(first['package'], 'kata-containers-4.2.0')
        self.assertNotEqual(first, second)
        self.assertEqual(ci.destination_for(settings, request('development'))['package'], 'kata-containers')
        self.assertEqual(settings['channels']['release']['package'], 'kata-containers')

    def test_package_creation_never_overwrites_existing_release_metadata(self):
        client = object.__new__(ci.OBS)
        client.project, client.package = 'home:test', 'kata-containers-4.2.0'
        with patch.object(client, 'api', return_value=b'<package/>') as api:
            client.ensure_package()
        self.assertEqual(api.call_count, 1)
        with patch.object(client, 'api', side_effect=[None, b'']) as api:
            client.ensure_package()
        metadata = ET.fromstring(api.call_args.kwargs['data'])
        self.assertEqual(metadata.get('name'), 'kata-containers-4.2.0')
        self.assertIsNotNone(metadata.find('publish/disable'))

    def test_publication_preserves_other_package_metadata_and_is_idempotent(self):
        client = object.__new__(ci.OBS)
        client.project, client.package = 'home:test', 'kata-containers-4.2.0'
        original = b'<package name="kata-containers-4.2.0"><title>Keep me</title><build><enable/></build><publish><disable/></publish></package>'
        with patch.object(client, 'api', side_effect=[original, b'']) as api:
            client.publish()
        metadata = ET.fromstring(api.call_args.kwargs['data'])
        self.assertIsNotNone(metadata.find('publish/enable'))
        self.assertIsNone(metadata.find('publish/disable'))
        self.assertEqual(metadata.findtext('title'), 'Keep me')
        self.assertIsNotNone(metadata.find('build/enable'))
        with patch.object(client, 'api', return_value=ET.tostring(metadata)) as api:
            client.publish()
        self.assertEqual(api.call_count, 1)

    def test_completion_publishes_only_after_provenance_and_build_checks(self):
        from unittest.mock import Mock
        current = dict(request(), targets=TARGETS)
        receipt = {'request_id': current['request_id'], 'project': 'home:test',
                   'package': 'kata-containers-4.2.0', 'source_md5': '1234'}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'build-request.json').write_text(json.dumps(current))
            (folder / 'obs-build.json').write_text(json.dumps(receipt))
            for source, jobs, expected in [(current, [{}], True),
                    (dict(current, request_id='different'), [{}], False), (current, None, False)]:
                client = Mock()
                client.source.return_value = (source, '1234')
                client.completed.return_value = jobs
                with patch('ci.OBS', return_value=client), patch('ci.write_development_state') as state, \
                        patch('ci.git', return_value=A), patch('ci.input_fingerprint', return_value='d' * 64):
                    if expected:
                        ci.finish_development(folder, channel='release')
                        client.publish.assert_called_once_with()
                        state.assert_called_once()
                    else:
                        with self.assertRaises(ValueError):
                            ci.finish_development(folder, channel='release')
                        client.publish.assert_not_called()
                        state.assert_not_called()

    def test_failed_publication_does_not_mark_release_complete(self):
        from unittest.mock import Mock
        current = dict(request(), targets=TARGETS)
        receipt = {'request_id': current['request_id'], 'project': 'home:test',
                   'package': 'kata-containers-4.2.0', 'source_md5': '1234'}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'build-request.json').write_text(json.dumps(current))
            (folder / 'obs-build.json').write_text(json.dumps(receipt))
            client = Mock()
            client.source.return_value = (current, '1234')
            client.completed.return_value = [{}]
            client.publish.side_effect = RuntimeError('publication failed')
            with patch('ci.OBS', return_value=client), patch('ci.write_development_state') as state:
                with self.assertRaisesRegex(RuntimeError, 'publication failed'):
                    ci.finish_development(folder, channel='release')
                state.assert_not_called()


class ReleaseSmokeCoordination(unittest.TestCase):
    def exercise(self, folder, state=None, rebuild=False):
        upstream = {'kind': 'release', 'source_ref': 'refs/tags/4.2.0',
                    'source_revision': A, 'url': 'https://github.com/example/release',
                    'bundles': {}}
        previous = dict(request(), upstream=upstream, targets=TARGETS, package_version='4.2.0-2', profiles=['qemu','nvidia-cpu'],
                        packaging_fingerprint='d' * 64)
        client = FakeOBS()
        client.current = previous
        argv = ['ci.py', 'release', '--output', str(folder)]
        if rebuild:
            argv += ['--rebuild-version', '4.2.0-2']
        with patch('sys.argv', argv), \
                patch.dict('os.environ', {'OBS_ENABLED': 'true', 'SMOKE_ENABLED': 'true',
                    'OBS_TARGETS_JSON': json.dumps(TARGETS)}), \
                patch('upstream.GitHub'), patch('upstream.release', return_value=upstream), \
                patch('ci.git', return_value=A), \
                patch('ci.select_source', return_value=(upstream['source_ref'], A)), \
                patch('ci.input_fingerprint', return_value='d' * 64), \
                patch('ci.read_development_state', return_value=state) as read_state, \
                patch('ci.OBS', return_value=client), patch('ci.output') as output, \
                patch('prepare_obs.prepare', return_value={}) as prepare, \
                patch.object(client, 'configure') as configure, patch.object(client, 'submit'), \
                patch.object(client, 'publish') as publish, patch.object(client, 'wait', return_value={}):
            ci.main()
        if rebuild:
            prepare.assert_called_once()
            read_state.assert_not_called()
            publish.assert_called_once_with(enabled=False)
            configure.assert_called_once_with(TARGETS, '4.2.0-2')
        else:
            prepare.assert_not_called()
            read_state.assert_called_once_with(channel='release')
        return previous, output

    def test_explicit_rebuild_bypasses_tested_state_and_successful_build_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            _, output = self.exercise(Path(directory), state={'already': 'tested'}, rebuild=True)
            self.assertIn(unittest.mock.call('builds_ready', 'true'), output.call_args_list)
            self.assertNotIn(unittest.mock.call('skipped', 'true'), output.call_args_list)

    def test_successful_stable_build_is_reused_for_missing_smoke_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            previous, output = self.exercise(folder)
            self.assertEqual(json.loads((folder / 'build-request.json').read_text()), previous)
            self.assertEqual(json.loads((folder / 'obs-build.json').read_text())['source_md5'], '1234')
            self.assertIn(unittest.mock.call('builds_ready', 'true'), output.call_args_list)

    def test_tested_stable_build_skips_both_build_and_smoke(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            previous, _ = self.exercise(folder)
            (folder / 'obs-build.json').unlink()
            _, output = self.exercise(folder, {'build_request': previous,
                                                'smoke_fingerprint': 'd' * 64})
            self.assertIn(unittest.mock.call('skipped', 'true'), output.call_args_list)
            self.assertNotIn(unittest.mock.call('builds_ready', 'true'), output.call_args_list)
            self.assertFalse((folder / 'obs-build.json').exists())


class SourceSubmission(unittest.TestCase):
    def client(self):
        client = object.__new__(ci.OBS)
        client.project, client.package = 'home:test', 'kata-containers'
        return client

    def test_upload_stages_files_and_commits_one_complete_validated_file_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'recipe.spec').write_bytes(b'recipe')
            (root / 'payload-sizes.json').write_text('{}')
            client = self.client()
            with patch.object(client, 'api') as api:
                client.submit(root)
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args_list[0].kwargs['rev'], 'upload')
            self.assertEqual(api.call_args_list[0].kwargs['method'], 'PUT')
            commit = api.call_args_list[1].kwargs
            self.assertEqual(commit['cmd'], 'commitfilelist')
            self.assertEqual(commit['withvalidate'], 1)
            entries = ET.fromstring(commit['data']).findall('entry')
            self.assertEqual([entry.get('name') for entry in entries], ['recipe.spec'])
            import hashlib
            self.assertEqual(entries[0].get('hash'), 'sha256:' + hashlib.sha256(b'recipe').hexdigest())

    def test_scm_sync_is_rejected_before_any_mutation(self):
        client = self.client()
        with patch.object(client, 'api', return_value=b'<package><scmsync>url</scmsync></package>') as api:
            with self.assertRaisesRegex(ValueError, 'SCM Sync'):
                client.configure(TARGETS, "0.0.0-0.123.1")
            self.assertEqual(api.call_count, 1)

    def test_project_setup_preserves_publication_and_source_release(self):
        client = self.client()
        values = [b'<package><scmsync/></package>', b'<project name="home:test"><publish><disable/></publish></project>',
                  b'', b'# existing configuration\n# Kata packaging: preserve the CI-selected source release.\nRelease: <CI_CNT>\n', b'']
        with patch.object(client, 'api', side_effect=values) as api:
            client.configure(TARGETS, "0.0.0-0.123.1")
        project = ET.fromstring(api.call_args_list[2].kwargs['data'])
        self.assertEqual(len(project.findall('repository')), 2)
        self.assertIsNotNone(project.find('publish/disable'))
        config = api.call_args_list[4].kwargs['data']
        self.assertIn(b'# existing configuration', config)
        self.assertTrue(config.endswith(b'Release: <CI_CNT> spec:<SPEC_REL>\n'))
        self.assertEqual(config.count(b'Release:'), 1)

    def test_stable_project_keeps_all_versions_in_published_metadata(self):
        client = self.client()
        values = [b'<package/>', b'<project name="home:test"><publish><disable/></publish></project>',
                  b'', b'PublishFlags: archsync\n', b'']
        with patch.object(client, 'api', side_effect=values) as api:
            client.configure(TARGETS, '4.2.0-1')
        config = api.call_args_list[4].kwargs['data']
        self.assertIn(b'PublishFlags: archsync', config)
        self.assertIn(b'PublishFlags: !singleexport', config)
        self.assertIn(b'Release: <CI_CNT> spec:<SPEC_REL>', config)


class BuildInputFingerprints(unittest.TestCase):
    def test_docs_and_smoke_changes_do_not_invalidate_native_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', str(root)], check=True, capture_output=True)
            def git(*args):
                return ci.git(*args, cwd=root)
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.com')
            (root / 'scripts').mkdir()
            for name in ('README.md', 'scripts/setup.py', 'scripts/smoke.py'):
                (root / name).write_text('initial')
            git('add', '.')
            git('commit', '-m', 'initial')
            packages = ci.input_fingerprint(ci.PACKAGE_INPUTS, root=root)
            smoke = ci.input_fingerprint(ci.SMOKE_INPUTS, root=root)
            (root / 'README.md').write_text('documentation update')
            git('add', '.')
            git('commit', '-m', 'docs')
            self.assertEqual(ci.input_fingerprint(ci.PACKAGE_INPUTS, root=root), packages)
            self.assertEqual(ci.input_fingerprint(ci.SMOKE_INPUTS, root=root), smoke)
            (root / 'scripts/smoke.py').write_text('test update')
            git('add', '.')
            git('commit', '-m', 'smoke')
            self.assertEqual(ci.input_fingerprint(ci.PACKAGE_INPUTS, root=root), packages)
            self.assertNotEqual(ci.input_fingerprint(ci.SMOKE_INPUTS, root=root), smoke)
            (root / 'scripts/setup.py').write_text('installed binary update')
            git('add', '.')
            git('commit', '-m', 'package')
            self.assertNotEqual(ci.input_fingerprint(ci.PACKAGE_INPUTS, root=root), packages)


class DevelopmentState(unittest.TestCase):
    def test_snapshot_is_pinned_and_second_update_is_fast_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            root, origin = Path(directory) / "repo", Path(directory) / "origin.git"
            subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
            subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
            def git(*args, **kwargs):
                return ci.git(*args, cwd=root, **kwargs)
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            (root / "config.json").write_text('{}\n')
            (root / "code").write_text('original\n')
            git("add", ".")
            git("commit", "-m", "initial")
            revision = git("rev-parse", "HEAD")
            git("remote", "add", "origin", str(origin))
            state = {'build_request': dict(request('development'), packaging_revision=revision)}
            ci.write_development_state(state, root)
            self.assertEqual(ci.read_development_state(root), state)
            first = git('rev-parse', 'FETCH_HEAD')
            next_state = {'build_request': dict(state['build_request'], request_id='second')}
            ci.write_development_state(next_state, root)
            self.assertEqual(ci.read_development_state(root), next_state)
            second = git('rev-parse', 'FETCH_HEAD')
            self.assertEqual(git('rev-parse', second + '^1'), first)
            self.assertEqual(git('ls-tree', '--name-only', second), 'state.json')
            self.assertEqual(git('status', '--porcelain'), '')
            stable = {'build_request': dict(request(), packaging_revision=revision)}
            ci.write_development_state(stable, root, channel='release')
            self.assertEqual(ci.read_development_state(root, channel='release'), stable)
            self.assertEqual(ci.read_development_state(root), next_state)



class SmokeInputs(unittest.TestCase):
    def test_stale_receipt_is_rejected_before_download(self):
        with self.assertRaisesRegex(ValueError, "receipt differ"):
            smoke.download(request(), {"request_id": "other"}, TARGETS[0], Path("unused"))

    def test_package_version_or_architecture_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in smoke.PACKAGES:
                (root / (name + ".deb")).write_bytes(b"test")
            def metadata(path, package_format):
                name = path.stem
                return {"name": name, "version": "0.0.0-0.123.1", "arch": "all" if name == "kata-containers" else "amd64"}
            with patch("smoke.package_metadata", side_effect=metadata):
                self.assertEqual(len(smoke.check_packages(root, request(), TARGETS[0])), 6)
            for field, value in (("version", "old"), ("arch", "arm64")):
                def wrong_metadata(path, package_format):
                    data = metadata(path, package_format)
                    if data["name"] == "kata-containers-qemu":
                        data[field] = value
                    return data
                with self.subTest(field=field), patch("smoke.package_metadata", side_effect=wrong_metadata):
                    with self.assertRaisesRegex(ValueError, "another build or architecture"):
                        smoke.check_packages(root, request(), TARGETS[0])

class WorkloadLifecycle(unittest.TestCase):
    def exercise(self, directory, phase="Succeeded", runtime=True, exit_code=0):
        output = Path(directory)
        def kubectl(*args, **kwargs):
            if 'get' in args and 'pod' in args:
                identifier = args[args.index('pod') + 1]
                return json.dumps({'spec': {'runtimeClassName': identifier if runtime else 'other'},
                                   'status': {'phase': phase, 'containerStatuses': [
                                       {'state': {'terminated': {'exitCode': exit_code}}}]}})
            if 'logs' in args:
                return 'guest-kernel\nKATA_PACKAGE_SMOKE_OK'
            return ''
        with patch('smoke.run', side_effect=kubectl), patch('smoke.subprocess.run') as commands:
            try:
                smoke.workload('k3s', output)
            finally:
                self.assertTrue((output / 'pod-describe.txt').exists())
                self.assertEqual(sum('delete' in call.args[0] for call in commands.call_args_list), 1)
        return output

    def test_success_requires_a_completed_kata_pod(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.exercise(directory)
            self.assertIn('KATA_PACKAGE_SMOKE_OK', (output / 'pod.log').read_text())
            manifest = json.loads((output / 'smoke-pod.json').read_text())
            self.assertEqual(len([p for p in manifest['items'] if p['kind'] == 'Pod']), 1)
            self.assertEqual(manifest['items'][0]['handler'], 'kata-qemu')
            self.assertNotIn('hostNetwork', manifest['items'][1]['spec'])

    def test_other_runtime_is_a_failure_and_resources_are_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'did not select'):
                self.exercise(directory, runtime=False)

    def test_failed_pod_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'pod failed'):
                self.exercise(directory, phase='Failed')

    def test_nonzero_guest_exit_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'workload failed'):
                self.exercise(directory, exit_code=1)


class RuntimeConfigurationLayout(unittest.TestCase):
    def test_symlink_resolution_finds_isolated_profile_dropins(self):
        from assemble import stage_runtime_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'upstream.toml'
            source.write_text('[hypervisor.qemu]\npath = "/opt/kata/bin/qemu-system-x86_64"\n')
            qemu = stage_runtime_config(source, root, 'qemu')
            other = stage_runtime_config(source, root, 'other')
            self.assertTrue(qemu.is_symlink())
            self.assertTrue(qemu.resolve(strict=True).parent.joinpath('config.d').is_dir())
            import tomllib
            self.assertEqual(tomllib.loads(qemu.read_text())['hypervisor']['qemu']['path'],
                             '/opt/kata/bin/qemu-system-x86_64')
            self.assertNotEqual(qemu.resolve().parent, other.resolve().parent)
            (qemu.resolve().parent / 'config.d/50-user-overrides.toml').write_text('[runtime]\nenable_debug = true\n')
            self.assertEqual(list(other.resolve().parent.joinpath('config.d').iterdir()), [])


class GuestKernelPreparation(unittest.TestCase):
    def test_waits_for_a_new_boot_before_accepting_the_module_check(self):
        import smoke_vm
        with tempfile.TemporaryDirectory() as directory, patch('smoke_vm.subprocess.run'), \
                patch('smoke_vm.call', side_effect=['old', 'old', 'new', 'modules present']) as call, \
                patch('smoke_vm.time.sleep'):
            smoke_vm.prepare_guest(['ssh', 'root@test'], {'format': 'rpm'}, Path(directory))
        self.assertEqual(call.call_count, 4)
        self.assertIn('modprobe br_netfilter', call.call_args.args[-1])


class HostedMatrix(unittest.TestCase):
    def test_diagnostic_filter_selects_exactly_one_job(self):
        targets = json.loads((ci.ROOT / 'obs/ci.json').read_text())['targets']
        jobs = ci.smoke_matrix(targets, 'ubuntu-26.04-x86_64/kubeadm/qemu')['include']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['k8s_distro'], 'kubeadm')
        jobs = ci.smoke_matrix(targets, 'ubuntu-26.04-x86_64')['include']
        self.assertEqual({job['k8s_distro'] for job in jobs}, {'kubeadm', 'k0s', 'k3s', 'rke2', 'microk8s'})
        self.assertTrue(all(job['execution'] == 'direct' for job in jobs))
        with self.assertRaises(ValueError):
            ci.smoke_matrix(targets, 'missing/kubeadm')

    def test_each_combination_has_one_isolated_host(self):
        import smoke_vm
        targets = json.loads((ci.ROOT / 'obs/ci.json').read_text())['targets']
        matrix = ci.smoke_matrix(targets)['include']
        self.assertEqual(len(matrix), 6)
        self.assertEqual(len({(t['id'], t['k8s_distro'], t['profile']) for t in matrix}), 6)
        self.assertTrue(all(t['repository'] == 'Ubuntu_26.04' for t in matrix))
        self.assertTrue(any(t['format'] == 'rpm' for t in targets))
        for target in matrix:
            self.assertEqual(target['arch'], 'x86_64')
            if target['format'] == 'deb':
                self.assertEqual(target['runner'], 'ubuntu-26.04')
                self.assertEqual(target['execution'], 'direct')
                continue
            self.assertEqual(target['runner'], 'ubuntu-24.04')
            self.assertEqual(target['execution'], 'vm')
            _, plan = smoke_vm.vm_plan(target, 'test', 'public key')
            self.assertEqual(len(plan), 1)
            self.assertTrue(plan['test']['nested'])
            self.assertEqual(plan['test']['memory'], 4096)
        self.assertEqual({t['arch'] for t in targets}, {'x86_64', 'aarch64'})
        with self.assertRaisesRegex(ValueError, 'x86_64 only'):
            smoke_vm.vm_plan(next(t for t in targets if t['arch'] == 'aarch64'), 'test', 'key')


class RunnerGuards(unittest.TestCase):
    def test_missing_kvm_fails_before_installing_packages(self):
        with patch("smoke.os.geteuid", return_value=0), patch("smoke.platform.freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "26.04"}), patch("smoke.platform.machine", return_value="x86_64"), patch("smoke.os.access", return_value=False), patch("smoke.subprocess.run") as commands:
            with self.assertRaisesRegex(ValueError, "/dev/kvm"):
                smoke.smoke(request(), {}, TARGETS[0], Path("unused"), Path("unused"))
            commands.assert_not_called()


if __name__ == "__main__":
    unittest.main()
