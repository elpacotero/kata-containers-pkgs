#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Select upstream payloads, reuse matching OBS packages, and monitor native builds."""
import argparse
import base64
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from build_request import validate_request, validate_request_version

ROOT = Path(__file__).resolve().parent.parent
SUCCESS = {"succeeded", "succeed"}
FAILURE = {"failed", "broken", "unresolvable", "excluded", "disabled"}


def git(*args, cwd=ROOT, data=None, env=None):
    return subprocess.check_output(["git", *args], cwd=cwd, input=data, env=env, text=True).strip()


PACKAGE_INPUTS = ('config.json', 'LICENSE', 'LICENSES', 'packaging',
                  'scripts/build_request.py', 'scripts/assemble.py', 'scripts/elf_dependencies.py',
                  'scripts/import_bundle.py', 'scripts/obs_native.py', 'scripts/prepare_obs.py',
                  'scripts/rpm_backend.py', 'scripts/nydus_package.py', 'scripts/packages.py', 'scripts/setup.py', 'scripts/stage_profile.py', 'scripts/upstream.py')
SMOKE_INPUTS = ('.github/workflows/development.yml', '.github/workflows/releases.yml',
                '.github/workflows/smoke.yml', 'scripts/ci.py', 'scripts/smoke.py',
                'scripts/smoke_vm.py', 'scripts/smoke_debug.py', 'scripts/smoke_qemu.py',
                'scripts/kubernetes.py', 'scripts/setup-kubernetes.sh', 'scripts/prepare-smoke-runner.sh',
                'scripts/validate.py', 'scripts/validate_rpm.py', 'scripts/verify_image.py')


def input_fingerprint(paths, revision='HEAD', root=ROOT):
    # Git tree records include file contents, paths and modes, but no commit ID.
    # Documentation/test-only commits therefore leave package inputs unchanged.
    records = git('ls-tree', '-r', '--full-tree', revision, '--', *paths, cwd=root)
    return hashlib.sha256(('native-obs-v1\n' + records).encode()).hexdigest()


def select_source(channel, refs):
    entries = {}
    for line in refs.splitlines():
        sha, ref = line.split()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("invalid upstream commit SHA")
        entries[ref] = sha
    if channel == "development":
        return "refs/heads/main", entries["refs/heads/main"]
    stable = []
    for ref in entries:
        match = re.fullmatch(r"refs/tags/(v?([0-9]+)\.([0-9]+)\.([0-9]+))", ref)
        if match:
            stable.append((tuple(map(int, match.groups()[1:])), ref))
    if not stable:
        raise ValueError("no stable upstream release tags found")
    version = max(item[0] for item in stable)
    newest = [ref for candidate, ref in stable if candidate == version]
    commits = {entries.get(ref + "^{}", entries[ref]) for ref in newest}
    if len(commits) != 1:
        raise ValueError("ambiguous latest stable release tags")
    ref = sorted(newest)[-1]
    return ref, commits.pop()




