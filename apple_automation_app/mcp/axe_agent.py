"""Provision and locate **AXe** (github.com/cameroncooke/AXe) on the linked
macOS host.

AXe vendors idb's own Meta frameworks (FBControlCore / FBSimulatorControl /
FBDeviceControl / XCTestBootstrap) as a prebuilt universal tarball — the
thing idb itself cannot offer here, because the exec user `aw` has no
Homebrew bottle access (`/opt/homebrew` is owned by `fredericowu`) and a
`~/homebrew` user prefix means a from-source FBSimulatorControl build, the
exact ~90-minute wall the fastlane precedent hit.

**Pinned, not "latest".** A private-API tool that silently upgrades under
you is a debugging nightmare — see aw-app.json's `axe_version`. Bumping the
version means adding a new entry to :data:`_RELEASES` with its real sha256,
a deliberate act, not automatic.

**Idempotent by construction.** The install path is version-keyed
(``<axe_dir>/axe-<version>/``) — the path itself IS the idempotence check,
mirroring `windows_pilot_app`'s per-process `_ENSURED` cache
(`mcp/host_agent.py::ensure_agent`) but without that module's "upload a
file we own" step: AXe is a third-party release fetched directly by the
host via `curl`, verified with a pinned sha256 before it is trusted to run
on Frederico's own Mac.

Spike evidence (2026-09-10, Mac.Home 824decc7e0610089, user `aw`): the
release asset `AXe-macOS-v1.8.0-universal.tar.gz` (8,432,118 bytes)
extracts flat at its root — `axe`, `AXe_AXe.bundle/`, `Frameworks/` sit
directly under the tarball root with no wrapping directory — so provisioning
extracts straight into the version dir, no `--strip-components`.
"""
from __future__ import annotations

import logging
import shlex

from . import remote_host

log = logging.getLogger("aw_apps.apple-automation")

DEFAULT_AXE_VERSION = "1.8.0"
DEFAULT_AXE_DIR = "~/.aw-apple-automation"

# One entry per axe_version this app knows how to install. Extend this when
# bumping the pinned version in aw-app.json — never fetch an unpinned
# "latest", and never trust a download without checking it against a sha256
# recorded here ahead of time.
_RELEASES = {
    "1.8.0": {
        "url": "https://github.com/cameroncooke/AXe/releases/download/v1.8.0/AXe-macOS-v1.8.0-universal.tar.gz",
        "sha256": "7b76340b72e90d0f211bc7c4636f15009076eff07acef2f2b632b175debd8834",
    },
}

# Hosts (keyed by host_override, "default" for the configured one) this
# process has already confirmed have the right version installed. Per-process
# only — a workspace restart re-checks once, which costs one extra `axe
# --version` round trip and keeps a manually-deleted install from silently
# passing forever.
_ENSURED: dict[str, str] = {}


def known_versions() -> list[str]:
    return sorted(_RELEASES)


def release_for(version: str) -> dict:
    release = _RELEASES.get(version)
    if not release:
        raise ValueError(
            f"axe_version {version!r} has no pinned release/sha256 recorded in "
            f"this app — known versions: {', '.join(known_versions()) or '(none)'}. "
            f"Add an entry to axe_agent._RELEASES before using it.")
    return release


def axe_bin(version: str, axe_dir: str, host_override: str | None = None) -> str:
    root = axe_dir.rstrip("/")
    return remote_host.expanduser(f"{root}/axe-{version}/axe", host_override=host_override)


def status(version: str, axe_dir: str, host_override: str | None = None) -> dict:
    """Ask the host directly rather than trust a cache — a manually deleted
    or corrupted install must be visible, not papered over."""
    binp = axe_bin(version, axe_dir, host_override)
    out, err, code = remote_host.exec(f"{shlex.quote(binp)} --version",
                                      timeout=20, host_override=host_override)
    reported = out.strip() if code == 0 else None
    return {
        "installed": code == 0 and reported == version,
        "version": version,
        "path": binp,
        "reported_version": reported,
        "error": None if code == 0 else (err or out).strip() or f"exit {code}",
    }


def provision(version: str, axe_dir: str, host_override: str | None = None,
             force: bool = False) -> dict:
    """Install AXe on the host if it isn't already there. Safe to re-run."""
    release = release_for(version)
    key = host_override or "default"

    if not force:
        current = status(version, axe_dir, host_override)
        if current["installed"]:
            _ENSURED[key] = version
            return {**current, "downloaded": False}

    axe_root = remote_host.expanduser(axe_dir, host_override=host_override)
    version_dir = f"{axe_root}/axe-{version}"
    tarball = f"{version_dir}/axe.tar.gz"
    # One exec round trip: make the version dir, curl the pinned tarball
    # straight from GitHub (the Mac has internet egress — proven in the
    # feasibility spike), verify it against the pinned sha256 BEFORE
    # extracting, unpack flat (no wrapping dir in this release), drop the
    # tarball, and mark the binary executable.
    cmd = (
        f"mkdir -p {shlex.quote(version_dir)} && "
        f"curl -fsSL -o {shlex.quote(tarball)} {shlex.quote(release['url'])} && "
        f"echo {shlex.quote(release['sha256'] + '  ' + tarball)} | shasum -a 256 -c - && "
        f"tar xzf {shlex.quote(tarball)} -C {shlex.quote(version_dir)} && "
        f"rm -f {shlex.quote(tarball)} && "
        f"chmod +x {shlex.quote(version_dir)}/axe"
    )
    out, err, code = remote_host.exec(cmd, timeout=180, host_override=host_override)
    if code != 0:
        raise RuntimeError(
            f"AXe {version} install failed on host (exit {code}): {(err or out)[:800]}")

    verified = status(version, axe_dir, host_override)
    if not verified["installed"]:
        raise RuntimeError(
            f"AXe {version} was downloaded but `axe --version` does not confirm "
            f"it afterwards: {verified}")
    _ENSURED[key] = version
    return {**verified, "downloaded": True}


def ensure(version: str, axe_dir: str, host_override: str | None = None) -> None:
    """Raise a clear, actionable error if AXe isn't provisioned — a Layer-2
    tool must never silently attempt an install mid-call. Installing is
    `sim_provision`'s job, explicitly invoked once."""
    key = host_override or "default"
    if _ENSURED.get(key) == version:
        return
    current = status(version, axe_dir, host_override)
    if current["installed"]:
        _ENSURED[key] = version
        return
    raise RuntimeError(
        f"AXe {version} is not installed on host {host_override or '(default)'} — "
        f"call sim_provision once. ({current.get('error') or 'not found'})")
