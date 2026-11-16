"""Keep one-time setup tokens out of every log line.

Setup links have the form /account/setup/<token>. The token is a credential: anyone who reads it
before the intended administrator can take over the account.

Protection is installed on logging HANDLERS (not loggers), because Python applies a logger's
filters only to records created by that exact logger, never to records propagated from child
loggers such as ``uvicorn.asgi`` (which logs whole ASGI scopes at ``--log-level trace``).
Every handler that exists when the app starts gets three layers:

1. ``RedactArgsFilter``: redacts strings inside ``record.args`` recursively (tuples, lists, dicts,
   sets, bytes) WITHOUT changing their structure. Uvicorn's access formatter unpacks a 5-tuple.
2. ``RedactingFormatter``: wraps the handler's existing formatter and redacts the fully formatted
   output (message, nested structures, traceback, stack). It never raises: if formatting fails,
   it emits a placeholder with the arguments withheld, so Python's ``Handler.handleError`` (which
   prints raw arguments to stderr) is not triggered by formatting problems.
3. A safe ``handleError`` that reports a logging failure without printing the record's
   message or arguments.

The log *message template* is never modified, so parameterised messages such as
``log.info("/account/setup/%s", token)`` keep their placeholders and format correctly.

Reverse proxies keep their own access logs; see the README. Browser history still contains the
link: it is single-use and expires.
"""
from __future__ import annotations

import logging
import re
import sys
import types

# Any spelling of the setup path: case-insensitive, repeated separators, back-slashes and
# percent-encoded slashes (%2F). Everything after the prefix up to a delimiter is treated as token.
_SEP = r"(?:/|\\|%2f|%5c)+"
_SETUP_TOKEN = re.compile(rf"(?i)((?:{_SEP})account{_SEP}setup{_SEP})[^\s/?#\"'<>&\\]+")
REDACTED = "[REDACTED]"
_MAX_DEPTH = 6


def redact(text: str) -> str:
    return _SETUP_TOKEN.sub(lambda m: m.group(1) + REDACTED, text)


def redact_value(value, _depth: int = 0):
    """Redact strings (and bytes) anywhere inside a value, preserving its type and shape."""
    if _depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
        cleaned = redact(decoded)
        return cleaned.encode("utf-8") if cleaned != decoded else value
    if isinstance(value, tuple):
        return tuple(redact_value(v, _depth + 1) for v in value)
    if isinstance(value, list):
        return [redact_value(v, _depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return type(value)(redact_value(v, _depth + 1) for v in value)
    return value


class RedactArgsFilter(logging.Filter):
    """Handler filter: redacts record arguments in place (never drops a record)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = redact_value(record.args)
        return True


class RedactingFormatter(logging.Formatter):
    """Wraps a handler's formatter; redacts the final text and never raises."""

    def __init__(self, inner: logging.Formatter | None):
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        try:
            text = self.inner.format(record)
        except Exception as exc:  # noqa: BLE001 - logging must never fail or dump raw arguments
            template = record.msg if isinstance(record.msg, str) else type(record.msg).__name__
            text = (f"[unformattable log record from {record.name!r} ({type(exc).__name__}); "
                    f"arguments withheld] {template}")
        # Cached copies on the record are redacted too (other handlers / tools may reuse them).
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if getattr(record, "message", None):
            record.message = redact(record.message)
        return redact(text)

    def __getattr__(self, name):          # behave like the wrapped formatter (e.g. use_colors)
        if name == "inner":                # not yet assigned: avoid infinite recursion
            raise AttributeError(name)
        return getattr(self.inner, name)


def _safe_handle_error(self: logging.Handler, record: logging.LogRecord) -> None:
    """Replacement for Handler.handleError: never prints the record's message or arguments."""
    if not logging.raiseExceptions:
        return
    exc = sys.exc_info()[1]
    try:
        sys.stderr.write(f"--- Logging error in handler {type(self).__name__} for logger {record.name!r}: "
                         f"{type(exc).__name__ if exc else 'unknown'} (record contents withheld) ---\n")
    except Exception:  # noqa: BLE001
        pass


def protect_handler(handler: logging.Handler) -> None:
    """Install all three layers on one handler (idempotent)."""
    if not any(isinstance(f, RedactArgsFilter) for f in handler.filters):
        handler.addFilter(RedactArgsFilter())
    if not isinstance(handler.formatter, RedactingFormatter):
        handler.setFormatter(RedactingFormatter(handler.formatter))
    if not getattr(handler, "_bbms_safe_errors", False):
        handler.handleError = types.MethodType(_safe_handle_error, handler)
        handler._bbms_safe_errors = True


def _all_handlers() -> list[logging.Handler]:
    loggers = [logging.getLogger()] + [lg for lg in logging.Logger.manager.loggerDict.values()
                                       if isinstance(lg, logging.Logger)]
    seen: list[logging.Handler] = []
    for lg in loggers:
        for h in lg.handlers:
            if h not in seen:
                seen.append(h)
    return seen


def install() -> None:
    """Protect every handler currently attached anywhere (root and all named loggers). Idempotent.

    Called when the app is created and again at start-up, after Uvicorn has configured logging.
    Python's last-resort handler (used when a record finds no handler) is protected as well.
    """
    for handler in _all_handlers():
        protect_handler(handler)
    if logging.lastResort is not None:
        protect_handler(logging.lastResort)
