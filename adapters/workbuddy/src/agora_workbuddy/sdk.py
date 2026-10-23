"""Compatibility import for planetbaltan_workbuddy.sdk."""
import importlib
import sys

_module = importlib.import_module("planetbaltan_workbuddy.sdk")
sys.modules[__name__] = _module
