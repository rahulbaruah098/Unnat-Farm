class ApiError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


class ApiValidationError(ApiError):
    def __init__(self, message="Invalid request.", details=None, code="VALIDATION_ERROR"):
        super().__init__(code, message, 400, details)


class ApiAuthenticationError(ApiError):
    def __init__(self, message="Authentication required.", code="AUTHENTICATION_REQUIRED"):
        super().__init__(code, message, 401)


class ApiPermissionError(ApiError):
    def __init__(self, message="You do not have permission to perform this action.", code="FORBIDDEN"):
        super().__init__(code, message, 403)
