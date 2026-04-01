"""Internal cleanup-resolution helpers for SPECTER ownership APIs."""

OWNERSHIP_STOP_METHODS = (
    'stop',
    'close',
    'disconnect',
    'destroy',
    'terminate',
    'kill',
)


def resolve_cleanup(resource, stop_method=None):
    """
    Resolve a no-arg cleanup callable for a resource.

    Args:
        resource: Owned object.
        stop_method: Optional explicit method name.

    Returns:
        A callable that cleans up the resource.

    Raises:
        TypeError: If no cleanup method can be resolved.
    """
    if resource is None:
        raise TypeError("[SPECTER] Cannot resolve cleanup for None")

    methods = [stop_method] if stop_method else OWNERSHIP_STOP_METHODS
    for method_name in methods:
        if not method_name:
            continue
        method = getattr(resource, method_name, None)
        if callable(method):
            return method

    raise TypeError(
        f"[SPECTER] Could not resolve cleanup method for "
        f"{type(resource).__name__}. Tried: {methods}"
    )
