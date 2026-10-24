# Kata Containers packages

DEB packages for **Ubuntu 26.04** and native RPM packages for **AlmaLinux 10.2**,
on x86_64 and aarch64. Only the Rust runtime is shipped; public package names do
not distinguish runtime implementations. Profiles cover generic QEMU, Dragonball, NVIDIA CPU and NVIDIA GPU workloads, including GPU TEEs on x86_64.

Automated packaging consumes the binaries upstream already builds:

- Releases: official `kata-static-VERSION-{amd64,arm64}.tar.zst` assets from the
  latest stable release across all series.
- Development: both Rust tarball artifacts from the latest successful upstream
  push-to-main payload workflow. A daily check at 03:00 UTC discovers new completed builds.

Actions verifies upstream digests, selects the required assets, generates native
OBS source recipes and submits them. OBS assembles packages offline; it does not
rebuild the shim, VMM or guest. [Workflow setup](obs/ACTIONS.md) describes source
selection, credentials, submission, smoke testing and development cleanup.

## Install packages

Both channels publish signed APT and DNF repositories. New stable releases are
published automatically after the full Ubuntu smoke matrix succeeds; previous
stable versions remain available. Development currently publishes when OBS builds
finish; its smoke results are reported separately.

Installing `kata-containers` selects the latest published release from the
stable repository, or the latest published development build from the development
repository. No version needs to be specified.

Choose **one** channel. Both channels install the same package names, so do not
enable both repositories together. Stable packages follow upstream release
versions; development packages use preview versions. Switching from stable to
development requires an explicit downgrade. Previous stable releases remain
available; development replaces its previous build.

### Ubuntu 26.04: APT

Select the stable repository:

```sh
repo='https://download.opensuse.org/repositories/home:/elpacotero/Ubuntu_26.04'
```

Or select development:

```sh
repo='https://download.opensuse.org/repositories/home:/elpacotero:/development/Ubuntu_26.04'
```

Then add the repository and install the generic QEMU profile. This block
defaults to stable if you did not select a channel above:

```sh
(
set -eu
repo="${repo:-https://download.opensuse.org/repositories/home:/elpacotero/Ubuntu_26.04}"
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
curl -fsSL "$repo/Release.key" -o /tmp/kata-containers-Release.key
gpg --dearmor --yes --output /tmp/kata-containers-archive-keyring.gpg /tmp/kata-containers-Release.key
sudo install -m 0644 /tmp/kata-containers-archive-keyring.gpg /usr/share/keyrings/kata-containers-archive-keyring.gpg
printf 'deb [signed-by=/usr/share/keyrings/kata-containers-archive-keyring.gpg] %s/ /\n' "$repo" | sudo tee /etc/apt/sources.list.d/kata-containers.list
sudo apt-get update
sudo apt-get install -y kata-containers
)
```

The repository provides amd64 and arm64 packages; APT selects the host architecture.
`kata-containers` pulls in QEMU, the Rust shim, guest kernel/rootfs and virtiofsd,
with their host dependencies.

### AlmaLinux 10.2: DNF

RPM packages are built for x86_64 and aarch64. **AlmaLinux Kubernetes smoke tests
are temporarily disabled**; Ubuntu is the current workload validation target.

Select stable:

```sh
repo='https://download.opensuse.org/repositories/home:/elpacotero/AlmaLinux_10.2'
```

Or select development:

```sh
repo='https://download.opensuse.org/repositories/home:/elpacotero:/development/AlmaLinux_10.2'
```

Then add the selected repository and install:

```sh
sudo tee /etc/yum.repos.d/kata-containers.repo > /dev/null <<EOF
[kata-containers]
name=Kata Containers
baseurl=$repo/
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=$repo/repodata/repomd.xml.key
EOF
sudo dnf makecache --refresh
sudo dnf install -y kata-containers
```

DNF asks you to trust the OBS project signing key on first use. Keep signature
checking enabled. DNF selects packages for the host architecture.

### Dragonball profile

After adding your chosen repository, install the Dragonball profile:

