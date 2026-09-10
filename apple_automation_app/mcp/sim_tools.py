"""The 16 sim_* tools: Layer 1 (`xcrun simctl`, zero-install) and Layer 2
(AXe, needs `sim_provision`) — plus `sim_status`/`sim_provision`, which span
both.

**The two layers fail independently, deliberately.** Per the Architect's
design and the spike evidence: `xcrun simctl` already works today as user
`aw`, headless, with no GUI session and no companion binary. AXe is the one
piece that needs installing, and if it's ever broken on a future Xcode bump,
list/boot/screenshot/openurl/install/launch/terminate must keep working —
so Layer 1 handlers never import or call anything AXe-related.

**`sim_describe_ui` is the heart of this app**, not a convenience: raw
`axe describe-ui` output measured 126,596 bytes for a single Safari screen,
of which only 17 elements carried a label or identifier. Returned raw, the
tool would blow an agent's context on one call and "ship" while remaining
unusable — so this projects to a flat `{label, identifier, role, frame,
enabled, traits}` list, and the projection runs ON THE HOST in the same
`remote_host.exec()` round trip as the `describe-ui` call itself, so the
126 KB of nested AX scaffolding never crosses the wire. The projector
source is base64'd across the shell hop (the windows-pilot lesson —
`mcp/host_agent.py`'s docstring on why arguments there travel as base64:
a literal payload is one stray quote away from breaking).

**`sim_tap` refuses to guess a point.** identifier -> label -> explicit x/y,
in that order; nothing implicit. Coordinates are a deliberate downgrade the
caller has to opt into, not the default — the whole point of this app is
that they stop being the interaction model VNC forced.

**No retry-on-timeout anywhere in this module.** The exec channel has been
observed running a command twice during a link reconnect (2026-09-04). For
a read (`sim_list`, `sim_describe_ui`) that's harmless; for an input verb
(`sim_tap`, `sim_type`, `sim_swipe`, `sim_gesture`, `sim_button`) a
duplicated action is a wrong result that looks like a bug in whatever is
under test. So every exec call below runs exactly once.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import uuid
from typing import Callable

from . import axe_agent, remote_host

DEFAULT_SCREENSHOT_DIR = ".tmp/apple-automation/"

_config_resolver: Callable[[], dict] = lambda: {}


def set_config_resolver(resolver: Callable[[], dict]) -> None:
    """Installed once from ``plugin.activate`` — the same resolver instance
    the plugin hands to :mod:`remote_host`, so axe_version/axe_dir/
    default_udid/screenshot_dir also pick up a Settings change on the very
    next call."""
    global _config_resolver
    _config_resolver = resolver


def _cfg() -> dict:
    try:
        return _config_resolver() or {}
    except Exception:
        return {}


def current_config() -> dict:
    """The resolved config (including this app's own schema-default
    fallbacks — see plugin.py's ``_SCHEMA_DEFAULTS``), for display purposes."""
    return _cfg()


def _workspace_root() -> str:
    return os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")


def _resolve_local_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(_workspace_root(), path)


def _common(args: dict) -> dict:
    """The per-call knobs every tool shares, resolved against config."""
    cfg = _cfg()
    host_override = (args.get("remote_host_id") or "").strip() or None
    udid = (args.get("udid") or cfg.get("default_udid") or "").strip() or "booted"
    return {
        "host_override": host_override,
        "udid": udid,
        "axe_version": cfg.get("axe_version") or axe_agent.DEFAULT_AXE_VERSION,
        "axe_dir": cfg.get("axe_dir") or axe_agent.DEFAULT_AXE_DIR,
    }


def _axe_bin(common: dict) -> str:
    return axe_agent.axe_bin(common["axe_version"], common["axe_dir"], common["host_override"])


def _simctl(args_str: str, timeout: int, host_override: str | None) -> tuple[str, str, int]:
    return remote_host.exec(f"xcrun simctl {args_str}", timeout=timeout, host_override=host_override)


def _format_result(out: str, err: str, code: int) -> str:
    parts = []
    if out:
        parts.append(out.rstrip())
    if err:
        parts.append(f"[stderr]\n{err.rstrip()}")
    if code:
        parts.append(f"[exit {code}]")
    return "\n".join(parts) if parts else "(no output)"


def _text(payload) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _staged_path(name: str, host_override: str | None) -> str:
    return remote_host.expanduser(f"~/.aw-apple-automation/tmp/{name}", host_override=host_override)


# ── The accessibility-tree projector — runs ON THE MAC, base64'd across ────
# ── the shell hop. See module docstring for why this exists at all. ────────

_PROJECTOR_SRC = """\
import json, sys

def frame_of(n):
    f = n.get("frame")
    if isinstance(f, dict):
        return {"x": f.get("x"), "y": f.get("y"),
                "width": f.get("width"), "height": f.get("height")}
    return None

def walk(n, out, role_filter, query_filter):
    if isinstance(n, dict):
        label = n.get("AXLabel")
        ident = n.get("AXUniqueId")
        if label or ident:
            role = n.get("type") or n.get("role")
            role_ok = not role_filter or (role and role_filter.lower() in role.lower())
            text = ((label or "") + " " + (ident or "")).lower()
            query_ok = not query_filter or query_filter.lower() in text
            if role_ok and query_ok:
                out.append({
                    "label": label, "identifier": ident, "role": role,
                    "enabled": n.get("enabled"), "frame": frame_of(n),
                    "traits": n.get("traits") or [],
                })
        for c in (n.get("children") or []):
            walk(c, out, role_filter, query_filter)
    elif isinstance(n, list):
        for c in n:
            walk(c, out, role_filter, query_filter)

def main():
    role_filter = sys.argv[1] if len(sys.argv) > 1 else ""
    query_filter = sys.argv[2] if len(sys.argv) > 2 else ""
    data = json.load(sys.stdin)
    out = []
    walk(data, out, role_filter or None, query_filter or None)
    json.dump({"count": len(out), "elements": out}, sys.stdout)

main()
"""


def projector_b64() -> str:
    return base64.b64encode(_PROJECTOR_SRC.encode("utf-8")).decode("ascii")


# ── Tool schema ──────────────────────────────────────────────────────────

_REMOTE_HOST_PARAM = {
    "type": "string",
    "description": (
        "Id of the linked macOS host running Xcode/the Simulator (from "
        "'aw-workspace-cli remote-hosts hosts'). Defaults to this app's "
        "configured remote_host_id (Frederico's Mac.Home unless changed in "
        "Settings). The operation runs ON THAT HOST, not locally."
    ),
}

_UDID_PARAM = {
    "type": "string",
    "description": (
        "Simulator UDID to target (from sim_list). Defaults to this app's "
        "configured default_udid, or the literal 'booted' (the currently "
        "booted simulator) if that's unset."
    ),
}

TOOLS: list[dict] = [
    # ── Layer 1: zero-install, xcrun simctl ────────────────────────────
    {
        "name": "sim_list",
        "description": (
            "List available iOS/watchOS simulators and their UDIDs/boot "
            "state via `xcrun simctl list devices`. Zero-install — works "
            "even if AXe (Layer 2) is broken or not yet provisioned. Use "
            "this first to find a UDID to boot or target."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM,
                "search": {"type": "string", "description": "Optional substring filter, e.g. an iOS version or device name."},
                "json": {"type": "boolean", "description": "Return simctl's -j JSON output instead of human-readable text."},
            },
            "required": [],
        },
    },
    {
        "name": "sim_boot",
        "description": (
            "Boot a simulator via `xcrun simctl boot <udid>` — no "
            "Simulator.app required, proven headless. Idempotent: booting "
            "an already-booted simulator reports success rather than an error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM},
            "required": [],
        },
    },
    {
        "name": "sim_shutdown",
        "description": (
            "Shut down a simulator via `xcrun simctl shutdown <udid>`. "
            "Idempotent: shutting down an already-shutdown simulator "
            "reports success rather than an error."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM},
            "required": [],
        },
    },
    {
        "name": "sim_openurl",
        "description": "Open a URL (or a custom scheme deep link) on the simulator via `xcrun simctl openurl`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "url": {"type": "string", "description": "URL or custom-scheme deep link to open, e.g. https://example.com or myapp://path."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "sim_screenshot",
        "description": (
            "Capture a screenshot of the simulator screen and save it as a "
            "PNG on THIS workspace's own filesystem, NOT on the Mac. Staged "
            "on the Mac's disk via `xcrun simctl io screenshot` first, then "
            "pulled over the uncapped file-transfer route (never through "
            "exec's stdout, which truncates silently above 1 MiB). Defaults "
            "to a path under this app's configured screenshot_dir if "
            "`local_path` is omitted — repeated calls OVERWRITE that file "
            "unless you pass a distinct local_path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "local_path": {"type": "string", "description": "Absolute or workspace-relative path to save the PNG to."},
            },
            "required": [],
        },
    },
    {
        "name": "sim_install_app",
        "description": (
            "Install an app bundle onto the simulator via `xcrun simctl "
            "install`. `app_path` must already exist ON THE REMOTE HOST's "
            "filesystem (e.g. built there by the mac-fred CI runner) — this "
            "tool does not build or upload one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "app_path": {"type": "string", "description": "Path to the .app bundle on the REMOTE host's filesystem. A leading ~ expands against that host's home."},
            },
            "required": ["app_path"],
        },
    },
    {
        "name": "sim_launch_app",
        "description": "Launch an installed app by bundle identifier via `xcrun simctl launch`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "bundle_id": {"type": "string", "description": "App bundle identifier, e.g. com.example.MyApp."},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Optional launch arguments passed through to the app."},
            },
            "required": ["bundle_id"],
        },
    },
    {
        "name": "sim_terminate_app",
        "description": "Force-quit a running app by bundle identifier via `xcrun simctl terminate`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "bundle_id": {"type": "string", "description": "App bundle identifier to terminate."},
            },
            "required": ["bundle_id"],
        },
    },
    # ── Layer 2: AXe — needs sim_provision first ───────────────────────
    {
        "name": "sim_describe_ui",
        "description": (
            "Read the simulator's CURRENT SCREEN semantically — the "
            "accessibility tree, projected to a flat list of "
            "{label, identifier, role, frame, enabled, traits} for every "
            "element that carries a label or identifier. This is how you "
            "find WHAT is on screen and its identifier before calling "
            "sim_tap — do not guess coordinates. Projection happens on the "
            "Mac itself, so the ~126 KB of raw nested accessibility data "
            "never crosses the wire; pass `raw: true` to instead stage the "
            "full untouched tree and download it as a file (rarely needed). "
            "Requires AXe — call sim_provision once first if this errors "
            "saying AXe is not installed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "role": {"type": "string", "description": "Optional substring filter on the element's role/type, e.g. 'Button'."},
                "contains": {"type": "string", "description": "Optional substring filter on the element's label or identifier."},
                "raw": {"type": "boolean", "description": "Skip projection — stage the full raw tree and download it as a file instead. Rarely what you want; the file can be tens of KB per screen."},
                "local_path": {"type": "string", "description": "Only used with raw:true — where to save the downloaded JSON."},
            },
            "required": [],
        },
    },
    {
        "name": "sim_tap",
        "description": (
            "Tap an element BY IDENTIFIER or LABEL (from sim_describe_ui) — "
            "the semantic replacement for pixel-coordinate tapping. Pass "
            "`identifier` (AXUniqueId, preferred) or `label` (AXLabel); "
            "`x`/`y` is a deliberate downgrade to raw coordinates and is "
            "only used if neither is given. Exactly one of "
            "identifier/label/(x and y) is required — this tool refuses to "
            "guess a point. Requires AXe (sim_provision)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "identifier": {"type": "string", "description": "AXUniqueId of the element to tap, from sim_describe_ui."},
                "label": {"type": "string", "description": "AXLabel of the element to tap, from sim_describe_ui. Ignored if identifier is given."},
                "x": {"type": "integer", "description": "Fallback X coordinate. Only used if identifier and label are both omitted."},
                "y": {"type": "integer", "description": "Fallback Y coordinate. Only used if identifier and label are both omitted."},
            },
            "required": [],
        },
    },
    {
        "name": "sim_type",
        "description": (
            "Type text into the currently focused field via AXe. US-keyboard "
            "HID keycodes only — no accented/non-Latin characters (see "
            "AXe's own limitation). Requires AXe (sim_provision)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "text": {"type": "string", "description": "Text to type."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "sim_swipe",
        "description": "Swipe from (start_x, start_y) to (end_x, end_y) via AXe. Requires AXe (sim_provision).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "start_x": {"type": "integer"}, "start_y": {"type": "integer"},
                "end_x": {"type": "integer"}, "end_y": {"type": "integer"},
                "duration": {"type": "number", "description": "Swipe duration in seconds (optional)."},
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
        },
    },
    {
        "name": "sim_gesture",
        "description": (
            "Perform a preset gesture (scroll-up/down/left/right, "
            "swipe-from-*-edge) without specifying coordinates, via AXe. "
            "Requires AXe (sim_provision)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "preset": {
                    "type": "string",
                    "enum": ["scroll-up", "scroll-down", "scroll-left", "scroll-right",
                             "swipe-from-left-edge", "swipe-from-right-edge",
                             "swipe-from-top-edge", "swipe-from-bottom-edge"],
                },
                "duration": {"type": "number", "description": "Gesture duration in seconds (optional; preset default otherwise)."},
            },
            "required": ["preset"],
        },
    },
    {
        "name": "sim_button",
        "description": "Press a hardware button (home, lock, side-button, siri, apple-pay) via AXe. Requires AXe (sim_provision).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM, "udid": _UDID_PARAM,
                "button": {"type": "string", "enum": ["apple-pay", "home", "lock", "side-button", "siri"]},
                "duration": {"type": "number", "description": "Seconds to hold the button (optional)."},
            },
            "required": ["button"],
        },
    },
    # ── Both layers ─────────────────────────────────────────────────────
    {
        "name": "sim_status",
        "description": (
            "Diagnostic snapshot of the linked macOS host: reachable? Xcode "
            "version? currently booted simulators? is AXe installed, and "
            "which version? Call this first when anything behaves oddly, or "
            "before assuming a Layer-2 tool will work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"remote_host_id": _REMOTE_HOST_PARAM},
            "required": [],
        },
    },
    {
        "name": "sim_provision",
        "description": (
            "One-time setup: installs the pinned AXe version on the linked "
            "host (curl + sha256-verified tarball extract — no Homebrew, no "
            "sudo, no compile). Safe to re-run; idempotent by construction "
            "(a version-keyed install directory is the idempotence check). "
            "Layer 1 (simctl) tools do not need this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_host_id": _REMOTE_HOST_PARAM,
                "force": {"type": "boolean", "description": "Re-install even if the pinned version already reports as installed."},
            },
            "required": [],
        },
    },
]