class OBS:
    def __init__(self, project, package, base=None):
        self.project, self.package = project, package
        self.base = (base or os.environ.get("OBS_API_URL", "https://api.opensuse.org")).rstrip("/")
        url = urllib.parse.urlsplit(self.base)
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
            raise ValueError("OBS_API_URL must be an HTTPS API URL without credentials")
        username, password = os.environ.get("OBS_USERNAME"), os.environ.get("OBS_PASSWORD")
        if not username or not password:
            raise ValueError("configure OBS_USERNAME and OBS_PASSWORD secrets")
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def api(self, path, method="GET", optional=False, data=None, timeout=45, **query):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": self.auth, "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if optional and error.code == 404:
                return None
            detail = ''
            try:
                status = ET.fromstring(error.read(8192))
                detail = ': '.join(value for value in (status.get('code', ''), status.findtext('summary', '')) if value)
                detail = ' '.join(detail.split())[:400]
                for secret in (self.auth, self.auth.removeprefix('Basic '),
                               os.environ.get('OBS_PASSWORD', '')):
                    if secret:
                        detail = detail.replace(secret, '***')
            except (ET.ParseError, OSError):
                pass
            finally:
                error.close()
            raise RuntimeError(f"OBS {method} {path}: HTTP {error.code}" +
                               (f" ({detail})" if detail else '')) from None

    @property
    def source_path(self):
        return "/source/" + "/".join(urllib.parse.quote(x, safe="") for x in (self.project, self.package))

    def source(self):
        raw = self.api(self.source_path + "/build-request.json", optional=True, expand=1)
        if raw is None:
            return None, None
        request = validate_request(json.loads(raw))
        directory = ET.fromstring(self.api(self.source_path, expand=1))
        return request, directory.attrib["srcmd5"]

    def history(self, target, source_md5):
        path = "/build/" + "/".join(urllib.parse.quote(x, safe="") for x in
                                      (self.project, target["repository"], target["arch"])) + "/_jobhistory"
        history = ET.fromstring(self.api(path, package=self.package, limit=50))
        matching = [job.attrib for job in history.findall("jobhist")
                    if job.get("srcmd5") == source_md5 or job.get("verifymd5") == source_md5]
        return max(matching, key=lambda job: int(job.get("endtime", "0")), default=None)

    def completed(self, source_md5, targets):
        jobs = []
        for target in targets:
            job = self.history(target, source_md5)
            if not job or job.get("code") not in SUCCESS:
                return None
            jobs.append(dict(target, source_md5=source_md5, job=job))
        return jobs

    def ensure_package(self):
        if self.api(self.source_path + '/_meta', optional=True) is not None:
            return
        meta = ET.Element('package', name=self.package, project=self.project)
        ET.SubElement(meta, 'title').text = self.package
        ET.SubElement(meta, 'description').text = 'Retained upstream stable Kata Containers release'
        ET.SubElement(ET.SubElement(meta, 'publish'), 'disable')
        self.api(self.source_path + '/_meta', method='PUT', data=ET.tostring(meta))

    def publish(self, enabled=True):
        action = 'enable' if enabled else 'disable'
        meta = ET.fromstring(self.api(self.source_path + '/_meta'))
        flag = meta.find('publish')
        if flag is not None and len(flag) == 1 and flag[0].tag == action and not flag[0].attrib:
            return
        if flag is not None:
            meta.remove(flag)
        ET.SubElement(ET.SubElement(meta, 'publish'), action)
        self.api(self.source_path + '/_meta', method='PUT', data=ET.tostring(meta))
        print(f'{action.capitalize()}d publication of stable package {self.project}/{self.package}')

    def configure(self, targets, package_version):
        meta = ET.fromstring(self.api(self.source_path + "/_meta"))
        sync = (meta.findtext("scmsync") or "").strip()
        if sync:
            url = urllib.parse.urlsplit(sync)
            destination = (url.hostname or "unknown host") + url.path
            raise ValueError(f"remove package SCM Sync ({destination}) before submitting binary source inputs directly")
        path = "/source/" + urllib.parse.quote(self.project, safe="") + "/_meta"
        project = ET.fromstring(self.api(path))
        for repository in sorted({target["repository"] for target in targets}):
            if project.find(f"repository[@name='{repository}']") is not None:
                continue
            node = ET.SubElement(project, "repository", name=repository)
            if repository == "Ubuntu_26.04":
                ET.SubElement(node, "path", project="Ubuntu:26.04", repository="universe-update")
                ET.SubElement(node, "path", project="Ubuntu:26.04", repository="update")
            elif repository == "AlmaLinux_10.2":
                ET.SubElement(node, "path", project="AlmaLinux:10.2", repository="standard")
            else:
                raise ValueError("configure custom OBS repositories explicitly")
            for arch in sorted({t["arch"] for t in targets if t["repository"] == repository}):
                ET.SubElement(node, "arch").text = arch
        # Build results are available to smoke tests through the OBS API. Initial
        # repository publication remains disabled until explicitly enabled by owner.
        if project.find("publish") is None:
            ET.SubElement(ET.SubElement(project, "publish"), "disable")
        self.api(path, method="PUT", data=ET.tostring(project))
        # OBS CI_CNT is its source counter, not the spec Release. Override only
        # spec builds with the CI-selected RPM release; leave Debian handling alone.
        config_path = path.removesuffix("_meta") + "_config"
        config = (self.api(config_path, optional=True) or b"").decode()
        revision = validate_request_version(package_version).split('-', 1)[1]
        marker = "# Kata packaging: preserve the CI-selected source release."
        config = re.sub(r"(?m)^" + re.escape(marker) + r"\nRelease:[^\n]*(?:\nPublishFlags: !singleexport)?\n?", "", config)
        setting = marker + "\nRelease: <CI_CNT> spec:<SPEC_REL>\n"
        if not revision.startswith('0.'):
            setting += 'PublishFlags: !singleexport\n'

        self.api(config_path, method="PUT", data=(config.rstrip() + "\n\n" + setting).encode())

    def submit(self, folder):
        import hashlib
        directory = ET.Element("directory")
        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.name == "payload-sizes.json":
                continue
            print(f"Uploading OBS source {path.name}: {path.stat().st_size} bytes", flush=True)
            contents = path.read_bytes()
            ET.SubElement(directory, "entry", name=path.name, md5=hashlib.md5(contents).hexdigest(),
                          hash="sha256:" + hashlib.sha256(contents).hexdigest())
            # Upload revision is staged; the final file list commits atomically.
            self.api(self.source_path + "/" + urllib.parse.quote(path.name, safe=""),
                     method="PUT", data=contents, timeout=600, rev="upload")
        self.api(self.source_path, method="POST", data=ET.tostring(directory), cmd="commitfilelist",
                 comment="Package verified upstream Kata binary payloads", withvalidate=1, timeout=600)

    def wait(self, request, targets, timeout=7200, interval=30):
        deadline, observed = time.monotonic() + timeout, False
        while time.monotonic() < deadline:
            current, md5 = self.source()
            if current and current["request_id"] == request["request_id"]:
                observed = True
                jobs = self.completed(md5, targets)
                if jobs:
                    return {"schema_version": 1, "request_id": request["request_id"], "project": self.project,
                            "package": self.package, "source_md5": md5, "targets": jobs}
                for target in targets:
                    job = self.history(target, md5)
                    if job and job.get("code") in FAILURE:
                        raise RuntimeError(f"OBS {target['id']} failed: {job['code']}")
                print(f"Waiting for {request['channel']} build {md5}", flush=True)
            elif observed:
                raise RuntimeError("OBS build request was superseded; refusing stale build results")
            else:
                print("Waiting for OBS SCM sync", flush=True)
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise TimeoutError("OBS did not complete every requested distro/architecture before the deadline")


