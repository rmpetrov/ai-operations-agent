"""One local, shared request ceiling; not a provider-side billing cap."""

from threading import Lock

MAX_SMOKE_REQUESTS = 9


class ProviderRequestBudget:
    """Reserve immediately before each SDK request, including requests that later fail.

    Trusted Python code owns this object. It survives adapter run-scope cleanup and is
    never passed in a model conversation or accepted from tool arguments.
    """

    def __init__(self, max_requests: int = MAX_SMOKE_REQUESTS):
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_SMOKE_REQUESTS:
            raise ValueError("provider request budget must be an integer from 1 to 9")
        self._limit = max_requests
        self._used = 0
        self._blocked = False
        self._lock = Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._used >= self._limit

    @property
    def blocked(self) -> bool:
        with self._lock:
            return self._blocked

    def reserve(self) -> bool:
        with self._lock:
            if self._used >= self._limit:
                self._blocked = True
                return False
            self._used += 1
            return True
