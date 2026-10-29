"""Armory deployment (spec 10): the client-data mount, the deploy scripts, verifier step 8c.

Every script run here is a copy (or the original reading only tmp_path paths) with a
stub-only ``docker`` first on PATH (R6): no test reaches a Docker daemon or /opt/stacks.
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import yaml

from app.services.dbc import DBC_FILES

ADMIN_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ADMIN_ROOT / "docker-compose.yml"
DOCKERFILE = ADMIN_ROOT / "Dockerfile"
INSTALLER = ADMIN_ROOT / "scripts/install-azerothcore-admin.sh"
REDEPLOY = ADMIN_ROOT / "scripts/redeploy-azerothcore-admin.sh"
VERIFIER = ADMIN_ROOT / "scripts/verify-azerothcore-admin.sh"
UNINSTALLER = ADMIN_ROOT / "scripts/uninstall-azerothcore-admin.sh"
STATIC = ADMIN_ROOT / "app/static"

CLIENT_DATA_VOLUME = "azerothcore_ac-client-data"
JQUERY_URL = "https://unpkg.com/jquery@3.7.1/dist/jquery.min.js"
VENDOR_FILES = ("htmx.min.js", "htmx-sse.js", "jquery.min.js")


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _logical_lines(script: Path) -> list[str]:
    """The script's commands with backslash continuations joined, comment lines dropped."""
    joined = script.read_text().replace("\\\n", " ")
    return [line for line in joined.splitlines() if not line.lstrip().startswith("#")]


def _installer_urls() -> set[str]:
    """The URLs the installer fetches, with its ``*_VERSION`` variables filled in."""
    text = " ".join(_logical_lines(INSTALLER))
    versions = dict(re.findall(r"^([A-Z_]+_VERSION)=(\S+)$", INSTALLER.read_text(), re.M))
    urls = set()
    for url in re.findall(r'"(https://unpkg\.com/[^"]+)"', text):
        for name, value in versions.items():
            url = url.replace("${%s}" % name, value)
        assert "$" not in url, url
        urls.add(url)
    return urls


# --- docker-compose.yml ---


def _service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["azerothcore-admin"]


def _mounts(service: dict) -> list[tuple[str, str, bool]]:
    """(source, target, read_only) for every service volume, short or long syntax."""
    out = []
    for entry in service["volumes"]:
        if isinstance(entry, str):
            parts = entry.split(":")
            out.append((parts[0], parts[1], len(parts) > 2 and parts[2] == "ro"))
        else:
            out.append((entry["source"], entry["target"], bool(entry.get("read_only"))))
    return out


def test_compose_mounts_client_data_volume_read_only_and_external():
    doc = yaml.safe_load(COMPOSE.read_text())
    service = doc["services"]["azerothcore-admin"]

    entries = [e for e in service["volumes"] if isinstance(e, str) and e.split(":")[1:2] == ["/client-data"]]
    assert entries == ["client-data:/client-data:ro"]
    assert doc["volumes"]["client-data"] == {"external": True, "name": CLIENT_DATA_VOLUME}
    assert service["environment"]["CLIENT_DATA_DIR"] == "/client-data"
    # The AC project's volume name is the reason for the comment next to it.
    assert "Phase 4" in COMPOSE.read_text() and CLIENT_DATA_VOLUME in COMPOSE.read_text()

    # No new mount under /ac: exactly the three that existed before the armory.
    under_ac = sorted((src, dst, ro) for src, dst, ro in _mounts(service) if dst == "/ac" or dst.startswith("/ac/"))
    assert under_ac == [
        ("/opt/stacks/azerothcore", "/ac", True),
        ("/opt/stacks/azerothcore/backups", "/ac/backups", False),
        ("/opt/stacks/azerothcore/docker-compose.admin.yml", "/ac/docker-compose.admin.yml", False),
    ]


# --- install / redeploy ---


