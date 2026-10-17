#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Select and verify upstream binary bundles without rebuilding Kata components."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = 'kata-containers/kata-containers'
WORKFLOW = 'payload-after-push.yaml'
ARCHES = {'x86_64': 'amd64', 'aarch64': 'arm64'}


class NotReady(Exception):
    """The selected upstream build has not produced a complete usable payload."""


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        redirected = super().redirect_request(request, *args, **kwargs)
        if redirected and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(redirected.full_url).netloc:
            redirected.remove_header('Authorization')
        return redirected


class GitHub:
    def __init__(self):
        self.opener = urllib.request.build_opener(Redirect())
        self.token = os.environ.get('UPSTREAM_GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')

    def request(self, url):
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        if self.token and urllib.parse.urlsplit(url).hostname == 'api.github.com':
            headers['Authorization'] = 'Bearer ' + self.token
        return urllib.request.Request(url, headers=headers)

    def api(self, path, optional=False, repository=REPOSITORY):
        try:
            with self.opener.open(self.request('https://api.github.com/repos/' + repository + path), timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if optional and error.code == 404:
                return None
            raise RuntimeError(f'GitHub API {path}: HTTP {error.code}') from None

    def pages(self, path, key):
        separator = '&' if '?' in path else '?'
        page = 1
        while True:
            items = self.api(f'{path}{separator}per_page=100&page={page}')[key]
            yield from items
            if len(items) < 100:
                return
            page += 1

    def download(self, descriptor, output):
        digest = descriptor.get('digest') or ''
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('upstream payload lacks a valid SHA256 digest')
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.download')
        hasher = hashlib.sha256()
        try:
            with self.opener.open(self.request(descriptor['url']), timeout=600) as response, temporary.open('xb') as stream:
                while block := response.read(4 * 1024 * 1024):
                    hasher.update(block)
                    stream.write(block)
            if 'sha256:' + hasher.hexdigest() != digest:
                raise ValueError('upstream payload digest mismatch')
            if descriptor['kind'] == 'actions-artifact':
                with zipfile.ZipFile(temporary) as archive:
                    files = [item for item in archive.infolist() if not item.is_dir()]
                    if len(files) != 1 or files[0].filename != 'kata-static.tar.zst':
                        raise ValueError('expected exactly one kata-static.tar.zst in the upstream artifact')
                    with archive.open(files[0]) as source, output.open('xb') as dest:
                        shutil.copyfileobj(source, dest, 4 * 1024 * 1024)
            else:
                if output.exists():
                    raise FileExistsError(output)
                temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        with output.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()


def descriptor(item, kind):
    digest = item.get('digest') or ''
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError(f'upstream {item["name"]} has no valid SHA256 digest')
    return {'kind': kind, 'id': item['id'], 'name': item['name'], 'digest': digest,
            'size': item['size_in_bytes'] if kind == 'actions-artifact' else item['size'],
            'url': f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{item["id"]}/zip'
                   if kind == 'actions-artifact' else item['browser_download_url']}


def development(client, architectures, include_tee=False):
    runs = client.api('/actions/workflows/' + WORKFLOW + '/runs?branch=main&event=push&status=success&per_page=100')['workflow_runs']
    runs = [run for run in runs if run['event'] == 'push' and run['head_branch'] == 'main'
            and run['status'] == 'completed' and run['conclusion'] == 'success'
            and run['head_repository']['full_name'] == REPOSITORY]
    if not runs:
        raise NotReady('no successful upstream main payload build found')
    run = max(runs, key=lambda item: item['id'])
    if not re.fullmatch('[0-9a-f]{40}', run['head_sha']):
        raise ValueError('invalid upstream source commit')
    artifacts = list(client.pages(f'/actions/runs/{run["id"]}/artifacts', 'artifacts'))
    bundles = {}
    for architecture in sorted(set(architectures)):
        name = 'kata-static-tarball-' + ARCHES[architecture]
        matching = [a for a in artifacts if a['name'] == name and not a['expired']]
        if len(matching) != 1:
            raise NotReady(f'latest successful upstream run lacks a unique unexpired {name}')
        item = matching[0]
        if item['workflow_run']['id'] != run['id'] or item['workflow_run']['head_sha'] != run['head_sha']:
            raise ValueError('upstream artifact belongs to a different run or commit')
        bundles[architecture] = descriptor(item, 'actions-artifact')
    return {'kind': 'development', 'repository': REPOSITORY, 'workflow': WORKFLOW,
            'run_id': run['id'], 'source_revision': run['head_sha'], 'source_ref': 'refs/heads/main',
            'url': run['html_url'], 'bundles': bundles,
            **({'auxiliary': {'nydus_snapshotter': nydus_snapshotter(client, run['head_sha'])}} if include_tee else {})}


def release(client, ref, revision, architectures, include_tee=False):
    tag = ref.removeprefix('refs/tags/')
    item = client.api('/releases/tags/' + urllib.parse.quote(tag, safe=''), optional=True)
    if not item or item['draft'] or item['prerelease']:
        raise NotReady(f'latest stable tag {tag} does not yet have a published stable release')
    version = tag.removeprefix('v')
    bundles = {}
    for architecture in sorted(set(architectures)):
        name = f'kata-static-{version}-{ARCHES[architecture]}.tar.zst'
        matching = [asset for asset in item['assets'] if asset['name'] == name and asset.get('state') == 'uploaded']
        if len(matching) != 1:
            raise NotReady(f'latest stable release lacks a unique {name}')
        bundles[architecture] = descriptor(matching[0], 'release-asset')
    return {'kind': 'release', 'repository': REPOSITORY, 'release_id': item['id'], 'tag': tag,
            'source_revision': revision, 'source_ref': ref, 'url': item['html_url'], 'bundles': bundles,
            **({'auxiliary': {'nydus_snapshotter': nydus_snapshotter(client, revision)}} if include_tee else {})}


def identity(upstream):
    return {key: upstream[key] for key in ('kind', 'source_revision', 'bundles', 'auxiliary') if key in upstream}


def contents(client, path, revision, repository=REPOSITORY):
    import base64
    item = client.api('/contents/' + path + '?ref=' + revision, repository=repository)
    return base64.b64decode(item['content'], validate=False)


def nydus_snapshotter(client, revision):
    """Resolve exactly the snapshotter pinned by this Kata source commit."""
    text = contents(client, 'versions.yaml', revision).decode()
    section = re.search(r'(?m)^  nydus-snapshotter:\s*\n((?:    [^\n]*\n|\n)+)', text)
    if not section:
        raise ValueError('Kata versions.yaml has no nydus snapshotter pin')
    values = {}
    for key in ('url', 'version'):
        value = re.search(r'(?m)^    ' + key + r':\s*["\']?([^"\'\s]+)', section[1])
        if not value:
            raise ValueError('invalid pinned nydus snapshotter ' + key)
        values[key] = value[1]
    if values['url'] != 'https://github.com/containerd/nydus-snapshotter' or not re.fullmatch(r'[A-Za-z0-9_.-]+', values['version']):
        raise ValueError('unexpected nydus snapshotter source')
    item = client.api('/releases/tags/' + values['version'], repository='containerd/nydus-snapshotter')
    name = 'nydus-snapshotter-' + values['version'] + '-linux-amd64.tar.gz'
    assets = [asset for asset in item['assets'] if asset['name'] == name and asset.get('state') == 'uploaded']
    if len(assets) != 1:
        raise NotReady('pinned nydus snapshotter release asset is unavailable')
    return dict(descriptor(assets[0], 'release-asset'), repository='containerd/nydus-snapshotter', version=values['version'])
