"""upgrade-azerothcore.sh safety invariants.

Docker, crontab and sleep are stubs on a stub-only PATH; the stack, the work
root and every git repository live under tmp_path. Nothing here can reach the
host Docker daemon or /opt/stacks.
"""

import fcntl
import hashlib
import json
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
    # The previous tooling commit pins exactly the live checkouts, as the
    # tooling the stack was installed with would (preflight compares the two).
    _git("init", "-q", cwd=tmp_path / "repo")
    _git("add", ".", cwd=tmp_path / "repo")
    _git("commit", "-qm", "tooling", cwd=tmp_path / "repo")
    previous = _git("rev-parse", "HEAD", cwd=tmp_path / "repo")

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
    """The fixture's docker stub plus container states (always-stopped FAKE_STOPPED,
    and a stopped list in $UPGRADE_CALLS.stopped that `stop`/`compose stop` add to
    and `compose up` clears), missing volumes (FAKE_MISSING_VOLUMES), the db-import
    exit code (FAKE_DBIMPORT_EXIT), crash-looping containers (FAKE_RESTARTING), the
    data-version a volume read reports (FAKE_DATA_VERSION, default v19), and the
    cutover's *_STARTED markers as they stood in PEEK_STATE when each call was made.

    Final-review additions: ac-db-import is a one-shot container (exited unless
    FAKE_RESTARTING); FAKE_RESTARTED containers report a StartedAt after any stop;
    FAKE_UNSTOPPABLE containers ignore `compose stop`; the worldserver's exit code
    (FAKE_WORLD_EXIT) and OOM flag (FAKE_OOM); a failing database (FAKE_DB_FAIL) or
    volume probe (FAKE_PROBE_FAIL); the account/character counts (FAKE_ACCOUNTS,
    FAKE_CHARACTERS, default 0); per-volume probe facts keyed by the volume name
    with non-alphanumerics as "_": FAKE_VERSION_<vol>, FAKE_TREE_<vol> ("FILES BYTES",
    default "100 4096") and FAKE_MARKER_<vol> (the .acup-complete numbers, default
    the volume's own; empty = no marker) — a volume a copy wrote to (`-v VOL:/dst`)
    always carries a valid marker; and the rehearsal's compose render
    (FAKE_COMPOSE_JSON), isolated containers and rehearsal database answers.

    Follow-up additions: the rehearsal worldserver's exit code after its stop
    (FAKE_REHEARSAL_WORLD_EXIT, default 0).

    D5 additions: a `docker exec -i` without a SELECT in its arguments is a SQL
    file fed on stdin (pre-db-import SQL): its stdin is appended to
    $UPGRADE_CALLS.sql under a "== docker ARGS" header, and FAKE_SQL_FAIL makes it
    fail; a guard query (any `docker exec -i` SELECT from acore_world.gameobject,
    live or rehearsal database) prints FAKE_GUARD_COUNT (default 0)."""
    _executable(bind / "docker", r"""#!/bin/bash
markers=""
if [ -n "${PEEK_STATE:-}" ] && [ -f "$PEEK_STATE" ]; then
    markers="$(grep -oE '^[A-Z_]+_STARTED=1' "$PEEK_STATE" | tr '\n' ' ')"
fi
echo "docker $* | markers: $markers" >> "$UPGRADE_CALLS"
args="$*"
last="${args##* }"
stopped="${UPGRADE_CALLS}.stopped"
copied="${UPGRADE_CALLS}.copied"
case "$*" in
  "compose up "*) : > "$stopped" ;;
  "compose stop "*)
    for a in "$@"; do
      case "$a" in ac-*)
        case " ${FAKE_UNSTOPPABLE:-} " in *" $a "*) ;; *) echo "$a" >> "$stopped" ;; esac ;;
      esac
    done ;;
  "stop "*) echo "$last" >> "$stopped" ;;
  "inspect -f {{.State.Status}} "*)
    case " ${FAKE_RESTARTING:-} " in *" $last "*) echo restarting; exit 0 ;; esac
    if grep -qx "$last" "$stopped" 2>/dev/null; then echo exited; exit 0; fi
    case " ${FAKE_STOPPED:-} ac-db-import " in *" $last "*) echo exited ;; *) echo running ;; esac ;;
  "inspect -f {{.State.StartedAt}} "*)
    case " ${FAKE_RESTARTED:-} " in *" $last "*) echo 2099-01-01T00:00:00Z ;; *) echo 2026-10-27T08:00:00Z ;; esac ;;
  "inspect -f {{.State.ExitCode}} ac-worldserver") echo "${FAKE_WORLD_EXIT:-0}" ;;
  "inspect -f {{.State.ExitCode}} acup-rehearsal-world") echo "${FAKE_REHEARSAL_WORLD_EXIT:-0}" ;;
  "inspect -f {{.State.ExitCode}} "*) echo "${FAKE_DBIMPORT_EXIT:-0}" ;;
  "inspect -f {{.State.OOMKilled}} "*) echo "${FAKE_OOM:-false}" ;;
  "inspect -f {{.HostConfig.NetworkMode}}"*) echo "acup-rehearsal-net|{}|volume:acup-x:/y:false;" ;;
  "logs --since "*) echo "WORLD: World Initialized In 10 seconds" ;;
  "exec ac-database "*)
    [ -z "${FAKE_DB_FAIL:-}" ] || exit 1
    case "$*" in
      *"FROM acore_auth.account),"*) printf '%s\t%s\n' "${FAKE_ACCOUNTS-0}" "${FAKE_CHARACTERS-0}" ;;
      *) echo "${FAKE_HUMANS_ONLINE:-0}" ;;
    esac ;;
  "exec -i "*)
    case "$*" in
      *SELECT*)
        case "$*" in
          *"FROM acore_world.gameobject"*) echo "${FAKE_GUARD_COUNT:-0}" ;;
          *playerbots_speech*) echo 0 ;; *"column_name='id'"*) echo 1 ;; *) echo 42 ;;
        esac ;;
      *) { echo "== docker $*"; cat; } >> "${UPGRADE_CALLS}.sql"; [ -z "${FAKE_SQL_FAIL:-}" ] || exit 1 ;;
    esac ;;
  "image inspect "*) echo sha256:abc ;;
  "run --rm --network none --user 0 -v "*":/src:ro -v "*":/dst "*)
    dst="${args#*:/src:ro -v }"; echo "${dst%%:/dst *}" >> "$copied" ;;
  "run --rm --network none --user 0 -v "*":/d:ro --entrypoint sh "*)
    [ -z "${FAKE_PROBE_FAIL:-}" ] || exit 1
    vol="${args#*--user 0 -v }"; vol="${vol%%:/d:ro *}"
    key="$(printf '%s' "$vol" | tr -c 'A-Za-z0-9' '_')"
    ver="FAKE_VERSION_$key"; tree="FAKE_TREE_$key"; marker="FAKE_MARKER_$key"
    echo "INSTALLED_VERSION=${!ver-${FAKE_DATA_VERSION-v19}}"
    tree="${!tree:-100 4096}"
    if grep -qx "$vol" "$copied" 2>/dev/null; then marker="$tree"; else marker="${!marker-$tree}"; fi
    if [ -n "$marker" ]; then echo "MARKER_FILES=${marker% *}"; echo "MARKER_BYTES=${marker#* }"; fi
    echo "TREE_FILES=${tree% *}"; echo "TREE_BYTES=${tree#* }" ;;
  "run -d --name acup-rehearsal-dbimport "*)
    for a in "$@"; do
      case "$a" in *:/azerothcore/env/dist/etc) cp "$FAKE_WORLD_CONF" "${a%%:*}/worldserver.conf" ;; esac
    done ;;
  "volume inspect "*)
    case " ${FAKE_MISSING_VOLUMES:-} " in *" $last "*) exit 1 ;; esac ;;
  "network inspect "*|"container inspect "*) exit 1 ;;
  "compose --env-file "*" config --format json") cat "$FAKE_COMPOSE_JSON" ;;
  "wait "*) echo 0 ;;
esac
exit 0
""")


def _addendum_fixture(tmp_path: Path):
    """The plan fixture plus cp (rollback restores file copies), a logging sync
    stub (state markers are flushed), the richer docker stub and a logging
    backup.sh stub (deployed and repository copy, so preflight still matches)
    that writes a pre-rollback archive (FAKE_BACKUP_FAIL makes it fail)."""
    tools, stack, bind, previous = _fixture(tmp_path)
    _tool(bind, "cp")
    _stub_script(bind / "sync", "sync")
    _stub_docker(bind)
    for path in (stack / "backup.sh", tools / "backup.sh"):
        _executable(path, '#!/bin/bash\necho "backup.sh $*" >> "$UPGRADE_CALLS"\n'
                    '[ -z "${FAKE_BACKUP_FAIL:-}" ] || exit 1\n'
                    'if [ "$*" = "--label prerollback" ]; then\n'
                    '    echo archive > "$STACK_DIR/backups/azerothcore-backup-prerollback-2026-10-28T10-00-00.tar.gz"\n'
                    'fi\n')
    return tools, stack, bind, previous


def _stopped(tmp_path: Path, *names: str) -> None:
    """Containers the docker stub reports as exited until `compose up`."""
    (tmp_path / "calls.stopped").write_text("".join(f"{name}\n" for name in names))