def test_install_and_redeploy_check_client_data_volume_before_compose_up():
    for script in (INSTALLER, REDEPLOY):
        lines = _logical_lines(script)
        checks = [i for i, line in enumerate(lines) if f"docker volume inspect {CLIENT_DATA_VOLUME}" in line]
        assert len(checks) == 1, script.name
        (check,) = checks
        compose_runs = [
            i for i, line in enumerate(lines)
            if re.search(r"docker compose\b.*\s(up|build)\b", line)
        ]
        assert any(" up" in lines[i] for i in compose_runs), script.name
        # Before the build too: a missing volume should not cost a full image build first.
        assert all(check < i for i in compose_runs), (script.name, lines[check], [lines[i] for i in compose_runs])

        text = script.read_text()
        assert f"client-data volume {CLIENT_DATA_VOLUME} not found — install AzerothCore first (Phase 4 creates it)" in text
        block = text[text.index(f"docker volume inspect {CLIENT_DATA_VOLUME}"):]
        assert "exit 1" in block[: block.index("fi\n")], script.name  # a failure stops the script


def test_redeploy_checks_the_volume_only_after_taking_the_lock():
    # A second redeploy that loses the lock must not touch docker at all (root suite asserts calls == "").
    text = REDEPLOY.read_text()
    assert text.index('flock -n 9') < text.index(f"docker volume inspect {CLIENT_DATA_VOLUME}")


def test_jquery_is_a_vendor_file_like_htmx():
    placeholder = STATIC / "jquery.min.js"
    assert placeholder.is_file()
    assert placeholder.stat().st_size <= 200
    assert "placeholder" in placeholder.read_text()

    install = INSTALLER.read_text()
    fetch = '-o "$STACK_DIR/build/app/static/jquery.min.js"'
    assert fetch in install
    assert JQUERY_URL in _installer_urls()
    # Fetched after the source sync, which would otherwise put the placeholder back.
    assert install.index("rsync -a --delete") < install.index(fetch)

    redeploy = REDEPLOY.read_text()
    loops = re.findall(r"^for vendor in ([^;]+); do$", redeploy, re.M)
    assert len(loops) == 2  # carry the installed copies over, then the size guard
    for loop in loops:
        assert loop.split() == list(VENDOR_FILES)
    assert "-le 200" in redeploy


def test_every_rsync_delete_into_build_excludes_every_vendor_file():
    found = 0
    for script in (INSTALLER, REDEPLOY):
        for line in _logical_lines(script):
            if "rsync" in line and "--delete" in line:
                found += 1
                for name in VENDOR_FILES:
                    assert f"--exclude='app/static/{name}'" in line, (script.name, name)
    assert found == 2


def test_redeploy_names_the_fetch_for_a_placeholder_vendor_file():
    # The first redeploy after this change on an older install has no jquery.min.js yet:
    # the guard's message must say how to get it, with the installer's own URL.
    redeploy_urls = set(re.findall(r"https://unpkg\.com/\S+?(?=[\"'\s)])", REDEPLOY.read_text()))
    assert JQUERY_URL in redeploy_urls
    assert redeploy_urls == _installer_urls()
    assert len(redeploy_urls) == len(VENDOR_FILES)


def test_dockerfile_unchanged_by_armory():
    text = DOCKERFILE.read_text().lower()
    # jQuery reaches the image through app/static like HTMX (vendored into the build
    # context by the installer); the DBC files through the compose mount.
    for word in ("jquery", "armory", "client-data", "client_data", "wowhead", "zamimg"):
        assert word not in text, word


# --- verifier step 8c ---


def _verifier_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The verifier copied into a disposable repo layout whose AC delegate is a stub."""
    repo = tmp_path / "repo"
    admin_scripts = repo / "wow-server-sp-admin" / "scripts"
    admin_scripts.mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copy2(VERIFIER, admin_scripts / VERIFIER.name)
    _executable(repo / "scripts" / "verify-azerothcore.sh", "#!/bin/sh\nexit 0\n")

    admin_stack = tmp_path / "admin-stack"
    admin_stack.mkdir()
    admin_stack.joinpath(".env").write_text("TAILSCALE_IP=100.64.0.5\nADMIN_PORT=8000\n")
    ac_stack = tmp_path / "ac-stack"
    ac_stack.mkdir()
    ac_stack.joinpath(".env").write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.admin.yml\n")
    ac_stack.joinpath("docker-compose.admin.yml").write_text("services: {}\n")
    return admin_scripts / VERIFIER.name, admin_stack, ac_stack


def _stubs(tmp_path: Path) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir()
    # The docker stub answers the admin container's state and the read-only `test -r`
    # probe; every call is logged. FAKE_DBC_MISSING lists the files `test -r` denies.
    _executable(bind / "docker", """#!/bin/bash
