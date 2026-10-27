"""restore-drill.sh with a stub-only PATH: isolation is enforced before import."""

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

SCRIPTS_DIR = Path("/src") if Path("/src/restore-drill.sh").is_file() else Path(__file__).resolve().parents[1]
DRILL = SCRIPTS_DIR / "restore-drill.sh"
DBS = ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"]


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _archive(tmp_path: Path) -> Path:
    dump = "".join(
        f"-- Current Database: `{db}`\nCREATE TABLE `t_{db}` (id int);\n" for db in DBS
    ) + "-- Dump completed on 2026-10-27  3:00:00\n"
    manifest = {"format_version": 2, "databases": DBS, "skipped_databases": [],
                "dump_layout": "single-multi-database"}
    path = tmp_path / "azerothcore-backup-manual-x.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in (("manifest.json", json.dumps(manifest).encode()),
                              ("sql/azerothcore.sql", dump.encode())):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return path


def _bin(tmp_path: Path, inspect_output: str, extra_tools: tuple[str, ...] = ()) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir()
    _executable(bind / "docker", f"""#!/bin/bash
echo "$@" >> "$DRILL_CALLS"
case "$1 $2" in
  "container inspect") exit 1 ;;
  "network inspect") echo true ;;
  "run "*)
    if [ -n "${{DRILL_RUN_FAILS:-}}" ]; then exit 1; fi
    echo cid ;;
  "inspect -f") echo '{inspect_output}' ;;
  "exec "*)
    if [ -n "${{DRILL_FAIL_ON:-}}" ] && [[ "$*" == *"$DRILL_FAIL_ON"* ]]; then exit 1; fi
    case "$*" in
      *"ac-database"*)
        cat > "${{DRILL_LIVE_SQL:-/dev/null}}"
        printf 'acore_auth.account\\t7\\n' ;;
      *"table_schema LIKE"*)
        printf 'acore_auth.account\\nacore_world.creature\\nacore_world.x;DROP TABLE y\\n' ;;
      *"information_schema.tables WHERE table_schema IN"*)
        printf 'acore_auth\\t1\\nacore_characters\\t1\\nacore_playerbots\\t1\\nacore_world\\t1\\n' ;;
      *"SELECT COUNT(*) FROM"*) echo 7 ;;
      *) cat >/dev/null 2>&1 || true ;;
    esac ;;
  "rm "*) ;;
esac
exit 0
""")
    _executable(bind / "sleep", "#!/bin/sh\nexit 0\n")
    for tool in ("bash", "cat", "date", "diff", "grep", "head", "mktemp", "python3", "rm", "sed",
                 "seq", "sort", "tar", "tr", "base64", "chmod", "gzip", *extra_tools):
        real = shutil.which(tool)
        assert real, tool
        (bind / tool).symlink_to(real)
    return bind


ROOT_GUARD = (
    'if [ "$EUID" -eq 0 ]; then\n'
    '    echo "ERROR: do not run as root; docker is invoked as your user (docker group)." >&2\n'
    '    exit 1\n'
    'fi\n'
)


