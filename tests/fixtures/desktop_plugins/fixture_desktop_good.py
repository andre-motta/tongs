"""Providers loaded through real ``importlib.metadata`` fixture entry points."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from tongs.plugins.desktop import (
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
    DesktopReadKind,
    FrozenJsonObject,
    JsonValue,
)


def make_manifest(
    plugin_id: str,
    *,
    api_major: int = 1,
    version: str = "1.0.dev2+fixture",
    minimum_host_version: str | None = "0.1.dev1",
) -> DesktopPluginManifest:
    return DesktopPluginManifest(
        plugin_id=plugin_id,
        title=f"{plugin_id.title()} fixture",
        version=version,
        compatibility=DesktopCompatibility(api_major, minimum_host_version),
        modules=(DesktopModule("review", "Review", "ui", "main", ("style",)),),
        asset_bundles=(
            DesktopAssetBundle(
                "ui",
                "fixture_desktop_assets",
                "assets",
                (
                    DesktopAsset("main", "main.mjs", DesktopAssetKind.MODULE),
                    DesktopAsset("style", "style.css", DesktopAssetKind.STYLESHEET),
                    DesktopAsset("help", "help.md", DesktopAssetKind.HELP),
                ),
            ),
        ),
        navigation=(DesktopNavigation("review", "Review", "review"),),
        commands=(DesktopCommand("open", "Open review", "review", "Open fixture"),),
        methods=(
            DesktopMethod("echo"),
            DesktopMethod("explode"),
            DesktopMethod("wait"),
            DesktopMethod("invalid"),
            DesktopMethod("observe_cancellation"),
        ),
        events=(DesktopEvent("refreshed"),),
        focus_targets=(DesktopFocusTarget("editor", "Editor", "review"),),
        help_asset_id="help",
        reads=(DesktopReadKind.REVIEWS, DesktopReadKind.REVIEW),
    )


class GoodProvider:
    instances: ClassVar[list[GoodProvider]] = []

    def __init__(self) -> None:
        self.context: DesktopPluginContext | None = None
        self.stop_called = False
        self.__class__.instances.append(self)

    def manifest(self) -> DesktopPluginManifest:
        return make_manifest("good")

    async def start(self, context: DesktopPluginContext) -> None:
        self.context = context
        if context.config.get("publish_on_start") is True:
            await context.notify("Fixture started")
            await context.publish_event("refreshed", {"ready": True})
        if context.config.get("fail_start") is True:
            raise RuntimeError("fixture start failure")
        if context.config.get("cancel_start") is True:
            raise asyncio.CancelledError
        if context.config.get("hang_start") is True:
            await asyncio.Event().wait()

    async def call(
        self,
        method: str,
        params: FrozenJsonObject,
        context: DesktopCallContext,
    ) -> JsonValue:
        if method == "explode":
            raise RuntimeError("fixture call failure")
        if method == "wait":
            await asyncio.Event().wait()
        if method == "invalid":
            return {"bad": float("nan")}
        if method == "observe_cancellation":
            return {"cancelled": context.cancellation.cancelled}
        return {"method": method, "value": params.get("value")}

    async def stop(self) -> None:
        self.stop_called = True
        if self.context is not None and self.context.config.get("fail_stop") is True:
            raise RuntimeError("fixture stop failure")
        if self.context is not None and self.context.config.get("cancel_stop") is True:
            raise asyncio.CancelledError
        if (
            self.context is not None
            and self.context.config.get("stubborn_stop") is True
        ):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
        if self.context is not None and self.context.config.get("hang_stop") is True:
            await asyncio.Event().wait()


class DualProvider(GoodProvider):
    def manifest(self) -> DesktopPluginManifest:
        return make_manifest("dual")


class MismatchProvider(GoodProvider):
    def manifest(self) -> DesktopPluginManifest:
        return make_manifest("different")


class Api2Provider(GoodProvider):
    def manifest(self) -> DesktopPluginManifest:
        return make_manifest("api2", api_major=2)


class FutureHostProvider(GoodProvider):
    def manifest(self) -> DesktopPluginManifest:
        return make_manifest("future", minimum_host_version="99.0")


class BadManifestProvider(GoodProvider):
    def manifest(self) -> DesktopPluginManifest:
        return make_manifest("bad_manifest", version="not a version")
