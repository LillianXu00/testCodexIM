"""Compatibility import for planetbaltan_workbuddy.transport."""
import importlib
import sys

_module = importlib.import_module("planetbaltan_workbuddy.transport")
sys.modules[__name__] = _module
