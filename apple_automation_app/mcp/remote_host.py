"""Drive a remote macOS host's `xcrun simctl` / AXe through **aw-remote-hosts**.

Structurally a copy of aw-app-android-studio's own `mcp/remote_host.py`
(itself copied from aw-app-crispal's pattern for the same problem — the
monolith's dead remote-agent bridge does not exist in this split
deployment):

    {remote_backend_url}/api/workspaces/{remote_workspace}/remote-hosts/{remote_host_id}/exec
    {remote_backend_url}/api/workspaces/{remote_workspace}/remote-hosts/{remote_host_id}/fs/upload
    {remote_backend_url}/api/workspaces/{remote_workspace}/remote-hosts/{remote_host_id}/fs/download
    Authorization: Bearer {remote_token}

Deliberately a small stdlib client rather than importing another app's code —
that package lives in a DIFFERENT app's container/process and is not
importable here.

Config comes through a resolver installed by the plugin (``ctx.config`` for
the plain fields, ``ctx.secrets`` for the token) rather than ``os.environ`` —
this app is Tier-1 in-process, so there is no container env injection step.
A value saved in Settings takes effect on the very next call.

Every entry point below accepts an optional ``host_override`` so a caller can
target a different linked host for one call without touching the app's
configured default.

**New here, not in android-studio**: :func:`_call` maps aw-backend's HTTP
status codes to typed exceptions instead of flattening everything into one
opaque ``RuntimeError``. aw-backend's exec route
(``repos/aw-backend/src/api/routes/host_link.py``) already returns a fast
409 for a disconnected host — no hang, no timeout — so representing that as
a distinct :class:`HostOffline` is one ``except`` clause, not a pre-flight
probe (which would double every call's round trips and still be racy).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable


class NotConfigured(RuntimeError):
    """Raised when remote_backend_url / remote_workspace / remote_token /
    remote_host_id are not all present (after any per-call override).

    Explicit rather than falling back to a default URL: a wrong-but-plausible
    default is exactly what made the old remote-agent bridge fail silently
    for so long.
    """


class HostOffline(RuntimeError):
    """The linked host is not currently connected to aw-backend (409).

    A fast, explicit failure — not a hang. Naming the host and the tool is
    the whole point: this is the resilience criterion the Product Owner
    made part of the card's acceptance, not an afterthought.
    """


class HostNotFound(RuntimeError):
    """No such remote_host_id is registered for this workspace (404) — a
    different failure than "offline" with a different fix (check the id)."""


class HostCommandFailed(RuntimeError):
    """The command itself timed out or failed on aw-backend's side (502)."""


_config_resolver: Callable[[], dict] = lambda: {}


def set_config_resolver(resolver: Callable[[], dict]) -> None:
    """Installed once from ``plugin.activate``; every request below reads
    through it, so a Settings save takes effect on the next tool call with
    no restart."""
    global _config_resolver
    _config_resolver = resolver


def _cfg(host_override: str | None = None) -> tuple[str, str, str, str]:
    try:
        values = _config_resolver() or {}
    except Exception:
        values = {}
    base = (values.get("remote_backend_url") or "").rstrip("/")
    workspace = values.get("remote_workspace") or ""
    token = values.get("remote_token") or ""
    host_id = host_override or values.get("remote_host_id") or ""
    missing = [name for name, value in (
        ("remote_backend_url", base), ("remote_workspace", workspace),
        ("remote_token", token), ("remote_host_id", host_id)) if not value]
    if missing:
        raise NotConfigured(
            "aw-remote-hosts is not configured — missing " + ", ".join(missing)
            + ". Open the Apple Automation app's Settings and fill them in "
            "(remote_host_id can also be passed per-call).")
    return base, workspace, token, host_id


def configured(host_override: str | None = None) -> bool:
    try:
        _cfg(host_override)
        return True
    except NotConfigured:
        return False


def missing_settings(host_override: str | None = None) -> str:
    """Which settings are absent, for an error message.

    Empty string when everything is present.
    """
    try:
        _cfg(host_override)
        return ""
    except NotConfigured as e:
        return str(e)


