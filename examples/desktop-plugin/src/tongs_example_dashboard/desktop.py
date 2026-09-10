"""Production S4 desktop provider for the deterministic dashboard example."""

from __future__ import annotations

from typing import Final

from tongs.plugins.desktop import (
    DESKTOP_PLUGIN_API_MAJOR,
    DesktopAsset,
    DesktopAssetBundle,
    DesktopAssetKind,
    DesktopCallContext,
    DesktopCommand,
    DesktopCompatibility,
    DesktopEvent,
    DesktopFocusTarget,
    DesktopMethod,
    DesktopModule,
    DesktopNavigation,
    DesktopPluginContext,
    DesktopPluginManifest,
    FrozenJsonObject,
    JsonValue,
)

PLUGIN_ID: Final = "example_dashboard"
_REFRESH_METHOD: Final = "refresh"
_REFRESHED_EVENT: Final = "refreshed"

_REVIEWS: Final = (
    {
        "id": "review-101",
        "title": "Document plugin contracts",
        "repository": "example/tongs",
        "status": "ready",
    },
    {
        "id": "review-102",
        "title": "Add keyboard focus coverage",
        "repository": "example/tongs",
        "status": "review",
    },
    {
        "id": "review-103",
        "title": "Refresh deterministic fixtures",
        "repository": "example/tongs",
        "status": "ci",
    },
)


def _dashboard_snapshot() -> dict[str, JsonValue]:
    """Return a fresh deterministic JSON value for each refresh invocation."""
    return {
        "generated_at": "2026-01-01T00:00:00Z",
        "summary": {
            "open_reviews": 3,
            "waiting_on_me": 1,
            "ci_passing": 2,
        },
        "reviews": [dict(review) for review in _REVIEWS],
    }


class ExampleDashboardProvider:
    """Reference provider showing the narrow S4 lifecycle and call contract."""

    def __init__(self) -> None:
        self._context: DesktopPluginContext | None = None

    def manifest(self) -> DesktopPluginManifest:
        return DesktopPluginManifest(
            plugin_id=PLUGIN_ID,
            title="Example dashboard",
            version="0.1.0",
            compatibility=DesktopCompatibility(api_major=DESKTOP_PLUGIN_API_MAJOR),
            modules=(
                DesktopModule(
                    id="dashboard",
                    title="Example dashboard",
                    bundle_id="dashboard",
                    entry_asset_id="dashboard-module",
                    stylesheet_asset_ids=("dashboard-style",),
                ),
            ),
            asset_bundles=(
                DesktopAssetBundle(
                    id="dashboard",
                    package="tongs_example_dashboard",
                    root="assets",
                    assets=(
                        DesktopAsset(
                            id="dashboard-module",
                            path="dashboard.mjs",
                            kind=DesktopAssetKind.MODULE,
                        ),
                        DesktopAsset(
                            id="dashboard-style",
                            path="dashboard.css",
                            kind=DesktopAssetKind.STYLESHEET,
                        ),
                        DesktopAsset(
                            id="dashboard-help",
                            path="help.md",
                            kind=DesktopAssetKind.HELP,
                        ),
                    ),
                ),
            ),
            navigation=(
                DesktopNavigation(
                    id="dashboard",
                    title="Dashboard",
                    module_id="dashboard",
                ),
            ),
            commands=(
                DesktopCommand(
                    id="open-dashboard",
                    title="Open dashboard",
                    navigation_id="dashboard",
                    help_text="Open the deterministic example dashboard",
                ),
            ),
            methods=(DesktopMethod(id=_REFRESH_METHOD),),
            events=(DesktopEvent(id=_REFRESHED_EVENT),),
            focus_targets=(
                DesktopFocusTarget(
                    id="summary",
                    title="Summary",
                    module_id="dashboard",
                ),
            ),
            help_asset_id="dashboard-help",
        )

    async def start(self, context: DesktopPluginContext) -> None:
        self._context = context

    async def call(
        self,
        method: str,
        params: FrozenJsonObject,
        call_context: DesktopCallContext,
    ) -> JsonValue:
        del call_context
        if method != _REFRESH_METHOD:
            raise ValueError("Unknown example dashboard method")
        if params:
            raise ValueError("Example dashboard refresh takes no parameters")
        snapshot = _dashboard_snapshot()
        context = self._context
        if context is None:
            raise RuntimeError("Example dashboard provider is not started")
        await context.publish_event(_REFRESHED_EVENT, snapshot)
        return snapshot

    async def stop(self) -> None:
        self._context = None
