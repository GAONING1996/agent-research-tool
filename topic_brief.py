"""Compatibility shim: the implementation lives in research_tool/topic_brief.py."""
import sys

from research_tool import topic_brief as _impl

sys.modules[__name__] = _impl
