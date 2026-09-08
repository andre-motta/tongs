const { contextBridge, ipcRenderer } = require("electron") as typeof import("electron");
import type {
  AcceptedResult, AssetDescriptor, CommitsResult, DesktopBridge, DesktopEvent,
  DesktopRead, DiffPage, DiscussionsResult, InvokePluginParams, JobsResult,
  JsonValue, ListPipelinesParams, ListReviewPipelinesParams, ListReviewsParams,
  LocationParams, LogPage, OpenDiffParams, OpenLogParams, OpenRepositoryParams,
  PageParams, PipelinesResult, PluginResult, PluginsResult,
  RepositoryDto, RepositoryListResult, ReviewListResult, ReviewSnapshotDto,
} from "../shared/bridge.js";

const IPC_CHANNELS = Object.freeze({
  discoverRepositories: "tongs:repositories.discover", openRepository: "tongs:repositories.open", listReviews: "tongs:reviews.list", getReview: "tongs:reviews.get",
  openDiff: "tongs:diff.open", pageDiff: "tongs:diff.page", listDiscussions: "tongs:discussions.list", listCommits: "tongs:commits.list",
  listPipelines: "tongs:pipelines.list", listReviewPipelines: "tongs:review-pipelines.list", listJobs: "tongs:jobs.list", openLog: "tongs:logs.open", pageLog: "tongs:logs.page",
  listPlugins: "tongs:plugins.list", invokePlugin: "tongs:plugins.invoke", setLocation: "tongs:host.set-location", listAssets: "tongs:assets.list",
  cancelRead: "tongs:read.cancel", openExternal: "tongs:external.open", event: "tongs:event",
} as const);

const owned = new Set<string>();
function read<T>(channel: string, params: object): DesktopRead<T> {
  const requestToken = crypto.randomUUID();
  owned.add(requestToken);
  const result = ipcRenderer.invoke(channel, { requestToken, params }).finally(() => owned.delete(requestToken)) as Promise<T>;
  return Object.freeze({ requestToken, result });
}
const bridge: DesktopBridge = Object.freeze({
  discoverRepositories: (): DesktopRead<RepositoryListResult> => read(IPC_CHANNELS.discoverRepositories, {}),
  openRepository: (params: OpenRepositoryParams): DesktopRead<RepositoryDto> => read(IPC_CHANNELS.openRepository, params),
  listReviews: (params: ListReviewsParams): DesktopRead<ReviewListResult> => read(IPC_CHANNELS.listReviews, params),
  getReview: (review: string): DesktopRead<ReviewSnapshotDto> => read(IPC_CHANNELS.getReview, { review }),
  openDiff: (params: OpenDiffParams): DesktopRead<DiffPage> => read(IPC_CHANNELS.openDiff, params),
  pageDiff: (params: PageParams): DesktopRead<DiffPage> => read(IPC_CHANNELS.pageDiff, params),
  listDiscussions: (review: string): DesktopRead<DiscussionsResult> => read(IPC_CHANNELS.listDiscussions, { review }),
  listCommits: (review: string): DesktopRead<CommitsResult> => read(IPC_CHANNELS.listCommits, { review }),
  listPipelines: (params: ListPipelinesParams): DesktopRead<PipelinesResult> => read(IPC_CHANNELS.listPipelines, params),
  listReviewPipelines: (params: ListReviewPipelinesParams): DesktopRead<PipelinesResult> => read(IPC_CHANNELS.listReviewPipelines, params),
  listJobs: (pipeline: string): DesktopRead<JobsResult> => read(IPC_CHANNELS.listJobs, { pipeline }),
  openLog: (params: OpenLogParams): DesktopRead<LogPage> => read(IPC_CHANNELS.openLog, params),
  pageLog: (params: PageParams): DesktopRead<LogPage> => read(IPC_CHANNELS.pageLog, params),
  listPlugins: (): DesktopRead<PluginsResult> => read(IPC_CHANNELS.listPlugins, {}),
  invokePlugin: (params: InvokePluginParams): DesktopRead<PluginResult> => read(IPC_CHANNELS.invokePlugin, params),
  setLocation: async (params: LocationParams): Promise<AcceptedResult> => ipcRenderer.invoke(IPC_CHANNELS.setLocation, params) as Promise<AcceptedResult>,
  listAssets: (): DesktopRead<readonly AssetDescriptor[]> => read(IPC_CHANNELS.listAssets, {}),
  cancelRead: async (token: string): Promise<boolean> => owned.has(token) && await ipcRenderer.invoke(IPC_CHANNELS.cancelRead, token) === true,
  onEvent: (listener: (event: DesktopEvent) => void): (() => void) => {
    const wrapped = (_event: unknown, value: JsonValue): void => listener(value as unknown as DesktopEvent);
    ipcRenderer.on(IPC_CHANNELS.event, wrapped);
    return () => ipcRenderer.removeListener(IPC_CHANNELS.event, wrapped);
  },
  openExternal: async (url: string): Promise<boolean> => await ipcRenderer.invoke(IPC_CHANNELS.openExternal, url) === true,
});
contextBridge.exposeInMainWorld("tongs", bridge);
