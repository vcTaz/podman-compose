---
name: verify-compose-diff
description: Install Docker Engine, the Compose plugin, and Podman on a fresh Ubuntu VM, run the compose diff script, and read its exit code.
---

# Verify compose diff

Install Docker Engine, the Compose plugin, and Podman. Run the commands in `harness/README.md`. Read the exit code.

## Install

Run this from the repo root on Ubuntu 24.04.

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 podman python3 python3-yaml uidmap slirp4netns fuse-overlayfs
python3 -m pip install -r requirements.txt
if ! docker info >/dev/null 2>&1; then
  sudo systemctl enable --now docker >/dev/null 2>&1 || \
    sudo dockerd --host=unix:///var/run/docker.sock --iptables=false >/tmp/dockerd.log 2>&1 &
fi
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if [ -S /var/run/docker.sock ] && ! docker info >/dev/null 2>&1; then
    sudo chmod 666 /var/run/docker.sock
  fi
  docker info >/dev/null 2>&1 && break
  sleep 1
done
docker info
docker compose version
user="$(id -un)"
if ! grep -q "^${user}:" /etc/subuid; then
  echo "${user}:100000:65536" | sudo tee -a /etc/subuid >/dev/null
fi
if ! grep -q "^${user}:" /etc/subgid; then
  echo "${user}:100000:65536" | sudo tee -a /etc/subgid >/dev/null
fi
podman ps
python3 podman_compose.py version
```

The harness runs `python3` on `podman_compose.py` in the repo root. Do not pip-install podman-compose.

`chmod 666` on the Docker socket is for a disposable VM. The block is safe to run again.

## Run

From the repo root, run the three commands in the Commands section of `harness/README.md`.

Run the `agree.yml` command and the `profiles.yml` command on every check. Run the `up -d` command when the change under test creates containers.

## Read the result

`python3 harness/compose_diff.py --help` defines the exit codes. The diff is on stdout. `docker compose` is the minus side. This checkout is the plus side. A reason for exit code 2 is on stderr.

The `agree.yml` command exits 0.

The `profiles.yml` command exits 1 while `podman_compose.py` rejects `--profiles`. Docker prints the profile names and exits 0. The command exits 0 after a change that prints those same lines, in that order, and exits 0.

The `up -d` command exits 1 on this tree. Container names use `_` here and `-` under Docker, and this tree creates a pod. Cleanup still runs after `ps`.
