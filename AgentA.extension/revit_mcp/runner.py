# -*- coding: utf-8 -*-
# RUNNER VERSION: v9-SHUTDOWN
from __future__ import print_function
import threading
import traceback
import sys
import os
import time
import socket

# --- LOGGING ---
from .utils import get_log_path
LOG_FILE = get_log_path()

def log(msg):
    try:
        path = get_log_path()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        formatted = "[{}] {}\n".format(timestamp, str(msg))
        with open(path, "a") as f:
            f.write(formatted)
            f.flush()
    except:
        pass

class SilentIO(object):
    """Replaces Revit's ScriptIO - routes all output to our log file."""
    def write(self, s):
        # Background threads MUST NOT touch the original sys.stdout (WPF console)
        if s and s.strip():
            try:
                log(s.rstrip())
            except:
                pass
    def flush(self): pass
    def isatty(self): return False
    @property
    def encoding(self): return "utf-8"
    def writelines(self, lines):
        for line in lines:
            self.write(line)

# --- GLOBALS ---
_server_thread = None
_uvicorn_server = None  # Reference to the uvicorn Server object for shutdown

def stop_server():
    """Signal the running uvicorn server to stop. Call before starting a new one."""
    global _uvicorn_server
    if _uvicorn_server is not None:
        try:
            _uvicorn_server.should_exit = True
            log("Shutdown: Signaled uvicorn to stop.")
            time.sleep(1)  # Give it a moment to release the port
        except:
            pass
        _uvicorn_server = None

def is_port_in_use(port=8001):
    """Check if port is in use by attempting a connection (not bind)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        result = s.connect_ex(('127.0.0.1', port))
        return result == 0  # 0 = connected = port in use
    except:
        return False
    finally:
        try:
            s.close()
        except:
            pass


def run_uvicorn_process(mcp_instance):
    """Background thread. SilentIO redirect must happen before any library code."""
    global _uvicorn_server
    sys.stdout = SilentIO()
    sys.stderr = SilentIO()
    log("UvicornThread: IO replaced - RUNNING")

    try:
        log("UvicornThread: Importing uvicorn...")
        import uvicorn
        log("UvicornThread: Importing asyncio...")
        import asyncio
        log("UvicornThread: Building SSE app...")
        app = mcp_instance.sse_app()
        log("UvicornThread: SSE app ready.")

        config = uvicorn.Config(
            app=app,
            host="0.0.0.0",
            port=8001,
            loop="asyncio",
            log_level="info",
            log_config={
                "version": 1,
                "disable_existing_loggers": False,
                "handlers": {"null": {"class": "logging.NullHandler"}},
                "loggers": {
                    "uvicorn":        {"handlers": ["null"], "level": "INFO", "propagate": False},
                    "uvicorn.error":  {"handlers": ["null"], "level": "INFO", "propagate": False},
                    "uvicorn.access": {"handlers": ["null"], "level": "INFO", "propagate": False},
                },
            },
            workers=1,
            timeout_keep_alive=30,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        _uvicorn_server = server  # Store for shutdown

        log("UvicornThread: Creating asyncio loop...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        log("UvicornThread: server.serve() STARTING on 0.0.0.0:8001")
        loop.run_until_complete(server.serve())
        log("UvicornThread: server.serve() exited.")

    except BaseException as e:
        log("UvicornThread KILLED ({}) : {}".format(type(e).__name__, str(e)))
        log(traceback.format_exc())
    finally:
        _uvicorn_server = None
        log("UvicornThread: Thread ending.")


def start_mcp_server():
    """Called by script.py. Returns True on success, False if already running."""
    global _server_thread
    log("Main: start_mcp_server() entered.")

    # If we have a running server, ensure we still refresh the Revit app reference
    if is_port_in_use(8001):
        log("Main: Port 8001 in use. Re-linking existing server context.")
        try:
            from revit_mcp.server import set_revit_app
            uiapp = __revit__
            set_revit_app(uiapp)
            from revit_mcp.gemini_client import client # Pre-cache to avoid background thread import hangs
            from revit_mcp.bridge import init_bridge
            init_bridge(uiapp)
            log("Main: Existing server context re-linked and bridge re-initialized.")
            return True 
        except Exception as e:
            log("Main: Re-link failed: " + str(e))
            return False

    try:
        import asyncio
        import importlib
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        from revit_mcp.gemini_client import client # Pre-import on main thread
        from revit_mcp.server import mcp, set_revit_app
        from revit_mcp.bridge import init_bridge
        
        try:
            from pyrevit import HOST_APP
            uiapp = HOST_APP.uiapp
            set_revit_app(uiapp)
            log("Main: UIApplication stored via set_revit_app.")
            init_bridge(uiapp)
        except Exception as e:
            log("Main: init_bridge failed: " + str(e))

        log("Main: All components pre-loaded.")

        # Pre-warm RAG imports in background so first query_vertex_rag() call is instant
        try:
            from revit_mcp.config import RAG_ENABLED
            if RAG_ENABLED:
                from revit_mcp.rag.vertex_rag import _ensure_imported
                _warmup = threading.Thread(target=_ensure_imported, args=(30.0,), daemon=True, name="RAG_Warmup")
                _warmup.start()
                log("Main: RAG import warmup thread started.")
        except Exception as _e:
            log("Main: RAG warmup skipped: " + str(_e))

    except Exception as e:
        log("Main Init ERROR: " + str(e))
        log(traceback.format_exc())
        return False

    log("Main: Spawning server thread (daemon=True)...")
    _server_thread = threading.Thread(
        target=run_uvicorn_process,
        args=(mcp,),
        name="GeminiMCP_Uvicorn",
        daemon=True,
    )
    _server_thread.start()
    log("Main: Thread started.")
    return True
