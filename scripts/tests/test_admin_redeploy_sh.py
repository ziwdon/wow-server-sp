import fcntl
import os
import shutil
import stat
import subprocess
from pathlib import Path


ROOT = Path("/repo") if Path("/repo/wow-server-sp-admin").is_dir() else Path(__file__).resolve().parents[2]
REDEPLOY_SH = ROOT / "wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _stack(tmp_path: Path) -> Path:
    stack = tmp_path / "admin-stack"
    (stack / "build" / "app" / "static").mkdir(parents=True)
    for name in ("htmx.min.js", "htmx-sse.js", "jquery.min.js"):
        (stack / "build" / "app" / "static" / name).write_bytes(b"x" * 201)
    stack.joinpath("docker-compose.yml").write_text("old-compose\n")
    stack.joinpath(".env").write_text("TAILSCALE_IP=100.64.0.5\nADMIN_PORT=8765\n")
    return stack


def _stubs(tmp_path: Path) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir(mode=0o777)
    bind.chmod(0o777)
    _executable(bind / "docker", """#!/bin/bash
echo "$@" >> "$ADMIN_REDEPLOY_CALLS"
if [ "$1" = compose ]; then
    for arg in "$@"; do
        if [ "$arg" = build ] && [ "${ADMIN_REDEPLOY_CASE:-healthy}" = build-fail ]; then exit 42; fi
        if [ "$arg" = build ] && [ -n "${ADMIN_REDEPLOY_DIST_LOG:-}" ]; then cat "$STACK_DIR"/.redeploy.*/build/dist/playerbots.conf.dist > "$ADMIN_REDEPLOY_DIST_LOG"; fi
        if [ "$arg" = up ]; then
            if [ ! -f "$ADMIN_REDEPLOY_STATE" ]; then echo candidate > "$ADMIN_REDEPLOY_STATE"; else echo rollback > "$ADMIN_REDEPLOY_STATE"; fi
        fi
    done
    exit 0
fi
if [ "$1" = image ]; then
    exit 0
fi
if [ "$1 $2" = "volume inspect" ]; then
    if [ "${ADMIN_REDEPLOY_CASE:-healthy}" = no-client-data ]; then exit 1; fi
    exit 0
fi
if [ "$1" = inspect ]; then
    if [ "$(cat "$ADMIN_REDEPLOY_STATE" 2>/dev/null)" = candidate ] && [ "${ADMIN_REDEPLOY_CASE:-healthy}" = unhealthy ]; then
        echo unhealthy
    else
        echo healthy
    fi
fi
""")
    _executable(bind / "rsync", """#!/bin/bash
if [ "${ADMIN_REDEPLOY_CASE:-healthy}" = rsync-fail ]; then
    exit 42
fi
if [ "${ADMIN_REDEPLOY_CASE:-healthy}" = rsync-delete-dist ]; then
    destination=""
    for arg in "$@"; do
        destination="$arg"
    done
    case "$destination" in
        "$STACK_DIR"/.redeploy.*/build/)
            /bin/rm -rf -- "${destination}/dist"
            ;;
        *)
            echo "refusing to remove unexpected rsync destination: $destination" >&2
            exit 99
            ;;
    esac
fi
exit 0
""")
    _executable(bind / "sudo", """#!/bin/sh
if [ -n "${ADMIN_REDEPLOY_SUDO_LOG:-}" ]; then echo "$@" >> "$ADMIN_REDEPLOY_SUDO_LOG"; fi
exec "$@"
""")
    _executable(bind / "sleep", "#!/bin/sh\nexit 0\n")
    # curl: logs each call; writes a 300-byte body to -o, or fails (ADMIN_REDEPLOY_CURL=fail).
    _executable(bind / "curl", """#!/bin/bash
echo "curl $*" >> "${ADMIN_REDEPLOY_CURL_LOG:-/dev/null}"
out=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        *) shift ;;
    esac
done
if [ "${ADMIN_REDEPLOY_CURL:-ok}" = fail ]; then
    echo "curl: (6) Could not resolve host: unpkg.com" >&2
    exit 6
fi
head -c 300 /dev/zero | tr '\\0' j > "$out"
""")
    return bind


