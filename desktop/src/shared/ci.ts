import type {
  DesktopRead,
  OpaqueHandle,
  ServiceErrorDto,
} from "./bridge.js";

export type CIMutationAction =
  | "retry_pipeline"
  | "cancel_pipeline"
  | "retry_job"
  | "cancel_job";

export interface CICapabilityFlags {
  readonly retry_pipeline: boolean;
  readonly cancel_pipeline: boolean;
  readonly retry_job: boolean;
  readonly cancel_job: boolean;
}

export interface CICapabilitiesResult {
  readonly repository: OpaqueHandle;
  readonly capabilities: CICapabilityFlags;
}

export interface PipelineMutationParams {
  readonly operation_id: string;
  readonly pipeline: OpaqueHandle;
}

export interface JobMutationParams extends PipelineMutationParams {
  readonly job: OpaqueHandle;
}

export type CIReceiptParams =
  | (PipelineMutationParams & {
      readonly action: "retry_pipeline" | "cancel_pipeline";
    })
  | (JobMutationParams & {
      readonly action: "retry_job" | "cancel_job";
    });

export interface CIMutationReceipt {
  readonly operation_id: string;
  readonly action: CIMutationAction;
  readonly outcome: "known" | "unknown";
  readonly error: ServiceErrorDto | null;
  readonly resync_required: boolean;
}

export interface CIReceiptResult {
  readonly receipt: CIMutationReceipt | null;
}

export type CIMutationIPCResult =
  | { readonly receipt: CIMutationReceipt; readonly error: null }
  | { readonly receipt: null; readonly error: ServiceErrorDto };

export interface CIDesktopBridge {
  getCICapabilities(repository: OpaqueHandle): DesktopRead<CICapabilitiesResult>;
  retryPipeline(params: PipelineMutationParams): Promise<CIMutationReceipt>;
  cancelPipeline(params: PipelineMutationParams): Promise<CIMutationReceipt>;
  retryJob(params: JobMutationParams): Promise<CIMutationReceipt>;
  cancelJob(params: JobMutationParams): Promise<CIMutationReceipt>;
  getCIReceipt(params: CIReceiptParams): DesktopRead<CIReceiptResult>;
}

export const CI_IPC_CHANNELS = Object.freeze({
  capabilities: "tongs:ci.capabilities",
  receipt: "tongs:ci.receipt",
  retryPipeline: "tongs:pipelines.retry",
  cancelPipeline: "tongs:pipelines.cancel",
  retryJob: "tongs:jobs.retry",
  cancelJob: "tongs:jobs.cancel",
} as const);
