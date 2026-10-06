from __future__ import annotations

from datetime import datetime


class ActiveDNSError(Exception):
    """Base class of every error this package raises."""


class TransportError(ActiveDNSError):
    """The request did not get an answer: a connection failure or a timeout."""


class APIError(ActiveDNSError):
    """An error response from the API.

    Attributes:
        status_code: HTTP status.
        message: The server's error message.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
        self.message = message


class UnauthorizedError(APIError):
    """401: the token is unknown or revoked.

    After a 401 the client sends no further requests; every later call
    raises the same error.
    """


class ForbiddenError(APIError):
    """403: the token does not allow the query.

    The cause is the query type, the width of a network, a wildcard or the
    cursor. Other queries are unaffected.
    """


class RateLimitError(ActiveDNSError):
    """A rate limit is reached and the client did not wait it out.

    Raised when the wait is longer than ``max_wait`` or the retries are used
    up.

    Attributes:
        exceeded: The limit: ``"address"`` (the client's network address),
            ``"network"`` (the network around it) or ``"token"``. ``None``
            if the server did not say.
        retry_at: When the limit allows another request. ``None`` if unknown.
        message: The server's error message.
        local: ``True`` when the request was not sent, because an earlier
            response had already reported the limit.
    """

    def __init__(
        self,
        message: str,
        *,
        exceeded: str | None = None,
        retry_at: datetime | None = None,
        local: bool = False,
    ) -> None:
        text = f"rate limited: {message}" if message else "rate limited"
        if retry_at is not None:
            text += f" (retry at {retry_at.isoformat(timespec='seconds')})"
        super().__init__(text)
        self.message = message
        self.exceeded = exceeded
        self.retry_at = retry_at
        self.local = local
