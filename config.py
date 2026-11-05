"""Compatibility shim: the implementation lives in research_tool/config.py."""
import sys

from research_tool import config as _impl

sys.modules[__name__] = _impl