# ── Tool handlers: Layer 1 ─────────────────────────────────────────────────

def _handle_sim_list(args: dict) -> str:
    common = _common(args)
    parts = ["list devices"]
    if args.get("json"):
        parts.append("-j")
    if args.get("search"):
        parts.append(shlex.quote(args["search"]))
    out, err, code = _simctl(" ".join(parts), timeout=20, host_override=common["host_override"])
    return _format_result(out, err, code)


def _handle_sim_boot(args: dict) -> str:
    common = _common(args)
    out, err, code = _simctl(f"boot {shlex.quote(common['udid'])}", timeout=60,
                             host_override=common["host_override"])
    if code and "current state: Booted" in (err or out):
        return f"Already booted: {common['udid']}"
    return _format_result(out, err, code)


def _handle_sim_shutdown(args: dict) -> str:
    common = _common(args)
    out, err, code = _simctl(f"shutdown {shlex.quote(common['udid'])}", timeout=30,
                             host_override=common["host_override"])
    if code and "current state: Shutdown" in (err or out):
        return f"Already shut down: {common['udid']}"
    return _format_result(out, err, code)


def _handle_sim_openurl(args: dict) -> str:
    common = _common(args)
    url = args.get("url")
    if not url:
        raise ValueError("url is required")
    out, err, code = _simctl(f"openurl {shlex.quote(common['udid'])} {shlex.quote(url)}",
                             timeout=20, host_override=common["host_override"])
    return _format_result(out, err, code)


