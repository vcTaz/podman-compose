from __future__ import annotations

import difflib
import fcntl
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from typing import Union

import yaml

MATCH = 0
DIFFER = 1
USAGE = 2

__all__ = ["main"]

Json = Union[None, bool, int, float, str, list["Json"], dict[str, "Json"]]
JsonObj = dict[str, Json]
Obs = Union["ConfigObs", "LinesObs", "NamesObs", "TextObs"]

_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_CONTAINER_ID = re.compile(r"\b[0-9a-fA-F]{12,64}\b")
_SCRUBBED_ENV = frozenset({
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "COMPOSE_PATH_SEPARATOR",
    "PODMAN_COMPOSE_NAME_SEPARATOR_COMPAT",
    "PODMAN_COMPOSE_IN_POD",
})
_OWNED_FLAGS = frozenset({"-f", "--file", "-p", "--project-name"})


class HarnessError(Exception):
    pass


@dataclass(frozen=True)
class ProjectName:
    value: str

    def __post_init__(self) -> None:
        if _PROJECT.fullmatch(self.value) is None:
            raise HarnessError(f"invalid project name {self.value!r}")

    @classmethod
    def for_compose(cls, compose: Path) -> ProjectName:
        digest = hashlib.sha256(str(compose.resolve()).encode()).hexdigest()
        return cls("pcd" + digest[:12])


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.argv:
            raise HarnessError("empty step")
        if self.argv[0] in _OWNED_FLAGS:
            raise HarnessError(f"step cannot start with {self.argv[0]}")


@dataclass(frozen=True)
class Engine:
    label: str
    program: tuple[str, ...]


@dataclass(frozen=True)
class Scenario:
    compose: Path
    project: ProjectName
    source: JsonObj
    steps: tuple[Step, ...]

    @classmethod
    def load(cls, argv: list[str]) -> Scenario:
        if len(argv) < 2:
            raise HarnessError("missing compose file")
        compose = Path(argv[1]).resolve()
        if not compose.is_file():
            raise HarnessError(f"not a file: {compose}")
        tokens = argv[2:]
        steps: tuple[Step, ...]
        if not tokens:
            steps = (Step(("config",)),)
        elif tokens[0] != "--":
            raise HarnessError("steps must follow --")
        else:
            groups: list[list[str]] = [[]]
            for token in tokens[1:]:
                if token == "--":
                    groups.append([])
                else:
                    groups[-1].append(token)
            parsed: list[Step] = []
            for group in groups:
                if not group:
                    raise HarnessError("empty step")
                parsed.append(Step(tuple(group)))
            steps = tuple(parsed)
        source = _as_json_obj(_load_yaml(compose.read_text(encoding="utf-8")))
        return cls(compose, ProjectName.for_compose(compose), source, steps)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ConfigObs:
    document: JsonObj


@dataclass(frozen=True)
class LinesObs:
    exit_code: int
    stdout_lines: tuple[str, ...]
    stderr_lines: tuple[str, ...]


@dataclass(frozen=True)
class NamesObs:
    exit_code: int
    names: tuple[str, ...]


@dataclass(frozen=True)
class TextObs:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class _Rule:
    match: Callable[[Step], bool]
    build: Callable[[Step, tuple[CommandResult, CommandResult], JsonObj], tuple[Obs, Obs]]


@dataclass(frozen=True)
class RunReap:
    argv: tuple[str, ...]
    absent_ok: tuple[str, ...]


@dataclass(frozen=True)
class ListReap:
    list_argv: tuple[str, ...]
    remove_prefix: tuple[str, ...]
    skip: frozenset[str]


ReapAction = Union[RunReap, ListReap]


@dataclass
class _HeldLock:
    fd: int


def main(argv: list[str] | None = None) -> int:
    """Run one scenario. Never raises. USAGE, DIFFER, or MATCH."""
    args = list(sys.argv) if argv is None else argv
    try:
        if len(args) < 2:
            sys.stdout.write(_help())
            return USAGE
        if args[1] in ("-h", "--help"):
            sys.stdout.write(_help())
            return MATCH
        return _run_scenario(Scenario.load(args))
    except HarnessError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return USAGE


