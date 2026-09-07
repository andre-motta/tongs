export interface RendererRecoverySteps {
  resetBindings(): void;
  restartSidecar(): Promise<void>;
  refreshAssets(): Promise<unknown>;
  loadDocument(): Promise<unknown>;
  showFailure(): Promise<unknown>;
}

export async function recoverRendererSession(
  steps: RendererRecoverySteps,
): Promise<boolean> {
  steps.resetBindings();
  try {
    await steps.restartSidecar();
    await steps.refreshAssets();
    await steps.loadDocument();
    return true;
  } catch {
    try {
      await steps.loadDocument();
      await steps.showFailure();
    } catch {
      // The owner may already be gone during application shutdown.
    }
    return false;
  }
}