printf 'docker %s\\n' "$*" >> "$VERIFY_CALL_LOG"
case "$1" in
  inspect)
    case "$*" in
      *Health*) printf '%s\\n' "${FAKE_ADMIN_HEALTH:-healthy}" ;;
      *) printf '%s\\n' "${FAKE_ADMIN_STATUS:-running}" ;;
    esac ;;
  exec)
    if [ "$3 $4" = "test -r" ]; then
      for name in ${FAKE_DBC_MISSING:-}; do
        [ "$5" = "/client-data/dbc/$name" ] && exit 1
      done
    fi
    exit 0 ;;
esac
""")
    _executable(bind / "ss", "#!/bin/sh\nprintf 'LISTEN 0 0 100.64.0.5:8000 0.0.0.0:*\\n'\n")
    _executable(bind / "curl", "#!/bin/sh\nexit 0\n")
    _executable(bind / "id", "#!/bin/sh\n[ \"$1\" = -un ] && echo verifier || echo verifygroup\n")
    _executable(bind / "stat", "#!/bin/sh\necho verifier:verifygroup\n")
    _executable(bind / "systemctl", "#!/bin/sh\nexit 0\n")
    return bind


def _verify(tmp_path: Path, admin_stack: Path, script: Path, bind: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    path = f"{bind}:{os.environ['PATH']}"
    # R6, fail closed: `docker` must resolve to the stub, never to a real client.
    assert shutil.which("docker", path=path) == str(bind / "docker")
    return subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": path,
            "STACK_DIR": str(admin_stack),
            "AC_STACK_DIR": str(tmp_path / "ac-stack"),
            "VERIFY_CALL_LOG": str(tmp_path / "calls.log"),
            **extra,
        },
        capture_output=True,
        text=True,
    )


def _armory_lines(stdout: str) -> list[str]:
    """Every verifier line about the armory, whatever its tag (tmp paths contain "armory" too)."""
    return [line for line in stdout.splitlines() if re.match(r"\[(INFO|OK|FAIL)\]\s+armory\b", line)]


def _summary(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith("Summary:"))


def _armory_dir(admin_stack: Path) -> Path:
    return admin_stack / "data" / "armory"


def _write_viewer(admin_stack: Path, meta: object | None, *, script: bytes = b"/* viewer */", raw: bool = False) -> None:
    viewer = _armory_dir(admin_stack) / "cache" / "live" / "viewer"
    viewer.mkdir(parents=True, exist_ok=True)
    viewer.joinpath("viewer.min.js").write_bytes(script)
    if meta is not None:
        viewer.joinpath("viewer.meta.json").write_text(meta if raw else json.dumps(meta))


def test_verifier_armory_info_line_shapes(tmp_path):
    script, admin_stack, _ = _verifier_layout(tmp_path)
    bind = _stubs(tmp_path)

    # Fresh install: nothing under data/armory yet; every DBC readable.
    fresh = _verify(tmp_path, admin_stack, script, bind)
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert _armory_lines(fresh.stdout) == ["[INFO] armory: dbc ok, cache 0 MB, viewer not fetched, 3d on"]

    # Switched off, viewer pinned, two DBC files unreadable (named in DBC_FILES order).
    _armory_dir(admin_stack).mkdir(parents=True)
    _armory_dir(admin_stack).joinpath("armory.json").write_text(json.dumps({"enabled": False}))
    stamp = "2026-10-29T12:34:56+00:00"
    _write_viewer(admin_stack, {"etag": '"abc"', "fetched_at": stamp, "sha256": "0" * 64, "size": 12})
    off = _verify(tmp_path, admin_stack, script, bind, FAKE_DBC_MISSING="RandPropPoints.dbc Spell.dbc")
    assert off.returncode == 0, off.stdout + off.stderr
    (line,) = _armory_lines(off.stdout)
    assert re.fullmatch(
        rf"\[INFO\] armory: dbc missing: Spell\.dbc,RandPropPoints\.dbc, cache [0-9]+ MB, "
        rf"viewer fetched {re.escape(stamp)}, 3d off",
        line,
    ), line

    # Back on; the cache size is du -sm of data/armory/cache.
    _armory_dir(admin_stack).joinpath("armory.json").write_text(json.dumps({"enabled": True}))
    model = _armory_dir(admin_stack) / "cache" / "wrath" / "m2" / "1.m2"
    model.parent.mkdir(parents=True)
    model.write_bytes(os.urandom(3 * 1024 * 1024))
    on = _verify(tmp_path, admin_stack, script, bind)
    (line,) = _armory_lines(on.stdout)
    size = int(re.fullmatch(r"\[INFO\] armory: dbc ok, cache ([0-9]+) MB, viewer fetched .+, 3d on", line).group(1))
    assert 3 <= size <= 5, line

    # The probe is exactly one read-only `test -r` per DBC_FILES name, in the admin container.
    probes = [c for c in (tmp_path / "calls.log").read_text().splitlines() if "/client-data/" in c]
    assert probes[-len(DBC_FILES):] == [
        f"docker exec azerothcore-admin test -r /client-data/dbc/{name}" for name in DBC_FILES
    ]


def test_verifier_armory_line_is_info_only(tmp_path):
    script, admin_stack, _ = _verifier_layout(tmp_path)
    bind = _stubs(tmp_path)
    clean = _verify(tmp_path, admin_stack, script, bind)

    every_dbc = " ".join(DBC_FILES)
    _armory_dir(admin_stack).mkdir(parents=True)
    _armory_dir(admin_stack).joinpath("armory.json").write_text(json.dumps({"enabled": False}))
    degraded = _verify(tmp_path, admin_stack, script, bind, FAKE_DBC_MISSING=every_dbc)

    assert degraded.returncode == clean.returncode == 0, degraded.stdout + degraded.stderr
    assert _summary(degraded.stdout) == _summary(clean.stdout)
    (line,) = _armory_lines(degraded.stdout)
    assert line.startswith("[INFO] armory: dbc missing: " + ",".join(DBC_FILES) + ", ")
    assert "[FAIL]" not in degraded.stdout


def test_verifier_armory_line_does_not_probe_a_stopped_container(tmp_path):
    script, admin_stack, _ = _verifier_layout(tmp_path)
    bind = _stubs(tmp_path)

    result = _verify(tmp_path, admin_stack, script, bind, FAKE_ADMIN_STATUS="exited")

    assert _armory_lines(result.stdout) == [
        "[INFO] armory: dbc unknown (admin container not running), cache 0 MB, viewer not fetched, 3d on"
    ]
    assert "/client-data/" not in (tmp_path / "calls.log").read_text()


def test_verifier_armory_line_uses_fixed_shapes_only(tmp_path):
    script, admin_stack, _ = _verifier_layout(tmp_path)
    bind = _stubs(tmp_path)
    _armory_dir(admin_stack).mkdir(parents=True)

    # The app's rule: an unreadable or odd armory.json means On.
    for raw in ("{not json", "[1, 2]", json.dumps({"enabled": "off"}), json.dumps({"enabled": 0})):
        _armory_dir(admin_stack).joinpath("armory.json").write_text(raw)
        result = _verify(tmp_path, admin_stack, script, bind)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _armory_lines(result.stdout)[0].endswith(", 3d on"), raw

    # Free text in viewer.meta.json never reaches the output; a pinned script without a
    # usable date is "fetched (date unknown)", like the footer strip.
    for meta, raw in (
        ({"fetched_at": "2026-10-29T12:00:00+00:00\n[FAIL] injected"}, False),
        ({"fetched_at": "x" * 500}, False),
        ({"fetched_at": "2026-13-45T99:99:99+00:00"}, False),
        ({"fetched_at": 12345}, False),
        ("{not json", True),
        ("[]", True),
    ):
        _write_viewer(admin_stack, meta, raw=raw)
        result = _verify(tmp_path, admin_stack, script, bind)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "injected" not in result.stdout and "xxxx" not in result.stdout
        (line,) = _armory_lines(result.stdout)
        assert ", viewer fetched (date unknown), " in line, (meta, line)

    # The script itself decides "fetched": a meta file alone, or an empty script, is not.
    viewer = _armory_dir(admin_stack) / "cache" / "live" / "viewer"
    viewer.joinpath("viewer.meta.json").write_text(json.dumps({"fetched_at": "2026-10-29T12:00:00+00:00"}))
    viewer.joinpath("viewer.min.js").unlink()
    gone = _verify(tmp_path, admin_stack, script, bind)
    assert ", viewer not fetched, " in _armory_lines(gone.stdout)[0]
    viewer.joinpath("viewer.min.js").write_bytes(b"")
    empty = _verify(tmp_path, admin_stack, script, bind)
    assert ", viewer not fetched, " in _armory_lines(empty.stdout)[0]


def test_verifier_armory_line_survives_a_failing_python3(tmp_path):
    script, admin_stack, _ = _verifier_layout(tmp_path)
    bind = _stubs(tmp_path)
    _executable(bind / "python3", "#!/bin/sh\nexit 1\n")

    result = _verify(tmp_path, admin_stack, script, bind)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _armory_lines(result.stdout) == [
        "[INFO] armory: dbc ok, cache 0 MB, viewer unknown, 3d unknown"
    ]


def test_verifier_dbc_list_is_dbc_files():
    text = VERIFIER.read_text()
    match = re.search(r"^armory_dbc_files=\(([^)]*)\)$", text, re.M)
    assert match, "armory_dbc_files=( … ) not found"
    assert tuple(match.group(1).split()) == DBC_FILES


def test_verifier_armory_step_runs_after_cloud_and_before_the_ac_delegate():
    text = VERIFIER.read_text()
    assert text.index("# 8b.") < text.index("# 8c.") < text.index("# 9. AC stack still functional")
    assert "$STACK_DIR/data/armory" in text


# --- uninstaller ---


ARMORY_UNINSTALL_NOTE = (
    "Armory: {}/data/armory/ holds only re-fetchable cache (Wowhead model data, the viewer "
    "script, the display-ID map) and the 3D viewer on/off switch; nothing else to do."
)


def _sandboxed_uninstaller(tmp_path: Path) -> tuple[Path, Path, Path]:
    ac_stack = tmp_path / "ac-stack"
    admin_stack = tmp_path / "admin-stack"
    ac_stack.mkdir()
    (admin_stack / "data" / "armory" / "cache").mkdir(parents=True)
    (ac_stack / ".env").write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.admin.yml\n")
    source = UNINSTALLER.read_text()
    for old, new in (
        ("STACK_DIR=/opt/stacks/azerothcore-admin", f"STACK_DIR={shlex.quote(str(admin_stack))}"),
        ("AC_STACK_DIR=/opt/stacks/azerothcore", f"AC_STACK_DIR={shlex.quote(str(ac_stack))}"),
        ("SYSTEMD_UNIT=/etc/systemd/system/$SYSTEMD_SERVICE", f"SYSTEMD_UNIT={shlex.quote(str(tmp_path / 'unit'))}"),
    ):
        assert old in source, old
        source = source.replace(old, new, 1)
    assert "/opt/stacks" not in source and "/etc/systemd" not in source
    script = tmp_path / "uninstall-admin.sh"
    _executable(script, source)
    return script, ac_stack, admin_stack


def test_uninstaller_says_armory_data_is_only_cache(tmp_path):
    for mode in ("--dry-run", "--yes"):
        root = tmp_path / mode.strip("-")
        root.mkdir()
        script, _ac_stack, admin_stack = _sandboxed_uninstaller(root)
        stubs = root / "stubs"
        stubs.mkdir()
        _executable(stubs / "docker", "#!/bin/sh\nexit 0\n")
        if mode == "--dry-run":
            _executable(stubs / "sudo", "#!/bin/sh\necho unexpected-sudo >&2\nexit 99\n")
        else:
            _executable(
                stubs / "sudo",
                "#!/bin/sh\ncase \"$1\" in rm|mktemp|awk|tee|chown|chmod|mv) exec \"$@\" ;; esac\n"
                "echo \"unexpected-sudo $*\" >&2\nexit 99\n",
            )
        assert shutil.which("docker", path=f"{stubs}:{os.environ['PATH']}") == str(stubs / "docker")

        result = subprocess.run(
            [str(script), mode],
            text=True,
            capture_output=True,
            env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"},
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "unexpected-sudo" not in result.stderr
        lines = result.stdout.splitlines()
        note = ARMORY_UNINSTALL_NOTE.format(admin_stack)
        assert note in lines, (mode, result.stdout)
        # The final two lines stay the summary and the Proton reminder.
        assert lines.index(note) < lines.index("Uninstall complete.") == len(lines) - 2
        assert admin_stack.exists() == (mode == "--dry-run")
