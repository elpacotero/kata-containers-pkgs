# Release and development workflows

| Workflow | Schedule | Upstream input | OBS destination |
| --- | --- | --- | --- |
| Releases | Hourly at minute 17 UTC; manual | Highest stable semantic-version tag, then its published Rust binary bundles | `home:elpacotero/kata-containers-VERSION` |
| Development | Daily at 03:00 UTC; manual | Latest successful push-to-main `payload-after-push.yaml` run with both architecture artifacts | `home:elpacotero:development/kata-containers` |

Release selection watches all stable series and excludes prereleases. It waits
for the latest tag's assets, rather than falling back to an older release. Once
that release has successfully built on every OBS target and passed the Ubuntu
smoke matrix, further polls skip it. Existing builds are reused for smoke retries.
Moved tags or changed release assets are rejected.

Development consumes `kata-static-tarball-amd64` and `kata-static-tarball-arm64`
from the same completed upstream run. Failed builds and PR builds are excluded.
Missing, expired or ambiguous artifacts prevent submission. The workflow records
upstream run ID, head commit, artifact IDs, SHA256 digests, packaging commit, package-input fingerprint and
package version. Upstream may reuse cached components: the recorded workflow
head identifies the producer run, not a promise that every binary embeds that
commit. Bundle/file hashes and installed binary versions identify the payload.

There is no cross-repository `workflow_run` subscription. Polling requires no
upstream workflow change. A later upstream `repository_dispatch` hook could
replace polling without changing ingestion.

## Package preparation

The coordinator verifies the downloaded asset or artifact ZIP digest, extracts
the Rust tarball, then selects generic QEMU and NVIDIA CPU/GPU assets. On x86_64
it also selects SNP/TDX VMMs and firmware, and the CoCo guest extension. Monolithic
GPU rootfs images are excluded. Guest image/kernel symlink targets are included.
Required files, archive paths and link boundaries are checked; imported component
archives are hashed again before assembly. No component is recompiled.

Dragonball uses the integrated Rust VMM, its dedicated experimental kernel and
the shared generic guest rootfs on both architectures. Its configuration and
containerd handler are owned by a separate profile package.

The reduced inputs and packaging code become a native Debian source package and
an RPM spec with sixteen x86_64 packages and thirteen aarch64 packages. Both build offline on OBS. Submission uploads to
a staging revision and commits the complete file list atomically with SHA256
validation. Old filenames are replaced by that revision. Leave package-level SCM
Sync empty: large binary inputs are uploaded to OBS, not committed to Git.

Stable packages use the upstream version, with packaging revision 1 by default;
`config.json` `release_revisions` can assign a higher revision for a specific
upstream release. Higher packaging revisions use separate OBS source containers
with `-rREVISION` appended, preserving older builds. For example
DEB `4.2.0-1` and RPM `4.2.0-1.el10`. Each stable version gets a separate OBS source
package (`kata-containers-4.2.0`) in the stable project. Existing release source
packages and their binaries are never deleted or replaced by later versions.
The stable project disables OBS singleexport so older versions remain eligible
for repository metadata when publication is enabled. Development keeps
`0.0.0-0.<GitHub run ID>.<attempt>` (RPM adds `.el10`) in one replaceable container. The embedded upstream version is unchanged. Installed provenance records
the complete request and explicitly labels upstream binary imports.

## GitHub and OBS configuration

Use the packaging repository's default branch for the workflows. Create GitHub
environments `release` and `development` (Actions can create them on first run).
Add these repository or environment secrets:

- `OBS_USERNAME`: OBS login, currently `elpacotero`.
- `OBS_PASSWORD`: OBS/openSUSE account password used for HTTP Basic API access.
  There is no separate API password to generate for this implementation. An OBS
  workflow/rebuild token is not a substitute for source-upload API permissions.
- `UPSTREAM_GITHUB_TOKEN`: optional token able to read upstream Actions artifacts
  if the workflow's built-in token cannot download across repositories. Selection
  and metadata use the built-in token by default.

