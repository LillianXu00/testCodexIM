"""Compatibility import for planetbaltan_workbuddy.app."""
import importlib
import sys

_module = importlib.import_module("planetbaltan_workbuddy.app")
sys.modules[__name__] = _module
