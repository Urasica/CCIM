"""py-tree-sitter 기반 다중 언어 AST 압축기.

설계 §3.2.2 — 시그니처/import/타입 어노테이션은 보존, 함수 본문만 마커로 치환.
중첩 함수는 외부 함수 본문에 포함되어 한 번에 마스킹된다 (V1 단순화).

지원 언어: python (V1), java (V1), csharp (V1)

API:
    cmp = ASTCompressor()
    result = cmp.compress(code, session_id="abc", language="python")
    result = cmp.compress(code, session_id="abc", language="java")
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from itertools import count

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser, Tree

from ccim.compress.markers import build_marker
from ccim.utils.tokens import estimate_text_tokens

_MIN_BODY_LINES = 2

# 언어별 파서 캐시 (lazy 로드)
_PARSER_CACHE: dict[str, Parser] = {}

# 언어별 함수/메서드 선언 노드 타입
# body 필드명은 모두 "body" (tree-sitter 공통)
_FUNC_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition"},
    "java":   {"method_declaration", "constructor_declaration"},
    "csharp": {"method_declaration", "constructor_declaration"},
}

# 언어 별칭 정규화
LANG_ALIASES: dict[str, str] = {
    "python": "python", "py": "python",
    "java":   "java",
    "csharp": "csharp", "c#": "csharp", "cs": "csharp",
}


def _get_parser(language: str) -> Parser:
    """언어별 Parser를 캐시에서 반환. 없으면 lazy 초기화."""
    lang = LANG_ALIASES.get(language.lower(), language)
    if lang in _PARSER_CACHE:
        return _PARSER_CACHE[lang]

    if lang == "python":
        ts_lang = Language(tspython.language())
    elif lang == "java":
        try:
            import tree_sitter_java as tsjava  # type: ignore[import]
            ts_lang = Language(tsjava.language())
        except ImportError as e:
            raise NotImplementedError(
                "Java 압축을 사용하려면 'uv add tree-sitter-java' 를 실행하세요."
            ) from e
    elif lang == "csharp":
        try:
            import tree_sitter_c_sharp as tscs  # type: ignore[import]
            ts_lang = Language(tscs.language())
        except ImportError as e:
            raise NotImplementedError(
                "C# 압축을 사용하려면 'uv add tree-sitter-c-sharp' 를 실행하세요."
            ) from e
    else:
        raise NotImplementedError(f"지원하지 않는 언어: {lang!r}")

    parser = Parser(ts_lang)
    _PARSER_CACHE[lang] = parser
    return parser


@dataclass(frozen=True)
class CompressedBlock:
    """원본 함수 본문 1개를 마커로 치환한 결과. Redis 저장 단위."""

    context_id: str
    marker: str
    original_code: str
    original_lines: tuple[int, int]  # 1-based [start, end] (inclusive)
    marker_line: int                  # 압축본에서 마커가 차지하는 1-based 라인 번호
    symbol_name: str | None = None
    fact_manifest: tuple[str, ...] = ()


@dataclass
class CompressionResult:
    """단일 입력(코드 문자열)에 대한 압축 결과."""

    compressed_text: str
    blocks: list[CompressedBlock] = field(default_factory=list)
    line_mapping: dict[int, int] = field(default_factory=dict)
    bytes_before: int = 0
    bytes_after: int = 0
    tokens_before_est: int = 0
    tokens_after_est: int = 0

    @property
    def bytes_saved(self) -> int:
        return max(0, self.bytes_before - self.bytes_after)

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before_est - self.tokens_after_est)


@dataclass(frozen=True)
class _FunctionCluster:
    start_row: int
    end_row: int
    first_body_row: int
    symbol_name: str
    count: int


class ASTCompressor:
    """다중 언어 AST 압축기. 단일 인스턴스를 모든 요청에서 재사용 가능."""

    def __init__(self) -> None:
        # 파서는 compress() 첫 호출 시 언어별로 lazy 초기화
        pass

    def parse(self, code: str, language: str = "python") -> Tree:
        """tree-sitter parse. 미완성 코드도 부분 트리를 돌려준다."""
        parser = _get_parser(language)
        return parser.parse(code.encode("utf-8"))

    def compress(
        self,
        code: str,
        *,
        session_id: str,
        language: str = "python",
        ctx_prefix: str = "",
        cluster_repeated_functions: bool = False,
    ) -> CompressionResult:
        """코드를 AST 압축.

        ctx_prefix: 동일 session_id 내 여러 compress() 호출 간 context_id 충돌을
        방지하기 위한 호출 단위 고유 접두사. 비어있으면 기존 '001' 형식 유지.
        """
        lang = LANG_ALIASES.get(language.lower(), language)
        parser = _get_parser(lang)
        func_types = _FUNC_NODE_TYPES.get(lang, _FUNC_NODE_TYPES["python"])

        source_bytes = code.encode("utf-8")
        tree = parser.parse(source_bytes)

        bodies: list[Node] = []
        self._collect_function_bodies(tree.root_node, bodies, func_types)
        eligible = [
            n for n in bodies
            if (n.end_point[0] - n.start_point[0] + 1) >= _MIN_BODY_LINES
        ]
        eligible.sort(key=lambda n: (n.start_point[0], n.start_point[1]))
        clusters_by_body_row = (
            self._repeated_function_clusters(eligible, source_bytes, lang)
            if cluster_repeated_functions
            else {}
        )

        original_lines = code.split("\n")
        relationship_facts = (
            self._python_relationship_facts(code) if lang == "python" else {}
        )
        compressed_lines: list[str] = []
        line_mapping: dict[int, int] = {}
        blocks: list[CompressedBlock] = []
        ctx_counter = count(1)

        cursor = 0
        for body_node in eligible:
            start_row = body_node.start_point[0]
            end_row = body_node.end_point[0]
            if start_row < cursor:
                continue

            cluster = clusters_by_body_row.get(start_row)
            if cluster is not None:
                while cursor < cluster.start_row:
                    line = original_lines[cursor] if cursor < len(original_lines) else ""
                    compressed_lines.append(line)
                    line_mapping[len(compressed_lines)] = cursor + 1
                    cursor += 1

                seq = f"{next(ctx_counter):03d}"
                ctx_id = f"{ctx_prefix}_{seq}" if ctx_prefix else seq
                marker = build_marker(session_id, ctx_id)
                summary_line_idx = len(compressed_lines) + 1
                compressed_lines.append(
                    f"# CCIM repeated function cluster: {cluster.symbol_name} "
                    f"({cluster.count} functions)"
                )
                line_mapping[summary_line_idx] = cluster.start_row + 1
                body_code = "\n".join(
                    original_lines[cluster.start_row : cluster.end_row + 1]
                )
                fact_manifest = (
                    self._python_fact_manifest(
                        body_code,
                        max_functions=2,
                        extra_facts=relationship_facts,
                    )
                    if lang == "python"
                    else ()
                )
                for fact in fact_manifest:
                    compressed_lines.append(f"# CCIM fact: {fact}")
                    line_mapping[len(compressed_lines)] = cluster.start_row + 1
                marker_line_idx = len(compressed_lines) + 1
                compressed_lines.append(marker)
                line_mapping[marker_line_idx] = cluster.start_row + 1

                blocks.append(
                    CompressedBlock(
                        context_id=ctx_id,
                        marker=marker,
                        original_code=body_code,
                        original_lines=(cluster.start_row + 1, cluster.end_row + 1),
                        marker_line=marker_line_idx,
                        symbol_name=cluster.symbol_name,
                        fact_manifest=fact_manifest,
                    )
                )
                cursor = cluster.end_row + 1
                continue

            # Java/C# block body starts on the same line as the parent signature.
            # Python body always starts on the next line after the def.
            parent = body_node.parent
            body_inline = parent is not None and parent.start_point[0] == start_row

            if body_inline:
                # Copy lines up to and including the signature line (start_row).
                while cursor <= start_row:
                    line = original_lines[cursor] if cursor < len(original_lines) else ""
                    compressed_lines.append(line)
                    line_mapping[len(compressed_lines)] = cursor + 1
                    cursor += 1
                # Derive indent from the first interior line (start_row + 1).
                inner_row = start_row + 1
                if inner_row < len(original_lines):
                    inner = original_lines[inner_row]
                    indent = inner[: len(inner) - len(inner.lstrip())]
                else:
                    sig = original_lines[start_row] if start_row < len(original_lines) else ""
                    indent = sig[: len(sig) - len(sig.lstrip())] + "    "
            else:
                while cursor < start_row:
                    line = original_lines[cursor] if cursor < len(original_lines) else ""
                    compressed_lines.append(line)
                    line_mapping[len(compressed_lines)] = cursor + 1
                    cursor += 1
                indent = ""
                if start_row < len(original_lines):
                    first = original_lines[start_row]
                    indent = first[: len(first) - len(first.lstrip())]

            seq = f"{next(ctx_counter):03d}"
            ctx_id = f"{ctx_prefix}_{seq}" if ctx_prefix else seq
            marker = build_marker(session_id, ctx_id)
            body_code = "\n".join(original_lines[start_row : end_row + 1])
            fact_source = body_code
            if lang == "python" and parent is not None:
                fact_source = "\n".join(
                    original_lines[parent.start_point[0] : parent.end_point[0] + 1]
                )
            fact_manifest = (
                self._python_fact_manifest(
                    fact_source,
                    extra_facts=relationship_facts,
                )
                if lang == "python"
                else ()
            )
            for fact in fact_manifest:
                compressed_lines.append(f"{indent}# CCIM fact: {fact}")
                line_mapping[len(compressed_lines)] = start_row + 1
            marker_line_idx = len(compressed_lines) + 1
            compressed_lines.append(f"{indent}{marker}")
            line_mapping[marker_line_idx] = start_row + 1

            blocks.append(
                CompressedBlock(
                    context_id=ctx_id,
                    marker=marker,
                    original_code=body_code,
                    original_lines=(start_row + 1, end_row + 1),
                    marker_line=marker_line_idx,
                    symbol_name=self._symbol_name(parent, source_bytes),
                    fact_manifest=fact_manifest,
                )
            )

            if body_inline and end_row < len(original_lines):
                # Keep the closing brace line (end_row).
                compressed_lines.append(original_lines[end_row])
                line_mapping[len(compressed_lines)] = end_row + 1
            cursor = end_row + 1

        while cursor < len(original_lines):
            compressed_lines.append(original_lines[cursor])
            line_mapping[len(compressed_lines)] = cursor + 1
            cursor += 1

        compressed_text = "\n".join(compressed_lines)
        compressed_bytes = compressed_text.encode("utf-8")

        return CompressionResult(
            compressed_text=compressed_text,
            blocks=blocks,
            line_mapping=line_mapping,
            bytes_before=len(source_bytes),
            bytes_after=len(compressed_bytes),
            tokens_before_est=estimate_text_tokens(code),
            tokens_after_est=estimate_text_tokens(compressed_text),
        )

    # ── 내부 트리 워크 ───────────────────────────────────────────────
    def _collect_function_bodies(
        self, node: Node, out: list[Node], func_types: set[str]
    ) -> None:
        """func_types에 해당하는 노드를 만나면 body만 수집하고 재귀 중단.

        중첩 함수/메서드는 외부 본문 안에 포함된 채로 한 번에 마스킹된다.
        """
        if node.type in func_types:
            body = node.child_by_field_name("body")
            if body is not None:
                out.append(body)
            return
        for child in node.children:
            self._collect_function_bodies(child, out, func_types)

    @staticmethod
    def _symbol_name(node: Node | None, source_bytes: bytes) -> str | None:
        if node is None:
            return None
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        return source_bytes[name_node.start_byte:name_node.end_byte].decode(
            "utf-8", errors="replace"
        )

    @classmethod
    def _python_fact_manifest(
        cls,
        code: str,
        *,
        max_functions: int | None = None,
        extra_facts: dict[str, tuple[str, ...]] | None = None,
    ) -> tuple[str, ...]:
        try:
            module = ast.parse(code)
        except SyntaxError:
            return ()

        functions = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        if max_functions is not None and len(functions) > max_functions:
            functions = [functions[0], functions[-1]]
        facts: list[str] = []
        for node in functions:
            facts.extend(cls._python_function_facts(node))
            if extra_facts is not None:
                facts.extend(extra_facts.get(node.name, ()))
        return tuple(facts)

    @classmethod
    def _python_relationship_facts(cls, code: str) -> dict[str, tuple[str, ...]]:
        try:
            module = ast.parse(code)
        except SyntaxError:
            return {}

        functions = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        numbered_groups: dict[str, list[tuple[int, str]]] = {}
        for func in functions:
            match = re.match(r"^(?P<prefix>.+?)(?P<number>\d+)$", func.name)
            if match is None:
                continue
            numbered_groups.setdefault(match.group("prefix"), []).append(
                (int(match.group("number")), func.name)
            )

        function_groups = [
            [name for _, name in sorted(group)]
            for group in numbered_groups.values()
            if len(group) >= 4
        ]
        if not function_groups:
            return {}

        facts_by_function: dict[str, list[str]] = {}
        for func in functions:
            direct_calls = set(cls._python_direct_call_names(func))
            for group in function_groups:
                called = [name for name in group if name in direct_calls]
                if not called or len(called) == len(group):
                    continue
                missing_edges = [
                    name for name in (group[0], group[-1]) if name not in direct_calls
                ]
                facts = facts_by_function.setdefault(func.name, [])
                facts.append(
                    f"{func.name}: relationship=calls {', '.join(called)} "
                    f"from {group[0]}..{group[-1]}"
                )
                if missing_edges:
                    facts.append(
                        f"{func.name}: does_not_call={', '.join(dict.fromkeys(missing_edges))}"
                    )

        return {
            name: tuple(dict.fromkeys(facts))
            for name, facts in facts_by_function.items()
        }

    @classmethod
    def _python_function_facts(
        cls, func: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> list[str]:
        facts = [f"{func.name}: signature={cls._python_signature(func)}"]
        calls = cls._python_calls(func)
        if calls:
            facts.append(f"{func.name}: calls={', '.join(calls)}")
        writes = cls._python_dict_writes(func)
        if writes:
            facts.append(f"{func.name}: writes={', '.join(writes)}")
        reads = cls._python_reads(func)
        if reads:
            facts.append(f"{func.name}: reads={', '.join(reads)}")
        return facts[:4]

    @staticmethod
    def _python_signature(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        def arg_text(arg: ast.arg) -> str:
            if arg.annotation is None:
                return arg.arg
            return f"{arg.arg}: {ast.unparse(arg.annotation)}"

        args = [arg_text(arg) for arg in (*func.args.posonlyargs, *func.args.args)]
        if func.args.vararg is not None:
            args.append(f"*{arg_text(func.args.vararg)}")
        if func.args.kwonlyargs:
            if func.args.vararg is None:
                args.append("*")
            args.extend(arg_text(arg) for arg in func.args.kwonlyargs)
        if func.args.kwarg is not None:
            args.append(f"**{arg_text(func.args.kwarg)}")
        returns = f" -> {ast.unparse(func.returns)}" if func.returns is not None else ""
        prefix = "async def" if isinstance(func, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {func.name}({', '.join(args)}){returns}"

    @classmethod
    def _python_calls(cls, func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        calls: list[str] = []
        seen: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            name = cls._python_call_name(node.func)
            if name is not None and name not in seen:
                calls.append(name)
                seen.add(name)
        return calls[:8]

    @staticmethod
    def _python_direct_call_names(
        func: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[str]:
        calls: list[str] = []
        seen: set[str] = set()
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id not in seen
            ):
                calls.append(node.func.id)
                seen.add(node.func.id)
        return calls

    @staticmethod
    def _python_call_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return ast.unparse(node)
        return None

    @classmethod
    def _python_dict_writes(
        cls, func: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> list[str]:
        writes: list[str] = []
        seen: set[str] = set()
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            value = cls._safe_unparse(node.value)
            if value is None:
                continue
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                key = cls._safe_unparse(target)
                if key is None:
                    continue
                fact = f"{key}={value}"
                if fact not in seen:
                    writes.append(fact)
                    seen.add(fact)
        return writes[:8]

    @classmethod
    def _python_reads(cls, func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        reads: list[str] = []
        seen: set[str] = set()
        for statement in func.body:
            for node in ast.walk(statement):
                text: str | None = None
                if isinstance(node, ast.Call):
                    text = cls._python_get_call_text(node)
                elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                    text = cls._safe_unparse(node)
                    if text is not None and re.match(
                        r"^(dict|Iterable|list|Mapping|set|tuple)\[", text
                    ):
                        text = None
                if text is not None and text not in seen:
                    reads.append(text)
                    seen.add(text)
        return reads[:8]

    @classmethod
    def _python_get_call_text(cls, node: ast.Call) -> str | None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
            return None
        if not node.args or not isinstance(node.args[0], ast.Constant):
            return None
        key = node.args[0].value
        if not isinstance(key, str):
            return None
        base = cls._safe_unparse(node.func.value)
        if base is None:
            return None
        return f"{base}.get({key!r})"

    @staticmethod
    def _safe_unparse(node: ast.AST) -> str | None:
        try:
            return ast.unparse(node)
        except ValueError:
            return None

    @classmethod
    def _repeated_function_clusters(
        cls, eligible: list[Node], source_bytes: bytes, language: str
    ) -> dict[int, _FunctionCluster]:
        if language != "python":
            return {}

        clusters: list[list[Node]] = []
        current: list[Node] = []
        current_prefix: str | None = None
        previous_end_row: int | None = None

        for body_node in eligible:
            parent = body_node.parent
            symbol = cls._symbol_name(parent, source_bytes)
            match = re.match(r"^(?P<prefix>.+?)(?P<number>\d+)$", symbol or "")
            prefix = match.group("prefix") if match else None
            start_row = parent.start_point[0] if parent is not None else body_node.start_point[0]
            end_row = parent.end_point[0] if parent is not None else body_node.end_point[0]
            adjacent = previous_end_row is None or start_row - previous_end_row <= 4

            if prefix and prefix == current_prefix and adjacent:
                current.append(body_node)
            else:
                if len(current) >= 4:
                    clusters.append(current)
                current = [body_node] if prefix else []
                current_prefix = prefix
            previous_end_row = end_row

        if len(current) >= 4:
            clusters.append(current)

        result: dict[int, _FunctionCluster] = {}
        for nodes in clusters:
            first_parent = nodes[0].parent
            last_parent = nodes[-1].parent
            if first_parent is None or last_parent is None:
                continue
            first_symbol = cls._symbol_name(first_parent, source_bytes) or "unknown"
            last_symbol = cls._symbol_name(last_parent, source_bytes) or first_symbol
            symbol_name = (
                first_symbol if first_symbol == last_symbol else f"{first_symbol}..{last_symbol}"
            )
            result[nodes[0].start_point[0]] = _FunctionCluster(
                start_row=first_parent.start_point[0],
                end_row=last_parent.end_point[0],
                first_body_row=nodes[0].start_point[0],
                symbol_name=symbol_name,
                count=len(nodes),
            )
        return result
