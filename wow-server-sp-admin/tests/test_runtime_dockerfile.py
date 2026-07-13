from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dockerfile_excludes_test_only_dependencies():
    df = (REPO_ROOT / "Dockerfile").read_text()

    assert "pip install --no-cache-dir -r requirements.txt" in df
    assert "requirements-dev.txt" not in df
    assert "pytest" not in df
    assert "httpx" not in df
