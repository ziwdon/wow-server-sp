from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dockerfile_excludes_test_only_dependencies():
    df = (REPO_ROOT / "Dockerfile").read_text()

    assert "pip install --no-cache-dir -r requirements.txt" in df
    assert "requirements-dev.txt" not in df
    assert "pytest" not in df
    assert "httpx" not in df


PROTON_DRIVE_CLI_SHA512 = (
    "3533025ba69ae112b64e3e01fbcc1ad0688136a4043f6cf6a72886967d85fdcd"
    "9ec235479c2e113171614be5225bfba93427509a05ca5aa6071d924fa7e91ca8"
)


def _run_instructions(dockerfile: str) -> list[str]:
    """Each RUN instruction with its backslash continuations joined."""
    joined = dockerfile.replace("\\\n", " ")
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


def test_dockerfile_pins_and_verifies_proton_drive_cli():
    df = (REPO_ROOT / "Dockerfile").read_text()

    assert "PROTON_DRIVE_CLI_VERSION=0.9.0" in df
    assert PROTON_DRIVE_CLI_SHA512 in df
    assert "sha512sum -c" in df and "/usr/local/bin/proton-drive" in df

    # Download and checksum are one RUN joined by &&, so a mismatch fails the
    # build and nothing unverified is ever installed or chmod'ed executable.
    install = next(r for r in _run_instructions(df) if "/usr/local/bin/proton-drive" in r)
    assert "curl -fsSL" in install
    assert "https://proton.me/download/drive/cli/${PROTON_DRIVE_CLI_VERSION}/linux-x64/proton-drive" in install
    assert install.index("curl") < install.index("sha512sum -c") < install.index("chmod 0755")
    flat = " ".join(install.split())
    assert "&& echo" in flat and "&& chmod 0755" in flat
    assert 'echo "${PROTON_DRIVE_CLI_SHA512}  /usr/local/bin/proton-drive" | sha512sum -c -' in install
    # Bumping the CLI is a deliberate two-value edit: both pins are build args
    # with defaults, and the hash is not duplicated anywhere else.
    assert "ARG PROTON_DRIVE_CLI_VERSION=0.9.0" in df
    assert f"ARG PROTON_DRIVE_CLI_SHA512={PROTON_DRIVE_CLI_SHA512}" in df
    assert df.count(PROTON_DRIVE_CLI_SHA512) == 1


def test_dockerfile_installs_vault_tools_in_the_base_apt_layer():
    df = (REPO_ROOT / "Dockerfile").read_text()

    base = next(r for r in _run_instructions(df) if "ca-certificates" in r)
    packages = base.split("apt-get install -y --no-install-recommends", 1)[1].split("&&", 1)[0].split()
    for pkg in ("gnupg", "pass", "clevis", "clevis-tpm2"):
        assert pkg in packages, f"{pkg} missing from the base apt-get install"
    assert "--no-install-recommends" in base
