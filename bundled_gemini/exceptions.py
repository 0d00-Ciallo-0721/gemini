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
