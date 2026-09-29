from pathlib import Path
from typing import Any

from requests import HTTPError


class SafetyError(Exception):
    """A machine-readable, secret-free reason to stop the pipeline."""

    def __init__(self, code: str, *, details: dict[str, Any] | None = None):
        self.code = code
        # Only explicitly selected diagnostic fields; never SDK bodies, messages or inputs.
        self.details = details or {}
        super().__init__(code)


def error_details(exc: Exception) -> dict[str, Any]:
    """Safe metadata only: no exception text, source lines, locals or chained exceptions."""
    if isinstance(exc, SafetyError):
        return dict(exc.details)
    details: dict[str, Any] = {"exception_type": type(exc).__name__}
    if isinstance(exc, HTTPError) and exc.response is not None:
        details["http_status"] = exc.response.status_code
    frame = exc.__traceback__
    while frame is not None:
        code = frame.tb_frame.f_code
        # Report the innermost application location, never absolute paths from an SDK.
        if Path(code.co_filename).parent == Path(__file__).parent:
            details["source"] = {
                "file": Path(code.co_filename).name,
                "function": code.co_name,
                "line": frame.tb_lineno,
            }
        frame = frame.tb_next
    return details