def same_release(request, previous):
    version = lambda ref: ref.removeprefix("refs/tags/").removeprefix("v")
    if not previous or previous["channel"] != "release" or version(previous["source_ref"]) != version(request["source_ref"]):
        return False
    if previous["source_revision"] != request["source_revision"]:
        raise ValueError("the latest stable tag moved; investigate before rebuilding")
    if request.get("upstream") and not previous.get("upstream"):
        return False
    if request.get("upstream") and request["upstream"]["bundles"] != previous["upstream"]["bundles"]:
        raise ValueError("published release assets changed; investigate before rebuilding")
    if previous.get('upstream', {}).get('auxiliary') and previous['upstream']['auxiliary'] != request.get('upstream', {}).get('auxiliary'):
        raise ValueError('pinned auxiliary release assets changed; investigate before rebuilding')
    return True


def release_built(request, client, targets):
    previous, md5 = client.source()
    return same_release(request, previous) and bool(client.completed(md5, targets))


def package_version(channel, source_ref, preview, run_id, attempt, revisions=None):
    if channel == 'release':
        match = re.fullmatch(r'refs/tags/v?([0-9]+\.[0-9]+\.[0-9]+)', source_ref)
        if not match:
            raise ValueError('stable packages require an upstream stable release tag')
        revision = (revisions or {}).get(match[1], 1)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError('stable packaging revision must be a positive integer')
        return match[1] + '-' + str(revision)
    return preview.split('-', 1)[0] + f'-0.{run_id}.{attempt}'


