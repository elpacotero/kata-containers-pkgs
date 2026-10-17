# OBS packaging

The automated path packages upstream Kata binaries, without compiling components
on OBS. Release inputs are official `kata-static-VERSION-{amd64,arm64}.tar.zst`
assets. Development inputs are the two Rust tarball artifacts from the latest
successful push-to-main `payload-after-push.yaml` run.

Actions verifies upstream SHA256 digests, selects the generic QEMU payload,
generates native `.dsc` and `.spec` recipes and submits one atomic OBS source
revision. Native OBS jobs assemble the same six packages without network access,
Docker or compilation. `scripts/prepare_obs.py` generates both recipe types;
`scripts/assemble.py --stage-only` shares payload selection with native RPM builds.

Use package containers `home:elpacotero/kata-containers` and
`home:elpacotero:development/kata-containers`. Leave their SCM Sync URL empty:
the binary inputs are uploaded directly through the API, rather than committed
to Git. Existing project metadata is preserved while missing Ubuntu 26.04 and
AlmaLinux 10.2 repositories are added for x86_64 and aarch64. A project with no
explicit publish setting is initially configured with publishing disabled;
packages remain downloadable through the authenticated build API for testing.

The optional legacy source-build entry point remains `network-build.sh`; it is
not used by automated OBS packaging.

See [workflow configuration](ACTIONS.md).
