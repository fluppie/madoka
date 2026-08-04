

import logging

# Extra-low verbosity TRACE level (lower than DEBUG)
TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")

def trace(self, msg, *args, **kwargs):
    if self.isEnabledFor(TRACE_LEVEL):
        self._log(TRACE_LEVEL, msg, args, **kwargs)

logging.Logger.trace = trace


def format_hex(obj):
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).hex()
    if isinstance(obj, dict):
        return {format_hex(k): format_hex(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [format_hex(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(format_hex(v) for v in obj)
    if isinstance(obj, set):
        return {format_hex(v) for v in obj}
    return obj


# Minimum width for the log context string (including trailing space).
# Adjust this constant if you want wider or narrower aligned prefixes.
LOG_CONTEXT_WIDTH = 16

def log_context(obj, width=LOG_CONTEXT_WIDTH):
    for attr in ("name", "_attr_name", "addr", "_mac"):
        if hasattr(obj, attr):
            val = getattr(obj, attr)
            base = f"<{val}>"
            # Ensure the returned string (including trailing space) is at least `width` chars.
            if len(base) + 1 < width:
                base = base.ljust(width - 1)
            return f"{base} "
    # No context available; return spaces to preserve alignment
    return " " * width


def log_trace(logger, msg, obj=None):
    """Log at TRACE level only if TRACE is enabled. Prepends log_context if obj provided."""
    if logger.isEnabledFor(TRACE_LEVEL):
        context = log_context(obj) if obj else ""
        logger.trace(f"{context}{msg}")


def log_debug(logger, msg, obj=None):
    """Log at DEBUG level only if DEBUG is enabled. Prepends log_context if obj provided."""
    if logger.isEnabledFor(logging.DEBUG):
        context = log_context(obj) if obj else ""
        logger.debug(f"{context}{msg}")


def log_info(logger, msg, obj=None):
    """Log at INFO level. Prepends log_context if obj provided."""
    context = log_context(obj) if obj else ""
    logger.info(f"{context}{msg}")


def log_warning(logger, msg, obj=None):
    """Log at WARNING level. Prepends log_context if obj provided."""
    context = log_context(obj) if obj else ""
    logger.warning(f"{context}{msg}")


def log_error(logger, msg, obj=None):
    """Log at ERROR level. Prepends log_context if obj provided."""
    context = log_context(obj) if obj else ""
    logger.error(f"{context}{msg}")


def log_exception(logger, msg, obj=None):
    """Log at EXCEPTION level. Prepends log_context if obj provided."""
    context = log_context(obj) if obj else ""
    logger.exception(f"{context}{msg}")