def destination_for(settings, request):
    destination = dict(settings['channels'][request['channel']])
    if request['channel'] == 'release':
        version = request['package_version'].split('-', 1)[0]
        destination['package'] += '-' + version
        revision = request['package_version'].split('-', 1)[1]
        if revision != '1':
            destination['package'] += '-r' + revision
    return destination


def output(name, value):
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"{name}={value}\n")


def smoke_matrix(targets, selection=None, profiles=("qemu", "nvidia-cpu", "nvidia-gpu", "nvidia-gpu-tee")):
    from kubernetes import DISTROS
    jobs = [dict(target, profile=profile, test_kind="installation" if profile in ("nvidia-gpu", "nvidia-gpu-tee") else "workload", k8s_distro=distro, execution="direct" if target["format"] == "deb" else "vm",
                 runner="ubuntu-26.04" if target["format"] == "deb" else "ubuntu-24.04")
            for target in targets if target["arch"] == "x86_64" and target["repository"] == "Ubuntu_26.04"
            for profile in profiles for distro in DISTROS if profile == "qemu" or distro == "kubeadm"]
    if selection:
        jobs = [job for job in jobs if selection in (job['id'], job['id'] + '/' + job['k8s_distro'],
                job['id'] + '/' + job['k8s_distro'] + '/' + job['profile'])]
        if not jobs:
            raise ValueError('smoke_filter must match a configured target or target/distro')
    return {'include': jobs}


