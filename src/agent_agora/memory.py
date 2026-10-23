"""Compatibility import for planetbaltan.memory."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.memory")
sys.modules[__name__] = _module
