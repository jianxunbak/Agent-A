import os
import sys

_EXT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "GeminiMCP.extension")
)
if _EXT_DIR not in sys.path:
    sys.path.insert(0, _EXT_DIR)
_LIB_DIR = os.path.join(_EXT_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