```sh
sudo apt-get install -y kata-containers-dragonball  # Ubuntu
# Or, on AlmaLinux:
sudo dnf install -y kata-containers-dragonball
```

Dragonball is integrated into the shared Rust shim. The profile installs its
configuration and dedicated upstream experimental guest kernel, reusing the
generic measured rootfs. Its inline virtio-fs needs no external virtiofsd or QEMU.
Use RuntimeClass handler `kata-dragonball` and run `kata-containers-setup` for
your Kubernetes distribution as described below. Profiles can coexist.

Packages support x86_64 and aarch64. Ubuntu x86_64 CI runs normal and debug
kubeadm smoke Pods. ARM64 packages are built; ARM64 and AlmaLinux workloads
remain outside the hosted smoke matrix.

### NVIDIA CPU profile

After adding your chosen repository, install the NVIDIA CPU profile:

```sh
sudo apt-get install -y kata-containers-nvidia-cpu  # Ubuntu
# Or, on AlmaLinux:
sudo dnf install -y kata-containers-nvidia-cpu
```

This installs the shared Rust shim, the NVIDIA base rootfs/kernel and QEMU
without shared filesystems. It does not require a host NVIDIA GPU or GPU driver.
Use RuntimeClass handler `kata-nvidia-cpu` and configure your Kubernetes runtime
as described below. The generic `kata-containers` metapackage continues to select
QEMU; the two profiles can coexist.

NVIDIA CPU requires **containerd >= 2.3.0** with its EROFS snapshotter and
**erofs-utils >= 1.8.2**, installed as a package dependency. Setup checks the
selected Kubernetes distribution's containerd and the required mkfs options.
The profile loads `erofs`, `loop`, `dm_mod` and `dm_verity` in addition to the
usual Kata modules. Host kernel support must be available. It configures
EROFS layers with memory-backed writable layers and dm-verity; fs-verity is disabled to avoid requiring it on the backing filesystem.
No virtiofsd or shared filesystem is used by this profile.

NVIDIA CPU workloads are initially smoke-tested with kubeadm on Ubuntu x86_64,
using the latest stable upstream containerd. Generic QEMU CI uses Ubuntu's
containerd package. Ubuntu's containerd 2.2 does not meet the NVIDIA profile's
minimum; install a supported containerd before running setup.
ARM64 packages are built and installation-tested; hosted ARM runners cannot run
Kata VMs. AlmaLinux workload tests remain disabled, and the host kernel must
supply the required EROFS/device-mapper modules.

### NVIDIA GPU profile

After adding your chosen repository:

```sh
sudo apt-get install -y kata-containers-nvidia-gpu  # Ubuntu
# Or, on AlmaLinux:
sudo dnf install -y kata-containers-nvidia-gpu
```

GPU reuses `kata-containers-nvidia-cpu` for QEMU and the measured NVIDIA base
rootfs/kernel, adding a measured GPU driver extension and UEFI firmware.
Both `kata-nvidia-cpu` and `kata-nvidia-gpu` handlers are installed. Select
`kata-nvidia-gpu` in the GPU RuntimeClass, then run setup as described below.
The generic QEMU profile can coexist with both NVIDIA profiles.

The same containerd >= 2.3.0 and EROFS requirements apply. The GPU profile also
loads `vfio`, `vfio_pci` and `iommufd`. Hosts must supply a compatible kernel,
IOMMU support and GPUs configured for VFIO passthrough. The guest driver comes
from the exact upstream Kata bundle; no host NVIDIA driver is installed.
Device binding and Kubernetes GPU resource/device allocation remain cluster
configuration tasks. AlmaLinux ELRepo integration is deferred.

CI validates GPU package installation, dependency closure, configuration,
firmware ownership and the guest extension's dm-verity tree. **GPU workloads
have no end-to-end coverage yet.** No GPU or guest is started by this check.

### NVIDIA GPU TEE profile (x86_64 only)

After adding your chosen repository:

```sh
sudo apt-get install -y kata-containers-nvidia-gpu-tee  # Ubuntu
# Or, on AlmaLinux:
sudo dnf install -y kata-containers-nvidia-gpu-tee
```

