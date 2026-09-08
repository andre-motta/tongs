import type { ReactNode } from "react";
import type {
  DesktopBridge,
  RepositoryDto,
  ReviewListItemDto,
} from "../../shared/bridge.js";
import type { QueryCoordinator } from "./query.js";

export type ReviewPanel = "overview" | "diff" | "commits";
export type AppRoute =
  | { readonly kind: "inbox"; readonly repository: RepositoryDto | null }
  | {
      readonly kind: "review";
      readonly item: ReviewListItemDto;
      readonly panel: ReviewPanel;
    };

export interface FeatureContext {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly repositories: readonly RepositoryDto[];
  readonly repositoriesReady: boolean;
  readonly repositoryGeneration: number;
  readonly navigate: (route: AppRoute) => void;
}

export interface FeatureContribution {
  readonly id: string;
  readonly order: number;
  readonly matches: (route: AppRoute) => boolean;
  readonly render: (context: FeatureContext, route: AppRoute) => ReactNode;
}

export class FeatureRegistry {
  private readonly features = new Map<string, FeatureContribution>();
  register(feature: FeatureContribution): void {
    if (this.features.has(feature.id))
      throw new Error(`Duplicate feature: ${feature.id}`);
    this.features.set(feature.id, feature);
  }
  find(route: AppRoute): FeatureContribution {
    const matches = [...this.features.values()].filter((feature) =>
      feature.matches(route),
    );
    matches.sort((left, right) => left.order - right.order);
    const feature = matches[0];
    if (!feature)
      throw new Error(`No feature registered for route: ${route.kind}`);
    return feature;
  }
}

export class Navigator {
  private listeners = new Set<(route: AppRoute) => void>();
  constructor(
    private routeValue: AppRoute = { kind: "inbox", repository: null },
  ) {}
  get route(): AppRoute {
    return this.routeValue;
  }
  navigate(route: AppRoute): void {
    this.routeValue = route;
    for (const listener of this.listeners) listener(route);
  }
  subscribe(listener: (route: AppRoute) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}
