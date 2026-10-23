"""Compatibility import for planetbaltan.peer_authority."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.peer_authority")
sys.modules[__name__] = _module
