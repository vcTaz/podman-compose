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

Run the `agree.yml` command and the `profiles.yml` command on every check. They compare `config` only, so they need neither the Docker daemon nor Podman. Run the `compat.yml` command when the change under test creates containers.

## Read the result

`python3 harness/compose_diff.py --help` defines the exit codes. The diff is on stdout. `docker compose` is the minus side. This checkout is the plus side. A reason for exit code 2 is on stderr.

All three commands exit 0 on this tree.

The `profiles.yml` command compares the profile names line by line, in order, along with the exit code.

The `compat.yml` command compares `up -d` by exit code, then compares the container names from `ps`. Cleanup still runs after `ps`. If Docker Hub rate-limits the pull, copy the image from Podman with `podman save busybox:1.36 | docker load`.