def _handle_sim_screenshot(args: dict) -> str:
    common = _common(args)
    host_override = common["host_override"]
    local_path = _resolve_local_path(
        args.get("local_path") or os.path.join(_cfg().get("screenshot_dir") or DEFAULT_SCREENSHOT_DIR,
                                                "screenshot.png"))
    staged = _staged_path(f"screenshot_{uuid.uuid4().hex}.png", host_override)
    # Redirected on the REMOTE HOST's own disk, never through exec's stdout
    # (silent 1 MiB truncation) — same lesson as android-studio's adb_screenshot.
    command = f"mkdir -p {shlex.quote(os.path.dirname(staged))} && " \
              f"xcrun simctl io {shlex.quote(common['udid'])} screenshot {shlex.quote(staged)}"
    out, err, code = remote_host.exec(command, timeout=30, host_override=host_override)
    if code:
        raise RuntimeError(f"simctl screenshot failed (exit {code}): {err or out}")

    try:
        size = remote_host.download(staged, local_path, host_override=host_override)
    finally:
        remote_host.exec(f"rm -f {shlex.quote(staged)}", timeout=10, host_override=host_override)

    if size == 0:
        raise RuntimeError("Screenshot came back empty (0 bytes) — the simulator may not be booted.")
    return f"Saved screenshot to {local_path} ({size} bytes)"


