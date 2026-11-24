#!/usr/bin/env bash
# CI only: after freeing the runner's disk, put Docker's data on the filesystem with the most
# free space (/ or the /mnt temp disk) and use the classic overlay2 store, so each layer is
# stored once (the containerd snapshotter keeps an extra compressed copy of every layer).
set -Eeuo pipefail

# What jlumbroso/free-disk-space leaves behind (none of it is used by this job).
sudo rm -rf /usr/share/swift /usr/local/share/powershell /usr/local/share/chromium \
  /opt/microsoft /opt/google /usr/lib/jvm /opt/az /usr/local/julia* /usr/share/miniconda \
  /opt/pipx /usr/local/aws-cli /usr/local/aws-sam-cli /usr/local/lib/node_modules \
  /home/linuxbrew /usr/local/share/vcpkg /opt/hostedtoolcache 2>/dev/null || true

free_kb() { df --output=avail -k "$1" 2>/dev/null | tail -1 || echo 0; }

df -h / /mnt 2>/dev/null || df -h /
root_kb="$(free_kb /)"
mnt_kb=0
[[ -d /mnt ]] && mnt_kb="$(free_kb /mnt)"
if (( mnt_kb > root_kb )); then data=/mnt/docker; else data=/var/lib/docker; fi
echo "docker data-root -> $data (free: / $((root_kb / 1048576)) GB, /mnt $((mnt_kb / 1048576)) GB)"

sudo systemctl stop docker docker.socket
sudo mkdir -p "$data"
echo "{\"data-root\": \"$data\", \"features\": {\"containerd-snapshotter\": false}}" \
  | sudo tee /etc/docker/daemon.json
sudo systemctl start docker
docker info --format 'driver={{.Driver}} root={{.DockerRootDir}}'