def _url(path: str, params: dict | None = None, host_override: str | None = None) -> str:
    base, workspace, _token, host_id = _cfg(host_override)
    url = f"{base}/api/workspaces/{workspace}/remote-hosts/{host_id}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def _call(method: str, path: str, *, body: dict | None = None,
          params: dict | None = None, timeout: float = 60.0,
          host_override: str | None = None) -> dict:
    _base, _ws, token, host_id = _cfg(host_override)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _url(path, params, host_override), data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        if e.code == 409:
            raise HostOffline(
                f"host {host_id} is not connected to aw-backend right now, "
                f"so {method} {path} could not run. ({detail})") from e
        if e.code == 404:
            raise HostNotFound(
                f"no host {host_id!r} is registered for this workspace "
                f"({method} {path}): {detail}") from e
        if e.code == 502:
            raise HostCommandFailed(
                f"{method} {path} failed on host {host_id}: {detail}") from e
        raise RuntimeError(f"remote-host {method} {path} -> {e.code}: {detail}") from e
    return json.loads(raw) if raw.strip() else {}


def exec(command: str, timeout: int = 30, host_override: str | None = None) -> tuple[str, str, int]:
    """Run ``command`` on the linked host, blocking until it finishes.

    Returns ``(stdout, stderr, exit_code)``.
    """
    started = _call("POST", "/exec", body={"command": command, "timeout_s": timeout},
                    host_override=host_override)
    job_id = started.get("job_id")
    if not job_id:
        raise RuntimeError(f"remote-host exec did not return a job_id: {started}")
    result = _call("POST", f"/exec/{job_id}/wait", body={"timeout_s": timeout},
                   timeout=timeout + 30.0, host_override=host_override)
    return (result.get("stdout") or "", result.get("stderr") or "",
            int(result.get("exit_code") or 0))


_HOME_CACHE: dict[str, str] = {}


def home(host_override: str | None = None) -> str:
    """The linked host's real home dir, resolved once per host and cached."""
    _base, _ws, _token, host_id = _cfg(host_override)
    if host_id not in _HOME_CACHE:
        out, _err, code = exec("echo $HOME", timeout=20, host_override=host_override)
        _HOME_CACHE[host_id] = out.strip() if code == 0 and out.strip() else ""
    return _HOME_CACHE[host_id]


def expanduser(path: str, host_override: str | None = None) -> str:
    """Resolve a leading ``~`` against the REMOTE host's home.

    Needed because every path handed to simctl/AXe goes through
    ``shlex.quote``, and a shell does not expand ``~`` inside quotes —
    Python's own ``os.path.expanduser`` is no use either, since it would
    resolve against THIS workspace container's home, not the Mac's.
    """
    if not path.startswith("~"):
        return path
    resolved = home(host_override)
    return (resolved + path[1:]) if resolved else path


def upload(local_path: str, remote_path: str, timeout: float = 300.0,
           host_override: str | None = None) -> dict:
    """Stream a local file to ``remote_path`` on the linked host."""
    _base, _ws, token, _host = _cfg(host_override)
    with open(local_path, "rb") as f:
        payload = f.read()
    req = urllib.request.Request(
        _url("/fs/upload", {"path": remote_path}, host_override), data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/octet-stream",
                 "Content-Length": str(len(payload))})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"upload of {local_path} -> {remote_path} failed "
                           f"({e.code}): {detail}") from e
    return json.loads(raw) if raw.strip() else {}


def download(remote_path: str, local_path: str, timeout: float = 300.0,
             host_override: str | None = None) -> int:
    """Stream a file OFF the linked host into ``local_path``; returns bytes.

    Exists because the exec channel cannot carry a file: aw-remote-hosts caps
    a job's stdout at 1 MiB and reports exit_code -1 past it. Always stage a
    file on the remote host's own disk (via `simctl io screenshot` / `axe
    describe-ui --raw` redirected to a path) and pull it with this instead of
    trying to read its bytes through exec's stdout.
    """
    _base, _ws, token, _host = _cfg(host_override)
    req = urllib.request.Request(
        _url("/fs/download", {"path": remote_path}, host_override),
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"download of {remote_path} failed ({e.code}): {detail}") from e
    import os
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    tmp = local_path + ".part"
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, local_path)
    return len(payload)
