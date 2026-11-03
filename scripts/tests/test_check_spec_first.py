import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_spec_first.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def commit(repo, *paths):
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_text() if target.exists() else ""
        target.write_text(previous + "change\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")


def check(repo):
    return subprocess.run([BASH, str(SCRIPT)], cwd=repo, capture_output=True, text=True).returncode


@pytest.fixture
def template_repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    commit(tmp_path, "SPEC.md", "mcp-server/README.md", "eval/harness/README.md", "README.md")
    return tmp_path


def test_template_alone_passes(template_repo):
    assert check(template_repo) == 0


def test_spec_then_implementation_passes(template_repo):
    commit(template_repo, "SPEC.md")
    commit(template_repo, "mcp-server/server.py")
    commit(template_repo, "agent/main.py")
    assert check(template_repo) == 0


def test_implementation_without_spec_fails(template_repo):
    commit(template_repo, "agent/main.py")
    assert check(template_repo) == 1


def test_implementation_before_spec_fails(template_repo):
    commit(template_repo, "eval/harness/run.py")
    commit(template_repo, "SPEC.md")
    assert check(template_repo) == 1


def test_spec_and_implementation_in_one_commit_fails(template_repo):
    commit(template_repo, "SPEC.md", "apps/view.html")
    assert check(template_repo) == 1


def test_editing_a_stub_readme_before_spec_passes(template_repo):
    commit(template_repo, "mcp-server/README.md")
    commit(template_repo, "SPEC.md")
    assert check(template_repo) == 0


def test_other_files_before_spec_pass(template_repo):
    commit(template_repo, "README.md")
    commit(template_repo, "SPEC.md")
    commit(template_repo, "host/index.html")
    assert check(template_repo) == 0
