# aw-app-apple-automation

Drive the iOS Simulator on a linked macOS host **semantically** — read
accessibility labels/identifiers and tap, type, swipe and gesture BY
IDENTIFIER instead of pixel coordinate. Replaces the VNC pixel-coordinate
workaround (crop+zoom measurement, no state reading, breaks if the window
moves) that Debugger/QA sessions were stuck with for iOS bug repro.

16 tools, gateway-prefixed `aw__aw_apple_automation__*`. Structurally a copy
of `aw-app-android-studio` (same transport: `mcp/remote_host.py` over
`aw-remote-hosts`), with `aw-app-windows-pilot`'s
`ensure_agent`/`_handle_provision` pattern for the install half.

**No fixed machine, but only macOS works.** The Simulator needs Xcode on a
real Mac — this app reaches whatever host you link through
**aw-remote-hosts**, pre-configured for Frederico's Mac.Home
(`remote_host_id = 824decc7e0610089`).

## Two layers that fail independently

- **Layer 1 — zero-install (`xcrun simctl`)**: `sim_list`, `sim_boot`,
  `sim_shutdown`, `sim_openurl`, `sim_screenshot`, `sim_install_app`,
  `sim_launch_app`, `sim_terminate_app`. Works today as user `aw`, headless,
  no GUI session, no companion binary — proven in the feasibility spike.
- **Layer 2 — AXe** (needs `sim_provision` once): `sim_describe_ui`,
  `sim_tap`, `sim_type`, `sim_swipe`, `sim_gesture`, `sim_button`.
- **Both**: `sim_status`, `sim_provision`.

If AXe is ever broken by a future Xcode bump, Layer 1 keeps working — the
split is deliberate, not accidental.

## Why AXe, not idb

`idb` (Meta's iOS Debug Bridge) is the de-facto standard but undeployable
here: `idb_companion` is a Homebrew bottle, and the exec user `aw` cannot
write `/opt/homebrew` (owned by `fredericowu`). A `~/homebrew` user prefix
gets no prebuilt bottles — a from-source `FBSimulatorControl` build, the
same ~90-minute wall the fastlane precedent hit. **AXe** vendors those exact
same Meta frameworks prebuilt, as a plain `curl`-able universal tarball — no
Homebrew, no sudo, no compile. Same engine, install problem deleted.
WebDriverAgent (driven directly) was the rejected plan B: it owns an
xcodebuild product and a long-lived server process to keep current across
Xcode bumps, versus AXe's one stateless process per call.

## `sim_describe_ui` is the point of this app

Tap/type/screenshot alone do not solve the problem VNC already did badly.
Raw `axe describe-ui` output measured 126,596 bytes for one Safari screen —
of which only 17 elements carried a label or identifier. Returned raw, the
tool would blow an agent's context on one call and "ship" while remaining
unusable. So `sim_describe_ui` **projects to a flat actionable-element list
on the Mac itself** (a ~20-line python3 projector, base64'd across the
shell hop so the 126 KB never crosses the wire), returning
`{label, identifier, role, frame, enabled, traits}` for each element that
carries a label or identifier. `raw: true` is a staged-and-downloaded
escape hatch for the rare case you need the full tree.

## Install

```bash
aw-workspace-cli marketplace install apple-automation
```

Then open **Apple Automation** in the Apps grid, fill in the backend URL /
workspace slug / bearer token for the aw-backend that fronts your linked
hosts, hit **Test the connection**, then call `sim_provision` once to
install AXe on the host (Layer 1 tools work without this step).

## Why the token is a secret and the rest isn't

`remote_backend_url` / `remote_workspace` / `remote_host_id` / `axe_version`
/ `axe_dir` / `default_udid` / `screenshot_dir` are plain config
(`POST /api/apps/apple-automation/config`). `remote_token` is a bearer
credential that can run arbitrary commands on the linked Mac, so it goes
through the zero-knowledge secret store instead
(`POST /api/apps/apple-automation/settings`) — never written to plain,
cloud-syncable app config.

## Routes

Under `/api/apps/apple-automation`, behind the workspace IdentityGuard.

| Route | Purpose |
|---|---|
| `GET /status` | Configured state, missing settings (if any), tool list. |
| `POST /settings` | Save the bearer token (secret store). Effective on the next call — no restart. |
| `POST /logout` | Forget the token. |
| `POST /test` | Run a real `sim_status` against the configured (or overridden) host. |
| `POST /mcp` | The MCP server itself (Streamable HTTP), scanned by MCP Gateway. |
| `GET /mcp.json` | What the gateway will see. |

Plain config (host id, AXe version/dir, default UDID, screenshot dir) goes
through core's generic `POST /api/apps/apple-automation/config`, not a route
in this app.

## Offline resilience

aw-backend's exec route already 409s fast for a disconnected host — no
hang. `mcp/remote_host.py` maps that (and 404/502) to typed exceptions —
`HostOffline`, `HostNotFound`, `HostCommandFailed` — that `mcp/http_handler.py`
turns into a tool error naming the host and the tool, in seconds. No
pre-flight "is it online" probe (extra round trip, TOCTOU-racy) — the 409 IS
the authoritative fast answer.

## Tests

```bash
python3 tests/validate_manifest.py aw-app.json
python3 -m pytest tests -q
```

See `skills/aw-apple-automation/SKILL.md` for the real usage flow (not just
the tool list) and the gotchas that bite: the exec channel's observed
duplicate-run hazard on input verbs, the 1 MiB exec-stdout cap, and why
`sim_tap` refuses to guess a coordinate.