Keep passwords/tokens in GitHub secrets, not Git or chat. `OBS_API_URL` defaults
to `https://api.opensuse.org`. `obs/ci.json` supplies target repository names.
Override the complete target array with `OBS_TARGETS_JSON` if necessary.

`OBS_ENABLED` defaults to false: scheduled/manual runs select an upstream payload,
record a request and run unit tests without changing OBS. Manual dispatch with
`prepare_sources=true` additionally downloads and verifies both bundles and
prepares the source package, while still forcing submission off. Source-size
measurements are retained with the request; large source archives stay in the
runner workspace rather than GitHub diagnostic artifacts.

Enable `OBS_ENABLED=true` and `SMOKE_ENABLED=true` in each channel environment
only when OBS credentials are configured. Both channels require smoke testing. The coordinator adds missing repositories
using `Ubuntu:26.04` update/universe-update and `AlmaLinux:10.2` standard, with both
architectures. Existing repositories are preserved. Project configuration uses `Release: <CI_CNT> spec:<SPEC_REL>` so every spec
retains its own RPM revision, without an OBS source/build-counter suffix. The
setting is independent of the most recently submitted source package.
Projects must already exist. Development uses its existing package; stable
version-specific package containers are created automatically with an explicit
publication-disable flag, even if project publishing is enabled. After the full
smoke matrix succeeds, completion verifies the exact source MD5 and all native
builds again, then enables that package's publication before recording success.
No manual OBS change is needed for subsequent stable versions. A failed publish
leaves the release retryable. Existing stable packages are retained. Project-level
publication settings are preserved; keep the old unversioned stable preview
package disabled. Development retains its current package publication setting;
it is not yet gated on successful smoke tests.

For focused debugging, manual `smoke_filter` accepts a `<target-id>` or `<target-id>/<distro>` or `<target-id>/<distro>/<profile>`,
for example `ubuntu-26.04-x86_64/kubeadm`. A filtered run never marks the build
fully tested or prunes previous artifacts. Scheduled runs keep the complete matrix.

## Release and development smoke tests and retention

After all four OBS targets build the matching source MD5, both channels call the
same reusable smoke workflow. Five generic-QEMU x86_64 jobs cover Ubuntu 26.04 with kubeadm,
k0s, k3s, MicroK8s and RKE2, directly on separate `ubuntu-26.04` hosted runners.
An additional Dragonball job tests normal and debug kubeadm Pods on x86_64.
The NVIDIA CPU job installs only that profile and tests kubeadm with the
latest stable upstream containerd. It verifies effective EROFS/dm-verity settings, normal
and debug Pods, and uses the `kata-nvidia-cpu` handler.
A seventh job installs NVIDIA GPU packages and verifies dependencies, both installed
NVIDIA handlers, firmware ownership and measured guest/driver images. It installs
containerd without Kubernetes and runs no guest or GPU workload. GPU end-to-end
testing is not available. An eighth job installs both GPU TEE handlers and checks
the nydus proxy connection without starting a guest.
AlmaLinux smoke jobs are temporarily disabled in `smoke_matrix`; all four native
OBS build targets remain enabled. The kcli VM harness remains available for
future RPM smoke testing. ARM64 packages are built but are not smoke-tested
because hosted ARM runners lack virtualization.

Every job checks out Kata CI at the exact upstream payload revision. Kubernetes
setup calls upstream `deploy_k8s` for kubeadm, k0s, k3s, RKE2 and MicroK8s.
For generic QEMU with Ubuntu kubeadm, a small adapter replaces only the containerd
download helper with installation of Ubuntu's `containerd` and `runc` packages.
NVIDIA jobs resolve the latest stable upstream containerd tag once and retain Kata's
installer; the exact release metadata and installed version are recorded. The other four
Kubernetes distributions use their own bundled containerd, as upstream does.
AlmaLinux kubeadm retains its native RPM prerequisite/bootstrap adapter because
the upstream kubeadm helper installs Debian packages.
RKE2's stable version is resolved from its official `channels.yaml`, validated,
recorded with the diagnostics and passed through to upstream's installer. This
avoids its channel redirect service treating an HTTP error as release `stable`.

