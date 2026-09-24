"""Application-level failures independent of protocol and infrastructure."""


class DocumentError(Exception):
    """Base class for safe document-processing failures."""


class EmptyDocumentError(DocumentError):
    """The uploaded or stored document has no content."""


class DocumentTooLargeError(DocumentError):
    """The document exceeds the configured upload limit."""


class UnsupportedMediaTypeError(DocumentError):
    """The declared or detected media type is unsupported."""


class InvalidDocumentError(DocumentError):
    """The document is corrupt, inconsistent, encrypted, or otherwise invalid."""


class DocumentPageLimitError(InvalidDocumentError):
    """A PDF exceeds the configured page limit."""


class DocumentIntegrityError(DocumentError):
    """Stored bytes no longer match their ingestion checksum."""


class StorageError(DocumentError):
    """File storage could not safely persist or retrieve a document."""

    reason_code = "storage.temporarily_unavailable"


class StorageObjectNotFoundError(StorageError):
    reason_code = "storage.object_not_found"


class StorageChecksumMismatchError(StorageError):
    reason_code = "storage.checksum_mismatch"


class StoragePermissionDeniedError(StorageError):
    reason_code = "storage.permission_denied"


class StorageInvalidReferenceError(StorageError):
    reason_code = "storage.invalid_reference"


class VisionExtractionError(Exception):
    """A vision provider failed to return a valid structured result."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "vision.extraction_failed",
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.trace_id = trace_id


class VisionProviderConfigurationError(VisionExtractionError):
    """The provider or requested output schema is not validly configured."""


class WorkflowError(Exception):
    """Base class for deterministic workflow failures."""


class WorkflowIdentityError(WorkflowError):
    """Runtime checkpoint identity does not match persisted workflow identity."""


class HumanCorrectionError(WorkflowError):
    """Human correction payload is malformed or cannot satisfy the output schema."""


class WorkflowPersistenceError(WorkflowError):
    """Business workflow data could not be persisted idempotently."""


class CorrectionMemoryError(WorkflowError):
    """Correction-memory embedding, redaction, or vector persistence failed."""


class FieldSemanticIndexError(WorkflowError):
    """Field semantic projection or derived-index persistence failed."""


class TrainingDataError(WorkflowError):
    """Controlled mining, review, redaction, or dataset export failed."""


class TrainingProviderError(Exception):
    """Base failure raised by an asynchronous training provider."""


class TrainingProviderPermanentError(TrainingProviderError):
    """Configuration or provider-contract failure that must be quarantined."""


class TrainingProviderUnavailableError(TrainingProviderError):
    """Temporary provider failure eligible for bounded worker retry."""


class RemoteInferenceError(Exception):
    """A remote inference provider failed without exposing provider SDK details."""


class RemoteInferenceRequestError(RemoteInferenceError):
    """A remote request, authorization, or response Schema is permanently invalid."""


class RemoteInferenceUnavailableError(RemoteInferenceError):
    """A remote service is temporarily unavailable after bounded provider retries."""


class ApplicationServiceError(Exception):
    """Base class for failures intentionally mapped by protocol adapters."""


class BadRequestError(ApplicationServiceError):
    """Required request semantics are absent or malformed."""


class UnprocessableEntityError(ApplicationServiceError):
    """A well-formed request violates the governed domain contract."""


class ResourceNotFoundError(ApplicationServiceError):
    """The requested business resource does not exist."""


class ResourceConflictError(ApplicationServiceError):
    """The request conflicts with immutable or current resource state."""


class IdempotencyConflictError(ResourceConflictError):
    """An idempotency key was reused with a different request."""


class IdempotencyInProgressError(ResourceConflictError):
    """An equivalent idempotent request is already being processed."""


class ForbiddenError(ApplicationServiceError):
    """The trusted request identity lacks the required tenant permission."""


class UnauthorizedError(ApplicationServiceError):
    """The request has no valid trusted authentication context."""


class RateLimitExceededError(ApplicationServiceError):
    """A configured application or upstream request quota was exceeded."""


class ServiceUnavailableError(ApplicationServiceError):
    """A required configured application capability is unavailable."""
