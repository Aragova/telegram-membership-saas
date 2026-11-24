"""Safe diagnostic metadata without exception payloads or source text."""


def safe_failure_site(exc: BaseException) -> str:
    """Return the deepest application frame, not a raw traceback or chain."""
    site = "unknown"
    frame = exc.__traceback__
    while frame is not None:
        module = frame.tb_frame.f_globals.get("__name__", "")
        if isinstance(module, str) and module.startswith("telegram_membership_saas."):
            function = frame.tb_frame.f_code.co_name
            site = f"{module}:{function}:{frame.tb_lineno}"
        frame = frame.tb_next
    return site