def main():
    from upstream import GitHub, NotReady, development, release
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('channel', choices=('release', 'development'))
    parser.add_argument('--output', type=Path, default=ROOT / 'out')
    parser.add_argument('--prepare', action='store_true', help='download upstream bundles and generate offline OBS sources, without submitting')
    parser.add_argument('--rebuild-version', default='', help='explicitly replace exactly this stable package version; never used by scheduled runs')
    parser.add_argument('--finish', action='store_true', help='record successful smoke tests; prune older development workflow artifacts')
    args = parser.parse_args()
    settings = json.loads((ROOT / 'obs/ci.json').read_text())
    config = json.loads((ROOT / 'config.json').read_text())
    targets = json.loads(os.environ['OBS_TARGETS_JSON']) if os.environ.get('OBS_TARGETS_JSON') else settings['targets']
    if not targets or len({t['id'] for t in targets}) != len(targets):
        raise ValueError('configure uniquely identified OBS targets')
    output('builds_ready', 'false')
    selection = os.environ.get('SMOKE_FILTER', '')
    output('matrix', json.dumps(smoke_matrix(targets, selection), separators=(',', ':')))
    output('full_smoke', 'false' if selection else 'true')
    args.output.mkdir(parents=True, exist_ok=True)
    if args.finish:
        finish_development(args.output, channel=args.channel)
        return
    revision = git('rev-parse', 'HEAD')
    repository = os.environ.get('GITHUB_REPOSITORY', 'elpacotero/kata-containers-pkgs')
    run_id, attempt = os.environ.get('GITHUB_RUN_ID', '0'), os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not run_id.isdigit() or not attempt.isdigit():
        raise ValueError('invalid run identifier')
    github = GitHub()
    try:
        if args.channel == 'development':
            upstream = development(github, [t['arch'] for t in targets], include_tee=True)
        else:
            ref, sha = select_source('release', git('ls-remote', '--tags', config['source_repository']))
            upstream = release(github, ref, sha, [t['arch'] for t in targets], include_tee=True)
    except NotReady as error:
        print(str(error) + '; no build submitted')
        output('skipped', 'true')
        return
    request = validate_request({'schema_version': 1, 'channel': args.channel,
        'source_repository': config['source_repository'], 'source_ref': upstream['source_ref'],
        'source_revision': upstream['source_revision'], 'upstream': upstream,
        'packaging_repository': repository, 'packaging_revision': revision,
        'packaging_fingerprint': input_fingerprint(PACKAGE_INPUTS),
        'package_version': package_version(args.channel, upstream['source_ref'], config['package_version'], run_id, attempt, config.get('release_revisions')),
        'request_id': f'{args.channel}-{run_id}-{attempt}-{upstream["source_revision"]}',
        'created': datetime.now(timezone.utc).isoformat(), 'targets': targets,
        'profiles': ['qemu', 'nvidia-cpu', 'nvidia-gpu', 'nvidia-gpu-tee']})
    if args.rebuild_version and (args.channel != 'release' or args.rebuild_version != request['package_version']):
        raise ValueError('--rebuild-version must exactly match the latest stable package version')
    if not args.prepare and not args.rebuild_version:
        state = read_development_state(channel=args.channel)
        if args.channel == 'release' and state:
            same_release(request, state['build_request'])
        if (not selection and state and same_inputs(request, state['build_request'])
                and state.get('smoke_fingerprint') == input_fingerprint(SMOKE_INPUTS)):
            print('Latest upstream payload already packaged and smoke-tested; skipping')
            output('skipped', 'true')
            return
    (args.output / 'smoke-inputs.json').write_text(json.dumps({
        'revision': revision, 'fingerprint': input_fingerprint(SMOKE_INPUTS)}, indent=2) + '\n')
    request_path = args.output / 'build-request.json'
    request_path.write_text(json.dumps(request, indent=2) + '\n')
    enabled = os.environ.get('OBS_ENABLED', 'false') == 'true' and not args.prepare
    client = None
    if enabled:
        if os.environ.get('SMOKE_ENABLED') != 'true':
            raise ValueError('OBS submission requires SMOKE_ENABLED=true')
        destination = destination_for(settings, request)
        client = OBS(destination['project'], destination['package'])
        if args.channel == 'release':
            client.ensure_package()
        previous, md5 = client.source()
        stable_ready = args.channel == 'release' and release_built(request, client, targets)
        if not args.rebuild_version and previous and (stable_ready or (args.channel == 'development' and same_inputs(request, previous))):
            jobs = client.completed(md5, targets)
            if jobs:
                # Retry failed smoke tests against the existing immutable package build.
                request_path.write_text(json.dumps(previous, indent=2) + '\n')
                output('matrix', json.dumps(smoke_matrix(targets, selection, previous.get('profiles', ['qemu'])), separators=(',', ':')))
                receipt = {'schema_version': 1, 'request_id': previous['request_id'], 'project': client.project,
                           'package': client.package, 'source_md5': md5, 'targets': jobs}
                (args.output / 'obs-build.json').write_text(json.dumps(receipt, indent=2) + '\n')
                message = f'Reusing OBS source {md5}, package {previous["package_version"]}; no OBS rebuild'
                print(message)
                if 'GITHUB_STEP_SUMMARY' in os.environ:
                    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
                        stream.write(message + '\n')
                output('builds_ready', 'true')
                return
    if enabled or args.prepare:
        from prepare_obs import prepare
        sources = args.output.parent / 'obs-sources'
        sizes = prepare(request, sources, github)
        (args.output / 'payload-sizes.json').write_text(json.dumps(sizes, indent=2) + '\n')
        if enabled:
            if args.rebuild_version:
                client.publish(enabled=False)
            client.configure(targets, request["package_version"])
            client.submit(sources)
            receipt = client.wait(request, targets)
            (args.output / 'obs-build.json').write_text(json.dumps(receipt, indent=2) + '\n')
            output('builds_ready', 'true')
    print(f'{args.channel}: {request["source_ref"]} at {request["source_revision"]}; package {request["package_version"]}')
    print('OBS submission: ' + ('completed' if enabled else 'disabled; no OBS changes'))
    if 'GITHUB_STEP_SUMMARY' in os.environ:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(f'Upstream [{upstream["source_revision"]}]({upstream["url"]})\n\n'
                         f'Package: `{request["package_version"]}`\n\nOBS submission: {"completed" if enabled else "disabled"}\n')


