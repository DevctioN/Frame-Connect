"""Local, size-limited diagnostics, initialized before importing the GUI."""

import atexit
from datetime import datetime
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading


MAX_LOG_BYTES = 1024 * 1024
LOGGER = logging.getLogger("frame_connect")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
LOGGER.addHandler(logging.NullHandler())

_directory = None
_handler = None
_crash_stream = None
_errors = []
_previous_sys_hook = None
_previous_thread_hook = None
_previous_qt_handler = None
_qt_handler_installed = False
_exit_registered = False


def get_log_directory() -> Path | None:
    return _directory


def get_logging_error() -> str:
    return "\n".join(_errors)


def configure_logging(directory: Path | None = None) -> None:
    """Failure to create logs must not prevent the application from starting."""
    global _directory, _handler, _exit_registered
    if _handler is not None:
        return
    _errors.clear()
    if directory is None:
        local_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        directory = local_data / "Frame Connect" / "logs"
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "frame-connect-log.txt", maxBytes=MAX_LOG_BYTES,
            backupCount=2, encoding="utf-8",
        )
    except OSError as error:
        _errors.append(f"Could not create diagnostic log: {error}")
        return
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(process)d:%(threadName)s] %(message)s",
    ))
    LOGGER.addHandler(handler)
    _handler, _directory = handler, directory
    _install_exception_hooks()
    _enable_crash_logging()
    if not _exit_registered:
        atexit.register(shutdown_logging)
        _exit_registered = True


def _log_exception(exception_type, exception, traceback):
    if not issubclass(exception_type, KeyboardInterrupt):
        LOGGER.critical("Unhandled Python exception", exc_info=(exception_type, exception, traceback))
    if sys.stderr is not None and _previous_sys_hook is not None:
        _previous_sys_hook(exception_type, exception, traceback)


def _log_thread_exception(args):
    if not issubclass(args.exc_type, SystemExit):
        name = args.thread.name if args.thread is not None else "unknown"
        LOGGER.critical(
            "Unhandled exception in background thread %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    if sys.stderr is not None and _previous_thread_hook is not None:
        _previous_thread_hook(args)


def _install_exception_hooks():
    global _previous_sys_hook, _previous_thread_hook
    _previous_sys_hook, _previous_thread_hook = sys.excepthook, threading.excepthook
    sys.excepthook, threading.excepthook = _log_exception, _log_thread_exception


def _enable_crash_logging():
    global _crash_stream
    # A rotating logger closes its old file descriptor. Keep a separate file
    # open for faulthandler, which needs the same descriptor until shutdown.
    if faulthandler.is_enabled():
        return
    stream = None
    try:
        path = _directory / "frame-connect-crash.txt"
        if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
            path.replace(_directory / "frame-connect-crash.previous.txt")
        stream = path.open("a", encoding="utf-8")
        stream.write(f"\n--- Session {datetime.now().isoformat(timespec='seconds')} (PID {os.getpid()}) ---\n")
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
        _crash_stream = stream
    except (OSError, RuntimeError) as error:
        if stream is not None:
            stream.close()
        _errors.append(f"Native crash logging unavailable: {error}")
        LOGGER.warning("%s", _errors[-1])


def _log_qt_message(kind, context, message):
    from PySide6.QtCore import QtMsgType

    level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(kind, logging.WARNING)
    LOGGER.log(level, "Qt: %s", message)
    if _previous_qt_handler is not None:
        _previous_qt_handler(kind, context, message)


def install_qt_message_handler() -> None:
    global _previous_qt_handler, _qt_handler_installed
    from PySide6.QtCore import qInstallMessageHandler

    if not _qt_handler_installed:
        _previous_qt_handler = qInstallMessageHandler(_log_qt_message)
        _qt_handler_installed = True


def shutdown_logging() -> None:
    """Close owned resources; also lets tests restore process-global hooks."""
    global _directory, _handler, _crash_stream
    global _previous_sys_hook, _previous_thread_hook, _previous_qt_handler, _qt_handler_installed
    if _qt_handler_installed:
        from PySide6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(_previous_qt_handler)
        _previous_qt_handler, _qt_handler_installed = None, False
    if sys.excepthook is _log_exception:
        sys.excepthook = _previous_sys_hook
    if threading.excepthook is _log_thread_exception:
        threading.excepthook = _previous_thread_hook
    _previous_sys_hook = _previous_thread_hook = None
    if _crash_stream is not None:
        faulthandler.disable()
        _crash_stream.close()
        _crash_stream = None
    if _handler is not None:
        LOGGER.info("Frame Connect process exiting")
        LOGGER.removeHandler(_handler)
        _handler.close()
        _handler = None
    _directory = None
