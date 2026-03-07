# Debug middleware: logs 500s to stderr (docker logs) and optionally to file
import json
import sys
import traceback
from django.conf import settings


def _log(payload):
    path = getattr(settings, "DEBUG_LOG_PATH", None)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass


class DebugLogMiddleware:
    """Log request start and any uncaught exception; print traceback to stderr for docker logs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            _log({
                "sessionId": "e47e3c",
                "runId": "request",
                "hypothesisId": "H1",
                "location": "config/debug_middleware.py:request_start",
                "message": "Request start",
                "data": {
                    "path": request.path,
                    "host": request.get_host(),
                    "allowed_hosts": getattr(settings, "ALLOWED_HOSTS", []),
                },
                "timestamp": __import__("time").time() * 1000,
            })
        except Exception:
            pass

        try:
            return self.get_response(request)
        except Exception as exc:
            tb = traceback.format_exc()
            msg = f"{type(exc).__name__}: {exc}"
            try:
                _log({
                    "sessionId": "e47e3c",
                    "runId": "exception",
                    "hypothesisId": "H2_H3_H4_H5",
                    "location": "config/debug_middleware.py:exception",
                    "message": "Uncaught exception",
                    "data": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "path": getattr(request, "path", None),
                        "host": getattr(request, "get_host", lambda: None)(),
                        "traceback": tb,
                    },
                    "timestamp": __import__("time").time() * 1000,
                })
            except Exception:
                pass
            print(f"\n[SWIVL 500] {msg}\n{tb}", file=sys.stderr, flush=True)
            raise
