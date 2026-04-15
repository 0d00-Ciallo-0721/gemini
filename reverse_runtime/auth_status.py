from enum import Enum

class AuthStatus(str, Enum):
    HEALTHY = "healthy"
    STALE = "stale"
    EXPIRED = "expired"
    FALLBACK = "fallback"
    INVALIDATED = "invalidated"
    RECOVERING = "recovering"
    
    def __str__(self):
        return self.value
