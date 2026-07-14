import os
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
    for name in ("htmx.min.js", "htmx-sse.js"):
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
        if [ "$arg" = up ]; then
            if [ ! -f "$ADMIN_REDEPLOY_STATE" ]; then echo candidate > "$ADMIN_REDEPLOY_STATE"; else echo rollback > "$ADMIN_REDEPLOY_STATE"; fi
        fi
    done
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
    _executable(bind / "sudo", "#!/bin/sh\nexec \"$@\"\n")
    _executable(bind / "sleep", "#!/bin/sh\nexit 0\n")
    return bind


def _run(stack: Path, bind: Path, case: str) -> tuple[subprocess.CompletedProcess[str], str]:
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
            "VERIFY_SCRIPT": str(verify),
            "ADMIN_REDEPLOY_CASE": case,
            "ADMIN_REDEPLOY_CALLS": str(calls),
            "ADMIN_REDEPLOY_STATE": str(bind / "state"),
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
