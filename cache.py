"""Response cache hook used by the lever package.

The hosted API keeps responses in a database; this package keeps none, so
calls simply run. `DontCache` is the marker lever/jobs.py wraps a result in
when it must not be stored (a search answered from a half-warm index);
routes.py unwraps it before replying."""


class DontCache:
    """Wrap a result to return it without storing it."""

    def __init__(self, data):
        self.data = data


def cached_call(namespace, data, fn, ttl, cache=True, refresh=False):
    """Run fn(**data). Same signature as the hosted cache."""
    result = fn(**data)
    return result.data if isinstance(result, DontCache) else result
