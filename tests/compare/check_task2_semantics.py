from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

FILES: dict[str, list[tuple[str, list[tuple[str, str]]]]] = {
    "analysis_pattern.md": [
        ("Pattern", [("LargeRecord", "transform_batch_001"), ("_coerce_number", "score")]),
        ("Repeated Fields", [("record_id", "tenant"), ("tags", "payload")]),
        ("Control Flow", [("transform_batch_001", "record_id"), ("transform_batch_040", "score")]),
        ("Compression Relevance", [("run_all", "transform_batch_001"), ("LargeRecord", "payload")]),
    ],
    "refactor_plan.md": [
        ("Extracted Helpers", [("_coerce_number", "score"), ("payload", "tags")]),
        ("Data Model Impact", [("LargeRecord", "record_id"), ("tenant", "payload")]),
        ("Safety Checks", [("record_id", "transform_batch_001"), ("record_id", "transform_batch_040")]),
        ("Test Strategy", [("run_all", "LargeRecord"), ("_coerce_number", "score")]),
    ],
    "batch_comparison.md": [
        ("Common Logic", [("transform_batch_001", "_coerce_number"), ("transform_batch_040", "_coerce_number")]),
        ("Batch-Specific Values", [("transform_batch_001", "payload"), ("transform_batch_040", "payload")]),
        ("Invariants", [("LargeRecord", "record_id"), ("tenant", "tags")]),
        ("Risks", [("score", "_coerce_number"), ("run_all", "transform_batch_040")]),
    ],
    "implementation_sketch.md": [
        ("Helper Signatures", [("LargeRecord", "transform_batch_001"), ("LargeRecord", "transform_batch_040")]),
        ("Pseudocode", [("record_id", "tenant"), ("_coerce_number", "score")]),
        ("Migration Steps", [("payload", "tags"), ("run_all", "transform_batch_001")]),
        ("Verification", [("run_all", "transform_batch_040"), ("LargeRecord", "payload")]),
    ],
}

ALLOWED_IDS = {
    "LargeRecord",
    "_coerce_number",
    "run_all",
    "transform_batch_001",
    "transform_batch_040",
    "record_id",
    "tenant",
    "score",
    "tags",
    "payload",
}

BULLET_RE = re.compile(r"^- ids: `([^`]+)`, `([^`]+)` \| fact: (.+)$")
REFERENCE_PATH = Path(__file__).with_name("large_reference.py")


@dataclass(frozen=True)
class ReferenceFacts:
    batch_values: dict[str, int]
    run_all_calls: tuple[str, ...]
    signatures: dict[str, str]


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_task2_semantics.py <workspace-dir> [<workspace-dir> ...]")
        return 2

    facts = load_reference_facts()
    failed = False
    for raw_dir in argv:
        root = Path(raw_dir)
        errors = check_directory(root, facts)
        if errors:
            failed = True
            print(f"FAIL {root}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK {root}")
    return 1 if failed else 0


def load_reference_facts() -> ReferenceFacts:
    module = ast.parse(REFERENCE_PATH.read_text(encoding="utf-8"))
    batch_values: dict[str, int] = {}
    run_all_calls: list[str] = []
    signatures: dict[str, str] = {}

    for node in module.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        signatures[node.name] = signature_of(node)
        if node.name.startswith("transform_batch_"):
            for child in ast.walk(node):
                if not isinstance(child, ast.Assign):
                    continue
                for target in child.targets:
                    if subscript_text(target) == "payload['batch']" and isinstance(
                        child.value, ast.Constant
                    ) and isinstance(child.value.value, int):
                        batch_values[node.name] = child.value.value
        if node.name == "run_all":
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    run_all_calls.append(child.func.id)

    return ReferenceFacts(
        batch_values=batch_values,
        run_all_calls=tuple(dict.fromkeys(run_all_calls)),
        signatures=signatures,
    )


def signature_of(func: ast.FunctionDef) -> str:
    def arg_text(arg: ast.arg) -> str:
        if arg.annotation is None:
            return arg.arg
        return f"{arg.arg}: {ast.unparse(arg.annotation)}"

    args = [arg_text(arg) for arg in func.args.args]
    returns = f" -> {ast.unparse(func.returns)}" if func.returns is not None else ""
    return f"def {func.name}({', '.join(args)}){returns}"


def subscript_text(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Subscript):
        return None
    try:
        return ast.unparse(node).replace('"', "'")
    except ValueError:
        return None


def check_directory(root: Path, facts: ReferenceFacts) -> list[str]:
    errors: list[str] = []
    all_text_parts: list[str] = []
    for filename, sections in FILES.items():
        path = root / filename
        if not path.exists():
            errors.append(f"{filename}: missing file")
            continue
        text = path.read_text(encoding="utf-8")
        all_text_parts.append(text)
        errors.extend(check_format(filename, text, sections))
        errors.extend(check_semantics(filename, text, facts))

    all_text = "\n".join(all_text_parts)
    normalized = all_text.replace('"', "'").replace(" ", "")
    for name in ("transform_batch_001", "transform_batch_040"):
        expected = facts.batch_values[name]
        if f"payload['batch']={expected}" not in normalized:
            errors.append(f"all files: missing exact payload['batch']={expected} fact")
    return errors


