"""AST 기반 가역 압축 — Python 한정(V1).

설계 §3.2.2: 시그니처/import/타입 어노테이션은 보존, 함수 본문만 마스킹.
"""

from ccim.compress.ast_compressor import (
    ASTCompressor,
    CompressedBlock,
    CompressionResult,
)
from ccim.compress.markers import build_marker, parse_marker
from ccim.compress.trigger import should_compress

__all__ = [
    "ASTCompressor",
    "CompressedBlock",
    "CompressionResult",
    "build_marker",
    "parse_marker",
    "should_compress",
]