def _help() -> str:
    return (
        "usage: python3 harness/compose_diff.py FILE [-- STEP [-- STEP ...]]\n"
        "\n"
        "Compare FILE under docker compose and this checkout.\n"
        "With no STEP, the script runs config.\n"
        "\n"
        f"Exit {MATCH} when every step matches.\n"
        f"Exit {DIFFER} when at least one step differs.\n"
        "The unified diff is on stdout. docker compose is the minus side.\n"
        f"Exit {USAGE} when the script cannot run, or cleanup fails after a match.\n"
        "The reason is on stderr.\n"
    )


def _engines() -> tuple[Engine, Engine]:
    script = Path(__file__).resolve().parent.parent / "podman_compose.py"
    return (
        Engine("docker compose", ("docker", "compose")),
        Engine("podman_compose.py", (sys.executable, str(script))),
    )


def _spawn(argv: tuple[str, ...], cwd: Path) -> CommandResult:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=_child_env(),
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise HarnessError(f"failed to run {' '.join(argv)}: {exc}") from exc
    return CommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout.decode("utf-8", errors="replace"),
        stderr=completed.stderr.decode("utf-8", errors="replace"),
    )


def _child_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}


def _load_yaml(text: str) -> Json:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HarnessError(f"invalid yaml: {exc}") from exc
    return _as_json(loaded)


