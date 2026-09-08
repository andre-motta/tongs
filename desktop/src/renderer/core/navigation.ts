import type { ReactNode } from "react";
import type {
  DesktopBridge,
  RepositoryDto,
  ReviewRevisionDto,
  ReviewListItemDto,
} from "../../shared/bridge.js";
import type { QueryCoordinator } from "./query.js";

export type ReviewPanel = string;
export type AppRoute =
  | { readonly kind: "inbox"; readonly repository: RepositoryDto | null }
  | {
      readonly kind: "review";
      readonly item: ReviewListItemDto;
      readonly panel: ReviewPanel;
      readonly diffTarget?: DiscussionDiffTarget;
    }
  | {
      readonly kind: "plugin";
      readonly pluginId: string;
      readonly navigationId: string;
      readonly moduleId: string;
    };

export interface FeatureContext {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly repositories: readonly RepositoryDto[];
  readonly repositoriesReady: boolean;
  readonly repositoryGeneration: number;
  readonly reviewPanels: readonly ReviewPanelContribution[];
  readonly inlineAnchor: InlineAnchorSelection | null;
  readonly selectInlineAnchor: (
    selection: InlineAnchorSelection | null,
  ) => void;
  readonly navigate: (route: AppRoute) => void;
}

export interface InlineAnchorSelection {
  readonly review: string;
  readonly snapshotId: string;
  readonly resource: string;
  readonly revision: ReviewRevisionDto;
  readonly fileIndex: number;
  readonly hunkIndex: number;
  readonly rowIndex: number | null;
  readonly oldPath: string;
  readonly newPath: string;
  readonly side: "old" | "new";
  readonly oldLine: number | null;
  readonly newLine: number | null;
  readonly lineType: string;
  readonly contextLines: readonly string[];
  readonly contextComplete: boolean;
  readonly rangeOriginOldLine?: number | null;
  readonly rangeOriginNewLine?: number | null;
  readonly selectedLines?: readonly InlineSelectedLine[];
}

export interface InlineSelectedLine {
  readonly oldLine: number | null;
  readonly newLine: number | null;
  readonly lineType: string;
  readonly content: string;
}

export interface DiscussionDiffTarget {
  readonly discussionId: string;
  readonly path: string;
  readonly side: "old" | "new";
  readonly line: number;
}

export interface ReviewPanelContribution {
  readonly id: ReviewPanel;
  readonly label: string;
  readonly order: number;
}

export interface CommandContribution {
  readonly id: string;
  readonly label: string;
  readonly order: number;
  readonly isVisible: (context: FeatureContext, route: AppRoute) => boolean;
  readonly disabledReason: (
    context: FeatureContext,
    route: AppRoute,
  ) => string | null;
  readonly run: (
    context: FeatureContext,
    route: AppRoute,
  ) => void | Promise<void>;
}

export interface FeatureContribution {
  readonly id: string;
  readonly order: number;
  readonly reviewPanel?: ReviewPanelContribution;
  readonly commands?: readonly CommandContribution[];
  readonly matches: (route: AppRoute) => boolean;
  readonly render: (context: FeatureContext, route: AppRoute) => ReactNode;
}

export class FeatureRegistry {
  private readonly features = new Map<string, FeatureContribution>();
  private readonly panels = new Map<string, ReviewPanelContribution>();
  private readonly contributedCommands = new Map<string, CommandContribution>();
  register(feature: FeatureContribution): void {
    if (this.features.has(feature.id))
      throw new Error(`Duplicate feature: ${feature.id}`);
    if (feature.reviewPanel && this.panels.has(feature.reviewPanel.id))
      throw new Error(`Duplicate review panel: ${feature.reviewPanel.id}`);
    const commands = feature.commands ?? [];
    const commandIds = new Set<string>();
    for (const command of commands) {
      if (
        commandIds.has(command.id) ||
        this.contributedCommands.has(command.id)
      )
        throw new Error(`Duplicate command: ${command.id}`);
      commandIds.add(command.id);
    }
    this.features.set(feature.id, feature);
    if (feature.reviewPanel)
      this.panels.set(feature.reviewPanel.id, feature.reviewPanel);
    for (const command of commands)
      this.contributedCommands.set(command.id, command);
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
  reviewPanels(): readonly ReviewPanelContribution[] {
    return Object.freeze([...this.panels.values()].sort(orderedContribution));
  }
  commands(
    context: FeatureContext,
    route: AppRoute,
  ): readonly CommandContribution[] {
    return Object.freeze(
      [...this.contributedCommands.values()]
        .filter((command) => command.isVisible(context, route))
        .sort(orderedContribution),
    );
  }
}

function orderedContribution(
  left: { readonly id: string; readonly order: number },
  right: { readonly id: string; readonly order: number },
): number {
  return left.order - right.order || left.id.localeCompare(right.id);
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