def same_inputs(a, b):
    from upstream import identity
    return (a.get('upstream') is not None and b.get('upstream') is not None
            and identity(a['upstream']) == identity(b['upstream'])
            and a.get('packaging_fingerprint', a['packaging_revision'])
                == b.get('packaging_fingerprint', b['packaging_revision'])
            and a.get('targets') == b.get('targets')
            and a.get('profiles', ['qemu']) == b.get('profiles', ['qemu']))


def read_development_state(root=ROOT, channel='development'):
    branch = channel + '-state'
    if not git('ls-remote', '--heads', 'origin', 'refs/heads/' + branch, cwd=root):
        return None
    git('fetch', '--no-tags', 'origin', 'refs/heads/' + branch, cwd=root)
    return json.loads(git('show', 'FETCH_HEAD:state.json', cwd=root))


def write_development_state(state, root=ROOT, channel='development'):
    branch = channel + '-state'
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/' + branch, cwd=root)
    parents = []
    if remote:
        git('fetch', '--no-tags', 'origin', 'refs/heads/' + branch, cwd=root)
        parents = ['-p', git('rev-parse', 'FETCH_HEAD', cwd=root)]
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'),
                   GIT_AUTHOR_NAME='github-actions[bot]', GIT_COMMITTER_NAME='github-actions[bot]',
                   GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                   GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com')
        git('read-tree', '--empty', cwd=root, env=env)
        blob = git('hash-object', '-w', '--stdin', cwd=root, data=json.dumps(state, indent=2) + '\n')
        git('update-index', '--add', '--cacheinfo', '100644', blob, 'state.json', cwd=root, env=env)
        tree = git('write-tree', cwd=root, env=env)
        commit = git('commit-tree', tree, *parents, cwd=root, env=env, data=f'Record latest smoke-tested {channel} payload\n')
    git('push', 'origin', commit + ':refs/heads/' + branch, cwd=root)


def obsolete_development_artifacts(artifacts, current_run):
    result = []
    for artifact in artifacts:
        producer = (artifact.get('workflow_run') or {}).get('id')
        if (artifact['name'].startswith(('development-build-', 'development-smoke-'))
                and producer is not None and str(producer) != str(current_run)):
            result.append(artifact['id'])
    return result


def finish_development(folder, channel='development'):
    import subprocess
    request = validate_request(json.loads((folder / 'build-request.json').read_text()))
    receipt = json.loads((folder / 'obs-build.json').read_text())
    if request['channel'] != channel or request['request_id'] != receipt['request_id']:
        raise ValueError('smoke completion provenance mismatch')
    client = OBS(receipt['project'], receipt['package'])
    current, md5 = client.source()
    if not current or current['request_id'] != request['request_id'] or md5 != receipt['source_md5']:
        raise ValueError('OBS sources changed before smoke completion')
    if not client.completed(md5, request['targets']):
        raise ValueError('OBS build is no longer successful on every target')
    if channel == 'release':
        # The workflow calls completion only after the full smoke matrix passes.
        # Publish before recording state, so publication failures remain retryable.
        client.publish()
    if channel == 'development':
        # Only our previous development artifacts; never upstream assets or releases.
        repository, current_run = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_RUN_ID']
        raw = subprocess.check_output(['gh', 'api', '--paginate', f'repos/{repository}/actions/artifacts?per_page=100'], text=True)
        decoder, offset = json.JSONDecoder(), 0
        while offset < len(raw):
            while offset < len(raw) and raw[offset].isspace():
                offset += 1
            if offset == len(raw):
                break
            page, consumed = decoder.raw_decode(raw[offset:])
            offset += consumed
            for artifact_id in obsolete_development_artifacts(page['artifacts'], current_run):
                subprocess.run(['gh', 'api', '-X', 'DELETE', f'repos/{repository}/actions/artifacts/{artifact_id}'], check=True)
    write_development_state({'build_request': request, 'obs_build': receipt,
                             'smoke_revision': git('rev-parse', 'HEAD'),
                             'smoke_fingerprint': input_fingerprint(SMOKE_INPUTS)}, channel=channel)


if __name__ == "__main__":
    main()