def _as_json(value: object) -> Json:
    """Check bool with int, since bool is an int."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, list):
        return [_as_json(item) for item in value]
    if isinstance(value, dict):
        mapped: dict[str, Json] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise HarnessError(f"yaml key is {type(key).__name__}, not str")
            mapped[key] = _as_json(item)
        return mapped
    raise HarnessError(f"yaml value is {type(value).__name__}, not json")


def _as_json_obj(value: object) -> JsonObj:
    parsed = _as_json(value)
    if not isinstance(parsed, dict):
        raise HarnessError("yaml root is not a mapping")
    return parsed


def _observations(
    step: Step,
    results: tuple[CommandResult, CommandResult],
    source: JsonObj,
) -> tuple[Obs, Obs]:
    for rule in _RULES:
        if rule.match(step):
            return rule.build(step, results, source)
    return _build_text(step, results, source)


def _match_profiles(step: Step) -> bool:
    return step.argv[0] == "config" and "--profiles" in step.argv[1:]


def _match_ps(step: Step) -> bool:
    return step.argv[0] == "ps"


def _match_config(step: Step) -> bool:
    return step.argv[0] == "config"


def _build_lines(
    step: Step,
    results: tuple[CommandResult, CommandResult],
    source: JsonObj,
) -> tuple[Obs, Obs]:
    def one(result: CommandResult) -> LinesObs:
        return LinesObs(
            result.exit_code,
            tuple(result.stdout.splitlines()),
            tuple(result.stderr.splitlines()),
        )

    return one(results[0]), one(results[1])


def _build_names(
    step: Step,
    results: tuple[CommandResult, CommandResult],
    source: JsonObj,
) -> tuple[Obs, Obs]:
    if results[0].exit_code != 0 or results[1].exit_code != 0:
        return _build_text(step, results, source)
    try:
        docker_names = _names_from_table(results[0].stdout)
        podman_names = _names_from_table(results[1].stdout)
    except HarnessError:
        return _build_text(step, results, source)
    return (
        NamesObs(results[0].exit_code, docker_names),
        NamesObs(results[1].exit_code, podman_names),
    )


def _build_config(
    step: Step,
    results: tuple[CommandResult, CommandResult],
    source: JsonObj,
) -> tuple[Obs, Obs]:
    documents: list[JsonObj] = []
    for result in results:
        if result.exit_code != 0:
            return _build_text(step, results, source)
        try:
            parsed = _as_json_obj(_load_yaml(result.stdout))
        except HarnessError:
            return _build_text(step, results, source)
        documents.append(_canonicalize(parsed, source))
    return ConfigObs(documents[0]), ConfigObs(documents[1])


def _build_text(
    step: Step,
    results: tuple[CommandResult, CommandResult],
    source: JsonObj,
) -> tuple[Obs, Obs]:
    return (
        TextObs(results[0].exit_code, _mask(results[0].stdout), _mask(results[0].stderr)),
        TextObs(results[1].exit_code, _mask(results[1].stdout), _mask(results[1].stderr)),
    )


_RULES = (
    _Rule(_match_profiles, _build_lines),
    _Rule(_match_ps, _build_names),
    _Rule(_match_config, _build_config),
)


def _names_from_table(stdout: str) -> tuple[str, ...]:
    rows = [line for line in stdout.splitlines() if line.split()]
    if not rows:
        raise HarnessError("ps output has no header")
    header = rows[0].split()
    if header[-1] == "NAMES":
        slot = -1
    elif header[0] == "NAME":
        slot = 0
    else:
        raise HarnessError("ps output has no name column")
    names: list[str] = []
    for row in rows[1:]:
        cells = row.split()
        try:
            names.append(cells[slot])
        except IndexError as exc:
            raise HarnessError("ps row has no name cell") from exc
    names.sort()
    return tuple(names)


def _canonicalize(emitted: JsonObj, source: JsonObj) -> JsonObj:
    document = _as_json_obj(emitted)
    document.pop("name", None)
    source_networks_raw = source.get("networks")
    source_networks: JsonObj = source_networks_raw if isinstance(source_networks_raw, dict) else {}
    if "default" not in source_networks:
        emitted_networks = document.get("networks")
        if isinstance(emitted_networks, dict):
            emitted_networks.pop("default", None)
            if not emitted_networks:
                document.pop("networks", None)
    for section in ("networks", "volumes"):
        emitted_section = document.get(section)
        if not isinstance(emitted_section, dict):
            continue
        source_section_raw = source.get(section)
        source_section: JsonObj = source_section_raw if isinstance(source_section_raw, dict) else {}
        for key, spec in emitted_section.items():
            source_spec = source_section.get(key)
            unnamed = source_spec is None or source_spec == {}
            if isinstance(source_spec, dict) and "name" not in source_spec:
                unnamed = True
            if unnamed and isinstance(spec, dict):
                spec.pop("name", None)
    emitted_services = document.get("services")
    source_services = source.get("services")
    if isinstance(emitted_services, dict) and isinstance(source_services, dict):
        for key, spec in emitted_services.items():
            source_spec = source_services.get(key)
            if not isinstance(spec, dict) or not isinstance(source_spec, dict):
                continue
            if "networks" in source_spec:
                continue
            service_networks = spec.get("networks")
            empty_default = service_networks == {"default": None} or service_networks == {
                "default": {}
            }
            if empty_default:
                spec.pop("networks", None)
    return document


def _mask(text: str) -> str:
    return "\n".join(_CONTAINER_ID.sub("<id>", text).splitlines())


def _matches(docker: Obs, podman: Obs) -> bool:
    return type(docker) is type(podman) and docker == podman


def _render(obs: Obs) -> str:
    if isinstance(obs, ConfigObs):
        text = yaml.safe_dump(obs.document, sort_keys=True, width=1000)
        if not text.endswith("\n"):
            text += "\n"
        return text
    if isinstance(obs, LinesObs):
        stdout = "".join(f"{line}\n" for line in obs.stdout_lines)
        stderr = "".join(f"{line}\n" for line in obs.stderr_lines)
        return f"exit_code: {obs.exit_code}\nstdout_lines:\n{stdout}stderr_lines:\n{stderr}"
    if isinstance(obs, NamesObs):
        names = "".join(f"{name}\n" for name in obs.names)
        return f"exit_code: {obs.exit_code}\nnames:\n{names}"
    stdout = obs.stdout
    if stdout and not stdout.endswith("\n"):
        stdout += "\n"
    stderr = obs.stderr
    if stderr and not stderr.endswith("\n"):
        stderr += "\n"
    return f"exit_code: {obs.exit_code}\nstdout:\n{stdout}stderr:\n{stderr}"


def _render_diff(
    index: int,
    step: Step,
    docker: Obs,
    podman: Obs,
    engines: tuple[Engine, Engine],
) -> str:
    left_name = type(docker).__name__
    right_name = type(podman).__name__
    class_name = left_name if left_name == right_name else f"{left_name} {right_name}"
    header = f"{index} {' '.join(step.argv)} {class_name}"
    if isinstance(docker, (LinesObs, NamesObs, TextObs)) and isinstance(
        podman, (LinesObs, NamesObs, TextObs)
    ):
        header = f"{header} exit {docker.exit_code} {podman.exit_code}"
    diff = difflib.unified_diff(
        _render(docker).splitlines(keepends=True),
        _render(podman).splitlines(keepends=True),
        fromfile=engines[0].label,
        tofile=engines[1].label,
    )
    return header + "\n" + "".join(diff)


def _reap_plan(scenario: Scenario) -> tuple[ReapAction, ...]:
    compose = str(scenario.compose)
    project = scenario.project.value
    label = f"label=io.podman.compose.project={project}"
    return (
        RunReap(
            (
                "docker",
                "compose",
                "-f",
                compose,
                "-p",
                project,
                "down",
                "--volumes",
                "--remove-orphans",
            ),
            (),
        ),
        ListReap(
            ("podman", "ps", "-aq", "--filter", label),
            ("podman", "rm", "-f"),
            frozenset({""}),
        ),
        RunReap(
            ("podman", "pod", "rm", "-f", f"pod_{project}"),
            ("no such pod", "not found"),
        ),
        ListReap(
            (
                "podman",
                "network",
                "ls",
                "--noheading",
                "--filter",
                label,
                "--format",
                "{{.Name}}",
            ),
            ("podman", "network", "rm"),
            frozenset(),
        ),
        ListReap(
            (
                "podman",
                "volume",
                "ls",
                "--noheading",
                "--filter",
                label,
                "--format",
                "{{.Name}}",
            ),
            ("podman", "volume", "rm"),
            frozenset(),
        ),
    )


def _reap(scenario: Scenario) -> None:
    failures: list[str] = []
    cwd = scenario.compose.parent

    def failed(argv: tuple[str, ...], result: CommandResult) -> None:
        detail = result.stderr.strip() or result.stdout.strip()
        failures.append(f"{' '.join(argv)} exited {result.exit_code}: {detail}")

    for action in _reap_plan(scenario):
        try:
            if isinstance(action, RunReap):
                result = _spawn(action.argv, cwd)
                stderr = result.stderr.lower()
                absent = any(marker in stderr for marker in action.absent_ok)
                if result.exit_code != 0 and not absent:
                    failed(action.argv, result)
            else:
                listed = _spawn(action.list_argv, cwd)
                if listed.exit_code != 0:
                    failed(action.list_argv, listed)
                    continue
                names: list[str] = []
                for line in listed.stdout.splitlines():
                    name = line.strip()
                    if name and name not in action.skip:
                        names.append(name)
                if not names:
                    continue
                removed = _spawn(action.remove_prefix + tuple(names), cwd)
                if removed.exit_code != 0:
                    failed(action.remove_prefix + tuple(names), removed)
        except HarnessError as exc:
            failures.append(str(exc))
    if failures:
        raise HarnessError("\n".join(failures))


def _lock(project: ProjectName) -> _HeldLock:
    path = f"/tmp/compose-diff-{project.value}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return _HeldLock(fd)


def _unlock(held: _HeldLock) -> None:
    try:
        fcntl.flock(held.fd, fcntl.LOCK_UN)
    finally:
        os.close(held.fd)


def _probe(engines: tuple[Engine, Engine]) -> None:
    cwd = Path(__file__).resolve().parent.parent
    for engine in engines:
        result = _spawn(engine.program + ("version",), cwd)
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HarnessError(f"{engine.label} version exited {result.exit_code}: {detail}")


def _run_scenario(scenario: Scenario) -> int:
    held = _lock(scenario.project)
    code = MATCH
    try:
        engines = _engines()
        _probe(engines)
        _reap(scenario)
        cwd = scenario.compose.parent
        placed = ("-f", str(scenario.compose), "-p", scenario.project.value)
        for index, step in enumerate(scenario.steps):
            docker_result = _spawn(engines[0].program + placed + step.argv, cwd)
            podman_result = _spawn(engines[1].program + placed + step.argv, cwd)
            pair = (docker_result, podman_result)
            docker_obs, podman_obs = _observations(step, pair, scenario.source)
            if not _matches(docker_obs, podman_obs):
                sys.stdout.write(_render_diff(index, step, docker_obs, podman_obs, engines))
                code = DIFFER
    except HarnessError as exc:
        print(str(exc), file=sys.stderr)
        code = USAGE
    finally:
        try:
            _reap(scenario)
        except HarnessError as exc:
            print(str(exc), file=sys.stderr)
            if code == MATCH:
                code = USAGE
        finally:
            _unlock(held)
    return code


if __name__ == "__main__":
    sys.exit(main())