One package installs both `kata-nvidia-gpu-snp` and `kata-nvidia-gpu-tdx`
handlers, their experimental QEMU binaries and SEV-SNP/TDX firmware. It reuses
the NVIDIA base rootfs/kernel and GPU driver extension, adding the measured
CoCo guest extension. The monolithic NVIDIA GPU rootfs is not shipped. No GPU
TEE packages are built for aarch64.

`kata-containers-nydus-snapshotter` supplies `containerd-nydus-grpc` and
`nydus-overlayfs` from the nydus release pinned by the selected Kata source.
Its guest-pull proxy configuration and service follow kata-deploy. Both TEE
handlers select the `nydus-for-kata-tee` snapshotter; the CPU and ordinary GPU
handlers retain EROFS. Nydus state lives in `/var/lib/nydus-for-kata-tee` and is
preserved across upgrades and removal. Local snapshotter settings belong in
`/etc/kata-containers/nydus-snapshotter.toml`.

Run setup for your Kubernetes distribution as below. Setup binds nydus to the
selected CRI service, including server and agent roles. Then run:

```sh
sudo systemctl daemon-reload
sudo systemctl start nydus-for-kata-tee.service
# Restart your Kubernetes distribution's CRI service from the table below.
```

Choose `kata-nvidia-gpu-snp` or `kata-nvidia-gpu-tdx` as the RuntimeClass handler.
The same containerd and host dependencies apply as for the ordinary NVIDIA
profiles. Confidential GPU operation also needs suitable hardware, host kernel
and firmware, VFIO/device allocation, and attestation/key services configured for
your deployment. Installing this package does not provision those services.
CI checks both configurations, dependency resolution, verity trees and a live
nydus/containerd connection. **GPU TEE workloads have no end-to-end coverage.**

### Configure the Kubernetes runtime

Install your Kubernetes distribution first. After installing Kata, run
`kata-containers-setup` as shown below, choosing your `--k8s-distro`, then restart
the corresponding service:

| Kubernetes distribution | Service to restart with `sudo systemctl restart SERVICE` |
| --- | --- |
| kubeadm | `containerd` |
| k0s controller | `k0scontroller` |
| k0s worker | `k0sworker` |
| k3s server | `k3s` |
| k3s agent | `k3s-agent` |
| MicroK8s | `snap.microk8s.daemon-containerd` |
| RKE2 server | `rke2-server` |
| RKE2 agent | `rke2-agent` |

Use a Kubernetes RuntimeClass named `kata-qemu` with `handler: kata-qemu`, and
set `runtimeClassName: kata-qemu` on Pods that should use Kata. Hardware
virtualization must be available on each node running Kata workloads.

## Package layout

| Package | Contents |
| --- | --- |
| `kata-containers` | Metapackage selecting the generic QEMU installation |
| `kata-containers-core` | Rust shim, module loading and `kata-containers-setup` |
| `kata-containers-rootfs-generic` | Generic measured guest root filesystem |
| `kata-containers-kernel-generic` | Generic guest kernel |
| `kata-containers-virtiofsd` | Shared virtio-fs daemon |
| `kata-containers-qemu` | QEMU, firmware, configuration and profile-owned containerd registration |
| `kata-containers-dragonball` | Integrated Dragonball configuration and containerd registration |
| `kata-containers-kernel-dragonball` | Dedicated upstream experimental Dragonball guest kernel |
| `kata-containers-rootfs-nvidia` | NVIDIA base guest root filesystem |
| `kata-containers-kernel-nvidia` | NVIDIA guest kernel |
| `kata-containers-nvidia-cpu` | QEMU without shared filesystems, NVIDIA CPU configuration and EROFS registration |
| `kata-containers-rootfs-nvidia-gpu` | Measured NVIDIA GPU driver guest extension |
| `kata-containers-nvidia-gpu` | GPU configuration, private UEFI firmware and EROFS registration; depends on the CPU profile |
| `kata-containers-nvidia-gpu-tee` | x86_64 SNP/TDX QEMU, firmware, configurations and nydus registration; depends on the GPU profile |
| `kata-containers-rootfs-coco` | Measured CoCo guest extension; x86_64 |
| `kata-containers-nydus-snapshotter` | Guest-pull snapshotter, overlay helper, configuration and systemd service; x86_64 |