For workload jobs, Kubernetes is installed before the selected profile and its exact-version Kata dependencies. Each job
checks hashes/provenance, dependencies, asset ownership and dm-verity, then runs
setup twice for idempotence. It always attempts normal and debug smoke Pods;
both must succeed. Each Pod uses the selected installed profile handler, normal CNI
networking and a BusyBox success marker.

Per-mode results retain effective Kata/containerd configuration, shim arguments,
Pod status/events, runtime journals and `host-kernel.log`. The debug phase also
captures the Kata guest's early serial output in `guest-kernel-<pid>.log` and
QMP/CPU diagnostics. Direct-runner files are uploaded directly; VM diagnostics
are copied into `guest-diagnostics.tar` before the job destroys its VM. Test
workloads are removed even on failure.

After every smoke job passes, completion records one small JSON state on the
`development-state` or `release-state` Git branch, according to the channel. Further polls skip when the upstream payload,
package-input fingerprint and smoke-input fingerprint match. If OBS already built
the packages but smoke testing failed, the next poll reuses the existing packages
and retries smoke tests. It does not rebuild.

Development completion explicitly deletes older `development-build-*` and
`development-smoke-*` artifacts in **this repository**, retaining the current
run and leaving releases/unrelated artifacts untouched. Development uses one OBS
package container and replaces its current outputs. The download validator rejects
any set containing extra/old package versions. This does not purge historical OBS
source blobs: the public source-history API has no per-revision deletion operation,
and OBS's wipe operation would delete the current build too. Instance-side source
retention/quotas remain outside the workflow's control. No current numeric hosted
OBS upload quota has been verified. Generic ARM64 QEMU inputs measured about
85.6 MB, compared with a roughly 656 MB upstream bundle.

The state-writing completion job alone has `contents:write` and `actions:write`;
selection/build/smoke jobs have read-only repository access. OBS credentials stay
on the hosted runner and are not copied into the VM. Kubernetes stable channels
and official cloud-image aliases remain moving test inputs; installed versions
and installer bytes are recorded. Setup currently supports default distro data
paths; custom paths and removal/rollback remain follow-up work.

OBS resubmission is keyed by upstream payload identity, target matrix and the Git
content fingerprint of native recipes, imported-payload handling, packaged host
integration, packaging metadata and licenses. The whole repository commit is
recorded for provenance but does not itself trigger rebuilding. Smoke scripts,
workflow orchestration and documentation are excluded from package inputs.

A separate smoke-input fingerprint determines whether tests need rerunning.
Changing only smoke inputs reuses the last successful matching OBS revision and
reruns the tests against those exact packages. Documentation-only changes skip
both. A focused diagnostic run always executes its selected test. Reused package
requests retain their original build commit/version; `smoke-inputs.json` and the
tested state record the current smoke-code revision separately.

Set the repository variable `DEVELOPMENT_SCHEDULE_ENABLED=false` to suspend
scheduled execution during integration/debugging; manual dispatch remains available.
Unset it or set it to `true` to resume the regular schedule.

For an explicitly authorized replacement of an existing stable package, manual
release dispatch accepts `rebuild_version` matching the exact latest package
version. It bypasses reuse and disables that package's publication before source
submission. Full smoke success enables publication again. Scheduled runs never
supply this override; normal stable builds remain retained without replacement.

GPU TEE packaging is x86_64 only. The upstream source also pins the nydus
snapshotter release; Actions verifies that release asset and uses the deployment
templates from the exact Kata commit. Both SNP and TDX configurations use the
NVIDIA base plus GPU and CoCo extensions. The TEE installation job checks a live
nydus/containerd proxy connection without starting a guest or requiring a GPU.
ARM64 continues to produce the existing generic and ordinary NVIDIA packages.
