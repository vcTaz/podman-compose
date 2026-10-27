# Compose diff

`harness/compose_diff.py` runs a compose file under `docker compose` and under this checkout's `podman_compose.py`. It prints a unified diff. The script passes the same project name to both engines.

When every step is `config`, the script reads files only. It needs the `docker compose` plugin, but no Docker daemon, no Podman, and no cleanup.

Any other step runs real containers. The script then deletes the containers, pods, networks, and volumes for that project when a run starts and when a run ends. Deletion runs after the last step, including `up -d` and then `ps`.

## Commands

From the repo root:

```bash
python3 harness/compose_diff.py harness/fixtures/agree.yml
python3 harness/compose_diff.py harness/fixtures/profiles.yml -- config --profiles
python3 harness/compose_diff.py harness/fixtures/compat.yml -- up -d -- ps
```

The first command compares `config`. The second compares `config --profiles`. The third runs `up -d`, then `ps`, then deletion. `up` and `down` are compared by exit code only, because their progress output differs by design. The `ps` step after them shows the effect.

`compat.yml` sets `x-podman: docker_compose_compat: true`, so container names use `-` and no pod is created. Without it, `ps` always differs: podman-compose names containers with `_` by default.

`python3 harness/compose_diff.py --help` prints the exit codes.

Install steps and how to read a result are in `.cursor/skills/verify-compose-diff/SKILL.md`.
