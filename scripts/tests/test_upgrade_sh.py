"""upgrade-azerothcore.sh safety invariants.

Docker, crontab and sleep are stubs on a stub-only PATH; the stack, the work
root and every git repository live under tmp_path. Nothing here can reach the
host Docker daemon or /opt/stacks.
"""

import fcntl
import hashlib
import os
import re
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


# ---------------------------------------------------------------------------
# Step-aware rollback (controller addendum R4), rehearsal memory gate (R6) and
# resume/rollback hardening. Same stub-only PATH; the scripts upgrade-azerothcore.sh
# calls (restore, verifiers, admin redeploy, backup) are replaced by logging stubs
# inside tmp_path, never run for real.

LABELS = {"core": ".", "mod_playerbots": "modules/mod-playerbots",
          "mod_ah_bot_plus": "modules/mod-ah-bot-plus",
          "mod_individual_progression": "modules/mod-individual-progression"}
STEPS = ("snapshot_tooling", "stop", "backup", "snapshot_state", "switch_code", "switch_config",
         "switch_client_data", "switch_images", "boot", "verify", "admin")


def _before(step: str) -> tuple:
    """The steps a cutover completed when it stopped inside STEP."""
    return STEPS[:STEPS.index(step)]


def _stub_script(path: Path, name: str, rc: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _executable(path, f'#!/bin/sh\necho "{name} $*" >> "$UPGRADE_CALLS"\nexit {rc}\n')


def _tool(bind: Path, name: str) -> None:
    """Add one more real, non-destructive tool to the stub-only PATH."""
    real = shutil.which(name)
    assert real, name
    (bind / name).symlink_to(real)


def _stub_docker(bind: Path) -> None:
    """The fixture's docker stub plus per-container states (FAKE_STOPPED), missing
    volumes (FAKE_MISSING_VOLUMES), a clean db-import exit, and the cutover's
    *_STARTED markers as they stood in PEEK_STATE when each call was made."""
    _executable(bind / "docker", r"""#!/bin/bash
markers=""
if [ -n "${PEEK_STATE:-}" ] && [ -f "$PEEK_STATE" ]; then
    markers="$(grep -oE '^[A-Z_]+_STARTED=1' "$PEEK_STATE" | tr '\n' ' ')"
fi
echo "docker $* | markers: $markers" >> "$UPGRADE_CALLS"
args="$*"
last="${args##* }"
case "$*" in
  "inspect -f {{.State.Status}} "*)
    case " ${FAKE_STOPPED:-} " in *" $last "*) echo exited ;; *) echo running ;; esac ;;
  "inspect -f {{.State.StartedAt}} "*) echo 2026-10-27T08:00:00Z ;;
  "inspect -f {{.State.ExitCode}} "*) echo 0 ;;
  "logs --since "*) echo "WORLD: World Initialized In 10 seconds" ;;
  "exec ac-database "*) echo "${FAKE_HUMANS_ONLINE:-0}" ;;
  "image inspect "*) echo sha256:abc ;;
  "volume inspect "*)
    case " ${FAKE_MISSING_VOLUMES:-} " in *" $last "*) exit 1 ;; esac ;;
esac
exit 0
""")


def _addendum_fixture(tmp_path: Path):
    """The plan fixture plus cp (rollback restores file copies) and the richer docker stub."""
    tools, stack, bind, previous = _fixture(tmp_path)
    _tool(bind, "cp")
    _stub_docker(bind)
    return tools, stack, bind, previous


def _cutover_state(tmp_path: Path, stack: Path, steps: tuple, archive=None, **facts) -> Path:
    """A cutover state directory as cmd_cutover leaves it after completing STEPS."""
    state = tmp_path / "work" / "cutover-x"
    for sub in ("configs", "etc", "logs", "patches-live", "admin-stack"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    candidate = f"upgrade-{_git('rev-parse', 'HEAD', cwd=stack)[:12]}"  # the fixture pins the stack HEADs
    lines = ["TS=x", f"CANDIDATE_TAG={candidate}", "PREVIOUS_REPO_REV=prev"]
    lines += [f"OLD_REV_{label}={_git('rev-parse', 'HEAD', cwd=stack / rel)}" for label, rel in LABELS.items()]
    if "snapshot_tooling" in steps:
        _stub_script(state / "repo-snapshot/scripts/verify-azerothcore.sh", "old-verifier")
    if "stop" in steps:
        (state / "admin-stack/docker-compose.yml").write_text("services: {} # pre-upgrade admin\n")
    if archive if archive is not None else "backup" in steps:
        path = state / "azerothcore-backup-preupgrade-x.tar.gz"
        path.write_bytes(b"archive")
        (state / f"{path.name}.sha256").write_text(f"{hashlib.sha256(b'archive').hexdigest()}  {path.name}\n")
        lines.append(f"PREUPGRADE_ARCHIVE={path}")
    if "snapshot_state" in steps:
        (state / "docker-compose.override.yml").write_text("services: {} # pre-upgrade override\n")
        (state / "docker-compose.admin.yml").write_text("services: {} # pre-upgrade admin.yml\n")
        shutil.copytree(stack / "configs", state / "configs", dirs_exist_ok=True)
        for name in ("worldserver", "authserver", "dbimport"):
            shutil.copy(stack / "env/dist/etc" / f"{name}.conf", state / "etc")
        lines += [f"OLD_ID_{svc}=sha256:abc" for svc in ("worldserver", "authserver", "db_import", "client_data")]
    lines += [f"STEP_{step}=1" for step in steps]
    lines += [f"{key}={value}" for key, value in facts.items()]
    (state / "state.env").write_text("\n".join(lines) + "\n")
    return state


def _index(lines: list, fragment: str) -> int:
    hits = [i for i, line in enumerate(lines) if fragment in line]
    assert hits, f"no call contains {fragment!r}:\n" + "\n".join(lines)
    return hits[0]


def test_rollback_after_a_cutover_stopped_at_the_backup_restarts_the_untouched_old_stack(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    state = _cutover_state(tmp_path, stack, _before("backup"), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _calls(tmp_path)
    assert "docker compose up -d --no-build" in calls
    assert "docker start azerothcore-admin" in calls
    assert "docker tag" not in calls
    assert "restore-azerothcore.sh" not in calls
    assert "docker run" not in calls
    assert "old-verifier" in calls
    assert "ROLLED_BACK=1" in (state / "state.env").read_text()


def test_rollback_restores_client_data_from_the_volume_recorded_in_the_cutover_state(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    state = _cutover_state(tmp_path, stack, _before("switch_client_data"), ADMIN_WAS_RUNNING="true",
                           CLIENT_DATA_LIVE="v19", CLIENT_DATA_ROLLBACK_VOLUME="acup-client-data-rollback-v19",
                           CLIENT_DATA_SWITCH_STARTED="1")
    assert not (tmp_path / "work" / "staging").exists()
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    runs = [line for line in _calls(tmp_path).splitlines() if line.startswith("docker run ")]
    assert any("-v acup-client-data-rollback-v19:/src:ro" in line for line in runs), runs
    assert "restore-azerothcore.sh" not in _calls(tmp_path)


def test_rollback_refuses_when_boot_started_but_no_preupgrade_archive_was_recorded(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("boot"), archive=False, ADMIN_WAS_RUNNING="true",
                           BOOT_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "no pre-upgrade archive was recorded" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_rollback_after_boot_restores_client_data_images_databases_then_admin(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin/docker-compose.yml").write_text("services: {} # upgraded admin\n")
    state = _cutover_state(tmp_path, stack, _before("admin"), ADMIN_WAS_RUNNING="true",
                           CLIENT_DATA_ROLLBACK_VOLUME="acup-client-data-rollback-v19",
                           CLIENT_DATA_SWITCH_STARTED="1", IMAGES_SWITCH_STARTED="1", BOOT_STARTED="1",
                           ADMIN_STEP_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _calls(tmp_path).splitlines()
    archive = state / "azerothcore-backup-preupgrade-x.tar.gz"
    order = [_index(lines, "-v acup-client-data-rollback-v19:/src:ro"),
             _index(lines, "docker tag acore/ac-wotlk-worldserver:preupgrade-x acore/ac-wotlk-worldserver:playerbot-local"),
             _index(lines, f"restore-azerothcore.sh {archive} --stack-dir {stack} --yes"),
             _index(lines, "docker compose up -d --no-build"),
             _index(lines, "docker tag azerothcore-admin:preupgrade-x azerothcore-admin:local"),
             _index(lines, "up -d --force-recreate"),
             _index(lines, "old-verifier")]
    assert order == sorted(order)
    for svc in ("worldserver", "authserver", "db-import", "client-data"):
        _index(lines, f"docker tag acore/ac-wotlk-{svc}:preupgrade-x acore/ac-wotlk-{svc}:playerbot-local")
    assert (tmp_path / "admin/docker-compose.yml").read_text() == "services: {} # pre-upgrade admin\n"
    assert (stack / "docker-compose.admin.yml").read_text() == "services: {} # pre-upgrade admin.yml\n"


def test_resumed_cutover_records_each_started_marker_before_its_action(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "verify-azerothcore.sh", "new-verifier")
    _stub_script(tmp_path / "repo/wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh", "admin-redeploy")
    state = _cutover_state(tmp_path, stack, _before("switch_client_data"), ADMIN_WAS_RUNNING="true",
                           CLIENT_DATA_LIVE="v19", CLIENT_DATA_TARGET="v20.0",
                           CLIENT_DATA_ROLLBACK_VOLUME="acup-client-data-rollback-v19",
                           CLIENT_DATA_TARGET_VOLUME="acup-client-data-v20.0")
    (state / "counts-before.txt").write_text("0\n")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             PEEK_STATE=str(state / "state.env"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CUTOVER_OK=1" in (state / "state.env").read_text()
    lines = _calls(tmp_path).splitlines()
    copy = lines[_index(lines, "-v acup-client-data-v20.0:/src:ro")]
    tag = lines[_index(lines, "docker tag acore/ac-wotlk-worldserver:upgrade-")]
    up = lines[_index(lines, "docker compose up -d --no-build")]
    start = lines[_index(lines, "docker start azerothcore-admin")]
    assert "CLIENT_DATA_SWITCH_STARTED=1" in copy and "IMAGES_SWITCH_STARTED" not in copy
    assert "IMAGES_SWITCH_STARTED=1" in tag and "BOOT_STARTED" not in tag
    assert "BOOT_STARTED=1" in up and "ADMIN_STEP_STARTED" not in up
    assert "ADMIN_STEP_STARTED=1" in start
    assert "admin-redeploy" in _calls(tmp_path)


def test_finalize_reads_client_data_volumes_from_the_cutover_state(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(bind / "rm", "rm")
    state = _cutover_state(tmp_path, stack, STEPS, CUTOVER_OK="1",
                           CLIENT_DATA_ROLLBACK_VOLUME="acup-client-data-rollback-v19",
                           CLIENT_DATA_TARGET_VOLUME="acup-client-data-v20.0")
    r = _run(tmp_path, tools, stack, bind, "finalize", "--state", str(state), "--yes",
             FAKE_MISSING_VOLUMES="acup-client-data-v20.0")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _calls(tmp_path)
    assert "docker volume rm acup-client-data-rollback-v19" in calls
    assert "docker volume rm acup-client-data-v20.0" not in calls
    assert "FINALIZED=1" in (state / "state.env").read_text()


def test_rehearsal_memory_gate_default_covers_the_rehearsal_container_caps():
    script = (SCRIPTS_DIR / "upgrade-azerothcore.sh").read_text()
    drill = (SCRIPTS_DIR / "restore-drill.sh").read_text()
    gate_gb = int(re.search(r'MIN_REHEARSAL_MEM_GB="\$\{MIN_REHEARSAL_MEM_GB:-(\d+)\}"', script).group(1))
    world_gb = int(re.search(r"--name acup-rehearsal-world [^\n]*--memory (\d+)g", script).group(1))
    import_gb = int(re.search(r"--name acup-rehearsal-dbimport [^\n]*--memory (\d+)g", script).group(1))
    drill_mb = int(re.search(r'--memory "\$\{DRILL_MEMORY:-(\d+)m\}"', drill).group(1))
    # The drill DB lives for the whole rehearsal; db-import and the worldserver run one after the other.
    assert gate_gb * 1024 >= max(world_gb, import_gb) * 1024 + drill_mb


def test_rollback_before_snapshot_state_keeps_local_module_changes(tmp_path):
    # Before snapshot_state no live diff was saved, so discarding one could not be undone.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    readme = stack / "modules/mod-playerbots/README"
    readme.write_text("managed patch\n")
    state = _cutover_state(tmp_path, stack, _before("backup"), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert readme.read_text() == "managed patch\n"


def test_rollback_restarts_an_admin_app_it_stopped_when_the_cutover_never_recorded_it(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, ())
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _calls(tmp_path).splitlines()
    assert _index(lines, "docker stop azerothcore-admin") < _index(lines, "docker start azerothcore-admin")


@pytest.mark.parametrize("command", ["rollback", "cutover"])
def test_a_rolled_back_cutover_is_neither_rolled_back_again_nor_resumed(tmp_path, command):
    # A second rollback would restore the pre-upgrade dump over everything played since.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    _stub_script(tools / "verify-azerothcore.sh", "new-verifier")
    state = _cutover_state(tmp_path, stack, _before("verify"), ADMIN_WAS_RUNNING="true",
                           BOOT_STARTED="1", ROLLED_BACK="1")
    r = _run(tmp_path, tools, stack, bind, command, "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "rolled back" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)
    assert "restore-azerothcore.sh" not in _calls(tmp_path)


def test_resumed_stop_step_keeps_the_admin_state_recorded_by_the_first_attempt(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(stack / "backup.sh", "backup.sh")
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin/docker-compose.yml").write_text("services: {}\n")
    state = _cutover_state(tmp_path, stack, ("snapshot_tooling",), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             FAKE_STOPPED="azerothcore-admin ac-worldserver")
    assert r.returncode == 1
    assert "pre-upgrade archive not found" in r.stderr
    recorded = [line for line in (state / "state.env").read_text().splitlines()
                if line.startswith("ADMIN_WAS_RUNNING=")]
    assert recorded == ["ADMIN_WAS_RUNNING=true"]
    assert "docker stop azerothcore-admin" not in _calls(tmp_path)
    assert f"cutover --state {state}" in r.stderr
    assert f"rollback --state {state}" in r.stderr


def test_fresh_cutover_refuses_while_an_earlier_cutover_is_incomplete(tmp_path):
    # Snapshotting a half-upgraded stack would replace the real rollback point.
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("verify"), ADMIN_WAS_RUNNING="true", BOOT_STARTED="1")
    (tmp_path / "work" / "current").symlink_to(state)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes")
    assert r.returncode == 1
    assert "incomplete" in r.stderr and str(state) in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_a_second_run_refuses_while_another_holds_the_upgrade_lock(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("backup"), ADMIN_WAS_RUNNING="true")
    with open(tmp_path / "work" / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "another upgrade-azerothcore.sh run" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_rollback_checks_every_rollback_artifact_before_stopping_anything(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("switch_code"), ADMIN_WAS_RUNNING="true")
    (state / "docker-compose.override.yml").unlink()
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "configuration copies missing" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


@pytest.mark.parametrize("steps, facts, shown", [
    ((), {}, "Cutover: "),
    (_before("verify"), {"BOOT_STARTED": "1"}, "BOOT_STARTED=1"),
])
def test_status_reports_any_cutover_state(tmp_path, steps, facts, shown):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, steps, **facts)
    (tmp_path / "work" / "current").symlink_to(state)
    r = _run(tmp_path, tools, stack, bind, "status")
    assert r.returncode == 0, r.stdout + r.stderr
    assert shown in r.stdout


def test_resume_refuses_when_the_pinned_versions_changed_since_the_cutover_started(tmp_path):
    # A resumed cutover must switch to the candidate it started with, never a newer pin.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("switch_code"), ADMIN_WAS_RUNNING="true")
    text = (state / "state.env").read_text()
    (state / "state.env").write_text(re.sub(r"(?m)^CANDIDATE_TAG=.*$", "CANDIDATE_TAG=upgrade-0123456789ab", text))
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "pinned versions changed" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)
    assert "STEP_switch_code" not in (state / "state.env").read_text()


# ---------------------------------------------------------------------------
# Live-switch collision check (controller addendum R7).

def _commit_beside_head(repo: Path, path: str, content: str, scratch: Path) -> str:
    """Create a child commit of HEAD that adds PATH, without touching HEAD, the
    index or the working tree (the pinned revision's objects are then present
    in the live checkout, as after 'stage' fetched them)."""
    env = {**os.environ, **GIT_ENV, "GIT_INDEX_FILE": str(scratch / f"index-{repo.name}")}
    def git(*args: str, stdin: str = "") -> str:
        return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                              text=True, input=stdin).stdout.strip()
    git("read-tree", "HEAD")
    blob = git("hash-object", "-w", "--stdin", stdin=content)
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    return git("commit-tree", git("write-tree"), "-p", "HEAD", "-m", f"add {path}")


def _pin(tools: Path, key: str, rev: str) -> None:
    versions = tools / "upstream-versions.env"
    versions.write_text(re.sub(rf"(?m)^{key}=.*$", f"{key}={rev}", versions.read_text()))


def test_stage_refuses_when_a_local_file_collides_with_a_pinned_path(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    module = stack / "modules/mod-individual-progression"
    pinned = _commit_beside_head(module, "new.txt", "upstream\n", tmp_path)
    _pin(tools, "MOD_IP_REV", pinned)
    # An already-prepared staging checkout keeps the test offline.
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    shutil.copytree(stack, tmp_path / "work" / "staging" / f"src-{core12}", symlinks=True)
    (module / "new.txt").write_text("operator's local file\n")
    r = _run(tmp_path, tools, stack, bind, "stage")
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "new.txt" in out
    assert "would be overwritten" in out or "collide" in out
    calls = _calls(tmp_path)
    assert "compose" not in calls and "volume create" not in calls and "docker run" not in calls


@pytest.mark.parametrize("pinned_path, collides", [
    ("modules/CMakeLists.txt", False),
    ("modules/mod-ah-bot-plus/extra.txt", True),
])
def test_cutover_reports_nested_module_checkouts_only_when_the_pinned_core_tracks_paths_inside(
        tmp_path, pinned_path, collides):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    pinned = _commit_beside_head(stack, pinned_path, "upstream\n", tmp_path)
    _pin(tools, "AC_CORE_REV", pinned)
    staging = tmp_path / "work" / "staging"
    staging.mkdir(parents=True)
    (staging / f"stage-{pinned[:12]}.env").write_text("".join(
        f"CANDIDATE_ID_{svc}=sha256:abc\n" for svc in ("worldserver", "authserver", "db_import", "client_data")))
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             "--skip-rehearsal", "--dry-run")
    assert not any(m in _calls(tmp_path) for m in MUTATING)
    if collides:
        assert r.returncode == 1
        assert "[FAIL] core: 1 local file(s)" in r.stdout and "collide" in r.stdout
        assert "modules/mod-ah-bot-plus/" in r.stdout
        assert "modules/mod-playerbots/" not in r.stdout
    else:
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"[OK]   core: no local file collides with {pinned[:12]}" in r.stdout
        assert "[FAIL]" not in r.stdout


def test_stage_reports_an_ignored_local_file_at_a_pinned_path_as_overwritten(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    module = stack / "modules/mod-ah-bot-plus"
    pinned = _commit_beside_head(module, "conf/local.log", "upstream\n", tmp_path)
    _pin(tools, "MOD_AHBOT_REV", pinned)
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    shutil.copytree(stack, tmp_path / "work" / "staging" / f"src-{core12}", symlinks=True)
    (module / ".git/info/exclude").write_text("*.log\n")
    (module / "conf").mkdir()
    (module / "conf/local.log").write_text("operator's local file\n")
    r = _run(tmp_path, tools, stack, bind, "stage")
    assert r.returncode == 1
    assert "ignored: conf/local.log" in r.stdout
    assert "would be overwritten" in r.stdout
    assert "compose" not in _calls(tmp_path)