def _handle_sim_install_app(args: dict) -> str:
    common = _common(args)
    app_path = args.get("app_path")
    if not app_path:
        raise ValueError("app_path is required")
    resolved = remote_host.expanduser(app_path, host_override=common["host_override"])
    out, err, code = _simctl(f"install {shlex.quote(common['udid'])} {shlex.quote(resolved)}",
                             timeout=120, host_override=common["host_override"])
    return _format_result(out, err, code)


def _handle_sim_launch_app(args: dict) -> str:
    common = _common(args)
    bundle_id = args.get("bundle_id")
    if not bundle_id:
        raise ValueError("bundle_id is required")
    cmd = f"launch {shlex.quote(common['udid'])} {shlex.quote(bundle_id)}"
    extra = " ".join(shlex.quote(a) for a in (args.get("args") or []))
    if extra:
        cmd += f" {extra}"
    out, err, code = _simctl(cmd, timeout=30, host_override=common["host_override"])
    return _format_result(out, err, code)


def _handle_sim_terminate_app(args: dict) -> str:
    common = _common(args)
    bundle_id = args.get("bundle_id")
    if not bundle_id:
        raise ValueError("bundle_id is required")
    out, err, code = _simctl(f"terminate {shlex.quote(common['udid'])} {shlex.quote(bundle_id)}",
                             timeout=20, host_override=common["host_override"])
    return _format_result(out, err, code)


