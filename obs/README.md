# OBS packaging

The automated path packages upstream Kata binaries, without compiling components
on OBS. Release inputs are official `kata-static-VERSION-{amd64,arm64}.tar.zst`
assets. Development inputs are the two Rust tarball artifacts from the latest
successful push-to-main `payload-after-push.yaml` run.

Actions verifies upstream SHA256 digests, selects generic QEMU and NVIDIA CPU/GPU payloads (including x86_64 GPU TEEs),
generates native `.dsc` and `.spec` recipes and submits one atomic OBS source
revision. Native OBS jobs assemble fourteen x86_64 or eleven aarch64 packages without network access,
Docker or compilation. `scripts/prepare_obs.py` generates both recipe types;
`scripts/assemble.py --stage-only` shares payload selection with native RPM builds.
Architecture inputs are split into 64 MiB payload pieces, wrapped as supplementary
Debian orig tarballs and RPM Source entries. Both builders reconstruct the
selected architecture offline. The packaging code stays in the small main orig
archive, avoiding a single large binary upload request.

Stable releases use retained containers `home:elpacotero/kata-containers-VERSION`,
with `-rREVISION` for later packaging revisions. Development uses
`home:elpacotero:development/kata-containers`. Leave their SCM Sync URL empty:
the binary inputs are uploaded directly through the API, rather than committed
to Git. Existing project metadata is preserved while missing Ubuntu 26.04 and
AlmaLinux 10.2 repositories are added for x86_64 and aarch64. A project with no
explicit publish setting is initially configured with publishing disabled;
packages remain downloadable through the authenticated build API for testing.

The optional legacy source-build entry point remains `network-build.sh`; it is
not used by automated OBS packaging.

See [workflow configuration](ACTIONS.md).