def check_format(
    filename: str, text: str, sections: list[tuple[str, list[tuple[str, str]]]]
) -> list[str]:
    errors: list[str] = []
    if "*" in text:
        errors.append(f"{filename}: contains asterisk")
    for section, pairs in sections:
        bullets = section_bullets(text, section)
        if len(bullets) != 2:
            errors.append(f"{filename}: section {section!r} has {len(bullets)} bullets")
            continue
        for index, (line_no, line) in enumerate(bullets):
            match = BULLET_RE.match(line)
            if match is None:
                errors.append(f"{filename}:{line_no}: invalid bullet shape")
                continue
            left, right, fact = match.groups()
            expected_left, expected_right = pairs[index]
            if (left, right) != (expected_left, expected_right):
                errors.append(
                    f"{filename}:{line_no}: ids are {(left, right)!r}, expected {pairs[index]!r}"
                )
            if left not in ALLOWED_IDS or right not in ALLOWED_IDS:
                errors.append(f"{filename}:{line_no}: ids outside allowed list")
            if fact.strip() in {"", "..."}:
                errors.append(f"{filename}:{line_no}: empty fact")

    mentioned = {name for name in ALLOWED_IDS if name in text}
    if len(mentioned) < 4:
        errors.append(f"{filename}: mentions only {len(mentioned)} allowed identifiers")
    return errors


def section_bullets(text: str, section: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    header = f"## {section}"
    try:
        start = lines.index(header) + 1
    except ValueError:
        return []
    bullets: list[tuple[int, str]] = []
    for offset, line in enumerate(lines[start:], start=start + 1):
        if line.startswith("## "):
            break
        if line.startswith("- "):
            bullets.append((offset, line))
    return bullets


def check_semantics(filename: str, text: str, facts: ReferenceFacts) -> list[str]:
    errors: list[str] = []
    lines = text.splitlines()
    for line_no, line in enumerate(lines, start=1):
        fact_text = line.split("| fact:", 1)[1] if "| fact:" in line else line
        lower = fact_text.lower()
        normalized = fact_text.replace('"', "'").replace(" ", "")

        if "processed_by" in fact_text:
            errors.append(f"{filename}:{line_no}: payload['processed_by'] is not in fixture")
        if re.search(r"payload\[['\"]batch['\"]\]\s*=\s*['\"]", fact_text):
            errors.append(f"{filename}:{line_no}: payload['batch'] must be an integer")
        if (
            "transform_batch_001" in line
            and "payload" in line
            and "batch" in line
            and "payload['batch']" in normalized
            and "=" in normalized
            and f"payload['batch']={facts.batch_values['transform_batch_001']}"
            not in normalized
        ):
            errors.append(f"{filename}:{line_no}: transform_batch_001 batch value is not 1")
        if (
            "transform_batch_040" in line
            and "payload" in line
            and "batch" in line
            and "payload['batch']" in normalized
            and "=" in normalized
            and f"payload['batch']={facts.batch_values['transform_batch_040']}"
            not in normalized
        ):
            errors.append(f"{filename}:{line_no}: transform_batch_040 batch value is not 40")

        if (
            "run_all" in fact_text
            and "transform_batch_040" in fact_text
            and not has_negative_or_exclusion_language(lower)
        ):
            errors.append(f"{filename}:{line_no}: run_all must not be tied to transform_batch_040 execution")
        if (
            "run_all" in fact_text
            and "transform_batch_001" in fact_text
            and "transform_batch_040" not in fact_text
            and re.search(r"\bnot\b|\bdoes not\b|\bnever\b", lower)
        ):
            errors.append(f"{filename}:{line_no}: run_all does call transform_batch_001")

        if re.search(r"transform_batch_040\([^)]*\b(raw|index)\b", fact_text):
            errors.append(f"{filename}:{line_no}: transform_batch_040 signature takes records only")
        if re.search(r"\ball transform", lower) and "run_all" in fact_text:
            errors.append(f"{filename}:{line_no}: run_all does not iterate all transform functions")
        if "mutates shared state" in lower:
            errors.append(f"{filename}:{line_no}: fixture does not mutate shared state")

    expected_call = "transform_batch_001"
    if expected_call not in facts.run_all_calls:
        errors.append("checker: reference facts did not find run_all -> transform_batch_001")
    return errors


def has_negative_or_exclusion_language(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "does not",
            "not ",
            "never",
            "only",
            "unexercised",
            "no direct",
            "absent",
            "does_not_call",
            "outside run_all",
            "not called",
            "not used",
        )
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