def _run(stack: Path, bind: Path, case: str, extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], str]:
    calls = bind / "calls"
    verify = stack.parent / "verify"
    _executable(verify, "#!/bin/sh\nexit 0\n")
    if os.geteuid() == 0:
        for p in stack.parents:
            if p == Path("/") or p == Path("/tmp"):
                break
            p.chmod(0o755)
        for p in (stack, stack / "build", stack / "build" / "app", stack / "build" / "app" / "static"):
            p.chmod(0o777)
        for p in stack.rglob("*"):
            if p.is_file():
                p.chmod(0o666)
        preexec_fn = lambda: (os.setgid(65534), os.setuid(65534))
    else:
        preexec_fn = None
    result = subprocess.run(
        ["bash", str(REDEPLOY_SH)],
        env={
            **os.environ,
            "PATH": f"{bind}:{os.environ['PATH']}",
            "STACK_DIR": str(stack),
            # Never read the live AC stack: default to a path that does not exist
            # (the helper then falls back to the repo's docs/configs copies).
            "AC_STACK_DIR": str(stack.parent / "no-such-ac-stack"),
            "VERIFY_SCRIPT": str(verify),
            "ADMIN_REDEPLOY_CASE": case,
            "ADMIN_REDEPLOY_CALLS": str(calls),
            "ADMIN_REDEPLOY_STATE": str(bind / "state"),
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
        preexec_fn=preexec_fn,
    )
    return result, calls.read_text() if calls.exists() else ""


def test_failed_staging_or_build_leaves_current_container_running(tmp_path):
    for case in ("rsync-fail", "build-fail"):
        stack = _stack(tmp_path / case)
        result, calls = _run(stack, _stubs(tmp_path / case), case)
        assert result.returncode != 0
        assert " down" not in calls
        assert " up" not in calls
        assert stack.joinpath("docker-compose.yml").read_text() == "old-compose\n"


def test_unhealthy_candidate_is_automatically_rolled_back(tmp_path):
    stack = _stack(tmp_path)
    result, calls = _run(stack, _stubs(tmp_path), "unhealthy")

    assert result.returncode == 1
    assert "restoring the previous admin app" in result.stderr
    assert calls.count(" up") == 2, f"calls={calls!r}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert " down" in calls
    assert stack.joinpath("docker-compose.yml").read_text() == "old-compose\n"


def test_healthy_candidate_replaces_app_after_build(tmp_path):
    stack = _stack(tmp_path)
    result, calls = _run(stack, _stubs(tmp_path), "healthy")

    assert result.returncode == 0, result.stderr
    assert calls.count(" up") == 1, f"calls={calls!r}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert " down" not in calls
    assert "ADMIN_IMAGE" in stack.joinpath("docker-compose.yml").read_text()


def test_candidate_staging_recreates_dist_after_rsync_delete(tmp_path):
    stack = _stack(tmp_path)
    result, calls = _run(stack, _stubs(tmp_path), "rsync-delete-dist")

    assert result.returncode == 0, result.stderr
    assert calls.count(" up") == 1, f"calls={calls!r}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert " down" not in calls


def test_healthy_candidate_is_promoted_to_durable_local_tag(tmp_path):
    stack = _stack(tmp_path)
    result, calls = _run(stack, _stubs(tmp_path), "healthy")
    assert result.returncode == 0, result.stderr
    assert "image tag azerothcore-admin:redeploy-" in calls
    assert "azerothcore-admin:local" in calls
    assert "image rm azerothcore-admin:redeploy-" in calls


def test_redeploy_lock_contention_is_non_disruptive(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    lock = stack / ".redeploy.lock"
    lock.touch()
    with lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, calls = _run(stack, bind, "healthy")
    assert result.returncode == 75
    assert "another admin redeploy is already running" in result.stderr
    assert calls == ""


def test_candidate_key_index_comes_from_the_ac_source_tree(tmp_path):
    stack = _stack(tmp_path)
    ac = tmp_path / "ac"
    for rel in ("src/server/apps/worldserver/worldserver.conf.dist",
                "modules/mod-playerbots/conf/playerbots.conf.dist",
                "modules/mod-ah-bot-plus/conf/mod_ahbot.conf.dist",
                "modules/mod-individual-progression/conf/individualProgression.conf.dist"):
        (ac / rel).parent.mkdir(parents=True, exist_ok=True)
        (ac / rel).write_text(f"# from {rel}\nPlayerbots.Enabled = 1\n")
    bind = _stubs(tmp_path)
    # bind is world-writable; tmp_path is not, and the script runs as nobody under root.
    dist_log = bind / "dist.log"
    result, _ = _run(stack, bind, "healthy",
                     extra_env={"AC_STACK_DIR": str(ac), "ADMIN_REDEPLOY_DIST_LOG": str(dist_log)})
    assert result.returncode == 0, result.stderr
    assert dist_log.read_text().startswith("# from modules/mod-playerbots/conf/playerbots.conf.dist")


def _own_data_dir(stack: Path) -> Path:
    """Create stack/data owned by the uid:gid that will actually run the script."""
    data = stack / "data"
    data.mkdir()
    if os.geteuid() == 0:
        # _run drops to nobody (65534:65534) when the suite runs as root.
        os.chown(data, 65534, 65534)
    return data


def test_redeploy_skips_sudo_when_data_dir_is_already_owned_by_caller(tmp_path):
    stack = _stack(tmp_path)
    data = _own_data_dir(stack)
    bind = _stubs(tmp_path)
    sudo_log = bind / "sudo.log"
    result, calls = _run(stack, bind, "healthy", extra_env={"ADMIN_REDEPLOY_SUDO_LOG": str(sudo_log)})

    assert result.returncode == 0, result.stderr
    assert calls.count(" up") == 1, f"calls={calls!r}\nstdout={result.stdout}\nstderr={result.stderr}"
    assert data.is_dir()
    assert not sudo_log.exists() or sudo_log.read_text() == "", sudo_log.read_text()


def test_redeploy_uses_sudo_to_create_missing_data_dir(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    sudo_log = bind / "sudo.log"
    assert not (stack / "data").exists()
    result, calls = _run(stack, bind, "healthy", extra_env={"ADMIN_REDEPLOY_SUDO_LOG": str(sudo_log)})

    assert result.returncode == 0, result.stderr
    assert calls.count(" up") == 1, f"calls={calls!r}\nstdout={result.stdout}\nstderr={result.stderr}"
    uid_gid = f"{65534}:{65534}" if os.geteuid() == 0 else f"{os.getuid()}:{os.getgid()}"
    assert sudo_log.read_text().splitlines() == [
        f"mkdir -p {stack}/data",
        f"chown {uid_gid} {stack}/data",
    ]
    assert (stack / "data").is_dir()


def test_missing_client_data_volume_stops_before_anything_is_staged(tmp_path):
    stack = _stack(tmp_path)
    result, calls = _run(stack, _stubs(tmp_path), "no-client-data")

    assert result.returncode == 1
    assert (
        "client-data volume azerothcore_ac-client-data not found — install AzerothCore first "
        "(Phase 4 creates it)"
    ) in result.stderr
    assert calls == "volume inspect azerothcore_ac-client-data\n"
    assert not [p for p in stack.glob(".redeploy.*") if p.is_dir()]  # no stage dir (the lock file stays)
    assert stack.joinpath("docker-compose.yml").read_text() == "old-compose\n"


JQUERY_URL = "https://unpkg.com/jquery@3.7.1/dist/jquery.min.js"


def _without_jquery(stack: Path, placeholder: bool) -> Path:
    """An install from before the Armory: the HTMX files, no (or only a placeholder) jquery.min.js."""
    kept = stack / "build" / "app" / "static" / "jquery.min.js"
    if placeholder:
        kept.write_text("// placeholder; real jQuery 3.7.1 vendored at install time\n")
    else:
        kept.unlink()
    return kept


def test_redeploy_backfills_jquery_once_from_the_pinned_url(tmp_path):
    for placeholder in (False, True):
        root = tmp_path / ("placeholder" if placeholder else "missing")
        root.mkdir()
        stack = _stack(root)
        kept = _without_jquery(stack, placeholder)
        bind = _stubs(root)
        curl_log = bind / "curl.log"
        assert shutil.which("curl", path=f"{bind}:{os.environ['PATH']}") == str(bind / "curl")

        result, calls = _run(stack, bind, "healthy", extra_env={"ADMIN_REDEPLOY_CURL_LOG": str(curl_log)})

        assert result.returncode == 0, result.stderr
        fetches = curl_log.read_text().splitlines()
        assert len(fetches) == 1 and fetches[0].endswith(" " + JQUERY_URL)
        # Bounded while redeploy holds .redeploy.lock: a stalled unpkg cannot keep it.
        assert fetches[0].startswith("curl -sSfL --connect-timeout 15 --max-time 120 -o "), fetches[0]
        assert f"-o {stack}/build/app/static/jquery.min.js " not in fetches[0]  # a temp file, moved into place
        assert calls.count(" up") == 1, f"calls={calls!r}\nstderr={result.stderr}"
        # A copy is kept for the next redeploys, readable like the installer's curl output.
        assert kept.read_bytes() == b"j" * 300
        assert stat.S_IMODE(kept.stat().st_mode) == 0o644
        assert not list(kept.parent.glob(".jquery.min.js.*"))

        # The next redeploy carries the kept copy: no second fetch.
        (root / "again").mkdir()
        again_bind = _stubs(root / "again")
        again_log = again_bind / "curl.log"
        again, _ = _run(stack, again_bind, "healthy", extra_env={"ADMIN_REDEPLOY_CURL_LOG": str(again_log)})
        assert again.returncode == 0, again.stderr
        assert not again_log.exists()


def test_failed_jquery_fetch_aborts_before_any_build(tmp_path):
    stack = _stack(tmp_path)
    kept = _without_jquery(stack, placeholder=False)
    bind = _stubs(tmp_path)
    result, calls = _run(
        stack, bind, "healthy",
        extra_env={"ADMIN_REDEPLOY_CURL": "fail", "ADMIN_REDEPLOY_CURL_LOG": str(bind / "curl.log")},
    )

    assert result.returncode == 1
    assert len((bind / "curl.log").read_text().splitlines()) == 1  # fetched once, never retried
    assert "WARNING: could not fetch jquery.min.js." in result.stderr
    assert "ERROR: jquery.min.js is a placeholder (0 bytes)" in result.stderr
    assert f'curl -sSfL -o "{stack}/build/app/static/jquery.min.js" "{JQUERY_URL}"' in result.stderr
    assert " build" not in calls and " up" not in calls and " down" not in calls
    assert stack.joinpath("docker-compose.yml").read_text() == "old-compose\n"
    assert not kept.exists()


def test_redeploy_never_fetches_htmx(tmp_path):
    stack = _stack(tmp_path)
    (stack / "build" / "app" / "static" / "htmx.min.js").unlink()
    bind = _stubs(tmp_path)
    curl_log = bind / "curl.log"
    result, calls = _run(stack, bind, "healthy", extra_env={"ADMIN_REDEPLOY_CURL_LOG": str(curl_log)})

    assert result.returncode == 1
    assert not curl_log.exists()
    assert "ERROR: htmx.min.js is a placeholder (0 bytes)" in result.stderr
    assert '"https://unpkg.com/htmx.org@2.0.3/dist/htmx.min.js"' in result.stderr
    assert " build" not in calls and " up" not in calls
