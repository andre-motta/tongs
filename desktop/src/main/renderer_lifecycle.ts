export interface RendererRecoverySteps {
  resetBindings(): void;
  restartSidecar(): Promise<void>;
  refreshAssets(): Promise<unknown>;
  loadDocument(): Promise<unknown>;
  showFailure(): Promise<unknown>;
}

export class RendererRecoveryCoordinator {
  readonly #steps: RendererRecoverySteps;
  #requested = 0;
  #active: Promise<void> | null = null;

  constructor(steps: RendererRecoverySteps) {
    this.#steps = steps;
  }

  request(): Promise<void> {
    this.#requested += 1;
    if (!this.#active) {
      const task = this.#drain();
      let owned: Promise<void>;
      owned = task.finally(() => {
        if (this.#active === owned) this.#active = null;
      });
      this.#active = owned;
    }
    return this.#active;
  }

  async #drain(): Promise<void> {
    let completed = 0;
    while (completed !== this.#requested) {
      completed = this.#requested;
      await recoverRendererSession(this.#steps);
    }
  }
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
