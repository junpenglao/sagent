import asyncio
import pytest
from pathlib import Path
from sagent.testing import with_fake_agent

# Add plugin root to path so we can import sandboxed_tools
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import sandboxed_tools

def _run(coro):
    return asyncio.run(coro)

# --- SandboxedWrite ---

def test_sandboxed_write_rejects_outside(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside.txt"
    tool = sandboxed_tools.SandboxedWrite(sandbox_root=sandbox)
    with with_fake_agent():
        result = _run(tool.run({"file_path": str(outside), "content": "hi"}))
    assert result.is_error
    assert "outside the statistician sandbox" in result.content

def test_sandboxed_write_accepts_inside(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target = sandbox / "test.txt"
    tool = sandboxed_tools.SandboxedWrite(sandbox_root=sandbox)
    with with_fake_agent():
        result = _run(tool.run({"file_path": str(target), "content": "ok"}))
    assert not result.is_error
    assert target.read_text() == "ok"

# --- SandboxedBash ---

@pytest.mark.parametrize("command,expected_blocked", [
    ("ls", False),
    ("git status", False),
    ("rm -rf /tmp/foo", False),
    ("rm -rf /tmp/foo /tmp/bar", False),
    ("rm -rf /tmp/foo; ls", False),
    
    ("git reset --hard", True),
    ("git reset --hard HEAD", True),
    ("git push origin main", True),
    ("git push origin master", True),
    ("git push --force origin feat", True),
    ("git push -f origin feat", True),
    ("git clean -fd", True),
    ("sudo ls", True),
    (":(){ :|:& };:", True),
    ("dd if=/dev/zero of=/dev/sda", True),
    ("echo x > /dev/sda", True),
    ("echo x > /dev/nvme0n1", True),
    
    ("rm -rf .venv", True),
    ("rm -rf /home/jp/project", True),
    ("rm -rf /tmp/foo /home/jp/bad", True),
    ("rm -r -f /etc", True),
    ("rm -fr /etc", True),
])
def test_sandboxed_bash_denylist(command, expected_blocked):
    tool = sandboxed_tools.SandboxedBash()
    with with_fake_agent():
        result = _run(tool.run({"command": command}))
    if expected_blocked:
        assert result.is_error
        assert "Refused" in result.content
    else:
        # If not blocked, it will try to run and probably fail because of missing binaries or paths in fake env,
        # but it shouldn't be blocked by our sandbox logic.
        # We check that it doesn't contain our "Refused" message.
        assert "Refused" not in result.content
