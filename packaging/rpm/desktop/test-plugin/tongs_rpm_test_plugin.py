"""Installed desktop plugin used only by the Fedora RPM lifecycle proof."""

from __future__ import annotations

from tongs.plugins.desktop import (
    DesktopAsset,
    DesktopAssetBundle,
    DesktopAssetKind,
    DesktopCallContext,
    DesktopCompatibility,
    DesktopMethod,
    DesktopModule,
    DesktopPluginContext,
    DesktopPluginManifest,
    FrozenJsonObject,
    JsonValue,
)


class RPMTestPlugin:
    """Small installed provider with one packaged asset and one method."""

    def manifest(self) -> DesktopPluginManifest:
        return DesktopPluginManifest(
            plugin_id="rpm-test",
            title="RPM lifecycle plugin",
            version="1.0.0",
            compatibility=DesktopCompatibility(api_major=1),
            modules=(
                DesktopModule(
                    id="main",
                    title="RPM lifecycle module",
                    bundle_id="ui",
                    entry_asset_id="module",
                ),
            ),
            asset_bundles=(
                DesktopAssetBundle(
                    id="ui",
                    package="tongs_rpm_test_plugin_assets",
                    root="assets",
                    assets=(
                        DesktopAsset(
                            id="module",
                            path="module.mjs",
                            kind=DesktopAssetKind.MODULE,
                        ),
                    ),
                ),
            ),
            methods=(DesktopMethod(id="echo"),),
        )

    async def start(self, context: DesktopPluginContext) -> None:
        self._context = context

    async def call(
        self,
        method: str,
        params: FrozenJsonObject,
        context: DesktopCallContext,
    ) -> JsonValue:
        assert method == "echo"
        return {
            "invocation": context.invocation_id,
            "value": params.get("value"),
        }

    async def stop(self) -> None:
        self._context = None
