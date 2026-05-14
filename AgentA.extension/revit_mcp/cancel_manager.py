# -*- coding: utf-8 -*-
import threading

_cancel_event = threading.Event()


def request_cancel():
    _cancel_event.set()


def clear_cancel():
    _cancel_event.clear()


def is_cancelled():
    return _cancel_event.is_set()


def check_cancelled(label=""):
    """Raise RuntimeError if cancellation has been requested."""
    if _cancel_event.is_set():
        raise RuntimeError("Build cancelled by user" + (" ({})".format(label) if label else "."))
