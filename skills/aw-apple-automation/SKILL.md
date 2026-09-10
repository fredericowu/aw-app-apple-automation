---
name: aw-apple-automation
description: Drive the iOS Simulator on a linked macOS host semantically — list/boot a simulator, read its accessibility tree, and tap/type/swipe/gesture BY IDENTIFIER instead of pixel coordinates. Use for anything about the iOS Simulator, iOS bug repro, or piloting aw-mobile's iOS app.
---

# aw-apple-automation — semantic iOS Simulator automation

## What this is

16 `sim_*` tools that drive the iOS/watchOS Simulator on a **linked remote
macOS host** — reached through **aw-remote-hosts**, not a fixed machine.
Pre-configured for Frederico's Mac.Home (`remote_host_id =
824decc7e0610089`). Replaces the VNC pixel-coordinate workaround: instead of
cropping/zooming a screen capture to measure a pixel center, you ask what is
on screen and get accessibility labels/identifiers back, then tap **by
identifier**.

## The real flow, not just the tool list

1. **`sim_list`** first, always — zero-install, works even if AXe is
   broken. Shows UDIDs and boot state.
2. **`sim_boot`** the simulator you want (idempotent — booting an
   already-booted one just reports that).
3. **`sim_describe_ui`** to read the CURRENT SCREEN. This is the heart of
   the app — it returns a flat list of `{label, identifier, role, frame,
   enabled, traits}` for every element that carries a label or identifier,
   NOT the raw accessibility tree (which runs 100+ KB for one screen and
   would blow your context). Narrow it with `role` or `contains` if the
   list is long.
4. **`sim_tap`** the element **by `identifier` or `label`** from step 3 —
   not by coordinate. `x`/`y` exist as a fallback only; passing neither an
   identifier/label nor both x and y is a hard error, not a silent guess.
5. **`sim_type`** to type into the now-focused field. US-keyboard HID
   keycodes only — no accented/non-Latin characters (AXe's own limitation,
   same class of gap as `adb_text` on Android).
6. **`sim_screenshot`** saves a PNG onto THIS workspace's own filesystem
   (under the app's configured `screenshot_dir`, default
   `.tmp/apple-automation/`) — read it back to see the screen, or to attach
   to a bug report.

`sim_swipe`/`sim_gesture`/`sim_button` round out input; `sim_openurl` opens
a URL or deep link; `sim_install_app`/`sim_launch_app`/`sim_terminate_app`
manage an app already on the host (this app does not build or upload one —
that's the `mac-fred` CI runner's job).

## First-time setup on a fresh host

`sim_provision` installs the pinned AXe version (curl + sha256-verified
tarball extract — no Homebrew, no sudo, no compile). **Layer 1 tools
(list/boot/shutdown/openurl/screenshot/install/launch/terminate) work
without this** — only `sim_describe_ui`/`sim_tap`/`sim_type`/`sim_swipe`/
`sim_gesture`/`sim_button` need it, and will tell you to call
`sim_provision` if you skip it. `sim_status` reports whether it's installed.

## Gotchas

- **`sim_tap` refuses to guess a point.** Pass `identifier` (preferred),
  `label`, or both `x` and `y` — nothing else is accepted. This is
  deliberate: the whole point of this app is that coordinates stop being
  the interaction model.
- **The exec channel has been observed running a command TWICE** during a
  link reconnect. Harmless for a read (`sim_list`, `sim_describe_ui`); for
  an input verb (`sim_tap`, `sim_type`, `sim_swipe`, `sim_gesture`,
  `sim_button`) a duplicated action can look like a bug in whatever you're
  testing. If an action's effect looks doubled, check the app under test
  before filing anything against this one — there is no retry-on-timeout
  built into any input verb here.
- **Screenshots never travel through exec's stdout.** The exec channel
  caps a job's output at 1 MiB and returns exit −1 past it — a screenshot
  above that size would silently come back truncated. Every screenshot
  stages on the Mac's own disk first, then downloads over the uncapped
  file-transfer route.
- **US-keyboard characters only in `sim_type`.** Accented/non-Latin text
  is an AXe/HID limitation, not something this app works around.
- **Host offline is fast and explicit, not a hang.** A disconnected
  Mac.Home returns an error naming the host within seconds — no pre-flight
  probe, no silent timeout.
- **Simulator.app is not required.** Verified 2026-09-10: `axe describe-ui`
  returns a real tree against a simulator booted purely via `xcrun simctl
  boot`, with zero `Simulator.app` process running.
- **watchOS is reachable but not the MVP focus.** Every tool takes `udid`,
  so a watchOS simulator UDID works the same way — just untested as a
  first-class flow.

## What this deliberately does NOT do

Physical iOS devices (Simulator only); building/testing/TestFlight (that's
the `mac-fred` GitHub Actions runner, `ios.yml` — this app doesn't touch
xcodebuild or fastlane); Appium; a UI window/live viewer; generic
multi-host fleet orchestration. The VNC workaround (`mac_home_vnc_password`
in the secret store) stays available as a fallback — this app doesn't
retire it.
