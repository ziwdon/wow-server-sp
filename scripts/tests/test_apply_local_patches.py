"""apply-local-patches.sh and lib/upstream-versions.sh against real git repos.

Needs `git` in the sandbox (see CLAUDE.md test command). No network, no Docker.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path("/src") if Path("/src/apply-local-patches.sh").is_file() else Path(__file__).resolve().parents[1]
APPLY = SCRIPTS_DIR / "apply-local-patches.sh"
LIB = SCRIPTS_DIR / "lib" / "upstream-versions.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed in the sandbox")

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
PATCH = """\
diff --git a/src/hook.cpp b/src/hook.cpp
--- a/src/hook.cpp
+++ b/src/hook.cpp
@@ -1,2 +1,3 @@
 int a;
+int patched;
 int b;
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def _source_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src-root"
    module = src / "modules" / "mod-playerbots"
    (module / "src").mkdir(parents=True)
    (module / "src" / "hook.cpp").write_text("int a;\nint b;\n")
    _git("init", "-q", cwd=module)
    _git("add", ".", cwd=module)
    _git("commit", "-qm", "base", cwd=module)
    return src


def _patches(tmp_path: Path, body: str = PATCH) -> Path:
    patches = tmp_path / "patches" / "mod-playerbots"
    patches.mkdir(parents=True)
    (patches / "0001-test.patch").write_text(body)
    return tmp_path / "patches"


def _apply(src: Path, patches: Path, *mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(APPLY), str(src), *mode], env={**GIT_ENV, "PATCH_DIR": str(patches)},
                          capture_output=True, text=True, timeout=30)


def test_apply_is_idempotent_and_reverse_restores_the_tree(tmp_path):
    src, patches = _source_tree(tmp_path), _patches(tmp_path)
    hook = src / "modules" / "mod-playerbots" / "src" / "hook.cpp"
    assert _apply(src, patches, "--check").returncode == 1
    assert _apply(src, patches).returncode == 0
    assert "int patched;" in hook.read_text()
    again = _apply(src, patches)
    assert again.returncode == 0 and "already applied" in again.stdout
    assert hook.read_text().count("int patched;") == 1
    assert _apply(src, patches, "--check").returncode == 0
    assert _apply(src, patches, "--reverse").returncode == 0
    assert hook.read_text() == "int a;\nint b;\n"


def test_a_patch_that_no_longer_fits_upstream_fails_loudly(tmp_path):
    src, patches = _source_tree(tmp_path), _patches(tmp_path)
    (src / "modules" / "mod-playerbots" / "src" / "hook.cpp").write_text("int changed_upstream;\n")
    r = _apply(src, patches)
    assert r.returncode == 1
    assert "refresh the patch" in r.stderr


def test_missing_module_checkout_is_an_error(tmp_path):
    patches = _patches(tmp_path)
    (tmp_path / "empty").mkdir()
    r = _apply(tmp_path / "empty", patches)
    assert r.returncode == 1
    assert "is not a git checkout" in r.stderr


def _lib(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", f"set -euo pipefail; source {LIB}; {script}"], env=GIT_ENV,
                          capture_output=True, text=True, timeout=60)


VERSIONS = """\
# comment
AC_CORE_REPO=https://example.invalid/core.git
AC_CORE_BRANCH=Playerbot
AC_CORE_REV={rev}
MOD_PLAYERBOTS_REPO=https://example.invalid/pb.git
MOD_PLAYERBOTS_BRANCH=master
MOD_PLAYERBOTS_REV={rev}
MOD_AHBOT_REPO=https://example.invalid/ah.git
MOD_AHBOT_BRANCH=master
MOD_AHBOT_REV={rev}
MOD_IP_REPO=https://example.invalid/ip.git
MOD_IP_BRANCH=master
MOD_IP_REV={rev}
CLIENT_DATA_VERSION=v20.0
"""


def test_versions_file_is_parsed_not_sourced(tmp_path):
    good = tmp_path / "v.env"
    good.write_text(VERSIONS.format(rev="a" * 40))
    r = _lib(f"load_upstream_versions {good}; upstream_repo_specs")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[1] == f"mod-playerbots|modules/mod-playerbots|https://example.invalid/pb.git|master|{'a' * 40}"
    for bad in ("AC_CORE_REV=abc123", "EVIL=$(touch /tmp/pwned)", "AC_CORE_REV=" + "a" * 40 + "; rm -rf /"):
        f = tmp_path / "bad.env"
        f.write_text(VERSIONS.format(rev="a" * 40) + bad + "\n")
        assert _lib(f"load_upstream_versions {f}").returncode == 1, bad


def test_checkout_pinned_rev_moves_the_branch_and_refuses_to_clobber_edits(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "master", cwd=upstream)
    (upstream / "f").write_text("one\n")
    _git("add", ".", cwd=upstream)
    _git("commit", "-qm", "one", cwd=upstream)
    first = _git("rev-parse", "HEAD", cwd=upstream)
    (upstream / "f").write_text("two\n")
    _git("commit", "-qam", "two", cwd=upstream)
    second = _git("rev-parse", "HEAD", cwd=upstream)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=upstream)

    clone = tmp_path / "clone"
    r = _lib(f"checkout_pinned_rev {clone} file://{upstream} master {first}")
    assert r.returncode == 0, r.stderr
    assert (clone / "f").read_text() == "one\n"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone) == "master"
    assert _lib(f"checkout_pinned_rev {clone} file://{upstream} master {second}").returncode == 0
    assert (clone / "f").read_text() == "two\n"
    (clone / "f").write_text("local edit\n")
    assert _lib(f"checkout_pinned_rev {clone} file://{upstream} master {first}").returncode != 0
    assert (clone / "f").read_text() == "local edit\n"
