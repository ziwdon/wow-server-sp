# PR #25 Boundary-Focused Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve every confirmed Critical, Important, and actionable Minor defect still present in PR #25 behavior while preserving all valid post-PR #25 and PR #26 corrections.

**Architecture:** Correct behavior at existing lifecycle boundaries: validate backups during creation/import/restore, serialize database mutation with the shared backup lock, keep read-only paths metadata-only, and make cancellation wait for underlying threads before cleanup or lock release. Shell and admin corrections remain separate, reviewable commits; no new worker, cache, dependency, authentication layer, or broad refactor is introduced.

**Tech Stack:** Bash, Python 3.12, Python standard-library `tarfile`/`fcntl`/`asyncio`, FastAPI 0.115.0, mysql-connector-python 9.1.0, pytest, Node.js syntax checks, Playwright 1.61.1/Chromium/axe.

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-07-14-pr25-boundary-corrections-design.md`.
- Audit baseline/result/current: `8c5c0cb877a4ccf0183cd089dbb71e0ee0b02f6d`, `0931a5f0ea2ba773ad54b84bc1929ce3c0960d00`, `eedc7c2ae968f785eb8b900c0fe52042f7be8b29`.
- Work from `audit/pr25-behavioral-audit`; never reset legitimate later history or discard unrelated work.
- Treat `/opt/stacks/azerothcore`, `/opt/stacks/azerothcore-admin`, the host Docker daemon/socket, systemd, crontab, and AC/admin containers/networks/volumes as live production.
- Never run lifecycle/destructive tests on the host. Disposable test containers must have no Docker socket and no host `/opt` mount.
- After every lifecycle-related test command, immediately run the read-only host `docker ps` health check; if an expected container is down, stop and report without recovery action.
- Use `superpowers:systematic-debugging` before each fix, `superpowers:test-driven-development` for every RED→GREEN cycle, and `superpowers:verification-before-completion` before every commit or completion claim.
- Implement serially. No concurrent implementation agents may edit the shared working tree.
- Backups GET paths and general verification must never open/decompress archive contents.
- Preserve e16f877, b3e6cd9, and every PR #26 correction.
- Do not change production dependencies, add authentication/CSRF architecture, or address pre-PR #25 adjacent risks in this plan.

## Cold-session bootstrap

- [ ] **Step 1: Re-establish mandatory context and persistent goal**

Read completely, in this order:

```text
AGENTS.md
.codex/continuity.md
CLAUDE.md
README.md
wow-server-sp-admin/README.md
docs/superpowers/specs/2026-07-14-pr25-boundary-corrections-design.md
docs/superpowers/plans/2026-07-14-pr25-boundary-corrections.md
.codex/pr25-audit-ledger.md
```

Invoke `superpowers:using-superpowers`, `superpowers:executing-plans`, `superpowers:systematic-debugging`, `superpowers:test-driven-development`, and keep `superpowers:verification-before-completion` available. Call `get_goal`; continue the existing whole-audit objective if present. If no goal exists or it is complete, create the complete whole-audit objective without `token_budget`.

- [ ] **Step 2: Prove repository provenance before edits**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git fetch origin --prune
git rev-list --left-right --count HEAD...origin/main
git merge-base --is-ancestor eedc7c2ae968f785eb8b900c0fe52042f7be8b29 HEAD
git log --oneline --decorate -8
```

Expected: branch `audit/pr25-behavioral-audit`; only intentional planning commits beyond current main; no unrelated work; current history contains `eedc7c2`. Do not reset if origin legitimately advanced—record and re-audit overlap first.

- [ ] **Step 3: Establish exact disposable test helpers in the cold shell**

Define these shell functions once. They pass every pytest selector as a real argv element and always perform the mandatory live-state check after scripts tests:

```bash
scripts_pytest() {
  docker run --rm \
    -v "$(pwd):/repo:ro" -w /repo \
    python:3.12-slim bash -ec '
      test ! -S /var/run/docker.sock
      test ! -e /opt/stacks/azerothcore
      test ! -e /opt/stacks/azerothcore-admin
      pip install pytest -q
      python -m pytest -p no:cacheprovider "$@"
    ' bash "$@"
  local status=$?
  docker ps --format '{{.Names}} {{.Status}}'
  return "$status"
}

admin_pytest() {
  docker run --rm \
    -v "$(pwd)/wow-server-sp-admin:/src:ro" -w /src \
    python:3.12-slim bash -ec '
      test ! -S /var/run/docker.sock
      test ! -e /opt/stacks/azerothcore
      test ! -e /opt/stacks/azerothcore-admin
      pip install -r requirements-dev.txt -q
      python -m pytest -p no:cacheprovider "$@"
    ' bash "$@"
}

browser_test() {
  docker run --rm --init --ipc=host \
    -v "$(pwd)/wow-server-sp-admin:/work" -w /work \
    mcr.microsoft.com/playwright:v1.61.1-noble \
    bash -ec '
      test ! -S /var/run/docker.sock
      test ! -e /opt/stacks/azerothcore
      test ! -e /opt/stacks/azerothcore-admin
      npm ci --silent
      npx playwright test "$@"
    ' bash "$@"
}
```

Expected health output includes `ac-authserver`, `ac-worldserver`, `ac-database`, and `azerothcore-admin` up; DB/admin retain their healthy status.

---

### Task 1: Enforce the backup publisher's canonical v2 contract (F01)

**Files:**
- Modify: `scripts/backup.sh:75-145`
- Modify/Test: `scripts/tests/test_backup_sh.py:1-360`

**Interfaces:**
- Consumes: staged `sql/azerothcore.sql`, canonical database tuple `(acore_auth, acore_characters, acore_world, acore_playerbots)`.
- Produces: `validate_v2_dump PATH` returning 0 only for one ordered canonical stream with a terminal completion footer.

- [ ] **Step 1: Make successful test dumps canonical and add the malformed-success regression**

Add this helper near `DBS` and make the docker stub print it unless `MALFORMED_SUCCESS=1`:

```python
def _v2_dump() -> str:
    sections = "\n".join(
        f"-- Current Database: `{db}`\nCREATE DATABASE `{db}`;\nUSE `{db}`;"
        for db in DBS
    )
    return f"-- MySQL dump 10.13\n{sections}\n-- Dump completed on 2026-07-14 12:00:00\n"
```

Pass the canonical text to the stub through `CANONICAL_DUMP`; its mysqldump branch becomes:

```bash
if [ "${MALFORMED_SUCCESS:-0}" = 1 ]; then
  printf '%s\n' '-- exit-zero malformed dump --'
else
  printf '%s' "$CANONICAL_DUMP"
fi
exit 0
```

Add:

```python
def test_exit_zero_malformed_dump_is_not_published_or_reported_complete(tmp_path):
    stack = _stack(tmp_path)
    bind = _stubs(tmp_path)
    backups = stack / "backups"
    backups.mkdir()
    old = _old_archive(backups)

    result = _run(
        stack,
        bind,
        "--label",
        "manual",
        extra_env={"MALFORMED_SUCCESS": "1", "CANONICAL_DUMP": _v2_dump()},
    )

    assert result.returncode == 1
    assert "SQL stream failed canonical validation" in result.stderr
    assert "Backup complete." not in result.stdout
    assert old.read_bytes() == b"recoverable-old-archive"
    assert not list(backups.glob("azerothcore-backup-manual-*.tar.gz"))
    assert not list(backups.glob(".*.tmp.*"))
```

Ensure `_run` supplies `CANONICAL_DUMP=_v2_dump()` by default.

- [ ] **Step 2: Run RED**

Run:

```bash
scripts_pytest scripts/tests/test_backup_sh.py::test_exit_zero_malformed_dump_is_not_published_or_reported_complete -q
```

Expected: FAIL because current `backup.sh` publishes the malformed exit-zero output and reports completion. Inspect the assertion, then run the mandatory read-only health check.

- [ ] **Step 3: Add bounded staged-stream validation before tar creation**

Insert this function before the dump invocation and call it immediately after mysqldump succeeds:

```bash
validate_v2_dump() {
    python3 - "$1" <<'PY'
import re
import sys

DATABASES = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
HEADER_LIMIT = 4096
TAIL_LIMIT = 8192
path = sys.argv[1]
found = []
prefix = b"-- Current Database: `"

