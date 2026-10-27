"""upgrade-azerothcore.sh safety invariants.

Docker, crontab and sleep are stubs on a stub-only PATH; the stack, the work
root and every git repository live under tmp_path. Nothing here can reach the
host Docker daemon or /opt/stacks.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path("/src") if Path("/src/upgrade-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
ROOT_GUARD = (
    'if [ "$EUID" -eq 0 ]; then\n'
    '    echo "ERROR: do not run as root; docker is invoked as your user (docker group)." >&2\n'
    '    exit 1\n'
    'fi\n'
)
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
MUTATING = (" stop", "docker tag", "docker run", "compose up", "docker volume create", "docker start",
            "docker rm", "docker image rm", "docker network create")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed in the sandbox")


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo(path: Path, branch: str, origin: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", branch, cwd=path)
    _git("remote", "add", "origin", origin, cwd=path)
    (path / "README").write_text(f"{path.name}\n")
    _git("add", "README", cwd=path)
    _git("commit", "-qm", "base", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _fixture(tmp_path: Path):
    tools = tmp_path / "repo" / "scripts"
    shutil.copytree(SCRIPTS_DIR, tools, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    for name in ("upgrade-azerothcore.sh", "restore-drill.sh"):
        text = (tools / name).read_text()
        assert ROOT_GUARD in text
        (tools / name).write_text(text.replace(ROOT_GUARD, "", 1))
    _git("init", "-q", cwd=tmp_path / "repo")
    _git("add", ".", cwd=tmp_path / "repo")
    _git("commit", "-qm", "tooling", cwd=tmp_path / "repo")
    previous = _git("rev-parse", "HEAD", cwd=tmp_path / "repo")

    stack = tmp_path / "stack"
    core = _repo(stack, "Playerbot", "https://example.invalid/core.git")
    revs = {"core": core}
    for module in ("mod-playerbots", "mod-ah-bot-plus", "mod-individual-progression"):
        revs[module] = _repo(stack / "modules" / module, "master", f"https://example.invalid/{module}.git")
    (stack / ".env").write_text("COMPOSE_PROJECT_NAME=azerothcore\nDOCKER_IMAGE_TAG=playerbot-local\n"
                                "DOCKER_USER_ID=1000\nDOCKER_GROUP_ID=1000\n")
    for sub in ("logs", "backups", "configs/modules", "env/dist/etc"):
        (stack / sub).mkdir(parents=True, exist_ok=True)
    (stack / "logs" / "Errors.log").write_text("")
    for name in ("worldserver", "authserver", "dbimport"):
        for suffix in (".conf", ".conf.dist"):
            (stack / "env/dist/etc" / f"{name}{suffix}").write_text(f"{name} = 1\n")
    for name in ("playerbots", "individualProgression"):
        for suffix in (".conf", ".conf.dist"):
            (stack / "configs/modules" / f"{name}{suffix}").write_text(f"{name}.X = 1\n")
    (stack / "configs/modules/mod_ahbot.conf.dist").write_text("A = 1\nAuctionHouseBot.GUIDs = 0\n")
    (stack / "configs/modules/mod_ahbot.conf").write_text("A = 1\n\nAuctionHouseBot.GUIDs = 2501\n")
    shutil.copy(tools / "backup.sh", stack / "backup.sh")

    (tools / "upstream-versions.env").write_text("".join([
        "AC_CORE_REPO=https://example.invalid/core.git\nAC_CORE_BRANCH=Playerbot\n",
        f"AC_CORE_REV={revs['core']}\n",
        "MOD_PLAYERBOTS_REPO=https://example.invalid/mod-playerbots.git\nMOD_PLAYERBOTS_BRANCH=master\n",
        f"MOD_PLAYERBOTS_REV={revs['mod-playerbots']}\n",
        "MOD_AHBOT_REPO=https://example.invalid/mod-ah-bot-plus.git\nMOD_AHBOT_BRANCH=master\n",
        f"MOD_AHBOT_REV={revs['mod-ah-bot-plus']}\n",
        "MOD_IP_REPO=https://example.invalid/mod-individual-progression.git\nMOD_IP_BRANCH=master\n",
        f"MOD_IP_REV={revs['mod-individual-progression']}\nCLIENT_DATA_VERSION=v20.0\n",
    ]))

    bind = tmp_path / "bin"
    bind.mkdir()
    _executable(bind / "docker", """#!/bin/bash
echo "docker $*" >> "$UPGRADE_CALLS"
case "$*" in
  "inspect -f {{.State.Status}} "*) echo running ;;
  "inspect -f {{.State.StartedAt}} "*) echo 2026-10-27T08:00:00Z ;;
  "logs --since "*) echo "WORLD: World Initialized In 10 seconds" ;;
  "exec ac-database "*) echo "${FAKE_HUMANS_ONLINE:-0}" ;;
  "image inspect "*) echo sha256:abc ;;
  "run --rm --network none --user 0 "*) echo "INSTALLED_VERSION=v19" ;;