# ── Tool handlers: Layer 2 (AXe) ────────────────────────────────────────────

def _handle_sim_describe_ui(args: dict) -> str:
    common = _common(args)
    host_override = common["host_override"]
    axe_agent.ensure(common["axe_version"], common["axe_dir"], host_override)
    axe = _axe_bin(common)

    if args.get("raw"):
        staged = _staged_path(f"describe_{uuid.uuid4().hex}.json", host_override)
        command = f"mkdir -p {shlex.quote(os.path.dirname(staged))} && " \
                  f"{axe} describe-ui --udid {shlex.quote(common['udid'])} > {shlex.quote(staged)}"
        out, err, code = remote_host.exec(command, timeout=45, host_override=host_override)
        if code:
            raise RuntimeError(f"axe describe-ui failed (exit {code}): {err or out}")
        local_path = _resolve_local_path(
            args.get("local_path") or os.path.join(_cfg().get("screenshot_dir") or DEFAULT_SCREENSHOT_DIR,
                                                    "describe-ui.json"))
        try:
            size = remote_host.download(staged, local_path, host_override=host_override)
        finally:
            remote_host.exec(f"rm -f {shlex.quote(staged)}", timeout=10, host_override=host_override)
        return _text({"raw": True, "path": local_path, "bytes": size})

    tmp_script = _staged_path(f"projector_{uuid.uuid4().hex}.py", host_override)
    role_filter = args.get("role") or ""
    query_filter = args.get("contains") or ""
    # ONE exec round trip: write the projector (base64'd — see module
    # docstring), pipe axe's raw tree straight into it over stdin, so the
    # ~126 KB of nested accessibility data is reduced to a flat list on the
    # Mac itself and never crosses the wire.
    command = (
        f"mkdir -p {shlex.quote(os.path.dirname(tmp_script))} && "
        f"echo {shlex.quote(projector_b64())} | base64 -d > {shlex.quote(tmp_script)} && "
        f"{axe} describe-ui --udid {shlex.quote(common['udid'])} | "
        f"python3 {shlex.quote(tmp_script)} {shlex.quote(role_filter)} {shlex.quote(query_filter)}"
    )
    try:
        out, err, code = remote_host.exec(command, timeout=45, host_override=host_override)
    finally:
        remote_host.exec(f"rm -f {shlex.quote(tmp_script)}", timeout=10, host_override=host_override)
    if code:
        raise RuntimeError(f"sim_describe_ui failed (exit {code}): {err or out}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sim_describe_ui: projector produced non-JSON output: {out[:500]}") from exc
    return _text(payload)


