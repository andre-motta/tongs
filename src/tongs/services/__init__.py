"""Shared UI-independent application services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tongs.services.ci_mutations import (
    CancelJobCommand,
    CancelPipelineCommand,
    CIMutationAction,
    CIMutationCapabilities,
    CIMutationCommand,
    CIMutationOutcome,
    CIMutationReceipt,
    CIMutationService,
    JobMutationTarget,
    PipelineMutationTarget,
    RetryJobCommand,
    RetryPipelineCommand,
)
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import (
    ForgeCapabilities,
    HostFailure,
    JobRef,
    PipelineRef,
    RawDiffSnapshot,
    RepositoryRef,
    RepositorySnapshot,
    ReviewListItem,
    ReviewPage,
    ReviewQuery,
    ReviewRef,
    ReviewRevision,
    ReviewScope,
    ReviewSnapshot,
    ServiceEvent,
    ServiceEventKind,
)
from tongs.services.mr_actions import (
    CloseReviewCommand,
    MergeReviewCommand,
    MRAction,
    MRActionCapabilities,
    MRActionCommand,
    MRActionOutcome,
    MRActionReceipt,
    MRActionService,
    ReopenReviewCommand,
    ReviewActionTarget,
    SourceBranchTarget,
    UnapproveReviewCommand,
)
from tongs.services.review_mutations import (
    DiffAnchor,
    DiffSide,
    GeneralComment,
    InlineComment,
    InlineDraft,
    MutationOutcome,
    MutationReceipt,
    MutationStatus,
    Reply,
    Resolve,
    ReviewMutationCapabilities,
    ReviewMutationCommand,
    ReviewMutationService,
    ReviewVerdict,
)
from tongs.services.workspace_utilities import (
    EditorLogPlan,
    EditorPlanStatus,
    EditorReservation,
    ReviewUrl,
    WorkspaceUtilityService,
)

if TYPE_CHECKING:
    from tongs.services.review_submission import (
        ReviewSubmissionService,
        SubmissionFailure,
        SubmissionOutcome,
        SubmissionProgress,
        SubmissionStep,
        SubmissionStepKind,
    )
    from tongs.services.session import ApplicationSession


def __getattr__(name: str) -> object:
    """Load draft-dependent service exports after package initialization."""
    if name == "ApplicationSession":
        from tongs.services.session import ApplicationSession

        globals()[name] = ApplicationSession
        return ApplicationSession
    if name in {
        "ReviewSubmissionService",
        "SubmissionFailure",
        "SubmissionOutcome",
        "SubmissionProgress",
        "SubmissionStep",
        "SubmissionStepKind",
    }:
        from tongs.services.review_submission import (
            ReviewSubmissionService,
            SubmissionFailure,
            SubmissionOutcome,
            SubmissionProgress,
            SubmissionStep,
            SubmissionStepKind,
        )

        exports = {
            "ReviewSubmissionService": ReviewSubmissionService,
            "SubmissionFailure": SubmissionFailure,
            "SubmissionOutcome": SubmissionOutcome,
            "SubmissionProgress": SubmissionProgress,
            "SubmissionStep": SubmissionStep,
            "SubmissionStepKind": SubmissionStepKind,
        }
        globals().update(exports)
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ApplicationSession",
    "CIMutationAction",
    "CIMutationCapabilities",
    "CIMutationCommand",
    "CIMutationOutcome",
    "CIMutationReceipt",
    "CIMutationService",
    "CancelJobCommand",
    "CancelPipelineCommand",
    "CloseReviewCommand",
    "DiffAnchor",
    "DiffSide",
    "EditorLogPlan",
    "EditorPlanStatus",
    "EditorReservation",
    "ForgeCapabilities",
    "GeneralComment",
    "HostFailure",
    "InlineComment",
    "InlineDraft",
    "JobMutationTarget",
    "JobRef",
    "MRAction",
    "MRActionCapabilities",
    "MRActionCommand",
    "MRActionOutcome",
    "MRActionReceipt",
    "MRActionService",
    "MergeReviewCommand",
    "MutationOutcome",
    "MutationReceipt",
    "MutationStatus",
    "PipelineMutationTarget",
    "PipelineRef",
    "RawDiffSnapshot",
    "ReopenReviewCommand",
    "Reply",
    "RepositoryRef",
    "RepositorySnapshot",
    "Resolve",
    "RetryJobCommand",
    "RetryPipelineCommand",
    "ReviewActionTarget",
    "ReviewListItem",
    "ReviewMutationCapabilities",
    "ReviewMutationCommand",
    "ReviewMutationService",
    "ReviewPage",
    "ReviewQuery",
    "ReviewRef",
    "ReviewRevision",
    "ReviewScope",
    "ReviewSnapshot",
    "ReviewSubmissionService",
    "ReviewUrl",
    "ReviewVerdict",
    "ServiceError",
    "ServiceErrorCode",
    "ServiceEvent",
    "ServiceEventKind",
    "SourceBranchTarget",
    "SubmissionFailure",
    "SubmissionOutcome",
    "SubmissionProgress",
    "SubmissionStep",
    "SubmissionStepKind",
    "UnapproveReviewCommand",
    "WorkspaceUtilityService",
]