with open(path, "rb") as stream:
    at_line_start = True
    while chunk := stream.readline(HEADER_LIMIT + 1):
        if at_line_start and chunk.startswith(b"-- Current Database:"):
            if len(chunk) > HEADER_LIMIT or not chunk.endswith(b"\n"):
                raise SystemExit("oversized database section header")
            line = chunk.rstrip(b"\r\n")
            if not (line.startswith(prefix) and line.endswith(b"`") and line.count(b"`") == 2):
                raise SystemExit("malformed database section header")
            try:
                found.append(line.split(b"`", 2)[1].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise SystemExit("unreadable database section header") from exc
        at_line_start = chunk.endswith(b"\n")

    if tuple(found) != DATABASES:
        raise SystemExit("database sections are not exactly canonical and ordered")
    size = stream.tell()
    if size <= 0:
        raise SystemExit("empty SQL stream")
    stream.seek(max(0, size - TAIL_LIMIT))
    tail = stream.read(TAIL_LIMIT)
    if re.search(rb"(?:^|\n)-- Dump completed on [^\r\n]+\s*\Z", tail) is None:
        raise SystemExit("missing terminal mysqldump completion footer")
PY
}

if ! validation_detail="$(validate_v2_dump "${STAGE}/sql/azerothcore.sql" 2>&1)"; then
    log "ERROR: SQL stream failed canonical validation: ${validation_detail}" >&2
    exit 1