The QEMU package depends on exact versions of core, rootfs, kernel and virtiofsd.
The cluster supplies containerd, including the bundled containerd where applicable.
ELF/library dependencies are determined natively. Core requires kmod and Python
3.11+. Profiles register only their installed handlers; generic QEMU uses
`kata-qemu`. Module requirements are loaded
at installation and boot; shared modules are not unloaded. Services are not
restarted automatically by package installation.

Configure containerd explicitly after installation:

```sh
sudo kata-containers-setup --k8s-distro kubeadm --dry-run
sudo kata-containers-setup --k8s-distro kubeadm --apply
sudo kata-containers-setup --k8s-distro kubeadm --status
```

Supported distro arguments are `kubeadm`, `k0s`, `k3s`, `microk8s` and `rke2`.
kubeadm setup migrates standard containerd configuration and enables imports.
k0s uses managed drop-ins; k3s/RKE2 extend their base templates; MicroK8s updates
its template. Setup preserves administrator settings and is idempotent, using
default distro data paths. Restart the selected runtime service when ready.
Custom data directories and setup removal/rollback are pending.

Stable package versions match the upstream release tag, with a packaging
revision and the appropriate distribution suffix. Development uses DEB `0.0.0-0.RUN.ATTEMPT`, RPM Version
`0.0.0` and Release `0.RUN.ATTEMPT.el10`. Stable releases use separate OBS source
packages named `kata-containers-VERSION` in the stable project, retaining previous
versions without changing the installed package names. Upstream's embedded
binary version remains unchanged. Additional VMMs and ELRepo integration are deferred.

## Validation

```sh
python3 -m unittest discover -s tests -v
# Requires Python 3.11+, zstd for bundle/source preparation.
```

Both source recipes have been built locally using a real upstream ARM64
binary bundle. The generic and NVIDIA package sets are checked for clean installation, dependency,
configuration, ownership/hash and dm-verity checks.
Authenticated OBS builds have passed on both distributions and architectures.
All five Ubuntu Kubernetes distributions passed normal and debug smoke Pods.
Both release and development workflows run this Ubuntu matrix.
AlmaLinux workload validation is temporarily disabled; native RPM builds remain enabled. Stable releases are automatically published after smoke tests pass.

The standalone standard-path validators require a separately installed system
containerd and run only on disposable target hosts:

```sh
# Ubuntu: also install dpkg-dev and cryptsetup-bin.
sudo python3 scripts/validate.py /path/to/debs --install --fresh
# AlmaLinux: also install EPEL containerd, createrepo_c and veritysetup.
sudo python3 scripts/validate_rpm.py /path/to/rpms --install --fresh
# Add --profile nvidia-cpu, nvidia-gpu or nvidia-gpu-tee for that installation.
```

The older optional `scripts/build.py` still builds components from source using
network-enabled privileged Docker builders. It is independent of the automated
upstream-binary packaging path.

Packaging code is licensed under [MIT](LICENSE). Bundled components retain their
original licenses, including Kata's [Apache-2.0](LICENSES/Apache-2.0.txt). Packaging
preserves upstream license/version information; the complete bundled-component
notice/source inventory remains pending.

Runtime configuration follows kata-deploy's layout, without the runtime-rs name:
`/opt/kata/share/defaults/kata-containers/configuration-qemu.toml` is a relative
symlink to `runtimes/qemu/configuration-qemu.toml`. The QEMU package installs
`runtimes/qemu/config.d/` beside the real base configuration. Containerd uses
the public configuration path; the shim resolves it before
loading that profile's drop-ins. Put local overrides in `config.d/50-user-overrides.toml`.
Later filenames override earlier ones. The base configuration is package-managed;
local drop-ins remain separate across upgrades and profiles.
