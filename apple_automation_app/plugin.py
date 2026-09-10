"""Entrypoint referenced by aw-app.json's ``runtime.entrypoint``
("apple_automation_app.plugin:AppleAutomationAppPlugin").

No subprocess, no venv, no port: every tool is a plain HTTPS call to
aw-backend's remote-hosts routes, served in-process — same shape as
aw-app-android-studio.

Config/secrets are resolved through a callable rather than read once, so
saving a setting takes effect on the next tool call with no restart. See
``mcp/remote_host.py``, ``mcp/axe_agent.py`` and ``mcp/sim_tools.py``.
"""
from __future__ import annotations

import logging
import os

from . import mcp_config, routes as routes_mod
from .mcp import axe_agent, remote_host, sim_tools

log = logging.getLogger("aw_apps.apple-automation")

# Mirrors aw-app.json's config_schema `default` values. Needed because core's
# Tier-1 (inprocess) activation path hands the plugin the raw stored config
# with NO schema-default merge — only the Tier-2 (container) path and the
# settings-UI display route apply `manifest.config_with_defaults()` (see
# aw-workspace src/apps/runtime.py, and aw-app-android-studio's plugin.py for
# the confirmed-live 2026-08-18 failure this works around). Until that gap is
# fixed in core, every Tier-1 app with a config_schema default has to fall
# back to it here itself — omit this and a freshly installed app reports
# remote_host_id: "" despite the manifest declaring a default.
_SCHEMA_DEFAULTS = {
    "remote_host_id": "824decc7e0610089",
    "axe_version": axe_agent.DEFAULT_AXE_VERSION,
    "axe_dir": axe_agent.DEFAULT_AXE_DIR,
    "screenshot_dir": sim_tools.DEFAULT_SCREENSHOT_DIR,
}


class AppleAutomationAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx

        def _resolve_config() -> dict:
            return {
                "remote_backend_url": ctx.config.get("remote_backend_url") or "",
                "remote_workspace": ctx.config.get("remote_workspace") or "",
                "remote_token": ctx.secrets.read(routes_mod.SECRET_KEY) or "",
                "remote_host_id": ctx.config.get("remote_host_id") or _SCHEMA_DEFAULTS["remote_host_id"],
                "axe_version": ctx.config.get("axe_version") or _SCHEMA_DEFAULTS["axe_version"],
                "axe_dir": ctx.config.get("axe_dir") or _SCHEMA_DEFAULTS["axe_dir"],
                "default_udid": ctx.config.get("default_udid") or "",
                "screenshot_dir": ctx.config.get("screenshot_dir") or _SCHEMA_DEFAULTS["screenshot_dir"],
            }

        remote_host.set_config_resolver(_resolve_config)
        sim_tools.set_config_resolver(_resolve_config)

        ctx.routes.register(routes_mod.build_routes(ctx))

        port = int(os.environ.get("AW_PORT") or 9030)
        # Rebuilt every boot rather than persisted: the entry embeds this
        # process's hostname and API key, both of which change when the
        # workspace container is recreated.
        doc = mcp_config.write_mcp_json(ctx.package_dir, port)

        log.info(
            "aw-app-apple-automation activated: mcp server=%s, tools=%s, remote-hosts=%s",
            sorted(doc["mcpServers"]),
            len(sim_tools.TOOLS),
            "configured" if remote_host.configured() else f"NOT SET ({remote_host.missing_settings()})",
        )

    async def deactivate(self) -> None:
        log.info("aw-app-apple-automation deactivated")
