class ServiceError(Exception):
    """A business-rule violation with a user-presentable message."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class PermissionDenied(ServiceError):
    pass


class InventoryError(ServiceError):
    pass


class EligibilityError(ServiceError):
    pass


class RecordNotFound(ServiceError):
    """Missing OR belonging to another facility (deliberately indistinguishable)."""