fi
```

Do not move archive validation to a page, verifier, or later restore boundary.

- [ ] **Step 4: Run GREEN and relevant backup suite**

Run:

```bash
scripts_pytest scripts/tests/test_backup_sh.py::test_exit_zero_malformed_dump_is_not_published_or_reported_complete -q
scripts_pytest scripts/tests/test_backup_sh.py -q
```

Expected: all pass; read-only live health output remains healthy after each command.

- [ ] **Step 5: Verify and commit**

Run `bash -n scripts/backup.sh`, `git diff --check`, inspect the diff, then:

```bash
git add scripts/backup.sh scripts/tests/test_backup_sh.py
git commit -m "fix: validate backups before publication"
```

---

### Task 2: Make host restore extraction bounded, lock-safe, and leak-free (F02, F04, host half of F08)

**Files:**
- Modify: `scripts/restore-azerothcore.sh:1-440`
- Modify/Test: `scripts/tests/test_restore_sh.py:1-700`

**Interfaces:**
- Consumes: backup archive and `${STACK_DIR}/backups/.backup.lock`.
- Produces: `safe_extract_archive ARCHIVE STAGE`, exclusive fd 8 held through restore readiness, and no temporary path outside `STAGE`.

- [ ] **Step 1: Add real-filesystem regressions**

Add a tar symlink helper and these tests:

```python
def _make_link_archive(tmp_path: Path) -> Path:
    archive = _make_archive(tmp_path)
    rewritten = tmp_path / "azerothcore-backup-manual-link.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(rewritten, "w:gz") as target:
        for member in source.getmembers():
            extracted = source.extractfile(member) if member.isfile() else None
            target.addfile(member, extracted)
        link = tarfile.TarInfo("config/unsafe-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../../outside"
        target.addfile(link)
    return rewritten


def test_dr_restore_rejects_links_before_docker_or_mutation(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_link_archive(tmp_path)
    bind = tmp_path / "bin"
    bind.mkdir()
    logf = tmp_path / "docker.log"
    _stateful_docker_stub(bind)

    result = _run(stack, archive, bind, logf)

    assert result.returncode == 1
    assert "unsupported archive member type" in result.stderr
    assert not logf.exists()


def test_dr_restore_cleans_every_tmpdir_artifact_after_success(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    bind = tmp_path / "bin"
    bind.mkdir()
    logf = tmp_path / "docker.log"
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    _stateful_docker_stub(bind)

    result = _run(stack, archive, bind, logf, {"TMPDIR": str(tmpdir)})

    assert result.returncode == 0, result.stderr
    assert list(tmpdir.iterdir()) == []
```

Import `fcntl` in the test module and add:

```python
def test_dr_restore_refuses_backup_lock_contention_before_mutation(tmp_path):
    stack = _stack(tmp_path)
    archive = _make_archive(tmp_path)
    backups = stack / "backups"
    backups.mkdir()
    lock_path = backups / ".backup.lock"
    lock_path.touch()
    bind = tmp_path / "bin"
    bind.mkdir()
    logf = tmp_path / "docker.log"
    _stateful_docker_stub(bind)

    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run(stack, archive, bind, logf)

    assert result.returncode == 75
    assert "backup or restore is already running" in result.stderr
    assert not logf.exists()
    assert "from: fresh" in (stack / "docker-compose.override.yml").read_text()
```

- [ ] **Step 2: Run RED**

Run:

```bash
scripts_pytest \
  scripts/tests/test_restore_sh.py::test_dr_restore_rejects_links_before_docker_or_mutation \
  scripts/tests/test_restore_sh.py::test_dr_restore_cleans_every_tmpdir_artifact_after_success \
  scripts/tests/test_restore_sh.py::test_dr_restore_refuses_backup_lock_contention_before_mutation \
  -q
```

Expected: link archive is extracted/accepted or fails for the wrong reason, the successful restore leaves the extra `custom.cnf` temp, and lock contention is ignored. Inspect each failure; the helper health-checks live state after the command.

- [ ] **Step 3: Replace listing/extraction with one bounded Python extractor**

Replace `validate_archive_members` and `tar -xzf` with `safe_extract_archive`. Its embedded Python body must use these exact limits and checks:

```python
MAX_MEMBERS = 10_000
MAX_MEMBER = 8 * 1024 ** 3
MAX_TOTAL = 16 * 1024 ** 3
MAX_MANIFEST = 1024 ** 2
MAX_OVERLAY = 1024 ** 2
SPECIAL_LIMITS = {
    "manifest.json": MAX_MANIFEST,
    "config/docker-compose.admin.yml": MAX_OVERLAY,
}

archive_path = Path(sys.argv[1])
stage = Path(sys.argv[2]).resolve()
seen: set[str] = set()
total = 0

with tarfile.open(archive_path, "r:gz") as archive:
    members = []
    for count, member in enumerate(archive, start=1):
        if count > MAX_MEMBERS:
            fail("archive has too many members")
        raw = member.name.rstrip("/")
        path = PurePosixPath(raw)
        if not raw or path.is_absolute() or ".." in path.parts or path.as_posix() != raw:
            fail(f"unsafe archive member: {member.name}")
        if raw in seen:
            fail(f"duplicate archive member: {raw}")
        seen.add(raw)
        if not (member.isdir() or member.isfile()):
            fail(f"unsupported archive member type: {raw}")
        if member.isfile():
            limit = SPECIAL_LIMITS.get(raw, MAX_MEMBER)
            if member.size > limit:
                fail(f"archive member exceeds expanded-size limit: {raw}")
            total += member.size
            if total > MAX_TOTAL:
                fail("archive exceeds total expanded-size limit")
        members.append((member, path))

    for member, relative in members:
        target = stage.joinpath(*relative.parts)
        if target.parent != stage and stage not in target.parents:
            fail(f"unsafe archive member: {member.name}")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            fail(f"unreadable archive member: {member.name}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(target, flags, member.mode & 0o777 or 0o600), "wb") as output:
            remaining = member.size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    fail(f"truncated archive member: {member.name}")
                output.write(chunk)
                remaining -= len(chunk)
```

The shell wrapper prints `ERROR: ...` and returns nonzero. Import `os`, `sys`, `tarfile`, `Path`, and `PurePosixPath`; define `fail(message)` to print to stderr and raise `SystemExit(1)`.

- [ ] **Step 4: Acquire the shared lock and move preserved config into stage**

After confirming `${STACK_DIR}/.env` and creating `${STACK_DIR}/backups`, add:

```bash
LOCK_FILE="${STACK_DIR}/backups/.backup.lock"
exec 8>"$LOCK_FILE"
if ! flock -n 8; then
    echo "ERROR: backup or restore is already running; retry after it completes." >&2
    exit 75
fi
```

Keep fd 8 open through the script. Replace the later second `mktemp` with:

```bash
custom_cnf_backup="${STAGE}/.fresh-custom.cnf"
```

Copy into that path after extraction, so the untrusted archive cannot overwrite the preserved copy. Do not add a second trap.

- [ ] **Step 5: Run GREEN and the full host restore test file**

Run:

```bash
scripts_pytest \
  scripts/tests/test_restore_sh.py::test_dr_restore_rejects_links_before_docker_or_mutation \
  scripts/tests/test_restore_sh.py::test_dr_restore_cleans_every_tmpdir_artifact_after_success \
  scripts/tests/test_restore_sh.py::test_dr_restore_refuses_backup_lock_contention_before_mutation \
  -q
scripts_pytest scripts/tests/test_restore_sh.py -q
```

Expected: all pass and live health remains unchanged.

- [ ] **Step 6: Verify and commit**

Run `bash -n scripts/restore-azerothcore.sh`, `git diff --check`, inspect extraction and lock ordering, then:

```bash
git add scripts/restore-azerothcore.sh scripts/tests/test_restore_sh.py
git commit -m "fix: bound and serialize host restore"
```

---

### Task 3: Constrain uninstaller targets and preserve its recovery unit (F03, F11)

**Files:**
- Modify: `scripts/uninstall-azerothcore.sh:18-370`
- Modify/Test: `scripts/tests/test_uninstaller_script.py`

**Interfaces:**
- Consumes: immutable production paths only.
- Produces: `safe_remove_literal PATH [sudo]`, preserved unit file on teardown failure, and hermetic rewritten test copies.

- [ ] **Step 1: Replace the test's production override seam with an isolated script copier**

Add:

```python
def _isolated_script(tmp_path: Path, *, stack: Path, state: Path, config: Path, unit: Path) -> Path:
    replacements = {
        r'^(?:readonly )?STACK_DIR=.*$': f'readonly STACK_DIR="{stack}"',
        r'^(?:readonly )?STATE_FILE=.*$': f'readonly STATE_FILE="{state}"',
        r'^(?:readonly )?CONFIG_FILE=.*$': f'readonly CONFIG_FILE="{config}"',
        r'^(?:readonly )?SYSTEMD_UNIT=.*$': f'readonly SYSTEMD_UNIT="{unit}"',
    }
    source = (SCRIPTS / "uninstall-azerothcore.sh").read_text()
    for pattern, replacement in replacements.items():
        source, count = re.subn(pattern, replacement, source, count=1, flags=re.MULTILINE)
        assert count == 1, pattern
    script = tmp_path / "uninstall-azerothcore.sh"
    script.write_text(source)
    return script
```

Import `re`. Create stubs for all dangerous commands (`docker`, `sudo`, `systemctl`, `rm`, `crontab`) and stop passing target overrides in `env`. `sudo` may execute only `systemctl` or `rm` from the stub directory:

```bash
#!/bin/sh
[ "$1" = -v ] && exit 0
case "$1" in
  systemctl|rm) exec "$@" ;;
  *) echo "unexpected sudo target: $1" >&2; exit 98 ;;
esac
```

Add regressions that assert:

```python
assert 'STACK_DIR="${STACK_DIR:-' not in (SCRIPTS / "uninstall-azerothcore.sh").read_text()
assert 'run sudo rm -rf "$STACK_DIR"' not in (SCRIPTS / "uninstall-azerothcore.sh").read_text()
```

For Compose failure, create the temporary unit file and assert:

```python
assert result.returncode == 1
assert unit.exists()
assert stack.exists() and state.exists() and config.exists()
assert "unit file was preserved" in result.stderr
```

Add:

```python
def test_uninstall_aborts_before_docker_when_systemd_disable_fails(tmp_path):
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "docker-compose.yml").write_text("services: {}\n")
    state = tmp_path / "state"
    state.write_text("phase=4\n")
    config = tmp_path / "config"
    config.write_text("secret\n")
    unit = tmp_path / "azerothcore.service"
    unit.write_text("[Unit]\n")
    bind = _dangerous_stubs(tmp_path, fail_systemctl=True)
    script = _isolated_script(
        tmp_path, stack=stack, state=state, config=config, unit=unit,
    )

    result = _run_isolated(script, bind, tmp_path)

    assert result.returncode == 1
    assert unit.exists() and stack.exists() and state.exists() and config.exists()
    docker_calls = (tmp_path / "docker.calls").read_text()
    assert "info" in docker_calls
    assert "compose" not in docker_calls
    assert " rm " not in docker_calls
```

Define the helpers as follows; `_run_isolated` drops to uid/gid 65534 when root and every dangerous stub refuses paths outside `TEST_ROOT`:

```python
def _dangerous_stubs(tmp_path: Path, *, fail_systemctl: bool = False) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir()
    _exe(bind / "docker", """#!/bin/sh
echo "$@" >> "$TEST_ROOT/docker.calls"
[ "$1" = info ] && exit 0
if [ "$1" = compose ] && [ "$2" = version ]; then exit 0; fi
if [ "$1" = compose ]; then exit 42; fi
exit 0
""")
    _exe(bind / "systemctl", f"#!/bin/sh\necho \"$@\" >> \"$TEST_ROOT/systemctl.calls\"\nexit {42 if fail_systemctl else 0}\n")
    _exe(bind / "rm", """#!/bin/sh
echo "$@" >> "$TEST_ROOT/rm.calls"
for arg in "$@"; do
  case "$arg" in -*) continue ;; "$TEST_ROOT"/*) ;; *) echo "unsafe rm: $arg" >&2; exit 97 ;; esac
done
exec /bin/rm "$@"
""")
    _exe(bind / "crontab", "#!/bin/sh\necho \"$@\" >> \"$TEST_ROOT/crontab.calls\"\nexit 1\n")
    _exe(bind / "sudo", """#!/bin/sh
[ "$1" = -v ] && exit 0
case "$1" in systemctl|rm) exec "$@" ;; *) exit 98 ;; esac
""")
    return bind


def _run_isolated(script: Path, bind: Path, tmp_path: Path):
    for path in (tmp_path, bind):
        path.chmod(0o777)
    if os.geteuid() == 0:
        for parent in tmp_path.parents:
            if parent in (Path("/"), Path("/tmp")):
                break
            parent.chmod(parent.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        preexec = lambda: (os.setgid(65534), os.setuid(65534))
    else:
        preexec = None
    return subprocess.run(
        ["bash", str(script), "--yes"],
        env={
            "HOME": str(tmp_path),
            "PATH": f"{bind}:/usr/bin:/bin",
            "TEST_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        preexec_fn=preexec,
    )
```

- [ ] **Step 2: Run RED in the disposable container**

Run:

```bash
scripts_pytest scripts/tests/test_uninstaller_script.py -q
```

Expected: failures show the environment override, direct privileged removal, removed unit file, and ignored systemctl failure. The helper health-checks live state immediately.

- [ ] **Step 3: Make target constants immutable and removal guarded**

Use:

```bash
readonly STACK_DIR="/opt/stacks/azerothcore"
readonly STATE_FILE="${HOME}/.azerothcore-install-state"
readonly CONFIG_FILE="${HOME}/.azerothcore-install-config"
readonly SYSTEMD_UNIT="/etc/systemd/system/azerothcore.service"
```

Change the literal remover to accept privilege only after the exact-path case succeeds:

```bash
safe_remove_literal() {
  local path="$1"
  local use_sudo="${2:-no}"
  case "$path" in
    "$STACK_DIR"|"$STATE_FILE"|"$CONFIG_FILE"|/tmp/ac-build.log) ;;
    *) echo "Refusing to remove unexpected path: $path" >&2; exit 1 ;;
  esac
  if [ "$use_sudo" = sudo ]; then
    run sudo rm -rf -- "$path"
  else
    run rm -rf -- "$path"
  fi
}
```

Step 6 calls `safe_remove_literal "$STACK_DIR" sudo`.

- [ ] **Step 4: Split systemd stop/disable from unit deletion**

Before Docker teardown, run `sudo systemctl disable --now` and record failure; abort through `preserve_recovery_context_and_exit` before any Docker command if it fails. Do not remove the unit there. After Compose/fallback cleanup has passed every `CLEANUP_FAILED` gate, remove the unit, daemon-reload, and reset-failed. Update the recovery message:

```bash
echo "The systemd unit file was preserved; re-enable it after Docker recovery if service management is needed." >&2
```

- [ ] **Step 5: Run GREEN, syntax, and full uninstaller file**

Run:

```bash
scripts_pytest scripts/tests/test_uninstaller_script.py -q
bash -n scripts/uninstall-azerothcore.sh
```

Verify all dangerous names resolve to test stubs from the call log; the helper health-checks live state.

- [ ] **Step 6: Verify and commit**

Run `git diff --check`, inspect every `rm`, systemctl, Docker, and crontab call, then:

```bash
git add scripts/uninstall-azerothcore.sh scripts/tests/test_uninstaller_script.py
git commit -m "fix: confine uninstaller cleanup targets"
```

---

### Task 4: Require current-container evidence for root redeploy readiness (F09)

**Files:**
- Modify: `scripts/redeploy-azerothcore.sh:103-143`
- Modify/Test: `scripts/tests/test_redeploy_sh.py`

**Interfaces:**
- Consumes: Docker `StartedAt` and logs for the recreated service.
- Produces: readiness only from `docker logs --since STARTED_AT ac-worldserver`.

- [ ] **Step 1: Make stale and current markers separate test inputs**

Teach the Docker stub to return `2026-07-14T12:00:00Z` when inspect arguments contain `StartedAt`, and emit the marker from `docker logs` only when `REDEPLOY_TEST_CURRENT_LOG_READY=1`.

Replace the existing happy test and add the stale regression:

```python
def test_redeploy_rejects_stale_host_log_without_current_container_marker(tmp_path):
    result = _run(
        _stack(tmp_path, initialized=True),
        _stubs(tmp_path),
        current_log_ready=False,
    )
    assert result.returncode == 1
    assert "did not observe 'World Initialized'" in result.stderr


def test_redeploy_accepts_current_container_initialization_marker(tmp_path):
    result = _run(
        _stack(tmp_path, initialized=True),
        _stubs(tmp_path),
        current_log_ready=True,
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run RED**

Run:

```bash
scripts_pytest \
  scripts/tests/test_redeploy_sh.py::test_redeploy_rejects_stale_host_log_without_current_container_marker \
  scripts/tests/test_redeploy_sh.py::test_redeploy_accepts_current_container_initialization_marker \
  -q
```

Expected: stale-host-log test incorrectly succeeds. The helper health-checks live state.

- [ ] **Step 3: Poll Docker logs since the recreated start**

After status is running:

```bash
started_at="$(docker inspect -f '{{.State.StartedAt}}' "$SERVICE" 2>/dev/null || true)"
if [ -z "$started_at" ]; then
    echo "ERROR: could not determine $SERVICE current start time." >&2
    exit 1
fi
```

Inside the existing status loop replace host-file grep with:

```bash
if docker logs --since "$started_at" "$SERVICE" 2>&1 | grep -q "World Initialized"; then
    init_ok=1
    break
fi
```

- [ ] **Step 4: Run GREEN and full redeploy file**

Run:

```bash
scripts_pytest scripts/tests/test_redeploy_sh.py -q
```

Expected all pass; the helper health-checks live state.

- [ ] **Step 5: Verify and commit**

Run `bash -n`, `git diff --check`, then commit:

```bash
git add scripts/redeploy-azerothcore.sh scripts/tests/test_redeploy_sh.py
git commit -m "fix: verify redeploy from current container logs"
```

---

### Task 5: Serialize and durably promote admin redeploys (F10)

**Files:**
- Modify: `wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh`
- Modify/Test: `scripts/tests/test_admin_redeploy_sh.py`

**Interfaces:**
- Consumes: `${STACK_DIR}/.redeploy.lock`, candidate image tag, durable `azerothcore-admin:local`.
- Produces: one redeploy at a time and a durable default image after verification.

- [ ] **Step 1: Extend Docker stubs and add promotion/lock tests**

Import `fcntl`. The stub must log `docker image tag` and `docker image rm`. Add:

```python
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
```

Retain `test_candidate_staging_recreates_dist_after_rsync_delete` unchanged.

- [ ] **Step 2: Run RED**

Run:

```bash
scripts_pytest scripts/tests/test_admin_redeploy_sh.py -q
```

Expected: promotion and contention regressions fail; e16f877 staging test remains green. The helper health-checks live state.

- [ ] **Step 3: Acquire the lock before candidate creation**

After stack preflight and before `CANDIDATE_IMAGE` is finalized:

```bash
exec 9>"${STACK_DIR}/.redeploy.lock"
if ! flock -n 9; then
    echo "ERROR: another admin redeploy is already running." >&2
    exit 75
fi
```

- [ ] **Step 4: Promote only after health and full verification**

Immediately after `"$VERIFY_SCRIPT"` succeeds:

```bash
echo "==> Promoting verified candidate to azerothcore-admin:local..."
if ! docker image tag "$CANDIDATE_IMAGE" azerothcore-admin:local; then
    rollback || true
    exit 1
fi
docker image rm "$CANDIDATE_IMAGE" >/dev/null 2>&1 || \
    echo "WARNING: could not remove temporary candidate tag $CANDIDATE_IMAGE" >&2
```

Do not prune other images. Do not move promotion before verification.

- [ ] **Step 5: Run GREEN and verify e16f877**

Run:

```bash
scripts_pytest scripts/tests/test_admin_redeploy_sh.py -q
```

Expected: all pass, including dist recreation and rollback. The helper health-checks live state.

- [ ] **Step 6: Verify and commit**

Run shell syntax and `git diff --check`, inspect promotion/rollback ordering, then:

```bash
git add wow-server-sp-admin/scripts/redeploy-azerothcore-admin.sh scripts/tests/test_admin_redeploy_sh.py
git commit -m "fix: make admin redeploy durable"
```

---

### Task 6: Bound manifest/overlay reads and reuse typed Settings validation (F05, F06)

**Files:**
- Modify: `wow-server-sp-admin/app/services/actions.py:33-280,523-577`
- Modify: `wow-server-sp-admin/app/services/compose_admin.py:20-75`
- Modify/Test: `wow-server-sp-admin/tests/test_restore_action.py`
- Modify/Test: `wow-server-sp-admin/tests/test_compose_admin.py`

**Interfaces:**
- Produces: `MAX_MANIFEST_BYTES = 1 MiB`, `MAX_ADMIN_OVERLAY_BYTES = 1 MiB`, `_load_manifest_member(tf) -> dict`, and `validate_restored_overlay(path, entries_by_env)`.

- [ ] **Step 1: Add exact-type and size regressions**

Add:

```python
@pytest.mark.parametrize("format_version", [True, 1.0])
def test_validate_canonical_backup_rejects_non_integer_format_version(
    tmp_path, format_version,
):
    manifest = json.dumps({
        "format_version": format_version,
        "databases": list(KNOWN_DBS),
        "skipped_databases": [],
    }).encode()
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", manifest=manifest,
    )
    assert "unsupported manifest format" in actions.validate_canonical_backup(archive)


@pytest.mark.parametrize("databases", [True, 1, "acore_auth", {"acore_auth": 1}])
def test_validate_canonical_backup_rejects_non_list_inventory(tmp_path, databases):
    manifest = json.dumps({
        "format_version": 2,
        "databases": databases,
        "skipped_databases": [],
        "dump_layout": "single-multi-database",
    }).encode()
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", manifest=manifest,
    )
    assert "canonical databases" in actions.validate_canonical_backup(archive)


def test_validate_canonical_backup_rejects_oversized_manifest_before_read(tmp_path):
    payload = b" " * (1024 ** 2 + 1)
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", manifest=payload,
    )
    assert "manifest" in actions.validate_canonical_backup(archive)
    assert "size limit" in actions.validate_canonical_backup(archive)


def test_validate_canonical_backup_rejects_oversized_admin_overlay(tmp_path):
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz",
    )
    _append_member(
        archive,
        "config/oversized-marker",
        b"x",
    )
    # Rebuild with one unique oversized canonical overlay, not a duplicate member.
    replacement = archive.with_name("replacement.tar.gz")
    with tarfile.open(archive, "r:gz") as source, tarfile.open(replacement, "w:gz") as target:
        for member in source:
            if member.name == "config/docker-compose.admin.yml":
                continue
            target.addfile(member, source.extractfile(member) if member.isfile() else None)
        payload = b"services: {}\n#" + b"x" * (1024 ** 2)
        member = tarfile.TarInfo("config/docker-compose.admin.yml")
        member.size = len(payload)
        target.addfile(member, io.BytesIO(payload))
    os.replace(replacement, archive)
    assert "admin overlay" in actions.validate_canonical_backup(archive)
    assert "size limit" in actions.validate_canonical_backup(archive)
```

The oversized tests use real tarfile behavior and payloads just over 1 MiB; they must not allocate generic multi-GiB limits.

Add typed overlay entries using real `KeyEntry` objects:

```python
INT_ENTRY = KeyEntry(
    key="AiPlayerbot.MinRandomBots",
    default="1000",
    inferred_type="int",
    comment="",
    source_file="playerbots.conf.dist",
    line_number=1,
    env_var="AC_AI_PLAYERBOT_MIN_RANDOM_BOTS",
)


def test_restored_overlay_rejects_invalid_typed_and_empty_values(tmp_path):
    path = tmp_path / "admin.yml"
    entries = {INT_ENTRY.env_var: INT_ENTRY}
    for value in ("not-an-int", ""):
        path.write_text(
            "services:\n  ac-worldserver:\n    environment:\n"
            f"      {INT_ENTRY.env_var}: '{value}'\n"
        )
        assert "invalid value" in validate_restored_overlay(path, entries_by_env=entries)
```

Add:

```python
@patch("app.services.actions.run_stop")
def test_run_restore_rejects_invalid_typed_overlay_before_stop(
    mock_stop, tmp_path, monkeypatch,
):
    from app.services.compose_admin import validate_restored_overlay
    from app.services.config_index import KeyEntry
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    entry = KeyEntry(
        key="AiPlayerbot.MinRandomBots",
        default="1000",
        inferred_type="int",
        comment="",
        source_file="playerbots.conf.dist",
        line_number=1,
        env_var="AC_AI_PLAYERBOT_MIN_RANDOM_BOTS",
    )
    archive = _make_archive(
        tmp_path / "backups",
        "azerothcore-backup-manual-x.tar.gz",
        admin_yml_text=(
            "services:\n  ac-worldserver:\n    environment:\n"
            "      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: not-an-int\n"
        ),
    )
    monkeypatch.setattr(
        "app.services.actions._validate_restored_admin_yml",
        lambda path: validate_restored_overlay(path, entries_by_env={entry.env_var: entry}),
    )
    result = actions.run_restore(archive.name, on_progress=lambda *_: None)
    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()
```

- [ ] **Step 2: Run RED**

Run:

```bash
admin_pytest tests/test_restore_action.py tests/test_compose_admin.py -q
```

Expected: oversized reads and malformed types are accepted/raise incorrectly; typed invalid overlay is accepted.

- [ ] **Step 3: Add type-specific member limits and bounded loader**

Define:

```python
MAX_MANIFEST_BYTES = 1024 ** 2
MAX_ADMIN_OVERLAY_BYTES = 1024 ** 2
```

In `_validate_archive_members`, reject exact normalized members above those caps before the generic 8 GiB check. Add:

```python
def _load_manifest_member(archive: tarfile.TarFile) -> dict:
    member = archive.getmember("manifest.json")
    if not member.isfile() or member.size > MAX_MANIFEST_BYTES:
        raise ValueError("archive manifest exceeds its size limit")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("archive manifest cannot be read")
    payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("archive manifest exceeds its size limit")
    manifest = _load_strict_json(payload)
    if not isinstance(manifest, dict):
        raise ValueError("archive manifest must be an object")
    return manifest
```

Use this helper in `read_manifest`, `validate_canonical_backup`, and `run_restore`. Validate with `type(format_version) is int`, `databases == list(KNOWN_DBS)`, and `skipped_databases == []`; catch `KeyError`, `TypeError`, `ValueError`, Unicode, tar, and OS errors into stable validator strings.

- [ ] **Step 4: Pass actual entries into overlay validation**

Change the signature to:

```python
def validate_restored_overlay(
    path: Path,
    *,
    entries_by_env: Mapping[str, KeyEntry],
) -> str | None:
```

Before `read_text`, reject `path.stat().st_size > 1024 ** 2`. After existing shape/blocklist checks:

```python
entry = entries_by_env.get(key)
if entry is None:
    return f"restored admin overlay key is not approved: {key}"
if value == "":
    return f"restored admin overlay has invalid value for {key}: empty overrides must be omitted"
if error := validate_value(entry, value):
    return f"restored admin overlay has invalid value for {key}: {error}"
```

`_validate_restored_admin_yml` builds `{entry.env_var: entry for entry in get_state().key_index.values()}`.

Update every existing `validate_restored_overlay(..., allowed_env_vars=...)` test call to `entries_by_env=...`, using real `KeyEntry` fixtures. Keep the existing malformed shape, extra service, unapproved key, and blocked-key assertions unchanged in meaning.

- [ ] **Step 5: Run GREEN and relevant admin suite**

Run:

```bash
admin_pytest tests/test_restore_action.py tests/test_compose_admin.py -q
admin_pytest tests/test_import_restore.py tests/test_backups_page.py -q
```

Expected: all pass; import validation remains streaming and listing remains metadata-only.

- [ ] **Step 6: Verify and commit**

Run `git diff --check`, inspect every manifest/overlay read, then:

```bash
git add wow-server-sp-admin/app/services/actions.py wow-server-sp-admin/app/services/compose_admin.py wow-server-sp-admin/tests/test_restore_action.py wow-server-sp-admin/tests/test_compose_admin.py
git commit -m "fix: bound restored archive metadata"
```

---

### Task 7: Reject backup-directory symlink escapes (F15)

**Files:**
- Modify: `wow-server-sp-admin/app/services/backups.py`
- Modify: `wow-server-sp-admin/app/main.py:421-486,614-627`
- Modify: `wow-server-sp-admin/app/services/actions.py:523-543`
- Modify/Test: `wow-server-sp-admin/tests/test_backups.py`
- Modify/Test: `wow-server-sp-admin/tests/test_backups_page.py`
- Modify/Test: `wow-server-sp-admin/tests/test_restore_action.py`

**Interfaces:**
- Produces: `resolve_backup_archive(backups_dir: Path, archive_name: str) -> Path | None`.

- [ ] **Step 1: Add symlink listing/download/restore regressions**

In `test_backups.py`, import `resolve_backup_archive` and add:

```python
def test_matching_backup_symlink_is_rejected_without_following(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"secret")
    link = backups / "azerothcore-backup-manual-link.tar.gz"
    link.symlink_to(outside)

    assert resolve_backup_archive(backups_dir=backups, archive_name=link.name) is None
    with pytest.raises(BackupListingError):
        list_backups(backups_dir=backups)
```

In `test_backups_page.py`, add:

```python
def test_backup_download_rejects_symlink_outside_backup_directory(client, tmp_path):
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"database-secret")
    link = tmp_path / "backups" / "azerothcore-backup-manual-link.tar.gz"
    link.symlink_to(outside)

    response = client.get(f"/api/backups/download/{link.name}")

    assert response.status_code == 404
    assert b"database-secret" not in response.content
```

In `test_restore_action.py`, add:

```python
@patch("app.services.actions.run_stop")
def test_run_restore_rejects_backup_symlink_before_archive_open(
    mock_stop, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    backups = tmp_path / "backups"
    backups.mkdir()
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"not an archive")
    link = backups / "azerothcore-backup-manual-link.tar.gz"
    link.symlink_to(outside)

    with patch("app.services.actions.tarfile.open", side_effect=AssertionError("archive opened")):
        result = actions.run_restore(link.name, on_progress=lambda *_: None)

    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()
```

- [ ] **Step 2: Run RED**

Run:

```bash
admin_pytest tests/test_backups.py tests/test_backups_page.py tests/test_restore_action.py -q
```

Expected: current `is_file()`/`DirEntry.stat()` follows the symlink and download exposes outside bytes.

- [ ] **Step 3: Implement the common non-following resolver**

Add `import stat` and:

```python
def resolve_backup_archive(*, backups_dir: Path, archive_name: str) -> Path | None:
    if (
        "/" in archive_name
        or ".." in archive_name
        or _ARCHIVE_RE.fullmatch(archive_name) is None
    ):
        return None
    try:
        root = backups_dir.resolve(strict=True)
        candidate = backups_dir / archive_name
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None
    if resolved.parent != root:
        return None
    return candidate
```

In listing, reject matching `entry.is_symlink()` and use `entry.is_file(follow_symlinks=False)` plus `entry.stat(follow_symlinks=False)`. Routes and `run_restore` call the resolver; the worker rechecks rather than trusting the route's earlier check.

- [ ] **Step 4: Run GREEN and preserve metadata-only negative assertions**

Run:

```bash
admin_pytest \
  tests/test_backups.py \
  tests/test_backups_page.py \
  tests/test_restore_action.py::test_run_restore_rejects_backup_symlink_before_archive_open \
  -q
```

Expected: all pass, including `test_list_and_summary_never_open_or_validate_archives`, and no archive API is reached by GET listing/summary.

- [ ] **Step 5: Verify and commit**

Run `git diff --check`, then:

```bash
git add wow-server-sp-admin/app/services/backups.py wow-server-sp-admin/app/main.py wow-server-sp-admin/app/services/actions.py wow-server-sp-admin/tests/test_backups.py wow-server-sp-admin/tests/test_backups_page.py wow-server-sp-admin/tests/test_restore_action.py
git commit -m "fix: reject backup symlink escapes"
```

---

### Task 8: Make import staging off-loop and cancellation-clean (F07)

**Files:**
- Modify: `wow-server-sp-admin/app/main.py:1-45,630-679`
- Modify/Test: `wow-server-sp-admin/tests/test_import_restore.py`

**Interfaces:**
- Produces: `_await_thread_completion(func, *args)`, `_copy_upload_to_staging(source, staged, max_bytes)`.

- [ ] **Step 1: Add thread/cancellation/exception regressions**

Import `pytest`, then add:

```python
@pytest.mark.asyncio
async def test_await_thread_completion_waits_for_worker_after_cancellation():
    import app.main as main
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def worker():
        started.set()
        release.wait()
        finished.set()

    request = asyncio.create_task(main._await_thread_completion(worker))
    assert await asyncio.to_thread(started.wait, 1)
    cancelled = False
    try:
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
        assert not finished.is_set()
    finally:
        release.set()
        try:
            await request
        except asyncio.CancelledError:
            cancelled = True
    assert cancelled is True
    assert finished.is_set()
```

Add:

```python
def test_import_restore_cleans_hidden_upload_when_validator_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    import app.main as main
    with patch("app.services.actions.validate_canonical_backup", side_effect=RuntimeError("validator crash")):
        response = TestClient(main.app, raise_server_exceptions=False).post(
            "/api/action/import-restore",
            files={"file": ("backup.tar.gz", _archive_bytes(), "application/gzip")},
        )
    assert response.status_code == 500
    assert not list((tmp_path / "backups").glob(".*.upload"))
```

Add:

```python
@pytest.mark.asyncio
async def test_import_copy_does_not_block_health_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    import app.main as main
    from fastapi import HTTPException, UploadFile
    started = threading.Event()
    release = threading.Event()

    def slow_copy(_source, _staged, _limit):
        started.set()
        release.wait()
        return 1

    upload = UploadFile(
        file=io.BytesIO(_archive_bytes()),
        filename="backup.tar.gz",
        size=len(_archive_bytes()),
    )
    with patch.object(main, "_copy_upload_to_staging", side_effect=slow_copy), patch(
        "app.services.actions.validate_canonical_backup", return_value="test rejection",
    ):
        request = asyncio.create_task(main.post_import_restore(upload))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert await asyncio.wait_for(main.healthz(), timeout=0.1) == {"status": "ok"}
        finally:
            release.set()
        with pytest.raises(HTTPException) as exc_info:
            await request
        assert "invalid restore archive" in str(exc_info.value.detail)
```

- [ ] **Step 2: Run RED**

Run:

```bash
admin_pytest tests/test_import_restore.py -q
```

Expected: `_await_thread_completion`/`_copy_upload_to_staging` are absent, validator exception leaves staging, and synchronous copy blocks the direct async route.

- [ ] **Step 3: Implement shield-and-wait thread execution**

Add:

```python
async def _await_thread_completion(func, /, *args):
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _copy_upload_to_staging(source, staged: Path, max_bytes: int) -> int:
    total = 0
    source.seek(0)
    with staged.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise OverflowError("uploaded archive exceeds the configured size limit")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return total
```

- [ ] **Step 4: Give the route one cleanup owner**

After paths are computed, wrap copy, validation, replace, and dispatch in one `try/finally`; the finally always calls `staged.unlink(missing_ok=True)`. Use `_await_thread_completion(_copy_upload_to_staging, file.file, staged, _MAX_IMPORT_BYTES)` and `_await_thread_completion(validate_canonical_backup, staged)`. Translate `OverflowError` to 413 and `OSError` to the existing safe 500. Keep busy-dispatch deletion of `dest`.

- [ ] **Step 5: Run GREEN and full import file**

Run:

```bash
admin_pytest tests/test_import_restore.py -q
```

Expected: all pass with no hidden uploads after any tested path.

- [ ] **Step 6: Verify and commit**

Run `git diff --check`, inspect cancellation ordering, then:

```bash
git add wow-server-sp-admin/app/main.py wow-server-sp-admin/tests/test_import_restore.py
git commit -m "fix: make backup import cancellation safe"
```

---

### Task 9: Hold progression reservation until its worker ends (F13)

**Files:**
- Modify: `wow-server-sp-admin/app/main.py:798-815`
- Modify/Test: `wow-server-sp-admin/tests/test_main.py`

**Interfaces:**
- Consumes: `_await_thread_completion` from Task 8 and `runner` mutation reservation.
- Produces: cancellation cannot release reservation before `apply_progression` exits.

- [ ] **Step 1: Add coordinated cancellation regression**

Add:

```python
@pytest.mark.asyncio
async def test_progression_cancellation_holds_reservation_until_worker_finishes(monkeypatch):
    import app.main as main
    from app.services.progression import ApplyProgressionResult, ProgressionConfig
    from app.services.runner import ActionRunner
    worker_started = threading.Event()
    release_worker = threading.Event()
    runner = ActionRunner()
    monkeypatch.setattr(main, "runner", runner)

    def blocking_apply(**_kwargs):
        worker_started.set()
        release_worker.wait()
        return ApplyProgressionResult("applied", 8, 8)

    with patch.object(
        main.progression_svc, "config_from_resolved_keys", return_value=ProgressionConfig(),
    ), patch.object(main, "list_keys_resolved", return_value=[]), patch.object(
        main, "db_credentials", return_value={
            "host": "h", "port": 3306, "user": "u", "password": "p",
        },
    ), patch.object(main.progression_svc, "apply_progression", side_effect=blocking_apply):
        request_task = asyncio.create_task(main.api_progression_apply(
            main.ProgressionApplyPayload(guid=101, target_expansion="tbc")
        ))
        assert await asyncio.to_thread(worker_started.wait, 1)
        cancelled = False
        try:
            request_task.cancel()
            await asyncio.sleep(0)
            acquired_early = runner.try_acquire_mutation()
            if acquired_early:
                runner.release_mutation()
            assert acquired_early is False
        finally:
            release_worker.set()
            try:
                await request_task
            except asyncio.CancelledError:
                cancelled = True

    assert cancelled is True
    assert runner.try_acquire_mutation() is True
    runner.release_mutation()
```

- [ ] **Step 2: Run RED**

Run:

```bash
admin_pytest tests/test_main.py::test_progression_cancellation_holds_reservation_until_worker_finishes -q
```

Expected: current route releases immediately after cancellation, so the first reservation assertion fails.

- [ ] **Step 3: Use the shielded helper**

Replace only `await asyncio.to_thread(progression_svc.apply_progression, ...)` with `await _await_thread_completion(progression_svc.apply_progression, ...)`. Keep `release_mutation()` in `finally`.

- [ ] **Step 4: Run GREEN and progression/main focused tests**

Run:

```bash
admin_pytest \
  tests/test_main.py::test_progression_cancellation_holds_reservation_until_worker_finishes \
  tests/test_main.py::test_api_progression_apply_uses_service \
  tests/test_main.py::test_api_progression_apply_rejects_while_an_action_is_running \
  tests/test_runner.py::test_runner_serializes_external_mutation_with_actions \
  -q
```

Expected all pass.

- [ ] **Step 5: Verify and commit**

Run `git diff --check`, then:

```bash
git add wow-server-sp-admin/app/main.py wow-server-sp-admin/tests/test_main.py
git commit -m "fix: retain progression mutation reservation"
```

---

### Task 10: Serialize in-app restore database mutation with backup writers (admin half of F08)

**Files:**
- Modify: `wow-server-sp-admin/app/services/actions.py:1-55,523-629`
- Modify/Test: `wow-server-sp-admin/tests/test_restore_action.py`

**Interfaces:**
- Produces: `_backup_mutation_lock(backups_dir)` context manager using the same `.backup.lock` as `backup.sh`.

- [ ] **Step 1: Add a real flock contention regression**

Import `fcntl`, then add:

```python
@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
def test_in_app_restore_refuses_backup_lock_before_database_mutation(
    mock_run, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz",
    )
    mock_backup.return_value = type(
        "Result", (), {"ok": True, "archive": "safety.tar.gz", "output": ""}
    )()
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    progress = []
    lock_path = archive.parent / ".backup.lock"
    lock_path.touch()

    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = actions.run_restore(
            archive.name,
            on_progress=lambda step, message: progress.append((step, message)),
        )

    assert result == ActionResult.ERROR
    mock_stop.assert_called_once()
    mock_backup.assert_called_once()
    mock_run.assert_not_called()
    mock_start.assert_called_once()
    assert any("backup or restore is already running" in message for _, message in progress)
```

- [ ] **Step 2: Run RED**

Run:

```bash
admin_pytest tests/test_restore_action.py::test_in_app_restore_refuses_backup_lock_before_database_mutation -q
```

Expected: current restore ignores the held lock and invokes DB subprocesses.

- [ ] **Step 3: Add the compatible lock context**

```python
@contextmanager
def _backup_mutation_lock(backups_dir: Path):
    backups_dir.mkdir(parents=True, exist_ok=True)
    with (backups_dir / ".backup.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RestoreBusy("backup or restore is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
```

Define `RestoreBusy(RuntimeError)` and import `fcntl` plus `contextmanager`.

- [ ] **Step 4: Acquire after safety backup and hold through start**

Keep validation, stop, and safety backup in their current order. Immediately after safety success, enter `_backup_mutation_lock`; perform DB import, overlay restore, and `run_start` inside it. On `RestoreBusy`, emit the safe message, call `_restart_after_restore_failure(on_progress)`, and return ERROR. Do not acquire before `create_backup`, which would deadlock against `backup.sh`.

- [ ] **Step 5: Run GREEN and restore suite**

Run:

```bash
admin_pytest tests/test_restore_action.py::test_in_app_restore_refuses_backup_lock_before_database_mutation -q
admin_pytest tests/test_restore_action.py -q
```

Expected: all pass; post-mutation failures retain existing stopped/recovery semantics.

- [ ] **Step 6: Verify and commit**

Run `git diff --check`, inspect lock scope and every return path, then:

```bash
git add wow-server-sp-admin/app/services/actions.py wow-server-sp-admin/tests/test_restore_action.py
git commit -m "fix: serialize backup and restore mutations"
```

---

### Task 11: Bound and offload dashboard backup-status polling (F12)

**Files:**
- Modify: `wow-server-sp-admin/app/services/backups.py:23-45`
- Modify: `wow-server-sp-admin/app/main.py:327-346`
- Modify/Test: `wow-server-sp-admin/tests/test_backups.py`
- Modify/Test: `wow-server-sp-admin/tests/test_main.py`

**Interfaces:**
- Consumes: `logs.tail_filtered(path, n=200, max_bytes=1024 * 1024)`.
- Produces: bounded latest-run status and off-event-loop route collection.

- [ ] **Step 1: Add bounded-read and route-offload regressions**

In `test_backups.py`, import `patch` from `unittest.mock`, then add:

```python
def test_backup_status_reads_only_a_bounded_log_tail(tmp_path):
    log = tmp_path / "backup.log"
    log.write_bytes(
        b"[old] ERROR: historical failure\n"
        + b"x" * (1024 * 1024 + 1024)
        + b"\n[recent] Backup complete.\n"
    )
    with patch.object(Path, "read_text", side_effect=AssertionError("whole log read")):
        status = backup_status(backups_dir=tmp_path / "backups", log_path=log)
    assert status.last_error is None
```

In `test_main.py`, import `AsyncMock` and `BackupStatus`, then add:

```python
def test_api_backups_offloads_status_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    import app.main as main
    with patch.object(
        main.asyncio,
        "to_thread",
        new=AsyncMock(return_value=BackupStatus(None, None)),
    ) as offload:
        response = TestClient(main.app).get("/api/backups")
    assert response.status_code == 200
    offload.assert_awaited_once_with(
        main.backups_svc.backup_status,
        backups_dir=tmp_path / "backups",
        log_path=tmp_path / "logs" / "backup.log",
    )
```

- [ ] **Step 2: Run RED**

Run:

```bash
admin_pytest \
  tests/test_backups.py::test_backup_status_reads_only_a_bounded_log_tail \
  tests/test_main.py::test_api_backups_offloads_status_collection \
  -q
```

Expected: current service calls whole-file `read_text`; route does not use `to_thread`.

- [ ] **Step 3: Reuse the bounded log tail and offload the service**

Import `app.services.logs` in `backups.py` and replace the whole-file scan with:

```python
for line in reversed(logs_svc.tail_filtered(log_path, n=10_000, max_bytes=1024 * 1024)):
    if "Backup complete." in line:
        break
    if "] ERROR:" in line:
        last_error = line
        break
```

In `api_backups`, obtain `s` with `await asyncio.to_thread(backups_svc.backup_status, backups_dir=..., log_path=...)`.

- [ ] **Step 4: Run GREEN and backups/main focused tests**

Run:

```bash
admin_pytest tests/test_backups.py tests/test_main.py -q
```

Expected all pass.

- [ ] **Step 5: Verify and commit**

Run `git diff --check`, then:

```bash
git add wow-server-sp-admin/app/services/backups.py wow-server-sp-admin/app/main.py wow-server-sp-admin/tests/test_backups.py wow-server-sp-admin/tests/test_main.py
git commit -m "fix: bound backup status polling"
```

---

### Task 12: Run focused preservation and compatibility gates

**Files:**
- Inspect only unless a regression proves a current correction was accidentally broken.
- Update local ledger: `.codex/pr25-audit-ledger.md` with RED/GREEN command evidence and final statuses.

**Interfaces:**
- Produces: evidence that later corrections remain intact.

- [ ] **Step 1: Prove no archive work on read-only/general verification paths**

Run:

```bash
scripts_pytest scripts/tests/test_verify_sh.py::test_general_verification_does_not_read_or_require_backup_archives -q
admin_pytest tests/test_backups.py::test_list_and_summary_never_open_or_validate_archives -q
```

Expected: pass. Inspect current call paths with:

```bash
rg -n "tarfile|tar -|validate_canonical_backup|extract" wow-server-sp-admin/app/services/backups.py scripts/verify-azerothcore.sh
```

Expected: no archive opening/decompression reachable from Backups GET or general verification.

- [ ] **Step 2: Prove creation/import/restore validation boundaries and cleanup**

Run:

```bash
scripts_pytest scripts/tests/test_backup_sh.py scripts/tests/test_restore_sh.py -q
admin_pytest tests/test_import_restore.py tests/test_restore_action.py tests/test_compose_admin.py -q
```

Expected: backup publication, import validation, both restore preflights, no-extracted-copy, and staging cleanup tests pass.

- [ ] **Step 3: Prove pinned connector and Online-card path**

Run:

```bash
admin_pytest \
  tests/test_db_stats.py::test_connection_options_are_supported_by_pinned_connector \
  tests/test_db_stats.py::test_count_online_returns_split_counts \
  tests/test_main.py \
  -q
```

inside the admin requirements container. Inspect that `CMySQLConnection.config(... connection_timeout=2, autocommit=True)` accepts the exact options, `read_timeout` is absent, and SQL includes `MAX_EXECUTION_TIME(2000)`.

- [ ] **Step 4: Prove App Events and browser behavior remain intact**

Run:

```bash
admin_pytest tests/test_app_events.py tests/test_main.py -q
browser_test browser-tests/admin-ui.spec.mjs
```

Expected: 200-record cap, restart-local store, sanitization, coalescing, best-effort response handling, incident correlation, one-shot links, tab preservation, filters, and keyboard tabs all pass. Verify no Playwright/runtime package was added to `requirements.txt` or production Dockerfile.

- [ ] **Step 5: Verify e16f877 and correction-chain tests**

Run:

```bash
scripts_pytest scripts/tests/test_admin_redeploy_sh.py::test_candidate_staging_recreates_dist_after_rsync_delete -q
scripts_pytest scripts/tests/test_verify_sh.py::test_general_verification_does_not_read_or_require_backup_archives -q
admin_pytest tests/test_backups.py tests/test_backups_page.py tests/test_db_stats.py tests/test_app_events.py -q
```

Expected: all pass. Record e16f877, b3e6cd9→158a3ad, bf4dadf, 831588d, and PR #26 as preserved in the ledger.

---

### Task 13: Run complete static, scripts, admin, browser, safety, and warning validation

**Files:**
- Inspect all intentional branch changes and `.codex/pr25-audit-ledger.md`.

- [ ] **Step 1: Static checks**

Run:

```bash
git diff --check origin/main...HEAD
find scripts wow-server-sp-admin/scripts -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
docker run --rm -v "$(pwd):/repo:ro" -w /repo \
  mcr.microsoft.com/playwright:v1.61.1-noble bash -ec '
    find wow-server-sp-admin/browser-tests -type f -name "*.mjs" -print0 | xargs -0 -n1 node --check
    node --check wow-server-sp-admin/playwright.config.mjs
  '
docker run --rm --entrypoint shellcheck -v "$(pwd):/mnt:ro" -w /mnt \
  koalaman/shellcheck-alpine:stable \
  scripts/*.sh wow-server-sp-admin/scripts/*.sh
```

Expected: exit 0, no syntax or diff errors, no new ShellCheck warning.

- [ ] **Step 2: Complete scripts suite in a socket-free container**

```bash
docker run --rm \
  -v "$(pwd):/repo:ro" -w /repo \
  python:3.12-slim bash -ec '
    test ! -S /var/run/docker.sock
    test ! -e /opt/stacks/azerothcore
    test ! -e /opt/stacks/azerothcore-admin
    pip install pytest -q
    python -m pytest -p no:cacheprovider scripts/tests/ -v --tb=short
  '
docker ps --format '{{.Names}} {{.Status}}'
```

Expected: zero failures; all expected live containers remain up/healthy in the read-only follow-up.

- [ ] **Step 3: Complete admin Python suite and exact warning capture**

```bash
set -o pipefail
docker run --rm \
  -v "$(pwd)/wow-server-sp-admin:/src:ro" -w /src \
  python:3.12-slim bash -ec '
    test ! -S /var/run/docker.sock
    test ! -e /opt/stacks/azerothcore
    pip install -r requirements-dev.txt -q
    python -m pytest -p no:cacheprovider -v --tb=short
  ' 2>&1 | tee /tmp/pr25-admin-pytest.log
```

Expected: zero failures. Inspect every warning line with `rg -n "warning|PendingDeprecationWarning" /tmp/pr25-admin-pytest.log`. The only application-test warning permitted is Starlette's documented multipart PendingDeprecationWarning caused by FastAPI 0.115.0/Starlette with `python-multipart==0.0.31`; record exact file, line, text, and count. Pip notices from the disposable root container are not application warnings.

- [ ] **Step 4: Complete Playwright/Chromium/axe suite**

```bash
docker run --rm --init --ipc=host \
  -v "$(pwd)/wow-server-sp-admin:/work" -w /work \
  mcr.microsoft.com/playwright:v1.61.1-noble \
  bash -ec 'test ! -S /var/run/docker.sock; test ! -e /opt/stacks/azerothcore; npm ci --silent; npx playwright test'
```

Expected: zero failures, including axe checks, one-shot incident navigation, preserved tab selection, filters, and keyboard behavior.

- [ ] **Step 5: Inspect branch contents and exact results**

Run:

```bash
git status --short --branch
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
```

Record exact counts, elapsed times, warning output, and every correction commit in the ledger. The worktree may contain only intentional audit artifacts and implementation changes.

---

### Task 14: Independent whole-branch review, resolution, and delivery

**Files:**
- Review: original `725eb73^1..725eb73^2`, `725eb73^1..HEAD`, every correction commit, design, plan, ledger, and exact validation logs.

- [ ] **Step 1: Invoke `superpowers:requesting-code-review`**

Give the independent reviewer:

```text
Original PR #25: 8c5c0cb877a4ccf0183cd089dbb71e0ee0b02f6d..0931a5f0ea2ba773ad54b84bc1929ce3c0960d00
Current net range: 8c5c0cb877a4ccf0183cd089dbb71e0ee0b02f6d..HEAD
Design: docs/superpowers/specs/2026-07-14-pr25-boundary-corrections-design.md
Plan: docs/superpowers/plans/2026-07-14-pr25-boundary-corrections.md
Ledger: .codex/pr25-audit-ledger.md
Findings: F01-F15, with F14 explicitly no-change
Validation: exact focused/full/static/browser/compatibility/warning command output
```

Require explicit per-finding correctness and reasonableness verdicts, archive/polling cost analysis, cleanup/cancellation/locking review, production-safety review, and PR #26 preservation review. A test-only/style-only response is insufficient.

- [ ] **Step 2: Resolve every Critical or Important review finding**

For each valid finding, re-enter systematic debugging and TDD: add a focused failing regression, observe RED, make the smallest correction, run focused and relevant full suites, verify, and commit. Record rejected feedback with concrete code/test evidence. Do not leave a Critical/Important item open.

- [ ] **Step 3: Run final verification-before-completion gate**

Repeat Task 13 after all review changes. Re-read the design acceptance criteria, all 44 ledger rows, F01–F15, every later correction, and every user-required validation. Confirm no agent remains active and no required evidence is indirect or missing.

- [ ] **Step 4: Push and open the corrective PR**

```bash
git push -u origin audit/pr25-behavioral-audit
gh pr create \
  --base main \
  --head audit/pr25-behavioral-audit \
  --title "fix: complete PR 25 operational hardening" \
  --body-file /tmp/pr25-audit-pr-body.md
```

The PR body must contain the executive readiness verdict, behavior table, every defect/root cause/impact/fix/test/commit, disproportionate designs, corrected regressions, PR #26 preservation, exact validation and warning output, and independent-review verdict.

- [ ] **Step 5: Complete the persistent goal only after remote evidence**

Inspect `gh pr view` and `git status`; verify the branch is pushed and PR points to the exact reviewed HEAD. Only then call `update_goal(status="complete")` and provide the full evidence-based final response required by the original `/goal`.

## Self-review checklist

- Spec coverage: Tasks 1–11 implement all twelve correction units; Task 12 proves later-correction preservation; Task 13 covers every required static/scripts/admin/browser/safety/compatibility/warning gate; Task 14 covers independent review and delivery.
- Finding mapping: F01→Task 1; F02/F04/host F08→Task 2; F03/F11→Task 3; F09→Task 4; F10→Task 5; F05/F06→Task 6; F15→Task 7; F07→Task 8; F13→Task 9; admin F08→Task 10; F12→Task 11; F14 remains documented no-change.
- Type/API consistency: Task 8 defines `_await_thread_completion` before Task 9 consumes it; Task 7 defines `resolve_backup_archive` before actions/routes consume it; Task 6 passes `Mapping[str, KeyEntry]`; Task 10 uses the same `.backup.lock` as Tasks 1–2.
- Safety: every lifecycle test command is socket-free/no-`/opt`, followed by a read-only live health check; no installer/uninstaller/redeploy/restore test runs on the host.
- Scope: no dependency upgrade, speculative refactor, auth/CSRF redesign, background worker, cache, retry loop, or pre-PR #25 cleanup is included.