def _run(tmp_path: Path, bind: Path, *args: str,
         env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    source = DRILL.read_text()
    assert ROOT_GUARD in source
    script = tmp_path / "restore-drill.sh"
    script.write_text(source.replace(ROOT_GUARD, "", 1))
    return subprocess.run(["/bin/bash", str(script), *args], capture_output=True, text=True, timeout=60,
                          env={"PATH": str(bind), "DRILL_CALLS": str(tmp_path / "calls"), "HOME": str(tmp_path),
                              "DRILL_LIVE_SQL": str(tmp_path / "live.sql"), **(env_extra or {})})


def test_isolated_drill_imports_and_cleans_up(tmp_path):
    bind = _bin(tmp_path, "none|{}|volume:/var/lib/docker/volumes/x/_data;")
    r = _run(tmp_path, bind, str(_archive(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: DRILL PASSED" in r.stdout
    calls = (tmp_path / "calls").read_text()
    assert "--network none" in calls
    assert " -p " not in calls and "--publish" not in calls
    assert "rm -f -v acup-drill-db" in calls
    assert "ac-database" not in calls, "the live DB is only read with --compare-live"


def test_drill_refuses_a_container_with_a_host_bind_mount(tmp_path):
    bind = _bin(tmp_path, "none|{}|bind:/opt/stacks/azerothcore/data;")
    r = _run(tmp_path, bind, str(_archive(tmp_path)))
    assert r.returncode == 1
    calls = (tmp_path / "calls").read_text()
    assert "exec" not in calls, "nothing may be imported into a non-isolated container"
    assert "rm -f -v acup-drill-db" in calls


def test_drill_refuses_names_outside_the_acup_namespace(tmp_path):
    bind = _bin(tmp_path, "none|{}|volume:x;")
    r = _run(tmp_path, bind, str(_archive(tmp_path)), "--keep", "--name", "ac-database",
             "--network", "n", "--password-file", str(tmp_path / "pw"))
    assert r.returncode == 1
    assert "must start with 'acup-'" in r.stderr
    assert not (tmp_path / "calls").exists()


def _docker_rm_calls(calls: str) -> list[str]:
    return [line for line in calls.splitlines() if line.startswith("rm ")]


def test_keep_run_that_aborts_midway_still_removes_container_and_password_file(tmp_path):
    # A set -e abort (inventory query fails) while FAILED is still 0 must not leak the kept container.
    bind = _bin(tmp_path, "n|{}|volume:x;")
    pw = tmp_path / "pw"
    r = _run(tmp_path, bind, str(_archive(tmp_path)), "--keep", "--name", "acup-keep",
             "--network", "n", "--password-file", str(pw),
             env_extra={"DRILL_FAIL_ON": "information_schema.tables WHERE table_schema IN"})
    assert r.returncode != 0, r.stdout + r.stderr
    assert "RESULT: DRILL PASSED" not in r.stdout
    assert "rm -f -v acup-keep" in (tmp_path / "calls").read_text()
    assert not pw.exists(), "the password is useless once the kept container is removed"


def test_keep_run_that_passes_keeps_container_and_private_password_file(tmp_path):
    bind = _bin(tmp_path, "n|{}|volume:x;")
    pw = tmp_path / "pw"
    r = _run(tmp_path, bind, str(_archive(tmp_path)), "--keep", "--name", "acup-keep",
             "--network", "n", "--password-file", str(pw))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: DRILL PASSED" in r.stdout
    assert _docker_rm_calls((tmp_path / "calls").read_text()) == []
    assert pw.read_text().strip()
    assert stat.S_IMODE(os.stat(pw).st_mode) == 0o600


def test_container_is_removed_even_when_docker_run_fails(tmp_path):
    # `docker run` can create the container and still exit non-zero (e.g. fail to start it).
    bind = _bin(tmp_path, "none|{}|volume:x;")
    r = _run(tmp_path, bind, str(_archive(tmp_path)), env_extra={"DRILL_RUN_FAILS": "1"})
    assert r.returncode != 0
    assert "rm -f -v acup-drill-db" in (tmp_path / "calls").read_text()


def test_readiness_probe_and_import_use_tcp_not_the_entrypoint_socket(tmp_path):
    # mysql:8.4's entrypoint runs a socket-only temporary server first; only TCP reaches the final one.
    bind = _bin(tmp_path, "none|{}|volume:x;")
    r = _run(tmp_path, bind, str(_archive(tmp_path)))
    assert r.returncode == 0, r.stdout + r.stderr
    execs = [line for line in (tmp_path / "calls").read_text().splitlines() if line.startswith("exec ")]
    assert execs
    for line in execs:
        assert " -h127.0.0.1 " in line and "--protocol=TCP" in line, line


def test_compare_live_sends_only_read_only_count_queries_to_the_live_db(tmp_path):
    bind = _bin(tmp_path, "none|{}|volume:x;", extra_tools=("awk",))
    r = _run(tmp_path, bind, str(_archive(tmp_path)), "--compare-live")
    assert r.returncode == 0, r.stdout + r.stderr
    live_sql = (tmp_path / "live.sql").read_text().splitlines()
    assert live_sql[0] == "SET SESSION TRANSACTION READ ONLY;"
    queries = [line for line in live_sql[1:] if line.strip()]
    assert len(queries) == 2, live_sql  # the hostile table name was filtered out
    for line in queries:
        assert line.startswith("SELECT '") and "COUNT(*) FROM" in line and line.endswith(";"), line
    assert "DROP" not in "\n".join(live_sql)