def _handle_sim_tap(args: dict) -> str:
    # Validate BEFORE touching axe_agent/remote_host — resolving the axe
    # binary path costs a real round trip (remote_host.home()), and a
    # malformed call should fail instantly, not after paying for one.
    identifier = args.get("identifier")
    label = args.get("label")
    x, y = args.get("x"), args.get("y")
    if identifier:
        target = f"--id {shlex.quote(identifier)}"
    elif label:
        target = f"--label {shlex.quote(label)}"
    elif x is not None and y is not None:
        target = f"-x {int(x)} -y {int(y)}"
    else:
        raise ValueError(
            "sim_tap needs identifier, label, or both x and y — coordinates "
            "are a deliberate downgrade from semantic targeting, not the "
            "default. Call sim_describe_ui first to find an identifier.")

    common = _common(args)
    host_override = common["host_override"]
    axe_agent.ensure(common["axe_version"], common["axe_dir"], host_override)
    axe = _axe_bin(common)
    cmd = f"{axe} tap {target} --udid {shlex.quote(common['udid'])}"
    out, err, code = remote_host.exec(cmd, timeout=30, host_override=host_override)
    return _format_result(out, err, code)


def _handle_sim_type(args: dict) -> str:
    text = args.get("text")
    if text is None:
        raise ValueError("text is required")
    common = _common(args)
    axe_agent.ensure(common["axe_version"], common["axe_dir"], common["host_override"])
    axe = _axe_bin(common)
    cmd = f"{axe} type {shlex.quote(text)} --udid {shlex.quote(common['udid'])}"
    out, err, code = remote_host.exec(cmd, timeout=30, host_override=common["host_override"])
    return _format_result(out, err, code)


def _handle_sim_swipe(args: dict) -> str:
    required = ("start_x", "start_y", "end_x", "end_y")
    missing = [k for k in required if args.get(k) is None]
    if missing:
        raise ValueError(f"sim_swipe requires {', '.join(missing)}")
    common = _common(args)
    axe_agent.ensure(common["axe_version"], common["axe_dir"], common["host_override"])
    axe = _axe_bin(common)
    cmd = (f"{axe} swipe --start-x {int(args['start_x'])} --start-y {int(args['start_y'])} "
           f"--end-x {int(args['end_x'])} --end-y {int(args['end_y'])}")
    if args.get("duration") is not None:
        cmd += f" --duration {args['duration']}"
    cmd += f" --udid {shlex.quote(common['udid'])}"
    out, err, code = remote_host.exec(cmd, timeout=30, host_override=common["host_override"])
    return _format_result(out, err, code)


