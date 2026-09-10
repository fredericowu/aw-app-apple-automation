"""sim_tools.py + http_handler.py — tool/handler parity, the tap-refuses-to-
guess contract, the screenshot two-hop path, and the accessibility-tree
projector that is this app's actual point.

No real network or host: every test that would call out stubs
remote_host.exec / remote_host.download / axe_agent.ensure.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apple_automation_app.mcp import axe_agent, http_handler, remote_host, sim_tools  # noqa: E402

_FULL_CFG = {
    "remote_backend_url": "https://api.aw.tekflox.com",
    "remote_workspace": "aw",
    "remote_token": "awlk_test",
    "remote_host_id": "824decc7e0610089",
    "axe_version": "1.8.0",
    "axe_dir": "~/.aw-apple-automation",
    "default_udid": "",
    "screenshot_dir": ".tmp/apple-automation/",
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sim_tools.set_config_resolver(lambda: dict(_FULL_CFG))
    remote_host.set_config_resolver(lambda: dict(_FULL_CFG))
    axe_agent._ENSURED.clear()
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
    yield
    sim_tools.set_config_resolver(lambda: {})
    remote_host.set_config_resolver(lambda: {})
    axe_agent._ENSURED.clear()


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _stub_axe_ensured(monkeypatch):
    axe_agent._ENSURED["default"] = "1.8.0"


# ── Manifest / schema parity ────────────────────────────────────────────────

def test_all_16_tools_have_a_handler_and_vice_versa():
    names = {t["name"] for t in sim_tools.TOOLS}
    assert len(names) == 16
    assert names == set(sim_tools.HANDLERS)


def test_manifest_advertises_exactly_the_tools_that_exist():
    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "aw-app.json").read_text())
    advertised = set(manifest["contributes"]["mcp"]["provides"])
    assert advertised == {t["name"] for t in sim_tools.TOOLS}


def test_every_tool_accepts_a_per_call_host_override():
    for tool in sim_tools.TOOLS:
        assert "remote_host_id" in tool["inputSchema"]["properties"], tool["name"]


def test_every_tool_is_described_for_an_agent_that_has_never_seen_it():
    for tool in sim_tools.TOOLS:
        assert len(tool["description"]) > 60, tool["name"]


def test_required_arguments_are_declared_as_properties():
    for tool in sim_tools.TOOLS:
        schema = tool["inputSchema"]
        assert set(schema["required"]) <= set(schema["properties"]), tool["name"]


# ── sim_tap: refuses to guess a coordinate ──────────────────────────────────

def test_tap_requires_identifier_label_or_both_coordinates(monkeypatch):
    _stub_axe_ensured(monkeypatch)
    with pytest.raises(ValueError, match="needs identifier, label"):
        sim_tools._handle_sim_tap({})


def test_tap_by_identifier_wins_over_label(monkeypatch):
    _stub_axe_ensured(monkeypatch)
    captured = {}

    def fake_exec(cmd, timeout=30, host_override=None):
        captured["cmd"] = cmd
        return ("ok", "", 0)

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    sim_tools._handle_sim_tap({"identifier": "loginButton", "label": "Log In"})
    assert "--id loginButton" in captured["cmd"]
    assert "--label" not in captured["cmd"]


def test_tap_falls_back_to_coordinates_only_when_both_given(monkeypatch):
    _stub_axe_ensured(monkeypatch)
    captured = {}

    def fake_exec(cmd, timeout=30, host_override=None):
        captured["cmd"] = cmd
        return ("ok", "", 0)

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    sim_tools._handle_sim_tap({"x": 100, "y": 200})
    assert "-x 100 -y 200" in captured["cmd"]


def test_tap_without_provisioned_axe_names_sim_provision(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=20, host_override=None: ("/Users/aw", "", 0)
                        if "echo $HOME" in cmd else ("", "not found", 1))
    with pytest.raises(RuntimeError, match="sim_provision"):
        sim_tools._handle_sim_tap({"identifier": "x"})


# ── sim_boot / sim_shutdown idempotence ─────────────────────────────────────

def test_boot_reports_already_booted_instead_of_an_error(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=60, host_override=None:
                        ("", "Unable to boot device in current state: Booted", 149))
    result = sim_tools._handle_sim_boot({"udid": "ABCD"})
    assert "Already booted" in result


def test_shutdown_reports_already_shut_down_instead_of_an_error(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=30, host_override=None:
                        ("", "Unable to shutdown device in current state: Shutdown", 149))
    result = sim_tools._handle_sim_shutdown({"udid": "ABCD"})
    assert "Already shut down" in result


def test_udid_defaults_to_booted_when_nothing_configured(monkeypatch):
    captured = {}

    def fake_exec(cmd, timeout=60, host_override=None):
        captured["cmd"] = cmd
        return ("", "", 0)

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    sim_tools._handle_sim_boot({})
    assert "boot booted" in captured["cmd"]


# ── sim_screenshot: stage-and-download, never through exec stdout ──────────

def test_screenshot_stages_on_remote_disk_then_downloads_never_via_exec_stdout(monkeypatch, tmp_path):
    exec_calls = []
    download_calls = []

    def fake_exec(cmd, timeout=30, host_override=None):
        exec_calls.append(cmd)
        return ("", "", 0)

    def fake_download(remote_path, local_path, host_override=None):
        download_calls.append((remote_path, local_path))
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"\x89PNG-fake-bytes")
        return len(b"\x89PNG-fake-bytes")

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    monkeypatch.setattr(remote_host, "download", fake_download)
    monkeypatch.setattr(remote_host, "expanduser", lambda p, host_override=None: p.replace("~", "/Users/aw"))

    out_path = str(tmp_path / "shot.png")
    result = sim_tools._handle_sim_screenshot({"local_path": out_path})

    assert "Saved screenshot" in result
    assert os.path.isfile(out_path)
    assert any("simctl io" in c and "screenshot" in c for c in exec_calls)
    assert not any("base64" in c for c in exec_calls)
    assert len(download_calls) == 1
    assert any(cmd.startswith("rm -f") for cmd in exec_calls)


def test_screenshot_raises_loudly_on_zero_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_host, "exec", lambda cmd, timeout=30, host_override=None: ("", "", 0))
    monkeypatch.setattr(remote_host, "download", lambda *a, **k: 0)
    monkeypatch.setattr(remote_host, "expanduser", lambda p, host_override=None: p.replace("~", "/Users/aw"))
    with pytest.raises(RuntimeError, match="empty"):
        sim_tools._handle_sim_screenshot({"local_path": str(tmp_path / "shot.png")})


# ── sim_describe_ui: the projection contract ────────────────────────────────

_SAMPLE_TREE = {
    "type": "Application", "AXLabel": "Safari", "AXUniqueId": None,
    "role": "AXApplication", "enabled": True, "traits": [],
    "frame": {"x": 0, "y": 0, "width": 402, "height": 874},
    "children": [
        {
            "type": "Button", "AXLabel": None, "AXUniqueId": "loginButton",
            "role": "AXButton", "enabled": True, "traits": ["button"],
            "frame": {"x": 10, "y": 20, "width": 100, "height": 40},
            "children": [],
        },
        {
            "type": "Group", "AXLabel": None, "AXUniqueId": None,
            "role": "AXGroup", "enabled": True, "traits": [],
            "frame": {"x": 0, "y": 0, "width": 1, "height": 1},
            "children": [
                {
                    "type": "StaticText", "AXLabel": "Welcome", "AXUniqueId": None,
                    "role": "AXStaticText", "enabled": True, "traits": [],
                    "frame": {"x": 5, "y": 5, "width": 50, "height": 20},
                    "children": [],
                },
            ],
        },
    ],
}


def _run_projector(tree: dict, role: str = "", contains: str = "") -> dict:
    """Exercises the ACTUAL projector source the app base64's onto the host —
    not a reimplementation — via a real python3 subprocess, the same way it
    runs there."""
    script = sim_tools._PROJECTOR_SRC
    proc = subprocess.run(
        [sys.executable, "-c", script, role, contains],
        input=json.dumps([tree]), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_projector_keeps_only_elements_with_a_label_or_identifier():
    result = _run_projector(_SAMPLE_TREE)
    assert result["count"] == 3  # Safari (label), loginButton (id), Welcome (label)
    idents = {e["identifier"] for e in result["elements"] if e["identifier"]}
    labels = {e["label"] for e in result["elements"] if e["label"]}
    assert "loginButton" in idents
    assert {"Safari", "Welcome"} <= labels


def test_projector_drops_the_unlabeled_scaffolding_group():
    result = _run_projector(_SAMPLE_TREE)
    assert not any(e["role"] == "AXGroup" for e in result["elements"])


def test_projector_role_filter_narrows_the_list():
    result = _run_projector(_SAMPLE_TREE, role="Button")
    assert result["count"] == 1
    assert result["elements"][0]["identifier"] == "loginButton"


def test_projector_contains_filter_matches_label_or_identifier():
    result = _run_projector(_SAMPLE_TREE, contains="welcome")
    assert result["count"] == 1
    assert result["elements"][0]["label"] == "Welcome"


def test_projector_payload_is_base64_transportable():
    encoded = sim_tools.projector_b64()
    decoded = base64.b64decode(encoded).decode("utf-8")
    assert decoded == sim_tools._PROJECTOR_SRC


def test_describe_ui_end_to_end_via_stubbed_exec(monkeypatch):
    _stub_axe_ensured(monkeypatch)
    calls = []

    def fake_exec(cmd, timeout=45, host_override=None):
        calls.append(cmd)
        if "python3" in cmd:
            payload = _run_projector(_SAMPLE_TREE)
            return (json.dumps(payload), "", 0)
        return ("", "", 0)

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    result = json.loads(sim_tools._handle_sim_describe_ui({}))
    assert result["count"] == 3
    main_cmd = next(c for c in calls if "python3" in c)
    assert "base64 -d" in main_cmd
    assert any(c.startswith("rm -f") for c in calls)  # tmp projector cleaned up


def test_describe_ui_raises_on_non_json_output(monkeypatch):
    _stub_axe_ensured(monkeypatch)
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=45, host_override=None: ("not json", "", 0))
    with pytest.raises(RuntimeError, match="non-JSON"):
        sim_tools._handle_sim_describe_ui({})


def test_describe_ui_without_provisioned_axe_names_sim_provision(monkeypatch):
    monkeypatch.setattr(remote_host, "exec",
                        lambda cmd, timeout=20, host_override=None: ("/Users/aw", "", 0)
                        if "echo $HOME" in cmd else ("", "not found", 1))
    with pytest.raises(RuntimeError, match="sim_provision"):
        sim_tools._handle_sim_describe_ui({})


# ── http_handler: typed errors surface as tool errors, not crashes ─────────

def test_not_configured_surfaces_as_a_tool_error_not_a_crash():
    sim_tools.set_config_resolver(lambda: {})
    remote_host.set_config_resolver(lambda: {})
    resp = _run(http_handler.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sim_list", "arguments": {}}}))
    assert resp["result"]["isError"] is True
    assert "missing" in resp["result"]["content"][0]["text"]


def test_host_offline_surfaces_as_a_tool_error_naming_the_host(monkeypatch):
    def fake_exec(cmd, timeout=20, host_override=None):
        raise remote_host.HostOffline("host 824decc7e0610089 is not connected to aw-backend right now")

    monkeypatch.setattr(remote_host, "exec", fake_exec)
    resp = _run(http_handler.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "sim_list", "arguments": {}}}))
    assert resp["result"]["isError"] is True
    assert "824decc7e0610089" in resp["result"]["content"][0]["text"]
    assert "not connected" in resp["result"]["content"][0]["text"]


def test_unknown_tool_is_an_error_not_a_crash():
    resp = _run(http_handler.handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}}}))
    assert resp["result"]["isError"] is True
    assert "Unknown tool" in resp["result"]["content"][0]["text"]


def test_initialize_and_tools_list():
    init = _run(http_handler.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"}))
    assert init["result"]["serverInfo"]["name"] == "aw-apple-automation"
    listed = _run(http_handler.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    assert len(listed["result"]["tools"]) == 16


def test_initialized_notification_gets_no_response():
    assert _run(http_handler.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


def test_unknown_method_is_a_jsonrpc_error():
    resp = _run(http_handler.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}))
    assert resp["error"]["code"] == -32601


def test_mcp_config_names_the_server(tmp_path):
    from apple_automation_app import mcp_config
    doc = mcp_config.write_mcp_json(str(tmp_path), 9030)
    entry = doc["mcpServers"]["aw-apple-automation"]
    assert entry["type"] == "http"
    assert entry["url"].endswith(":9030/api/apps/apple-automation/mcp")


def test_mcp_json_write_is_skipped_when_unchanged(tmp_path):
    from apple_automation_app import mcp_config
    mcp_config.write_mcp_json(str(tmp_path), 9030)
    before = (tmp_path / "mcp.json").stat().st_mtime_ns
    mcp_config.write_mcp_json(str(tmp_path), 9030)
    assert (tmp_path / "mcp.json").stat().st_mtime_ns == before
    mcp_config.write_mcp_json(str(tmp_path), 9999)
    assert (tmp_path / "mcp.json").stat().st_mtime_ns != before
