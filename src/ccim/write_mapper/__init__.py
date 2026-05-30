"""Write-side line mapper - compressed line -> original line for edit tools."""

from ccim.write_mapper.mapper import (
    LINE_ARG_KEYS,
    MapResult,
    WriteMapper,
    has_line_args,
    translate_line_with_mapping,
)

__all__ = [
    "LINE_ARG_KEYS",
    "MapResult",
    "WriteMapper",
    "has_line_args",
    "translate_line_with_mapping",
]