esac
exit 0
""")
    _executable(bind / "crontab", f"#!/bin/sh\necho '0 3 * * * {stack}/backup.sh'\n")
    _executable(bind / "sleep", "#!/bin/sh\nexit 0\n")
    for tool in ("awk", "basename", "bash", "cat", "chmod", "cmp", "comm", "cut", "date", "df", "diff",
                 "dirname", "find", "flock", "git", "grep", "head", "ln", "mkdir", "python3", "readlink",
                 "sed", "sha256sum", "sort", "stat", "tail", "tar", "tee", "tr", "wc"):
        real = shutil.which(tool)
        assert real, tool
        (bind / tool).symlink_to(real)
    assert shutil.which("docker", path=str(bind)) == str(bind / "docker")
    return tools, stack, bind, previous


def _run(tmp_path, tools, stack, bind, *args, **env):
    return subprocess.run(
        ["/bin/bash", str(tools / "upgrade-azerothcore.sh"), *args],
        capture_output=True, text=True, timeout=60,
        env={"PATH": str(bind), "HOME": str(tmp_path), "STACK_DIR": str(stack),
             "ADMIN_STACK_DIR": str(tmp_path / "admin"), "UPGRADE_ROOT": str(tmp_path / "work"),
             "UPGRADE_CALLS": str(tmp_path / "calls"), "MIN_FREE_GB": "0", **GIT_ENV, **env},
    )


def _calls(tmp_path: Path) -> str:
    path = tmp_path / "calls"
    return path.read_text() if path.exists() else ""


def test_preflight_passes_on_a_healthy_clean_stack_and_mutates_nothing(tmp_path):
    tools, stack, bind, previous = _fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "preflight", "--previous-repo-rev", previous)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "== Preflight: PASS" in r.stdout
    assert not any(m in _calls(tmp_path) for m in MUTATING)
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("breakage, message", [
    (lambda s: (s / "backup.sh").write_text("#!/bin/sh\n# stale\n"), "deployed"),
    (lambda s: (s / "configs/modules/playerbots.conf").write_text("edited = 1\n"), "playerbots.conf differs"),
    (lambda s: (s / "configs/modules/mod_ahbot.conf").write_text("A = 2\nAuctionHouseBot.GUIDs = 2501\n"), "beyond AuctionHouseBot.GUIDs"),
    (lambda s: (s / "modules/mod-playerbots/README").write_text("local edit\n"), "not managed patches"),
    (lambda s: (s / "env/dist/etc/worldserver.conf").write_text("edited = 1\n"), "worldserver.conf differs"),
])
def test_preflight_fails_closed_on_unsafe_state(tmp_path, breakage, message):
    tools, stack, bind, previous = _fixture(tmp_path)
    breakage(stack)
    r = _run(tmp_path, tools, stack, bind, "preflight", "--previous-repo-rev", previous)
    assert r.returncode == 1
    assert message in r.stdout


def test_cutover_refuses_online_players_without_touching_the_stack(tmp_path):
    tools, stack, bind, previous = _fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             FAKE_HUMANS_ONLINE="2")
    assert r.returncode == 1
    assert "real-player character(s) online" in r.stdout
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_cutover_refuses_without_a_staged_candidate(tmp_path):
    tools, stack, bind, previous = _fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes")
    assert r.returncode == 1
    assert "run 'stage' first" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_cutover_refuses_without_a_passing_rehearsal(tmp_path):
    tools, stack, bind, previous = _fixture(tmp_path)
    work = tmp_path / "work" / "staging"
    work.mkdir(parents=True)
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    (work / f"stage-{core12}.env").write_text("".join(
        f"CANDIDATE_ID_{svc}=sha256:abc\n" for svc in ("worldserver", "authserver", "db_import", "client_data")))
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes")
    assert r.returncode == 1
    assert "no passing rehearsal" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_rollback_refuses_a_corrupted_preupgrade_archive(tmp_path):
    tools, stack, bind, _ = _fixture(tmp_path)
    state = tmp_path / "work" / "cutover-x"
    state.mkdir(parents=True)
    archive = state / "azerothcore-backup-preupgrade-x.tar.gz"
    archive.write_bytes(b"archive")
    (state / f"{archive.name}.sha256").write_text(f"{'0' * 64}  {archive.name}\n")
    (state / "state.env").write_text(f"TS=x\nPREUPGRADE_ARCHIVE={archive}\n")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "checksum mismatch" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_finalize_refuses_an_incomplete_cutover(tmp_path):
    tools, stack, bind, _ = _fixture(tmp_path)
    state = tmp_path / "work" / "cutover-x"
    state.mkdir(parents=True)
    (state / "state.env").write_text("TS=x\nSTEP_stop=1\n")
    r = _run(tmp_path, tools, stack, bind, "finalize", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "did not complete" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)