_GESTURE_PRESETS = {"scroll-up", "scroll-down", "scroll-left", "scroll-right",
                    "swipe-from-left-edge", "swipe-from-right-edge",
                    "swipe-from-top-edge", "swipe-from-bottom-edge"}


def _handle_sim_gesture(args: dict) -> str:
    preset = args.get("preset")
    if preset not in _GESTURE_PRESETS:
        raise ValueError(f"preset must be one of {sorted(_GESTURE_PRESETS)}")
    common = _common(args)
    axe_agent.ensure(common["axe_version"], common["axe_dir"], common["host_override"])
    axe = _axe_bin(common)
    cmd = f"{axe} gesture {preset}"
    if args.get("duration") is not None:
        cmd += f" --duration {args['duration']}"
    cmd += f" --udid {shlex.quote(common['udid'])}"
    out, err, code = remote_host.exec(cmd, timeout=30, host_override=common["host_override"])
    return _format_result(out, err, code)


_BUTTONS = {"apple-pay", "home", "lock", "side-button", "siri"}


def _handle_sim_button(args: dict) -> str:
    button = args.get("button")
    if button not in _BUTTONS:
        raise ValueError(f"button must be one of {sorted(_BUTTONS)}")
    common = _common(args)
    axe_agent.ensure(common["axe_version"], common["axe_dir"], common["host_override"])
    axe = _axe_bin(common)
    cmd = f"{axe} button {button}"
    if args.get("duration") is not None:
        cmd += f" --duration {args['duration']}"
    cmd += f" --udid {shlex.quote(common['udid'])}"
    out, err, code = remote_host.exec(cmd, timeout=20, host_override=common["host_override"])
    return _format_result(out, err, code)


# ── Tool handlers: both layers ──────────────────────────────────────────────

def _handle_sim_status(args: dict) -> str:
    common = _common(args)
    host_override = common["host_override"]
    out, err, code = remote_host.exec(
        "xcodebuild -version; echo '---'; xcrun simctl list devices booted",
        timeout=30, host_override=host_override)
    xcode_part, _, booted_part = out.partition("---")
    axe_status = axe_agent.status(common["axe_version"], common["axe_dir"], host_override)
    return _text({
        "host_reachable": True,
        "xcode_version": xcode_part.strip(),
        "booted_simulators": booted_part.strip(),
        "axe": axe_status,
        "exit_code": code,
        "stderr": err.strip() or None,
    })


def _handle_sim_provision(args: dict) -> str:
    common = _common(args)
    result = axe_agent.provision(common["axe_version"], common["axe_dir"],
                                 host_override=common["host_override"], force=bool(args.get("force")))
    return _text(result)


HANDLERS: dict[str, Callable[[dict], str]] = {
    "sim_list": _handle_sim_list,
    "sim_boot": _handle_sim_boot,
    "sim_shutdown": _handle_sim_shutdown,
    "sim_openurl": _handle_sim_openurl,
    "sim_screenshot": _handle_sim_screenshot,
    "sim_install_app": _handle_sim_install_app,
    "sim_launch_app": _handle_sim_launch_app,
    "sim_terminate_app": _handle_sim_terminate_app,
    "sim_describe_ui": _handle_sim_describe_ui,
    "sim_tap": _handle_sim_tap,
    "sim_type": _handle_sim_type,
    "sim_swipe": _handle_sim_swipe,
    "sim_gesture": _handle_sim_gesture,
    "sim_button": _handle_sim_button,
    "sim_status": _handle_sim_status,
    "sim_provision": _handle_sim_provision,
}

assert {t["name"] for t in TOOLS} == set(HANDLERS), (
    "every declared tool needs a handler and vice versa")
