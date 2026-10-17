#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Provision tooling on one disposable GitHub-hosted runner. Creates no test VM.
set -euxo pipefail
sudo apt-get update
sudo apt-get install -y libvirt-daemon-system libvirt-clients python3-libvirt python3-venv \
    qemu-system-x86 qemu-utils ovmf genisoimage rpm
sudo systemctl start libvirtd
if ! sudo virsh net-info default >/dev/null 2>&1; then
    sudo virsh net-define /usr/share/libvirt/networks/default.xml
fi
if ! sudo virsh net-info default | grep -q 'Active:.*yes'; then
    sudo virsh net-start default
fi
if ! sudo virsh pool-info default >/dev/null 2>&1; then
    sudo virsh pool-define-as default dir --target /var/lib/libvirt/images
    sudo virsh pool-build default
fi
if ! sudo virsh pool-info default | grep -q 'State:.*running'; then
    sudo virsh pool-start default
fi
python3 -m venv --system-site-packages .smoke-venv
.smoke-venv/bin/pip install 'git+https://github.com/karmab/kcli.git@75bdfdf8a194d8cdf229a381a7ecbd018da1a247'
mkdir -p .smoke-vm
cat > .smoke-vm/kcli-config.yml <<'EOF'
default:
  client: local
local:
  type: kvm
  host: 127.0.0.1
  pool: default
EOF
sudo install -d -m0700 /root/.kcli
sudo install -m0600 .smoke-vm/kcli-config.yml /root/.kcli/config.yml
