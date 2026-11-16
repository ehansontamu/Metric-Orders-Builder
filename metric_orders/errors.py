class SyncError(RuntimeError):
    """An actionable failure that can be logged without exposing credentials."""


class ApiError(SyncError):
    def __init__(self, service, status, code="unknown", headers=None):
        self.service = service
        self.status = status
        self.code = code
        self.headers = headers or {}
        super().__init__(f"{service}: HTTP {status} ({code})")


class AmbiguousWriteError(SyncError):
    """A create may have succeeded; reconcile from SharePoint before trying again."""
