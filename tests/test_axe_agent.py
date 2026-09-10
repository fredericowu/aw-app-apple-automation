"""axe_agent.py — pinned-release lookup, the version-keyed idempotence
check, and the ensure/provision split (a Layer-2 tool must never silently
install; that's sim_provision's job alone).

No real network or host: every test stubs remote_host.exec.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apple_automation_app.mcp import axe_agent, remote_host  # noqa: E402

# axe_agent's own functions never touch remote_backend_url/workspace/token —
# only remote_host.exec, stubbed per-test below — but remote_host.home()
# (reached via expanduser -> axe_bin) calls _cfg() first, which needs a
# fully "configured" resolver or it raises NotConfigured before the stub
# is ever reached. Mirrors test_remote_host.py's own _FULL fixture value.
_FULL = {
    "remote_backend_url": "https://api.aw.tekflox.com",
    "remote_workspace": "aw",
    "remote_token": "awlk_test",
    "remote_host_id": "824decc7e0610089",
}


@pytest.fixture(autouse=True)
def _reset():
    axe_agent._ENSURED.clear()
    remote_host.set_config_resolver(lambda: dict(_FULL))
    remote_host._HOME_CACHE.clear()
    yield
    axe_agent._ENSURED.clear()
    remote_host.set_config_resolver(lambda: {})


def test_the_pinned_default_version_has_a_release_entry():
    release = axe_agent.release_for(axe_agent.DEFAULT_AXE_VERSION)
    assert release["url"].endswith(".tar.gz")
    assert len(release["sha256"]) == 64


def test_an_unpinned_version_is_a_clear_error_not_a_blind_download():
    with pytest.raises(ValueError, match="no pinned release"):
        axe_agent.release_for("999.0.0")


def test_axe_bin_is_version_keyed(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=20, host_override=None: ("/Users/aw", "", 0))
    path = axe_agent.axe_bin("1.8.0", "~/.aw-apple-automation")
    assert path == "/Users/aw/.aw-apple-automation/axe-1.8.0/axe"


def test_status_reports_installed_only_when_the_reported_version_matches(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=20, host_override=None: ("/Users/aw", "", 0)
                        if "echo $HOME" in cmd else ("1.8.0", "", 0))
    result = axe_agent.status("1.8.0", "~/.aw-apple-automation")
    assert result["installed"] is True
    assert result["reported_version"] == "1.8.0"


def test_status_is_not_installed_on_a_nonzero_exit(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=20, host_override=None: ("/Users/aw", "", 0)
                        if "echo $HOME" in cmd else ("", "command not found", 127))
    result = axe_agent.status("1.8.0", "~/.aw-apple-automation")
    assert result["installed"] is False
    assert "command not found" in result["error"]


def test_provision_skips_the_download_when_already_installed(monkeypatch):
    calls = []

    def fake_exec(cmd, timeout=20, host_override=None):
        calls.append(cmd)
        if "echo $HOME" in cmd:
            return ("/Users/aw", "", 0)
        if "--version" in cmd:
            return ("1.8.0", "", 0)
        raise AssertionError(f"provision should not have run: {cmd}")

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    result = axe_agent.provision("1.8.0", "~/.aw-apple-automation")
    assert result["downloaded"] is False
    assert not any("curl" in c for c in calls)


def test_provision_downloads_and_verifies_sha256_when_missing(monkeypatch):
    calls = []
    version_check_calls = {"n": 0}

    def fake_exec(cmd, timeout=20, host_override=None):
        calls.append(cmd)
        if "echo $HOME" in cmd:
            return ("/Users/aw", "", 0)
        if "curl" in cmd:
            assert "shasum -a 256 -c -" in cmd
            assert axe_agent._RELEASES["1.8.0"]["sha256"] in cmd
            return ("", "", 0)
        if "--version" in cmd:
            version_check_calls["n"] += 1
            # First check (pre-install): not installed. Second (post-install): confirmed.
            return ("", "no such file", 1) if version_check_calls["n"] == 1 else ("1.8.0", "", 0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    result = axe_agent.provision("1.8.0", "~/.aw-apple-automation")
    assert result["downloaded"] is True
    assert result["installed"] is True


def test_provision_raises_if_the_post_install_check_still_fails(monkeypatch):
    def fake_exec(cmd, timeout=20, host_override=None):
        if "echo $HOME" in cmd:
            return ("/Users/aw", "", 0)
        if "curl" in cmd:
            return ("", "", 0)
        return ("", "still missing", 1)

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    with pytest.raises(RuntimeError, match="does not confirm"):
        axe_agent.provision("1.8.0", "~/.aw-apple-automation")


def test_ensure_raises_an_actionable_error_when_not_installed(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=20, host_override=None: ("/Users/aw", "", 0)
                        if "echo $HOME" in cmd else ("", "not found", 1))
    with pytest.raises(RuntimeError, match="sim_provision"):
        axe_agent.ensure("1.8.0", "~/.aw-apple-automation")


def test_ensure_does_not_raise_once_verified_and_cached(monkeypatch):
    calls = {"n": 0}

    def fake_exec(cmd, timeout=20, host_override=None):
        if "echo $HOME" in cmd:
            return ("/Users/aw", "", 0)
        calls["n"] += 1
        return ("1.8.0", "", 0)

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    axe_agent.ensure("1.8.0", "~/.aw-apple-automation")
    axe_agent.ensure("1.8.0", "~/.aw-apple-automation")
    assert calls["n"] == 1  # cached after the first verify
