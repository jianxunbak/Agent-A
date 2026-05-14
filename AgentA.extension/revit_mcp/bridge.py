import threading
import queue
import datetime
import os

# --- Bridge State ---
from .utils import get_log_path
LOG_PATH = get_log_path()
_work_queue = queue.Queue()
_uiapp = None
_dispatch_timer = None   # WPF DispatcherTimer
_external_event = None   # Revit ExternalEvent (Strategy 1)
_pump_handler = None     # Strong reference — prevents GC


def pump_commands(uiapp):
    """Drains the bridge work queue.
    MUST be called from the Revit main thread in API context.
    """
    global _uiapp
    _uiapp = uiapp

    while not _work_queue.empty():
        try:
            func, args, kwargs, event, result_wrapper, queued_at = _work_queue.get_nowait()

            from revit_mcp.cancel_manager import is_cancelled
            if is_cancelled():
                result_wrapper['error'] = RuntimeError("Build cancelled by user.")
                event.set()
                _work_queue.task_done()
                continue

            latency = (datetime.datetime.now() - queued_at).total_seconds()
            if latency > 1.0:
                with open(LOG_PATH, "a") as f:
                    f.write("[{}] Bridge: HIGH LATENCY ({:.2f}s) for {}\n".format(
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), latency, str(func)))

            try:
                result = func(*args, **kwargs)
                result_wrapper['data'] = result
            except Exception as e:
                import traceback
                with open(LOG_PATH, "a") as f:
                    f.write("[{}] Bridge EXECUTION ERROR: {}\n{}\n".format(
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        str(e), traceback.format_exc()))
                result_wrapper['error'] = e
            finally:
                event.set()
                _work_queue.task_done()
        except queue.Empty:
            break
        except Exception:
            break


def run_on_main_thread(func, *args, **kwargs):
    """Submit a function for execution on the Revit main thread. Blocks until done."""
    from revit_mcp.cancel_manager import is_cancelled
    if is_cancelled():
        raise RuntimeError("Build cancelled by user.")

    event = threading.Event()
    result_wrapper = {'data': None, 'error': None}
    queued_at = datetime.datetime.now()

    _work_queue.put((func, args, kwargs, event, result_wrapper, queued_at))

    deadline = queued_at + datetime.timedelta(seconds=1200)
    while not event.wait(0.5):
        if is_cancelled():
            raise RuntimeError("Build cancelled by user.")
        if datetime.datetime.now() >= deadline:
            with open(LOG_PATH, "a") as f:
                f.write("[{}] Bridge: TIMEOUT waiting for main thread execution of {}\n".format(
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(func)))
            raise TimeoutError("Revit main thread did not respond within 1200s.")

    if result_wrapper['error']:
        raise result_wrapper['error']

    return result_wrapper['data']


def init_bridge(uiapp):
    """Bridge initialization.

    Tries three strategies in order to ensure pump_commands() runs in Revit API
    context without requiring mouse movement:

    Strategy 1 — ExternalEvent + DispatcherTimer (preferred):
        Define the IExternalEventHandler class HERE (inside init_bridge) so it is
        created after the Revit API is fully loaded. The DispatcherTimer calls
        Raise() every 100ms; Revit schedules Execute() in proper API context.

    Strategy 2 — Win32 PostMessage + Idling handler (fallback):
        Post WM_NULL to Revit's main window every 100ms. This wakes up Revit's
        message loop and causes the already-registered Idling handler to fire
        in proper API context, which calls pump_commands().

    Strategy 3 — Idling-only (original behaviour):
        Works only when the user is active in Revit. Used only if both above fail.
    """
    global _uiapp, _dispatch_timer, _external_event, _pump_handler
    _uiapp = uiapp

    if _dispatch_timer is not None:
        try:
            _dispatch_timer.Stop()
        except:
            pass
        _dispatch_timer = None

    def _log(msg):
        with open(LOG_PATH, "a") as f:
            f.write("[{}] Bridge: {}\n".format(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))

    # ------------------------------------------------------------------ #
    # Strategy 1: ExternalEvent                                           #
    # ------------------------------------------------------------------ #
    try:
        from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent  # type: ignore
        from System.Windows.Threading import DispatcherTimer, DispatcherPriority  # type: ignore
        from System import TimeSpan  # type: ignore

        # Class must be defined here (post API load) for IronPython to
        # properly inherit the .NET interface.
        class _LocalPumpHandler(IExternalEventHandler):
            def Execute(self, app):
                try:
                    pump_commands(app)
                except:
                    pass

            def GetName(self):
                return "BridgePump"

        _pump_handler = _LocalPumpHandler()
        _external_event = ExternalEvent.Create(_pump_handler)

        def _tick_ext(_s, _e):
            if not _work_queue.empty() and _external_event is not None:
                try:
                    _external_event.Raise()
                except:
                    pass

        _dispatch_timer = DispatcherTimer(DispatcherPriority.Background)
        _dispatch_timer.Interval = TimeSpan.FromMilliseconds(100)
        _dispatch_timer.Tick += _tick_ext
        _dispatch_timer.Start()

        _log("ExternalEvent+DispatcherTimer started (100ms) — mouse-independent pump active.")
        _log("Initialized (v11-EXTERNAL-EVENT).")
        return True

    except Exception as e:
        _log("Strategy 1 (ExternalEvent) failed: {}".format(str(e)))

    # ------------------------------------------------------------------ #
    # Strategy 2: Win32 PostMessage to keep Idling loop alive             #
    # ------------------------------------------------------------------ #
    try:
        import ctypes  # type: ignore
        from System.Windows.Threading import DispatcherTimer, DispatcherPriority  # type: ignore
        from System import TimeSpan  # type: ignore

        WM_NULL = 0
        _user32 = ctypes.windll.user32

        def _tick_win32(_s, _e):
            if not _work_queue.empty():
                try:
                    hwnd = uiapp.MainWindowHandle.ToInt64()
                    _user32.PostMessageW(hwnd, WM_NULL, 0, 0)
                except:
                    pass

        _dispatch_timer = DispatcherTimer(DispatcherPriority.Background)
        _dispatch_timer.Interval = TimeSpan.FromMilliseconds(100)
        _dispatch_timer.Tick += _tick_win32
        _dispatch_timer.Start()

        _log("Win32 PostMessage+DispatcherTimer started (100ms) — Idling-wake fallback active.")
        _log("Initialized (v11-WIN32-FALLBACK).")
        return True

    except Exception as e:
        _log("Strategy 2 (Win32 PostMessage) failed: {}".format(str(e)))

    # ------------------------------------------------------------------ #
    # Strategy 3: Idling-only (original — requires mouse activity)        #
    # ------------------------------------------------------------------ #
    _log("WARNING: Both ExternalEvent and Win32 fallback failed. Idling-only mode active (mouse required).")
    _log("Initialized (v11-IDLING-ONLY).")
    return True


def idling_handler(sender, args):
    """Event handler for UIApplication.Idling. Provides valid API context.
    Used directly by Strategy 3, and triggered indirectly by Strategy 2.
    """
    try:
        pump_commands(sender)
    except:
        pass


# Backward-compatibility shim
class MCPEventHandler:
    def run_on_main_thread(self, func, *args, **kwargs):
        return run_on_main_thread(func, *args, **kwargs)


mcp_event_handler = MCPEventHandler()