def _cutover_state(tmp_path: Path, stack: Path, steps: tuple, archive=None, pins=(), **facts) -> Path:
    """A cutover state directory as cmd_cutover leaves it after completing STEPS."""
    state = tmp_path / "work" / "cutover-x"
    for sub in ("configs", "etc", "logs", "patches-live", "admin-stack"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    candidate = f"upgrade-{_git('rev-parse', 'HEAD', cwd=stack)[:12]}"  # the fixture pins the stack HEADs
    lines = ["TS=x", f"CANDIDATE_TAG={candidate}", "PREVIOUS_REPO_REV=prev"]
    lines += [f"OLD_REV_{label}={_git('rev-parse', 'HEAD', cwd=stack / rel)}" for label, rel in LABELS.items()]
    lines += [f"PIN_REV_{label}={_git('rev-parse', 'HEAD', cwd=stack / rel)}" for label, rel in LABELS.items()]
    lines += [f"CANDIDATE_ID_{svc}=sha256:abc" for svc in ("worldserver", "authserver", "db_import", "client_data")]
    lines += [f"PIN={pin}" for pin in pins]
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
    copy = next(line for line in runs if "acup-client-data-rollback-v19:/src:ro" in line)
    assert "test -f /src/data-version || { echo " in copy and "exit 1; }; find /dst -mindepth 1 -delete" in copy
    # I3 ruling: the live code was switched (STEP_switch_code=1), so the databases are restored.
    assert "restore-azerothcore.sh" in _calls(tmp_path)


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
    state = _cutover_state(tmp_path, stack, _before("admin"), ADMIN_WAS_RUNNING="true", CLIENT_DATA_LIVE="v19",
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
    (state / "counts-before.txt").write_text("0 0\n")
    _stopped(tmp_path, "ac-worldserver", "ac-authserver")  # as the completed stop step left them
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
    assert "test -f /src/data-version || { echo " in copy and "exit 1; }; find /dst -mindepth 1 -delete" in copy
    for action in (copy, tag, up, start):  # each marker is flushed to disk right before its action
        assert lines[lines.index(action) - 1] == f"sync {state / 'state.env'}", action


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
    _stage_env(tmp_path, tools, pinned)
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


# ---------------------------------------------------------------------------
# Fix round 1 (task review I1-I11).

def _stub_verifier(tools: Path) -> None:
    """The new verifier fails, as the real one would, while ac-worldserver is stopped."""
    _executable(tools / "verify-azerothcore.sh", '#!/bin/bash\n'
                'if grep -qx ac-worldserver "${UPGRADE_CALLS}.stopped" 2>/dev/null; then\n'
                '    echo "new-verifier FAIL (worldserver stopped)" >> "$UPGRADE_CALLS"; exit 1\nfi\n'
                'echo "new-verifier" >> "$UPGRADE_CALLS"\n')


def _resume_fixture(tmp_path: Path, step: str, **facts):
    """A cutover that stopped inside STEP, with the realm as that step leaves it."""
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_verifier(tools)
    _stub_script(tmp_path / "repo/wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh", "admin-redeploy")
    state = _cutover_state(tmp_path, stack, _before(step), ADMIN_WAS_RUNNING="true", **facts)
    (state / "counts-before.txt").write_text("0 0\n")
    _stopped(tmp_path, "ac-worldserver", "ac-authserver")
    return tools, stack, bind, state


def test_i1_resumed_verify_restarts_the_stopped_realm_and_waits_before_verifying(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "verify", BOOT_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _calls(tmp_path).splitlines()
    assert _index(lines, "docker compose up -d --no-build") < _index(lines, "logs --since") \
        < _index(lines, "new-verifier")
    assert "CUTOVER_OK=1" in (state / "state.env").read_text()


def test_i2_rollback_waits_for_world_initialized_before_the_old_verifier(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("backup"), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _calls(tmp_path).splitlines()
    assert _index(lines, "docker compose up -d --no-build") < _index(lines, "logs --since ") \
        < _index(lines, "old-verifier")


def test_i3_rollback_restores_the_databases_once_the_code_switch_started(tmp_path):
    # A reboot restarts the realm on the switched ./modules checkout, whose SQL the
    # worldserver applies: the databases may have changed before any boot marker.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    state = _cutover_state(tmp_path, stack, _before("switch_code"), ADMIN_WAS_RUNNING="true",
                           SWITCH_CODE_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"restore-azerothcore.sh {state / 'azerothcore-backup-preupgrade-x.tar.gz'}" in _calls(tmp_path)


def test_i3_resume_refuses_a_realm_restarted_after_the_cutover_stopped_it(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("snapshot_state"), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "restarted" in r.stdout + r.stderr
    assert f"rollback --state {state}" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_i3_switch_code_writes_a_started_marker_before_the_first_checkout(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_code")
    _stub_script(tools / "apply-local-patches.sh", "apply-local-patches.sh", rc=1)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    text = (state / "state.env").read_text()
    assert "SWITCH_CODE_STARTED=1" in text and "STEP_switch_code" not in text
    assert f"sync {state / 'state.env'}" in _calls(tmp_path)


def test_i4_fresh_cutover_records_its_pins_revisions_and_candidate_ids(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    (tmp_path / "repo/wow-server-sp-admin/scripts").mkdir(parents=True)
    (tmp_path / "repo/wow-server-sp-admin/scripts/README").write_text("admin tooling\n")
    _git("add", ".", cwd=tmp_path / "repo")
    _git("commit", "-qm", "admin tooling", cwd=tmp_path / "repo")
    previous = _git("rev-parse", "HEAD", cwd=tmp_path / "repo")
    for path in (stack / "backup.sh", tools / "backup.sh"):  # preflight compares the two
        _stub_script(path, "backup.sh")
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin/docker-compose.yml").write_text("services: {}\n")
    _stage_env(tmp_path, tools, _git("rev-parse", "HEAD", cwd=stack))
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             "--skip-rehearsal", "--pin", "AC_DUNGEON_FINDER_OPTIONS_MASK=5",
             "--pin", "AC_INDIVIDUAL_PROGRESSION_ENFORCE_GROUP_RULES=1")
    assert r.returncode == 1  # the backup stub produces no archive
    assert "pre-upgrade archive not found" in r.stderr
    state = (tmp_path / "work/current").resolve()
    text = (state / "state.env").read_text().splitlines()
    assert [line for line in text if line.startswith("PIN=")] == [
        "PIN=AC_DUNGEON_FINDER_OPTIONS_MASK=5", "PIN=AC_INDIVIDUAL_PROGRESSION_ENFORCE_GROUP_RULES=1"]
    for label, rel in LABELS.items():
        assert f"PIN_REV_{label}={_git('rev-parse', 'HEAD', cwd=stack / rel)}" in text
    for svc in ("worldserver", "authserver", "db_import", "client_data"):
        assert f"CANDIDATE_ID_{svc}=sha256:abc" in text


@pytest.mark.parametrize("cli_pins", [(), ("AC_DUNGEON_FINDER_OPTIONS_MASK=7",)])
def test_i4_resumed_cutover_replays_the_recorded_pins_and_refuses_different_ones(tmp_path, cli_pins):
    pins = ("AC_DUNGEON_FINDER_OPTIONS_MASK=5", "AC_INDIVIDUAL_PROGRESSION_ENFORCE_GROUP_RULES=1")
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stopped(tmp_path, "ac-worldserver", "ac-authserver")
    _stub_script(bind / "rm", "rm")
    _stub_script(tools / "migrate-ac-env.sh", "migrate-ac-env.sh", rc=1)  # record argv, stop there
    for module, name in (("mod-playerbots", "playerbots"), ("mod-ah-bot-plus", "mod_ahbot"),
                         ("mod-individual-progression", "individualProgression")):
        (stack / "modules" / module / "conf").mkdir()
        (stack / "modules" / module / "conf" / f"{name}.conf.dist").write_text(f"{name}.X = 1\n")
    state = _cutover_state(tmp_path, stack, _before("switch_config"), pins=pins, ADMIN_WAS_RUNNING="true")
    args = [arg for pin in cli_pins for arg in ("--pin", pin)]
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", *args)
    assert r.returncode == 1
    if cli_pins:
        assert "pins differ from the cutover's" in r.stderr
        assert "migrate-ac-env.sh" not in _calls(tmp_path)
    else:
        lines = _calls(tmp_path).splitlines()
        assert "--pin " + " --pin ".join(pins) + " " in lines[_index(lines, "migrate-ac-env.sh ")]


def test_i5_rollback_refuses_a_missing_client_data_rollback_volume_before_stopping_anything(tmp_path):
    # `docker run -v` would create the missing volume empty and the copy would wipe live client data.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("switch_client_data"), ADMIN_WAS_RUNNING="true",
                           CLIENT_DATA_ROLLBACK_VOLUME="acup-client-data-rollback-v19",
                           CLIENT_DATA_SWITCH_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes",
             FAKE_MISSING_VOLUMES="acup-client-data-rollback-v19")
    assert r.returncode == 1
    assert "acup-client-data-rollback-v19" in r.stderr and "missing" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


@pytest.mark.parametrize("pin_key", ["MOD_PLAYERBOTS_REV", "MOD_IP_REV"])
def test_i6_resume_refuses_a_changed_module_pin(tmp_path, pin_key):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("switch_code"), ADMIN_WAS_RUNNING="true")
    _stopped(tmp_path, "ac-worldserver", "ac-authserver")
    _pin(tools, pin_key, "1" * 40)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "pinned versions changed" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_i6_resume_refuses_candidate_images_changed_since_the_cutover_started(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_images",
                                                CANDIDATE_ID_worldserver="sha256:rebuilt")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "candidate images changed since the cutover started" in r.stderr
    assert "docker tag" not in _calls(tmp_path)
    assert "IMAGES_SWITCH_STARTED" not in (state / "state.env").read_text()


def test_i7_an_unfinished_rollback_says_it_is_safe_to_re_run(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh", rc=1)
    state = _cutover_state(tmp_path, stack, _before("verify"), ADMIN_WAS_RUNNING="true", BOOT_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode != 0
    assert "safe to re-run" in r.stderr and f"rollback --state {state}" in r.stderr
    assert "ROLLED_BACK" not in (state / "state.env").read_text()


def test_i8_rollback_gives_restore_the_world_init_timeout(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _executable(tools / "restore-azerothcore.sh",
                '#!/bin/sh\necho "restore-azerothcore.sh $* ready=$RESTORE_READY_TIMEOUT_SECONDS" >> "$UPGRADE_CALLS"\n')
    state = _cutover_state(tmp_path, stack, _before("verify"), ADMIN_WAS_RUNNING="true", BOOT_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes", WORLD_INIT_TIMEOUT="777")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ready=777" in _calls(tmp_path)


@pytest.mark.parametrize("step, facts, env, before", [
    # F10 (operator decision): only a decrease stops the realm; an increase warns.
    ("verify", {"BOOT_STARTED": "1"}, {"FAKE_CHARACTERS": "4"}, "0 5\n"),   # characters 5 -> 4
    ("boot", {}, {"FAKE_DBIMPORT_EXIT": "1"}, "0 0\n"),
])
def test_i9_counts_mismatch_and_failed_db_import_stop_the_realm(tmp_path, step, facts, env, before):
    tools, stack, bind, state = _resume_fixture(tmp_path, step, **facts)
    (state / "counts-before.txt").write_text(before)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", STOP_TIMEOUT="77", **env)
    assert r.returncode == 1
    lines = _calls(tmp_path).splitlines()
    world = [i for i, line in enumerate(lines) if line.startswith("docker compose stop -t 77 ac-worldserver |")]
    auth = [i for i, line in enumerate(lines) if line.startswith("docker compose stop ac-authserver |")]
    assert world and auth and _index(lines, "docker compose up -d --no-build") < world[-1] < auth[-1], lines
    assert f"rollback --state {state}" in r.stderr


def test_i10_a_one_second_readiness_probe_still_looks_once(tmp_path):
    tools, stack, bind, previous = _fixture(tmp_path)
    real_date = shutil.which("date")
    counter = tmp_path / "date-counter"
    (bind / "date").unlink()
    # Every `date +%s` is one second later: a 1 s deadline has passed by the first loop test.
    _executable(bind / "date", "#!/bin/bash\n"
                'if [ "$*" = "+%s" ]; then\n'
                f'    n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 ))\n'
                f'    echo "$n" > "{counter}"\n'
                '    echo $(( 1800000000 + n ))\n'
                '    exit 0\n'
                'fi\n'
                f'exec {real_date} "$@"\n')
    r = _run(tmp_path, tools, stack, bind, "preflight", "--previous-repo-rev", previous)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[OK]   ac-worldserver reached World Initialized this boot" in r.stdout


def test_i11_rollback_saves_module_edits_before_discarding_them(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    readme = stack / "modules/mod-ah-bot-plus/README"
    original = readme.read_text()
    readme.write_text("edited after the cutover\n")
    state = _cutover_state(tmp_path, stack, _before("switch_config"), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    saved = state / "patches-rollback/mod-ah-bot-plus.diff"
    assert "+edited after the cutover" in saved.read_text()
    assert str(saved) in r.stdout
    assert readme.read_text() == original
    assert not (state / "patches-rollback/mod-playerbots.diff").exists()


# ---------------------------------------------------------------------------
# Fix round 2 (re-review N1-N4).

def test_n1_resume_after_an_interrupted_boot_step_reruns_the_boot(tmp_path):
    # Ctrl-C / SSH drop during the World Initialized wait: the running realm is the
    # cutover's own target stack, and the re-run boot step re-checks it.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_verifier(tools)
    _stub_script(tmp_path / "repo/wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh", "admin-redeploy")
    state = _cutover_state(tmp_path, stack, _before("boot"), ADMIN_WAS_RUNNING="true",
                           IMAGES_SWITCH_STARTED="1", BOOT_STARTED="1")
    (state / "counts-before.txt").write_text("0 0\n")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "restarted" not in r.stdout + r.stderr
    lines = _calls(tmp_path).splitlines()
    assert _index(lines, "docker compose up -d --no-build") < _index(lines, "inspect -f {{.State.ExitCode}} ac-db-import")
    text = (state / "state.env").read_text()
    assert "STEP_boot=1" in text and "CUTOVER_OK=1" in text


def test_n1_a_started_rollback_blocks_resume_and_finalize_until_it_finishes(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh", rc=1)
    _stub_script(bind / "rm", "rm")
    (tmp_path / "admin").mkdir()
    state = _cutover_state(tmp_path, stack, STEPS, ADMIN_WAS_RUNNING="true", IMAGES_SWITCH_STARTED="1",
                           BOOT_STARTED="1", ADMIN_STEP_STARTED="1", CUTOVER_OK="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode != 0  # the restore failed after the rollback had begun
    text = (state / "state.env").read_text()
    assert "ROLLBACK_STARTED=1" in text and "ROLLED_BACK" not in text
    for command in ("cutover", "finalize"):
        (tmp_path / "calls").write_text("")
        r = _run(tmp_path, tools, stack, bind, command, "--state", str(state), "--yes")
        assert r.returncode == 1, command
        assert "a rollback was started for this cutover; finish it" in r.stderr
        assert f"rollback --state {state}" in r.stderr
        assert not any(m in _calls(tmp_path) for m in MUTATING), command
        assert "volume rm" not in _calls(tmp_path) and "image rm" not in _calls(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr  # re-running the rollback stays allowed
    assert "ROLLED_BACK=1" in (state / "state.env").read_text()


def test_n1_fresh_cutover_refuses_while_a_rollback_is_unfinished(tmp_path):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, STEPS, ADMIN_WAS_RUNNING="true", BOOT_STARTED="1",
                           CUTOVER_OK="1", ROLLBACK_STARTED="1")
    (tmp_path / "work" / "current").symlink_to(state)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes")
    assert r.returncode == 1
    assert "a rollback was started" in r.stderr and f"rollback --state {state}" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


def test_n2_a_failed_verification_stops_the_worldserver_with_the_save_timeout(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "verify", BOOT_STARTED="1")
    _stub_script(tools / "verify-azerothcore.sh", "new-verifier", rc=1)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", STOP_TIMEOUT="77")
    assert r.returncode == 1
    lines = _calls(tmp_path).splitlines()
    world = _index(lines, "docker compose stop -t 77 ac-worldserver |")
    assert _index(lines, "new-verifier") < world < _index(lines, "docker compose stop ac-authserver |")


@pytest.mark.parametrize("found", ["", "v20.0"])
def test_n3_rollback_refuses_a_rollback_volume_without_the_recorded_version(tmp_path, found):
    # An empty or partial copy (interrupted stage) must be caught before the realm stops.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    state = _cutover_state(tmp_path, stack, _before("switch_client_data"), ADMIN_WAS_RUNNING="true",
                           CLIENT_DATA_LIVE="v19", CLIENT_DATA_ROLLBACK_VOLUME="acup-client-data-rollback-v19",
                           CLIENT_DATA_SWITCH_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes", FAKE_DATA_VERSION=found)
    assert r.returncode == 1
    assert "acup-client-data-rollback-v19" in r.stderr and "v19" in r.stderr
    calls = _calls(tmp_path)
    # The only container run is the read-only version probe of the rollback volume.
    runs = [line for line in calls.splitlines() if line.startswith("docker run ")]
    assert runs and all("-v acup-client-data-rollback-v19:/d:ro " in line for line in runs), runs
    assert not any(m in calls for m in MUTATING if m != "docker run")
    assert "ROLLBACK_STARTED" not in (state / "state.env").read_text()


def test_n3_stage_refuses_a_client_data_switch_when_the_live_version_is_unreadable(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "apply-local-patches.sh", "apply-local-patches.sh")
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    staging = tmp_path / "work" / "staging" / f"src-{core12}"
    shutil.copytree(stack, staging, symlinks=True)
    (staging / "modules/mod-ah-bot-plus/conf").mkdir()
    shutil.copy(stack / "configs/modules/mod_ahbot.conf.dist", staging / "modules/mod-ah-bot-plus/conf")
    r = _run(tmp_path, tools, stack, bind, "stage", FAKE_DATA_VERSION="")
    assert r.returncode == 1
    assert "cannot read the live client data version; refusing to stage a client-data switch" in r.stderr
    calls = _calls(tmp_path)
    assert "volume create" not in calls and "acup-client-data-download" not in calls


def test_n4_resume_treats_a_restarting_realm_as_running(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("snapshot_state"), ADMIN_WAS_RUNNING="true")
    _stopped(tmp_path, "ac-authserver")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             FAKE_RESTARTING="ac-worldserver")
    assert r.returncode == 1
    assert "restarted" in r.stderr and "restarting" in r.stderr
    assert not any(m in _calls(tmp_path) for m in MUTATING)


# ---------------------------------------------------------------------------
# Final PR2 review fixes (F1-F21). Same stub-only PATH; nothing reaches Docker.

STAGE_SVCS = ("worldserver", "authserver", "db_import", "client_data")
LIVE_VOL = "azerothcore_ac-client-data"
ROLLBACK_VOL = "acup-client-data-rollback-v19"
TARGET_VOL = "acup-client-data-v20.0"
CLIENT_FACTS = {"CLIENT_DATA_LIVE": "v19", "CLIENT_DATA_TARGET": "v20.0",
                "CLIENT_DATA_ROLLBACK_VOLUME": ROLLBACK_VOL, "CLIENT_DATA_TARGET_VOLUME": TARGET_VOL}


def _key(volume: str) -> str:
    """The docker stub's per-volume knob suffix."""
    return re.sub(r"[^A-Za-z0-9]", "_", volume)


def _build_inputs(tools: Path) -> str:
    """What a stage is built from, as upgrade-azerothcore.sh lists it (KEY=VALUE lines)."""
    versions_file = tools / "upstream-versions.env"
    versions = dict(line.split("=", 1) for line in versions_file.read_text().splitlines()
                    if "=" in line and not line.startswith("#"))
    listing = "".join(f"{key}={versions[key]}\n" for key in (
        "AC_CORE_REV", "MOD_PLAYERBOTS_REV", "MOD_AHBOT_REV", "MOD_IP_REV", "CLIENT_DATA_VERSION"))
    listing += f"VERSIONS_SHA256={hashlib.sha256(versions_file.read_bytes()).hexdigest()}\n"
    patches = tools / "patches"
    patch_lines = "".join(
        f"PATCH {rel} {hashlib.sha256((patches / rel).read_bytes()).hexdigest()}\n"
        for rel in sorted(str(p.relative_to(patches)) for p in patches.rglob("*.patch") if p.is_file()))
    listing += f"PATCHES_SHA256={hashlib.sha256(patch_lines.encode()).hexdigest()}\n"
    sql = tools / "patches" / "sql"
    sql_lines = "".join(
        f"SQL {rel} {hashlib.sha256((sql / rel).read_bytes()).hexdigest()}\n"
        for rel in sorted(str(p.relative_to(sql)) for p in sql.rglob("*.sql") if p.is_file()))
    listing += f"SQL_PATCHES_SHA256={hashlib.sha256(sql_lines.encode()).hexdigest()}\n"
    return listing


def _fingerprint(tools: Path) -> str:
    """The build fingerprint as upgrade-azerothcore.sh computes it."""
    return hashlib.sha256(_build_inputs(tools).encode()).hexdigest()


def _stage_env(tmp_path: Path, tools: Path, core_rev: str, fingerprint=None, **facts) -> Path:
    """The stage env `stage` leaves for the currently pinned versions."""
    staging = tmp_path / "work" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    lines = [f"CANDIDATE_TAG=upgrade-{core_rev[:12]}", f"STAGING_SRC={staging / ('src-' + core_rev[:12])}",
             f"BUILD_FINGERPRINT={_fingerprint(tools) if fingerprint is None else fingerprint}"]
    lines += [f"CANDIDATE_ID_{svc}=sha256:abc" for svc in STAGE_SVCS]
    lines += [f"{key}={value}" for key, value in facts.items()]
    path = staging / f"stage-{core_rev[:12]}.env"
    path.write_text("\n".join(lines) + "\n")
    return path


def _no_mutation(tmp_path: Path) -> None:
    """Nothing mutating ran; read-only volume probes (`-v VOL:/d:ro`) are allowed."""
    calls = _calls(tmp_path)
    assert not any(m in calls for m in MUTATING if m != "docker run"), calls
    runs = [line for line in calls.splitlines() if line.startswith("docker run ")]
    assert all(":/d:ro --entrypoint sh " in line for line in runs), runs


# F1 + F19 -------------------------------------------------------------------

ENV_AC = ("AC_CCACHE=true\nAC_RESTARTER_BINPATH=/azerothcore/env/dist/bin\n"
          "AC_RESTARTER_BINFILE=worldserver\nAC_RESTARTER_WITHGDB=false\n# comment\n")


def _rehearse_fixture(tmp_path: Path, admin_extra: dict | None = None):
    """A stage plus the staging tree, overlays and stubs a rehearsal reads."""
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    core = _git("rev-parse", "HEAD", cwd=stack)
    _stage_env(tmp_path, tools, core)
    src = tmp_path / "work" / "staging" / f"src-{core[:12]}"
    (src / "conf/dist").mkdir(parents=True)
    (src / "conf/dist/env.ac").write_text(ENV_AC)
    dists = {"mod-playerbots": ("playerbots", "Playerbots.Enabled = 1\nPlayerbots.MinRandomBots = 500\n"
                                              "Playerbots.MaxRandomBots = 500\n"),
             "mod-individual-progression": ("individualProgression", "IndividualProgression.Enable = 1\n"),
             "mod-ah-bot-plus": ("mod_ahbot", "A = 1\nAuctionHouseBot.GUIDs = 0\n")}
    for module, (name, text) in dists.items():
        (src / "modules" / module / "conf").mkdir(parents=True)
        (src / "modules" / module / "conf" / f"{name}.conf.dist").write_text(text)
    services = tmp_path / "repo/wow-server-sp-admin/app/services"
    services.mkdir(parents=True)
    shutil.copy(SCRIPTS_DIR.parent / "wow-server-sp-admin/app/services/env_var.py", services / "env_var.py")
    (stack / "docker-compose.override.yml").write_text(
        "services:\n  ac-worldserver:\n    environment:\n"
        '      AC_PLAYERBOTS_ENABLED: "1"\n      AC_PLAYERBOTS_MIN_RANDOM_BOTS: "500"\n')
    admin_env = {"AC_RATE_XP_KILL": "2", "AC_DUNGEON_FINDER_OPTIONS_MASK": "5", **(admin_extra or {})}
    (stack / "docker-compose.admin.yml").write_text(
        "services:\n  ac-worldserver:\n    environment:\n"
        + "".join(f"      {key}: '{value}'\n" for key, value in admin_env.items()))
    environment = {**dict(line.split("=", 1) for line in ENV_AC.splitlines() if line.startswith("AC_")),
                   "AC_DATA_DIR": "/azerothcore/env/dist/data", "AC_LOGS_DIR": "/azerothcore/env/dist/logs",
                   "AC_LOGIN_DATABASE_INFO": "ac-database;3306;root;livepw;acore_auth",
                   "AC_PLAYERBOTS_ENABLED": "1", "AC_PLAYERBOTS_MIN_RANDOM_BOTS": "500", **admin_env}
    (tmp_path / "compose.json").write_text(json.dumps({"services": {"ac-worldserver": {"environment": environment}}}))
    (tmp_path / "worldserver.conf").write_text("Rate.XP.Kill = 1\nDungeonFinder.OptionsMask = 7\n")
    _executable(tools / "restore-drill.sh", '#!/bin/bash\necho "restore-drill.sh $*" >> "$UPGRADE_CALLS"\n'
                'while [ $# -gt 0 ]; do case "$1" in --password-file) echo drillpw > "$2"; shift 2 ;; *) shift ;; esac; done\n')
    _executable(tools / "migrate-ac-env.sh", '#!/bin/bash\necho "migrate-ac-env.sh $*" >> "$UPGRADE_CALLS"\n')
    archive = tmp_path / "azerothcore-backup-daily-x.tar.gz"
    archive.write_bytes(b"archive")
    env = {"FAKE_COMPOSE_JSON": str(tmp_path / "compose.json"), "FAKE_WORLD_CONF": str(tmp_path / "worldserver.conf"),
           "MIN_REHEARSAL_MEM_GB": "0"}
    return tools, stack, bind, archive, env


def _rehearsal_dir(tmp_path: Path) -> Path:
    found = sorted((tmp_path / "work").glob("rehearsal-2*"))
    assert len(found) == 1, found
    return found[0]


def test_f1_rehearsal_checks_only_overlay_keys_not_env_ac(tmp_path):
    # env.ac's AC_CCACHE / AC_RESTARTER_* map to no config key; they are not overrides.
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[OK]   every AC_* override maps to a loaded config key" in r.stdout
    assert "Rehearsal PASSED" in r.stdout


def test_f1_rehearsal_still_reports_an_unmapped_overlay_key(tmp_path):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path, admin_extra={"AC_NOT_A_KEY": "1"})
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **env)
    assert r.returncode == 1
    line = next(line for line in r.stdout.splitlines() if "unmapped AC_* overrides" in line)
    assert "AC_NOT_A_KEY" in line and "AC_CCACHE" not in line and "AC_RESTARTER" not in line


def test_f19_rehearsal_keeps_no_rendered_compose_file_with_the_live_password(tmp_path):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **env)
    assert r.returncode == 0, r.stdout + r.stderr
    work = _rehearsal_dir(tmp_path)
    assert not (work / "compose.json").exists()
    assert stat.S_IMODE((work / "world.env").stat().st_mode) == 0o600
    assert not any("livepw" in p.read_text(errors="replace") for p in work.rglob("*") if p.is_file())


# F2 -------------------------------------------------------------------------

def _stage_fixture(tmp_path: Path):
    """A prepared staging checkout so `stage` stays offline (no clone, no fetch)."""
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    _stub_script(tools / "apply-local-patches.sh", "apply-local-patches.sh")
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    staging = tmp_path / "work" / "staging" / f"src-{core12}"
    shutil.copytree(stack, staging, symlinks=True)
    (staging / "modules/mod-ah-bot-plus/conf").mkdir()
    shutil.copy(stack / "configs/modules/mod_ahbot.conf.dist", staging / "modules/mod-ah-bot-plus/conf")
    return tools, stack, bind, staging


def test_f2_stage_recopies_a_rollback_volume_without_a_completion_marker(tmp_path):
    # An interrupted copy leaves the volume behind; it must never count as the rollback copy.
    tools, stack, bind, staging = _stage_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "stage", **{f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": "",
                                                      f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0"})
    assert r.returncode == 0, r.stdout + r.stderr
    copies = [line for line in _calls(tmp_path).splitlines() if f"-v {ROLLBACK_VOL}:/dst " in line]
    assert len(copies) == 1, copies
    assert f"-v {LIVE_VOL}:/src:ro" in copies[0]
    assert "find /dst -mindepth 1 -delete" in copies[0] and ".acup-complete" in copies[0]
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    assert f"CLIENT_DATA_ROLLBACK_VOLUME={ROLLBACK_VOL}" in (staging.parent / f"stage-{core12}.env").read_text()


def test_f2_stage_keeps_a_complete_rollback_copy(tmp_path):
    tools, stack, bind, _ = _stage_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "stage", **{f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"-v {ROLLBACK_VOL}:/dst " not in _calls(tmp_path)


@pytest.mark.parametrize("env, reason", [
    ({f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": ""}, "completion marker"),
    ({f"FAKE_TREE_{_key(LIVE_VOL)}": "101 4096"}, "live client data"),
    ({f"FAKE_TREE_{_key(ROLLBACK_VOL)}": "99 4096", f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": "100 4096"}, "marker"),
    ({f"FAKE_VERSION_{_key(ROLLBACK_VOL)}": "v18"}, "v18"),
    ({"FAKE_MISSING_VOLUMES": ROLLBACK_VOL}, "missing"),
])
def test_f2_cutover_verifies_the_rollback_copy_before_confirming(tmp_path, env, reason):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    _stage_env(tmp_path, tools, _git("rev-parse", "HEAD", cwd=stack), **CLIENT_FACTS)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             "--skip-rehearsal", **env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert ROLLBACK_VOL in r.stderr and reason in r.stderr
    assert "Type UPGRADE" not in r.stdout + r.stderr
    _no_mutation(tmp_path)
    assert not (tmp_path / "work" / "current").exists()


def test_f2_cutover_rechecks_the_rollback_copy_right_before_switching_client_data(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_client_data", **CLIENT_FACTS)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             **{f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": ""})
    assert r.returncode == 1
    assert ROLLBACK_VOL in r.stderr and "completion marker" in r.stderr
    assert "CLIENT_DATA_SWITCH_STARTED" not in (state / "state.env").read_text()
    assert f"-v {LIVE_VOL}:/dst" not in _calls(tmp_path)


def test_f2_resumed_client_data_switch_compares_the_copy_with_its_marker_not_the_live_volume(tmp_path):
    # After an interrupted switch the live volume is half v20: only the marker can vouch for the copy.
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_client_data", CLIENT_DATA_SWITCH_STARTED="1",
                                                **CLIENT_FACTS)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             **{f"FAKE_TREE_{_key(LIVE_VOL)}": "7 9"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CUTOVER_OK=1" in (state / "state.env").read_text()


def test_f2_rollback_refuses_a_rollback_volume_without_a_completion_marker(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    state = _cutover_state(tmp_path, stack, _before("switch_client_data"), ADMIN_WAS_RUNNING="true",
                           CLIENT_DATA_SWITCH_STARTED="1", **CLIENT_FACTS)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes",
             **{f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": ""})
    assert r.returncode == 1
    assert ROLLBACK_VOL in r.stderr and "completion marker" in r.stderr
    calls = _calls(tmp_path)
    assert not any(m in calls for m in MUTATING if m != "docker run")
    assert all(f"-v {ROLLBACK_VOL}:/d:ro " in line for line in calls.splitlines() if line.startswith("docker run "))
    assert "ROLLBACK_STARTED" not in (state / "state.env").read_text()


# F8 -------------------------------------------------------------------------

def test_f8_dry_run_cutover_without_the_admin_stack_plans_no_admin_steps(tmp_path):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    _stage_env(tmp_path, tools, _git("rev-parse", "HEAD", cwd=stack))
    assert not (tmp_path / "admin").exists()
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             "--skip-rehearsal", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker tag azerothcore-admin" not in r.stdout
    assert "docker start azerothcore-admin" not in r.stdout
    assert "redeploy-azerothcore-admin.sh" not in r.stdout
    assert "admin app is not installed" in r.stdout


def test_f8_resumed_stop_step_without_the_admin_stack_skips_the_admin_snapshot(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(stack / "backup.sh", "backup.sh")
    state = _cutover_state(tmp_path, stack, ("snapshot_tooling",), ADMIN_WAS_RUNNING="false",
                           ADMIN_PRESENT="false")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             FAKE_STOPPED="azerothcore-admin")
    assert "pre-upgrade archive not found" in r.stderr, r.stdout + r.stderr
    calls = _calls(tmp_path)
    assert "docker tag azerothcore-admin" not in calls and "backup.sh --label preupgrade" in calls


# F9 -------------------------------------------------------------------------

def test_f9_stage_explains_an_unreadable_live_client_data_volume(tmp_path):
    tools, stack, bind, _ = _stage_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "stage", FAKE_PROBE_FAIL="1")
    assert r.returncode == 1
    assert "cannot read the live client data version" in r.stderr
    assert "volume create" not in _calls(tmp_path)


# F13 ------------------------------------------------------------------------

@pytest.mark.parametrize("env, shown", [({"FAKE_WORLD_EXIT": "137"}, "code 137"),
                                        ({"FAKE_OOM": "true"}, "OOM")])
def test_f13_stop_step_refuses_a_worldserver_that_did_not_exit_cleanly(tmp_path, env, shown):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(stack / "backup.sh", "backup.sh")
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin/docker-compose.yml").write_text("services: {}\n")
    state = _cutover_state(tmp_path, stack, ("snapshot_tooling",), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", **env)
    assert r.returncode == 1
    assert "did not exit cleanly" in r.stderr and shown in r.stderr
    assert "saveall may be incomplete" in r.stderr
    assert "backup.sh" not in _calls(tmp_path)
    assert "STEP_stop" not in (state / "state.env").read_text()


# F14 ------------------------------------------------------------------------

def _stops_after_up(tmp_path: Path) -> bool:
    lines = _calls(tmp_path).splitlines()
    up = _index(lines, "docker compose up -d --no-build")
    return any(i > up for i, line in enumerate(lines) if line.startswith("docker compose stop -t "))


def test_f14_a_failing_count_query_stops_the_realm(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "verify", BOOT_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", FAKE_DB_FAIL="1")
    assert r.returncode == 1
    assert _stops_after_up(tmp_path), _calls(tmp_path)
    assert "Server is stopped" in r.stderr
    assert "STEP_verify" not in (state / "state.env").read_text()


def test_f14_a_failing_step_marker_stops_the_realm(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "verify", BOOT_STARTED="1")
    _stub_script(bind / "sync", "sync", rc=1)  # state_mark cannot flush: `mark_done verify` fails
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert _stops_after_up(tmp_path), _calls(tmp_path)
    assert "Server is stopped" in r.stderr


def test_f14_a_stop_that_fails_is_reported_as_still_running(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "verify", BOOT_STARTED="1")
    _stub_script(tools / "verify-azerothcore.sh", "new-verifier", rc=1)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             FAKE_UNSTOPPABLE="ac-worldserver")
    assert r.returncode == 1
    assert "STILL RUNNING: ac-worldserver" in r.stderr
    assert "Server is stopped" not in r.stderr


# F15 ------------------------------------------------------------------------

STOPPED_AT = "1793091600"  # 2026-10-27T09:00:00Z, after the stub's StartedAt (08:00)


@pytest.mark.parametrize("restarted", ["ac-db-import", "ac-worldserver", "ac-authserver"])
def test_f15_resume_refuses_a_container_started_after_the_stop_step(tmp_path, restarted):
    # Started and stopped again (e.g. a reboot ran db-import): the status alone reads "exited".
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("snapshot_state"), ADMIN_WAS_RUNNING="true",
                           STOPPED_AT=STOPPED_AT)
    _stopped(tmp_path, "ac-worldserver", "ac-authserver", "azerothcore-admin")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", FAKE_RESTARTED=restarted)
    assert r.returncode == 1
    assert "restarted" in r.stderr and restarted in r.stderr
    _no_mutation(tmp_path)


def test_f15_resume_accepts_containers_last_started_before_the_stop_step(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_images", STOPPED_AT=STOPPED_AT)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr


def test_f15_resume_stops_an_admin_app_that_came_back_before_its_step(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    (stack / "docker-compose.override.yml").write_text("services: {}\n")
    state = _cutover_state(tmp_path, stack, _before("snapshot_state"), ADMIN_WAS_RUNNING="true")
    _stopped(tmp_path, "ac-worldserver", "ac-authserver")  # azerothcore-admin runs again (reboot)
    _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    lines = _calls(tmp_path).splitlines()
    assert _index(lines, "docker stop azerothcore-admin") < _index(lines, "docker tag acore/ac-wotlk-")


def test_f15_resume_leaves_the_admin_app_alone_once_its_step_started(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "admin", BOOT_STARTED="1", ADMIN_STEP_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "docker stop azerothcore-admin" not in _calls(tmp_path)


def test_f15_the_stop_step_records_when_it_stopped_the_realm(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(stack / "backup.sh", "backup.sh")
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin/docker-compose.yml").write_text("services: {}\n")
    state = _cutover_state(tmp_path, stack, ("snapshot_tooling",), ADMIN_WAS_RUNNING="true")
    _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    text = (state / "state.env").read_text()
    assert re.search(r"(?m)^STOPPED_AT=\d{10}$", text), text
    assert text.index("STOPPED_AT=") < text.index("STEP_stop=1")


# F16 ------------------------------------------------------------------------

def test_f16_resume_refuses_inside_the_nightly_backup_window(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_images")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", UPGRADE_NOW_UTC_MIN="180")
    assert r.returncode == 1
    assert "02:30-03:45 UTC" in r.stderr
    _no_mutation(tmp_path)


@pytest.mark.parametrize("minute, passes", [("149", True), ("150", False), ("224", False), ("225", True)])
def test_f16_fresh_cutover_window_uses_the_clock_seam(tmp_path, minute, passes):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    _stage_env(tmp_path, tools, _git("rev-parse", "HEAD", cwd=stack))
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             "--skip-rehearsal", "--dry-run", UPGRADE_NOW_UTC_MIN=minute)
    assert (r.returncode == 0) == passes, r.stdout + r.stderr
    assert ("02:30-03:45 UTC overlaps" in r.stdout) != passes


# F20 ------------------------------------------------------------------------

def test_f20_a_command_reading_stdin_cannot_swallow_the_repository_list(tmp_path):
    tools, stack, bind, previous = _fixture(tmp_path)
    real_git = shutil.which("git")
    (bind / "git").unlink()
    _executable(bind / "git", f'#!/bin/bash\ncat > /dev/null\nexec {real_git} "$@"\n')
    r = subprocess.run(
        ["/bin/bash", str(tools / "upgrade-azerothcore.sh"), "preflight", "--previous-repo-rev", previous],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        env={"PATH": str(bind), "HOME": str(tmp_path), "STACK_DIR": str(stack),
             "ADMIN_STACK_DIR": str(tmp_path / "admin"), "UPGRADE_ROOT": str(tmp_path / "work"),
             "UPGRADE_CALLS": str(tmp_path / "calls"), "MIN_FREE_GB": "0", **GIT_ENV})
    for label in ("core", "mod-playerbots", "mod-ah-bot-plus", "mod-individual-progression"):
        assert f"[OK]   {label}: " in r.stdout, r.stdout


def test_f20_every_repository_spec_loop_reads_the_list_on_fd_3():
    for script in ("upgrade-azerothcore.sh", "install-azerothcore.sh", "verify-azerothcore.sh"):
        text = (SCRIPTS_DIR / script).read_text()
        feeds = re.findall(r"done [0-9]?< <\(upstream_repo_specs\)", text)
        assert feeds and all(feed == "done 3< <(upstream_repo_specs)" for feed in feeds), (script, feeds)
        assert text.count("<&3; do") == len(feeds), script


# F3 -------------------------------------------------------------------------

def _rehearsal_result(tmp_path: Path, tools: Path, core_rev: str, pins=(), **overrides) -> None:
    """rehearsal-latest/result.env as a passing `rehearse` of the current stage writes it."""
    work = tmp_path / "work" / "rehearsal-x"
    work.mkdir(parents=True)
    values = {"REHEARSAL_OK": "1", "CANDIDATE_TAG": f"upgrade-{core_rev[:12]}",
              "BUILD_FINGERPRINT": _fingerprint(tools),
              **{f"CANDIDATE_ID_{svc}": "sha256:abc" for svc in STAGE_SVCS}, **overrides}
    lines = [f"{key}={value}" for key, value in values.items()] + [f"PIN={pin}" for pin in sorted(pins)]
    (work / "result.env").write_text("\n".join(lines) + "\n")
    (tmp_path / "work" / "rehearsal-latest").symlink_to(work)


def test_f3_stage_records_what_the_candidate_was_built_from(tmp_path):
    tools, stack, bind, staging = _stage_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "stage", **{f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0"})
    assert r.returncode == 0, r.stdout + r.stderr
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    text = (staging.parent / f"stage-{core12}.env").read_text()
    assert f"BUILD_FINGERPRINT={_fingerprint(tools)}\n" in text
    for line in _build_inputs(tools).splitlines():
        assert line + "\n" in text, line


@pytest.mark.parametrize("change", ["module pin", "patch"])
def test_f3_cutover_refuses_a_stage_built_from_other_pins_or_patches(tmp_path, change):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    _stage_env(tmp_path, tools, _git("rev-parse", "HEAD", cwd=stack))
    if change == "module pin":
        module = stack / "modules/mod-playerbots"
        _pin(tools, "MOD_PLAYERBOTS_REV", _commit_beside_head(module, "hotfix.txt", "fix\n", tmp_path))
    else:
        patch = next((tools / "patches").rglob("*.patch"))
        patch.write_text(patch.read_text() + "\n")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes", "--skip-rehearsal")
    assert r.returncode == 1
    assert "re-run stage" in r.stderr
    assert ("MOD_PLAYERBOTS_REV" if change == "module pin" else "PATCHES_SHA256") in r.stderr
    _no_mutation(tmp_path)


@pytest.mark.parametrize("overrides, pins", [
    ({"CANDIDATE_ID_worldserver": "sha256:rebuilt"}, ("AC_DUNGEON_FINDER_OPTIONS_MASK=5",)),
    ({"CANDIDATE_ID_client_data": "sha256:rebuilt"}, ("AC_DUNGEON_FINDER_OPTIONS_MASK=5",)),
    ({"BUILD_FINGERPRINT": "0" * 64}, ("AC_DUNGEON_FINDER_OPTIONS_MASK=5",)),
    ({}, ()),                                              # rehearsed without the cutover's pins
    ({}, ("AC_DUNGEON_FINDER_OPTIONS_MASK=7",)),           # rehearsed with another value
])
def test_f3_cutover_refuses_a_rehearsal_of_another_build_or_pin_set(tmp_path, overrides, pins):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    core = _git("rev-parse", "HEAD", cwd=stack)
    _stage_env(tmp_path, tools, core)
    _rehearsal_result(tmp_path, tools, core, pins=pins, **overrides)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             "--pin", "AC_DUNGEON_FINDER_OPTIONS_MASK=5")
    assert r.returncode == 1
    assert "no passing rehearsal" in r.stderr
    _no_mutation(tmp_path)


def test_f3_cutover_accepts_the_rehearsal_of_the_staged_build_with_the_same_pins(tmp_path):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    core = _git("rev-parse", "HEAD", cwd=stack)
    _stage_env(tmp_path, tools, core)
    pins = ("AC_INDIVIDUAL_PROGRESSION_ENFORCE_GROUP_RULES=1", "AC_DUNGEON_FINDER_OPTIONS_MASK=5")
    _rehearsal_result(tmp_path, tools, core, pins=pins)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes", "--dry-run",
             *[arg for pin in pins for arg in ("--pin", pin)])
    assert r.returncode == 0, r.stdout + r.stderr


def test_f3_rehearsal_records_the_candidate_ids_fingerprint_and_pins(tmp_path):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive),
             "--pin", "AC_INDIVIDUAL_PROGRESSION_ENFORCE_GROUP_RULES=1", "--pin", "AC_DUNGEON_FINDER_OPTIONS_MASK=5",
             **env)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (tmp_path / "work" / "rehearsal-latest" / "result.env").read_text().splitlines()
    assert "REHEARSAL_OK=1" in text and f"BUILD_FINGERPRINT={_fingerprint(tools)}" in text
    for svc in STAGE_SVCS:
        assert f"CANDIDATE_ID_{svc}=sha256:abc" in text
    assert [line for line in text if line.startswith("PIN=")] == [
        "PIN=AC_DUNGEON_FINDER_OPTIONS_MASK=5", "PIN=AC_INDIVIDUAL_PROGRESSION_ENFORCE_GROUP_RULES=1"]


def test_f3_rehearsal_refuses_a_stage_built_from_other_pins(tmp_path):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    patch = next((tools / "patches").rglob("*.patch"))
    patch.write_text(patch.read_text() + "\n")
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **env)
    assert r.returncode == 1
    assert "re-run stage" in r.stderr
    assert "restore-drill.sh" not in _calls(tmp_path)


def test_f3_cutover_start_records_the_staging_dir_and_fingerprint(tmp_path):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    core = _git("rev-parse", "HEAD", cwd=stack)
    _stage_env(tmp_path, tools, core)
    # The fixture's tooling commit has no wow-server-sp-admin/: the run stops at
    # snapshot_tooling, after the state was recorded.
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes", "--skip-rehearsal")
    assert r.returncode != 0 and (tmp_path / "work/current").is_symlink(), r.stdout + r.stderr
    text = ((tmp_path / "work/current").resolve() / "state.env").read_text().splitlines()
    assert f"STAGING_SRC={tmp_path / 'work/staging' / ('src-' + core[:12])}" in text
    assert f"BUILD_FINGERPRINT={_fingerprint(tools)}" in text


def test_f3_resume_refuses_when_local_patches_changed_since_the_cutover_started(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_code")
    with open(state / "state.env", "a") as handle:
        handle.write(f"BUILD_FINGERPRINT={_fingerprint(tools)}\n")
    patch = next((tools / "patches").rglob("*.patch"))
    patch.write_text(patch.read_text() + "\n")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "changed since this cutover started" in r.stderr
    _no_mutation(tmp_path)


# F6 -------------------------------------------------------------------------

@pytest.mark.parametrize("facts, expected", [
    ({"STAGING_SRC": "{work}/staging/src-aaaaaaaaaaaa"}, "src-aaaaaaaaaaaa"),
    ({"CANDIDATE_TAG": "upgrade-bbbbbbbbbbbb"}, "src-bbbbbbbbbbbb"),       # a cutover from before STAGING_SRC
])
def test_f6_finalize_deletes_the_staging_dir_recorded_in_the_state_not_the_current_pins(tmp_path, facts, expected):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(bind / "rm", "rm")
    facts = {key: value.format(work=tmp_path / "work") for key, value in facts.items()}
    state = _cutover_state(tmp_path, stack, STEPS, CUTOVER_OK="1", **facts)
    r = _run(tmp_path, tools, stack, bind, "finalize", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    rm = [line for line in _calls(tmp_path).splitlines() if line.startswith("rm ")]
    assert rm == [f"rm -rf -- {tmp_path / 'work/staging' / expected}"], rm


def test_f6_finalize_never_deletes_a_recorded_path_outside_the_staging_root(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(bind / "rm", "rm")
    state = _cutover_state(tmp_path, stack, STEPS, CUTOVER_OK="1", STAGING_SRC=str(stack))
    r = _run(tmp_path, tools, stack, bind, "finalize", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [line for line in _calls(tmp_path).splitlines() if line.startswith("rm ")]
    assert "not deleting" in r.stderr


# F4 -------------------------------------------------------------------------

def _go_live_rollback(tmp_path: Path, **facts):
    """A cutover that went live (BOOT_STARTED) with upgraded configs in the stack."""
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    (stack / "docker-compose.override.yml").write_text("services: {} # upgraded override\n")
    (stack / "docker-compose.admin.yml").write_text("services: {} # upgraded admin.yml\n")
    (stack / "configs/modules/playerbots.conf").write_text("Playerbots.X = 1 # upgraded\n")
    state = _cutover_state(tmp_path, stack, STEPS, ADMIN_WAS_RUNNING="true", IMAGES_SWITCH_STARTED="1",
                           BOOT_STARTED="1", CUTOVER_OK="1", **facts)
    return tools, stack, bind, state


def test_f4_rollback_after_go_live_saves_the_upgraded_state_before_overwriting_it(tmp_path):
    tools, stack, bind, state = _go_live_rollback(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = _calls(tmp_path).splitlines()
    assert _index(lines, "docker compose stop -t ") < _index(lines, "backup.sh --label prerollback") \
        < _index(lines, "restore-azerothcore.sh")
    saved = state / "pre-rollback"
    archive = saved / "azerothcore-backup-prerollback-2026-10-28T10-00-00.tar.gz"
    assert archive.read_text() == "archive\n"
    assert (saved / f"{archive.name}.sha256").read_text().split()[0] == hashlib.sha256(b"archive\n").hexdigest()
    assert (saved / "docker-compose.override.yml").read_text() == "services: {} # upgraded override\n"
    assert (saved / "docker-compose.admin.yml").read_text() == "services: {} # upgraded admin.yml\n"
    assert (saved / "configs/modules/playerbots.conf").read_text() == "Playerbots.X = 1 # upgraded\n"
    assert (saved / "etc/worldserver.conf").is_file()
    assert (stack / "docker-compose.override.yml").read_text() == "services: {} # pre-upgrade override\n"
    text = (state / "state.env").read_text()
    assert "PRE_ROLLBACK_BACKUP=1" in text and f"PRE_ROLLBACK_ARCHIVE={archive}" in text


def test_f4_a_failing_safety_backup_refuses_the_rollback_unless_told_otherwise(tmp_path):
    tools, stack, bind, state = _go_live_rollback(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes", FAKE_BACKUP_FAIL="1")
    assert r.returncode == 1
    assert "safety backup failed" in r.stderr and "--no-safety-backup" in r.stderr
    calls = _calls(tmp_path)
    assert "docker compose stop -t " in calls and "restore-azerothcore.sh" not in calls
    assert "docker tag" not in calls and "docker run" not in calls and "compose up" not in calls
    assert (stack / "docker-compose.override.yml").read_text() == "services: {} # upgraded override\n"
    assert "PRE_ROLLBACK_BACKUP" not in (state / "state.env").read_text()
    (tmp_path / "calls").write_text("")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes", "--no-safety-backup",
             FAKE_BACKUP_FAIL="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backup.sh" not in _calls(tmp_path) and "restore-azerothcore.sh" in _calls(tmp_path)
    assert "--no-safety-backup" in r.stderr  # the warning names what was skipped
    assert (state / "pre-rollback/docker-compose.override.yml").read_text() == "services: {} # upgraded override\n"
    assert "ROLLED_BACK=1" in (state / "state.env").read_text()


def test_f4_a_rerun_does_not_take_the_safety_backup_twice(tmp_path):
    tools, stack, bind, state = _go_live_rollback(tmp_path, ROLLBACK_STARTED="1", PRE_ROLLBACK_BACKUP="1")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "backup.sh" not in _calls(tmp_path)
    assert not (state / "pre-rollback").exists()


def test_f4_no_safety_backup_when_the_new_version_never_booted(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    state = _cutover_state(tmp_path, stack, _before("switch_images"), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "restore-azerothcore.sh" in _calls(tmp_path) and "backup.sh" not in _calls(tmp_path)


# F5 -------------------------------------------------------------------------

def test_f5_a_failing_previous_verifier_is_an_open_item_and_a_rerun_only_verifies(tmp_path):
    tools, stack, bind, state = _go_live_rollback(tmp_path)
    _stub_script(state / "repo-snapshot/scripts/verify-azerothcore.sh", "old-verifier", rc=1)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 1
    text = (state / "state.env").read_text()
    assert "ROLLBACK_RESTORED=1" in text and "ROLLED_BACK" not in text
    assert "--accept-verify-failure" in r.stderr and "Rollback did not finish" not in r.stderr
    # A re-run neither stops nor restores anything again: it waits and verifies.
    for _attempt in range(2):
        (tmp_path / "calls").write_text("")
        r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
        assert r.returncode == 1
        calls = _calls(tmp_path)
        assert "logs --since" in calls and "old-verifier" in calls
        for mutation in (" stop", "docker tag", "docker run", "compose up", "docker start",
                         "restore-azerothcore.sh", "backup.sh"):
            assert mutation not in calls, mutation
    _stub_script(state / "repo-snapshot/scripts/verify-azerothcore.sh", "old-verifier")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ROLLED_BACK=1" in (state / "state.env").read_text()


def test_f5_accept_verify_failure_closes_a_restored_rollback(tmp_path):
    tools, stack, bind, state = _go_live_rollback(tmp_path, ROLLBACK_STARTED="1", ROLLBACK_RESTORED="1")
    _stub_script(state / "repo-snapshot/scripts/verify-azerothcore.sh", "old-verifier", rc=1)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--accept-verify-failure")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ROLLED_BACK=1" in (state / "state.env").read_text()
    assert "WARNING" in r.stderr and "verif" in r.stderr
    _no_mutation(tmp_path)


def test_f5_accept_verify_failure_needs_a_restored_stack(tmp_path):
    tools, stack, bind, state = _go_live_rollback(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes", "--accept-verify-failure")
    assert r.returncode == 1
    assert "ROLLBACK_RESTORED" in r.stderr
    _no_mutation(tmp_path)
    assert "ROLLBACK_STARTED" not in (state / "state.env").read_text()


def _tooling_commit(repo: Path, versions, scratch: Path) -> str:
    """A commit beside HEAD whose scripts/upstream-versions.env is VERSIONS (None: absent)."""
    env = {**os.environ, **GIT_ENV, "GIT_INDEX_FILE": str(scratch / "tooling-index")}

    def git(*args: str, stdin: str = "") -> str:
        return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                              text=True, input=stdin).stdout.strip()
    git("read-tree", "HEAD")
    if versions is None:
        git("update-index", "--force-remove", "scripts/upstream-versions.env")
    else:
        blob = git("hash-object", "-w", "--stdin", stdin=versions)
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},scripts/upstream-versions.env")
    return git("commit-tree", git("write-tree"), "-p", "HEAD", "-m", "previous tooling")


@pytest.mark.parametrize("variant, passes", [("pins other revisions", False), ("pins nothing", True)])
def test_f5_preflight_checks_the_previous_repo_rev_against_the_live_checkouts(tmp_path, variant, passes):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    if variant == "pins other revisions":
        versions = re.sub(r"(?m)^MOD_IP_REV=.*$", "MOD_IP_REV=" + "1" * 40, (tools / "upstream-versions.env").read_text())
    else:
        versions = None  # tooling from before upstream pinning (e.g. ec93de3)
    previous = _tooling_commit(tmp_path / "repo", versions, tmp_path)
    r = _run(tmp_path, tools, stack, bind, "preflight", "--previous-repo-rev", previous)
    assert (r.returncode == 0) == passes, r.stdout + r.stderr
    if not passes:
        assert "previous-repo-rev is not the tooling the live stack was installed with" in r.stdout
        assert "mod-individual-progression" in r.stdout


# F12 ------------------------------------------------------------------------

def test_f12_rollback_restores_with_the_archive_realmlist(tmp_path):
    tools, stack, bind, state = _go_live_rollback(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    restore = next(line for line in _calls(tmp_path).splitlines() if line.startswith("restore-azerothcore.sh "))
    assert "--keep-archive-realmlist" in restore.split()


# F17 ------------------------------------------------------------------------

def test_f17_rollback_refuses_a_local_file_where_the_old_revision_tracks_one(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, _before("switch_config"), ADMIN_WAS_RUNNING="true",
                           SWITCH_CODE_STARTED="1")
    # The switched core no longer tracks README, and a local file now sits there.
    _git("rm", "-q", "README", cwd=stack)
    _git("commit", "-qm", "upstream drops README", cwd=stack)
    (stack / "README").write_text("operator's notes\n")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "untracked: README" in r.stdout + r.stderr and "collide" in r.stderr
    _no_mutation(tmp_path)
    assert "ROLLBACK_STARTED" not in (state / "state.env").read_text()


def test_f17_rollback_saves_and_discards_core_edits_made_after_the_switch(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    original = (stack / "README").read_text()
    (stack / "README").write_text("edited after the cutover\n")
    state = _cutover_state(tmp_path, stack, _before("switch_config"), ADMIN_WAS_RUNNING="true")
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "+edited after the cutover" in (state / "patches-rollback/core.diff").read_text()
    assert (stack / "README").read_text() == original


# F18 ------------------------------------------------------------------------

@pytest.mark.parametrize("steps", [_before("snapshot_state"), _before("switch_config")])
def test_f18_rollback_applies_a_live_diff_exactly_once(tmp_path, steps):
    # Before STEP_snapshot_state the tree is not discarded and may already hold the diff.
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    module = stack / "modules/mod-playerbots"
    (module / "README").write_text("managed patch\n")
    diff = _git("diff", cwd=module) + "\n"
    if "snapshot_state" in steps:
        (module / "README").write_text("edited after the cutover\n")
    state = _cutover_state(tmp_path, stack, steps, ADMIN_WAS_RUNNING="true")
    (state / "patches-live/mod-playerbots.diff").write_text(diff)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (module / "README").read_text() == "managed patch\n"


# F10 ------------------------------------------------------------------------

@pytest.mark.parametrize("before, env, shown", [
    ("3 10\n", {"FAKE_ACCOUNTS": "2", "FAKE_CHARACTERS": "10"}, "accounts count DECREASED across the upgrade: 3 -> 2"),
    ("3 10\n", {"FAKE_ACCOUNTS": "4", "FAKE_CHARACTERS": "9"}, "characters count DECREASED across the upgrade: 10 -> 9"),
    ("3 10\n", {"FAKE_ACCOUNTS": "", "FAKE_CHARACTERS": ""}, "cannot compare accounts counts"),
])
def test_f10_a_count_decrease_stops_the_realm(tmp_path, before, env, shown):
    tools, stack, bind, state = _resume_fixture(tmp_path, "verify", BOOT_STARTED="1")
    (state / "counts-before.txt").write_text(before)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", **env)
    assert r.returncode == 1
    assert shown in r.stderr and "Server is stopped" in r.stderr
    assert _stops_after_up(tmp_path), _calls(tmp_path)
    assert "STEP_verify" not in (state / "state.env").read_text()


def test_f10_a_count_increase_warns_records_and_continues(tmp_path):
    # A player or the bot pool may create a character while the new version boots.
    tools, stack, bind, state = _resume_fixture(tmp_path, "verify", BOOT_STARTED="1")
    (state / "counts-before.txt").write_text("3 10\n")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             FAKE_ACCOUNTS="3", FAKE_CHARACTERS="12")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "WARNING: characters 10 -> 12 (+2) since the cold backup" in out
    assert "check before opening the realm" in out
    assert "WARNING: accounts" not in out
    assert not _stops_after_up(tmp_path), _calls(tmp_path)
    text = (state / "state.env").read_text()
    assert "COUNTS_WARNING=characters 10 -> 12 (+2)" in text
    assert "STEP_verify=1" in text and "CUTOVER_OK=1" in text


# Follow-up G1-G5 --------------------------------------------------------------

def _copies_carry_the_source_tree(bind: Path) -> None:
    """Make the docker stub model a stage copy: once the rollback volume was
    copied into, its probe reports the live volume's tree and data-version."""
    real = bind / "docker-stub"
    (bind / "docker").rename(real)
    rollback, live = _key(ROLLBACK_VOL), _key(LIVE_VOL)
    _executable(bind / "docker", "#!/bin/bash\n"
                f'if grep -qx {ROLLBACK_VOL} "$UPGRADE_CALLS.copied" 2>/dev/null; then\n'
                f'    export FAKE_TREE_{rollback}="${{FAKE_TREE_{live}:-100 4096}}"'
                f' FAKE_VERSION_{rollback}="${{FAKE_VERSION_{live}:-v19}}"\n'
                f'fi\nexec "{real}" "$@"\n')


@pytest.mark.parametrize("steps, facts, shown", [
    (_before("switch_images"), {"CLIENT_DATA_SWITCH_STARTED": "1"}, "incomplete"),
    (STEPS, {"CUTOVER_OK": "1", "BOOT_STARTED": "1", "CLIENT_DATA_SWITCH_STARTED": "1", "ROLLBACK_STARTED": "1"},
     "a rollback was started"),
])
def test_g1_stage_refuses_while_a_cutover_or_its_rollback_is_unfinished(tmp_path, steps, facts, shown):
    # The live client data may be half-switched or half-restored: a stage copy
    # would replace the only complete pre-upgrade copy with it.
    tools, stack, bind, staging = _stage_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, steps, ADMIN_WAS_RUNNING="true", **CLIENT_FACTS, **facts)
    (tmp_path / "work" / "current").symlink_to(state)
    r = _run(tmp_path, tools, stack, bind, "stage", **{f"FAKE_TREE_{_key(LIVE_VOL)}": "37 99",
                                                      f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0"})
    assert r.returncode == 1
    assert shown in r.stderr and str(state) in r.stderr
    assert "finish or roll back that cutover first" in r.stderr
    calls = _calls(tmp_path)
    assert "docker run" not in calls and "docker volume" not in calls and "compose" not in calls, calls
    assert not list(staging.parent.glob("stage-*.env"))


@pytest.mark.parametrize("facts", [{"CUTOVER_OK": "1"}, {"ROLLBACK_STARTED": "1", "ROLLED_BACK": "1"}])
def test_g1_stage_runs_after_a_finished_cutover_or_rollback(tmp_path, facts):
    tools, stack, bind, _ = _stage_fixture(tmp_path)
    state = _cutover_state(tmp_path, stack, STEPS, ADMIN_WAS_RUNNING="true", **CLIENT_FACTS, **facts)
    (tmp_path / "work" / "current").symlink_to(state)
    r = _run(tmp_path, tools, stack, bind, "stage", **{f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0"})
    assert r.returncode == 0, r.stdout + r.stderr


def test_g1_stage_never_overwrites_a_complete_rollback_copy_that_differs_from_the_live_data(tmp_path):
    # A switch or rollback killed mid-copy leaves the live volume partial: the
    # marked copy is then the only complete one and must never be replaced.
    tools, stack, bind, staging = _stage_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "stage", **{f"FAKE_TREE_{_key(LIVE_VOL)}": "37 99",
                                                      f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0"})
    assert r.returncode == 1
    assert "rollback copy and live client data differ" in r.stderr
    assert ROLLBACK_VOL in r.stderr and "by hand" in r.stderr
    calls = _calls(tmp_path)
    assert f"-v {ROLLBACK_VOL}:/dst" not in calls and "find /dst -mindepth 1 -delete" not in calls, calls
    assert "docker volume create" not in calls
    assert not list(staging.parent.glob("stage-*.env"))


@pytest.mark.parametrize("env", [
    {f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": "", f"FAKE_TREE_{_key(ROLLBACK_VOL)}": "37 99"},  # interrupted copy
    {f"FAKE_TREE_{_key(ROLLBACK_VOL)}": "99 4096", f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": "100 4096"},
    {f"FAKE_VERSION_{_key(ROLLBACK_VOL)}": "v18"},
])
def test_g1_stage_recopies_a_rollback_copy_that_is_itself_incomplete(tmp_path, env):
    # Only the copy's own state decides a re-copy, even when it also differs from the live data.
    tools, stack, bind, staging = _stage_fixture(tmp_path)
    _copies_carry_the_source_tree(bind)
    r = _run(tmp_path, tools, stack, bind, "stage", **{f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0", **env})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "differ" not in r.stderr
    copies = [line for line in _calls(tmp_path).splitlines() if f"-v {ROLLBACK_VOL}:/dst " in line]
    assert len(copies) == 1 and f"-v {LIVE_VOL}:/src:ro" in copies[0], copies
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    assert f"CLIENT_DATA_ROLLBACK_VOLUME={ROLLBACK_VOL}" in (staging.parent / f"stage-{core12}.env").read_text()


@pytest.mark.parametrize("code, rc, shown", [
    ("0", 0, "[OK]   candidate worldserver exited 0 on SIGTERM"),
    ("137", 1, "[FAIL] candidate worldserver did not exit cleanly on SIGTERM (code 137); "
               "the cutover's stop step would refuse"),
])
def test_g2_rehearsal_checks_the_candidate_worldserver_exit_code_after_its_stop(tmp_path, code, rc, shown):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive),
             FAKE_REHEARSAL_WORLD_EXIT=code, **env)
    assert r.returncode == rc, r.stdout + r.stderr
    assert shown in r.stdout
    lines = _calls(tmp_path).splitlines()
    assert _index(lines, "docker stop -t 120 acup-rehearsal-world") \
        < _index(lines, "docker inspect -f {{.State.ExitCode}} acup-rehearsal-world")
    assert ("Rehearsal PASSED" in r.stdout) == (rc == 0)
    assert (tmp_path / "work" / "rehearsal-latest").exists() == (rc == 0)


def test_g3_the_switch_step_probes_the_rollback_copy_with_the_live_client_data_image(tmp_path):
    # The marker was written and verified by the live image; the candidate image is not exercised mid-cutover.
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_client_data", **CLIENT_FACTS)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    probes = [line for line in _calls(tmp_path).splitlines() if ":/d:ro --entrypoint sh " in line]
    assert probes and all("--entrypoint sh acore/ac-wotlk-client-data:playerbot-local " in line
                          for line in probes), probes


@pytest.mark.parametrize("facts, shown, absent", [
    ({}, "the live client data was not touched", "partly switched"),
    ({"CLIENT_DATA_SWITCH_STARTED": "1"}, "partly switched", "was not touched"),
])
def test_g3_a_refused_switch_says_whether_the_live_client_data_was_touched(tmp_path, facts, shown, absent):
    tools, stack, bind, state = _resume_fixture(tmp_path, "switch_client_data", **CLIENT_FACTS, **facts)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             **{f"FAKE_MARKER_{_key(ROLLBACK_VOL)}": ""})
    assert r.returncode == 1
    assert "completion marker" in r.stderr and shown in r.stderr and absent not in r.stderr
    assert f"-v {LIVE_VOL}:/dst" not in _calls(tmp_path)


@pytest.mark.parametrize("env, shown", [
    ({}, None),
    ({f"FAKE_TREE_{_key(LIVE_VOL)}": "7 9"}, "7 entries / 9 bytes"),
    ({f"FAKE_VERSION_{_key(LIVE_VOL)}": "v20.0"}, "v20.0"),
])
def test_g4_rollback_verifies_the_restored_client_data_against_the_copy_marker(tmp_path, env, shown):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(tools / "restore-azerothcore.sh", "restore-azerothcore.sh")
    state = _cutover_state(tmp_path, stack, _before("switch_client_data"), ADMIN_WAS_RUNNING="true",
                           CLIENT_DATA_SWITCH_STARTED="1", **CLIENT_FACTS)
    r = _run(tmp_path, tools, stack, bind, "rollback", "--state", str(state), "--yes", **env)
    lines = _calls(tmp_path).splitlines()
    copy = _index(lines, f"-v {ROLLBACK_VOL}:/src:ro -v {LIVE_VOL}:/dst ")
    probe = _index(lines, f"-v {LIVE_VOL}:/d:ro ")
    assert copy < probe
    text = (state / "state.env").read_text()
    if shown is None:
        assert r.returncode == 0, r.stdout + r.stderr
        assert probe < _index(lines, "restore-azerothcore.sh")
        return
    assert r.returncode == 1
    assert LIVE_VOL in r.stderr and shown in r.stderr and "safe to re-run" in r.stderr
    calls = _calls(tmp_path)
    assert "restore-azerothcore.sh" not in calls and "compose up" not in calls
    assert "ROLLBACK_RESTORED" not in text and "ROLLED_BACK" not in text


def test_g5_a_stray_admin_container_without_an_admin_stack_is_stopped_and_left_stopped(tmp_path):
    tools, stack, bind, _ = _addendum_fixture(tmp_path)
    _stub_script(stack / "backup.sh", "backup.sh")
    state = _cutover_state(tmp_path, stack, ("snapshot_tooling",), ADMIN_PRESENT="false")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert "pre-upgrade archive not found" in r.stderr, r.stdout + r.stderr  # the stop step finished
    assert "docker stop azerothcore-admin" in _calls(tmp_path)
    assert "stray azerothcore-admin container" in r.stdout and "left stopped" in r.stdout


def test_g5_the_admin_step_says_a_stray_admin_container_stays_stopped(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "admin", BOOT_STARTED="1", ADMIN_PRESENT="false")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             FAKE_STOPPED="azerothcore-admin")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing to redeploy" in r.stdout and "left stopped" in r.stdout
    calls = _calls(tmp_path)
    assert "docker start azerothcore-admin" not in calls and "admin-redeploy" not in calls


# D4 fix: exact byte totals in the client-data copy marker -----------------------

def _awk_sum_programs() -> list[str]:
    """Every awk byte-sum word in upgrade-azerothcore.sh, exactly as the
    container's shell receives it: the double-quoted `awk "..."` argument."""
    text = (SCRIPTS_DIR / "upgrade-azerothcore.sh").read_text()
    return [word for word in re.findall(r'awk ("(?:\\.|[^"\\])*")', text) if "s+=" in word]


def test_d4_the_script_has_the_two_byte_sum_programs():
    # The tree probe and the copy marker; a third sum added later must be covered below too.
    assert len(_awk_sum_programs()) == 2, _awk_sum_programs()


@pytest.mark.parametrize("sizes, total", [
    (["1500000000", "1731879999", "124"], "3231880123"),  # the real client data: mawk printed 3.23188e+09
    (["20000000000000000000"], "20000000000000000000"),   # past 2^64 even this sandbox's newer mawk prints %.6g
    (["4096", "0", "7"], "4103"),
    ([], "0"),
])
def test_d4_the_awk_byte_sum_prints_an_exact_integer(sizes, total):
    # The client-data image's mawk prints sums above 2^31 through OFMT (%.6g); the
    # marker and the tree sum are compared as strings, so every byte must show.
    assert shutil.which("awk"), "awk is required"
    for word in _awk_sum_programs():
        r = subprocess.run(["sh", "-c", f"awk {word}"], input="".join(f"{s}\n" for s in sizes),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert r.stdout == f"{total}\n", (word, r.stdout)


def test_d4_stage_recopies_a_rollback_volume_whose_marker_bytes_are_not_an_exact_integer(tmp_path):
    # A marker written by the old code (BYTES=3.23188e+09) cannot vouch for a copy,
    # even when the tree probe prints the same rounded figure.
    tools, stack, bind, staging = _stage_fixture(tmp_path)
    real = bind / "docker-stub"
    (bind / "docker").rename(real)
    rollback, live = _key(ROLLBACK_VOL), _key(LIVE_VOL)
    # Once the copy ran, the fixed probe reports exact totals for both volumes.
    _executable(bind / "docker", "#!/bin/bash\n"
                f'if grep -qx {ROLLBACK_VOL} "$UPGRADE_CALLS.copied" 2>/dev/null; then\n'
                f'    export FAKE_TREE_{rollback}="100 3231880123" FAKE_TREE_{live}="100 3231880123"\n'
                f'fi\nexec "{real}" "$@"\n')
    r = _run(tmp_path, tools, stack, bind, "stage", **{
        f"FAKE_VERSION_{_key(TARGET_VOL)}": "v20.0",
        f"FAKE_MARKER_{rollback}": "100 3.23188e+09", f"FAKE_TREE_{rollback}": "100 3.23188e+09",
        f"FAKE_TREE_{live}": "100 3.23188e+09"})
    assert r.returncode == 0, r.stdout + r.stderr
    copies = [line for line in _calls(tmp_path).splitlines() if f"-v {ROLLBACK_VOL}:/dst " in line]
    assert len(copies) == 1, copies
    assert f"-v {LIVE_VOL}:/src:ro" in copies[0]
    assert "find /dst -mindepth 1 -delete" in copies[0] and ".acup-complete" in copies[0]
    assert "differ" not in r.stderr and "3.23188e+09" in r.stdout + r.stderr  # the log says why it copies
    core12 = _git("rev-parse", "--short=12", "HEAD", cwd=stack)
    assert f"CLIENT_DATA_ROLLBACK_VOLUME={ROLLBACK_VOL}" in (staging.parent / f"stage-{core12}.env").read_text()


# D5 fix: pre-db-import SQL (old mod-IP AUTO_INCREMENT gameobject guids) ----------

PRE_DB_IMPORT = "patches/sql/pre-db-import"
MOD_IP_GUIDS = "0001-mod-ip-fixed-gameobject-guids.sql"
REHEARSAL_DB_CLIENT = "docker exec -i -e MYSQL_PWD=drillpw acup-rehearsal-db mysql -uroot -N -B"
LIVE_DB_CLIENT = "docker exec -i ac-database sh -c "


def _guard(path: Path) -> str:
    guards = re.findall(r"(?m)^-- guard: (.+)$", path.read_text())
    assert len(guards) == 1, guards
    return guards[0]


def test_d5_the_pre_db_import_sql_moves_the_six_old_mod_ip_spawns_to_the_new_fixed_guids():
    path = SCRIPTS_DIR / PRE_DB_IMPORT / MOD_IP_GUIDS
    body = "\n".join(line for line in path.read_text().splitlines() if not line.startswith("--"))
    statements = [s.strip() for s in body.split(";") if s.strip()]
    assert len(statements) == 6, statements
    moves = set()
    for statement in statements:
        m = re.fullmatch(
            r"UPDATE acore_world\.gameobject SET guid = (\d+)\s+WHERE id = (\d+) AND map = (\d+) AND guid <> (\d+)"
            r"\s+AND NOT EXISTS \(SELECT 1 FROM \(SELECT guid FROM acore_world\.gameobject WHERE guid = (\d+)"
            r" LIMIT 1\) AS t\)", statement)
        assert m, statement
        target, spawn_id, spawn_map, unless_at, occupied = m.groups()
        assert target == unless_at == occupied, statement
        moves.add((int(spawn_id), int(spawn_map), int(target)))
    # 193166 also spawns on map 571 (core data): only the map-0 row may move.
    assert moves == {(176146, 1, 650111), (176147, 1, 650112), (176148, 1, 650113),
                     (361001, 0, 361001), (181056, 0, 361002), (193166, 0, 361003)}
    guard = _guard(path)
    assert guard.startswith("SELECT COUNT(*) FROM acore_world.gameobject WHERE guid >= 5714438 AND (id, map) IN ("), guard
    assert {(int(i), int(m)) for i, m in re.findall(r"\((\d+), (\d+)\)", guard)} == {(i, m) for i, m, _ in moves}


def test_d5_rehearsal_applies_the_pre_db_import_sql_to_the_restored_copy_before_the_db_import(tmp_path):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[OK]   pre-db-import SQL applied" in r.stdout
    sql = tools / PRE_DB_IMPORT / MOD_IP_GUIDS
    lines = _calls(tmp_path).splitlines()
    apply = _index(lines, f"{REHEARSAL_DB_CLIENT} |")
    guard = _index(lines, f"{REHEARSAL_DB_CLIENT} -e {_guard(sql)} |")
    assert _index(lines, "restore-drill.sh ") < apply < guard \
        < _index(lines, "docker run -d --name acup-rehearsal-dbimport "), lines
    assert (tmp_path / "calls.sql").read_text() == f"== {REHEARSAL_DB_CLIENT}\n{sql.read_text()}"


def test_d5_cutover_applies_the_pre_db_import_sql_to_ac_database_after_boot_started_before_compose_up(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "boot", IMAGES_SWITCH_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes",
             PEEK_STATE=str(state / "state.env"))
    assert r.returncode == 0, r.stdout + r.stderr
    sql = tools / PRE_DB_IMPORT / MOD_IP_GUIDS
    lines = _calls(tmp_path).splitlines()
    apply = _index(lines, LIVE_DB_CLIENT)
    guard = _index(lines, f" sh -e {_guard(sql)} |")
    assert LIVE_DB_CLIENT in lines[guard] and '"$@" sh | markers: ' in lines[apply]  # the file goes in on stdin
    assert apply < guard < _index(lines, "docker compose up -d --no-build"), lines
    assert "BOOT_STARTED=1" in lines[apply]
    assert lines[apply - 1] == f"sync {state / 'state.env'}"  # the boot marker is on disk before the first change
    header, _, fed = (tmp_path / "calls.sql").read_text().partition("\n")
    assert header.startswith(f"== {LIVE_DB_CLIENT}") and fed == sql.read_text()
    text = (state / "state.env").read_text()
    assert "PRE_DB_IMPORT_APPLIED=1" in text and "CUTOVER_OK=1" in text


@pytest.mark.parametrize("env, shown", [
    ({"FAKE_GUARD_COUNT": "1"}, f"the guard of {MOD_IP_GUIDS} counts '1' (must be 0)"),
    ({"FAKE_SQL_FAIL": "1"}, f"{MOD_IP_GUIDS} failed against acup-rehearsal-db"),
])
def test_d5_a_failing_pre_db_import_sql_fails_the_rehearsal_without_running_the_db_import(tmp_path, env, shown):
    tools, stack, bind, archive, fixture_env = _rehearse_fixture(tmp_path)
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **fixture_env, **env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "[FAIL] pre-db-import SQL" in r.stdout and "Rehearsal FAILED" in r.stdout
    calls = _calls(tmp_path)
    assert "docker run -d --name acup-rehearsal-dbimport" not in calls
    assert "--name acup-rehearsal-world" not in calls
    assert shown in (_rehearsal_dir(tmp_path) / "pre-db-import.log").read_text()
    assert not (tmp_path / "work" / "rehearsal-latest").exists()


@pytest.mark.parametrize("env, shown", [
    ({"FAKE_GUARD_COUNT": "1"}, f"the guard of {MOD_IP_GUIDS} counts '1' (must be 0)"),
    ({"FAKE_SQL_FAIL": "1"}, f"{MOD_IP_GUIDS} failed against ac-database"),
])
def test_d5_a_failing_pre_db_import_sql_stops_the_cutover_before_the_stack_starts(tmp_path, env, shown):
    tools, stack, bind, state = _resume_fixture(tmp_path, "boot", IMAGES_SWITCH_STARTED="1")
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes", **env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert shown in r.stderr
    assert "Server is stopped" in r.stderr and f"rollback --state {state}" in r.stderr
    assert "compose up" not in _calls(tmp_path)
    text = (state / "state.env").read_text()
    assert "BOOT_STARTED=1" in text and "STEP_boot" not in text and "PRE_DB_IMPORT_APPLIED" not in text


def test_d5_a_pre_db_import_sql_file_without_one_guard_line_fails_before_any_sql_runs(tmp_path):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    (tools / PRE_DB_IMPORT / "0002-unguarded.sql").write_text("UPDATE acore_world.gameobject SET state = 1;\n")
    _stage_env(tmp_path, tools, _git("rev-parse", "HEAD", cwd=stack))  # staged with it
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **env)
    assert r.returncode == 1, r.stdout + r.stderr
    log = (_rehearsal_dir(tmp_path) / "pre-db-import.log").read_text()
    assert "0002-unguarded.sql must carry exactly one '-- guard: " in log
    assert not (tmp_path / "calls.sql").exists()  # not even 0001 ran
    assert "docker run -d --name acup-rehearsal-dbimport" not in _calls(tmp_path)


def _edit_pre_db_import_sql(tools: Path) -> None:
    sql = tools / PRE_DB_IMPORT / MOD_IP_GUIDS
    sql.write_text(sql.read_text() + "-- edited after the stage\n")


def test_d5_a_changed_pre_db_import_sql_file_invalidates_the_stage_for_the_cutover(tmp_path):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    core = _git("rev-parse", "HEAD", cwd=stack)
    _stage_env(tmp_path, tools, core)
    _rehearsal_result(tmp_path, tools, core)
    _edit_pre_db_import_sql(tools)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes")
    assert r.returncode == 1
    assert "re-run stage" in r.stderr and "SQL_PATCHES_SHA256" in r.stderr
    _no_mutation(tmp_path)


def test_d5_a_changed_pre_db_import_sql_file_invalidates_the_stage_for_the_rehearsal(tmp_path):
    tools, stack, bind, archive, env = _rehearse_fixture(tmp_path)
    _edit_pre_db_import_sql(tools)
    r = _run(tmp_path, tools, stack, bind, "rehearse", "--archive", str(archive), **env)
    assert r.returncode == 1
    assert "re-run stage" in r.stderr and "SQL_PATCHES_SHA256" in r.stderr
    assert "restore-drill.sh" not in _calls(tmp_path)


def test_d5_a_resume_refuses_a_pre_db_import_sql_file_changed_since_the_cutover_started(tmp_path):
    tools, stack, bind, state = _resume_fixture(tmp_path, "boot", IMAGES_SWITCH_STARTED="1")
    with open(state / "state.env", "a") as handle:
        handle.write(f"BUILD_FINGERPRINT={_fingerprint(tools)}\n")
    _edit_pre_db_import_sql(tools)
    r = _run(tmp_path, tools, stack, bind, "cutover", "--state", str(state), "--yes")
    assert r.returncode == 1
    assert "changed since this cutover started" in r.stderr
    _no_mutation(tmp_path)
    assert "docker exec -i" not in _calls(tmp_path)


def test_d5_a_dry_run_cutover_prints_the_pre_db_import_sql_without_running_it(tmp_path):
    tools, stack, bind, previous = _addendum_fixture(tmp_path)
    _stage_env(tmp_path, tools, _git("rev-parse", "HEAD", cwd=stack))
    r = _run(tmp_path, tools, stack, bind, "cutover", "--previous-repo-rev", previous, "--yes",
             "--skip-rehearsal", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    sql = tools / PRE_DB_IMPORT / MOD_IP_GUIDS
    out = r.stdout.splitlines()
    apply = _index(out, f"< {sql}")
    assert out[apply].startswith("+ docker exec -i ac-database ")
    assert apply < _index(out, "+ compose up -d --no-build")
    assert "docker exec -i" not in _calls(tmp_path)
