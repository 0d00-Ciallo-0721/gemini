from gemini_webapi.exceptions import AuthError

class RelayAuthError(AuthError):
    """Base class for Relay Authentication Exceptions."""
    pass

class RelaySignatureError(RelayAuthError):
    """Raised when signature verification fails."""
    pass

class RelayTicketExpired(RelayAuthError):
    """Raised when the active ticket is definitively expired and unrecoverable."""
    pass

class RelayRefreshFailed(RelayAuthError):
    """Raised when an attempt to refresh the active ticket fails."""
    pass

class RuntimeStateCorrupted(RelayAuthError):
    """Raised when the runtime authorization state file is corrupted or unreadable."""
    pass

class UpstreamNetworkError(RelayAuthError):
    """Raised when an external network issue prevents reaching the upstream provider."""
    pass

class ProxyException(Exception):
    """Base class for Proxy mapped API errors."""
    def __init__(self, message, error_type):
        super().__init__(message)
        self.error_type = error_type

class ModelNotSupportedError(ProxyException):
    def __init__(self, message):
        super().__init__(message, "MODEL_NOT_SUPPORTED")

class AuthInvalidError(ProxyException):
    def __init__(self, message):
        super().__init__(message, "AUTH_INVALID")

class NetworkOrProxyError(ProxyException):
    def __init__(self, message):
        super().__init__(message, "NETWORK_OR_PROXY_ERROR")

class GoogleSilentAbortError(ProxyException):
    def __init__(self, message):
        super().__init__(message, "GOOGLE_SILENT_ABORT")

class UnknownUpstreamError(ProxyException):
    def __init__(self, message):
        super().__init__(message, "UNKNOWN_UPSTREAM_ERROR")

class UpstreamQueueTimeoutError(ProxyException):
    def __init__(self, message):
        super().__init__(message, "UPSTREAM_QUEUE_TIMEOUT")

class IPBlockedError(ProxyException):
    def __init__(self, message):
        super().__init__(message, "IP_BLOCKED")

