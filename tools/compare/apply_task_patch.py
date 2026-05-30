"""Apply the compare task patch inside tools/compare/workspace/current.

This script is intentionally deterministic. It keeps the task focused on
reading the reference file through the agent while avoiding repeated edit-tool
failures around Python indentation.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "tools" / "compare" / "workspace" / "current"
TARGET = WORKSPACE / "reference_pipeline_patched.py"
TEST_FILE = WORKSPACE / "test_reference_pipeline.py"


OLD_COMPARE = '''    def _compare(self, actual: Any, operator: str, expected: Any) -> bool:
        op = operator.strip().lower()
        if op in {"eq", "=="}:
            return actual == expected
        if op in {"ne", "!="}:
            return actual != expected
        if op in {"gt", ">"}:
            return self._as_number(actual) > self._as_number(expected)
        if op in {"ge", ">="}:
            return self._as_number(actual) >= self._as_number(expected)
        if op in {"lt", "<"}:
            return self._as_number(actual) < self._as_number(expected)
        if op in {"le", "<="}:
            return self._as_number(actual) <= self._as_number(expected)
        if op == "in":
            return actual in expected
        if op == "not_in":
            return actual not in expected
        if op == "has_tag":
            return str(expected).lower() in {str(tag).lower() for tag in actual or ()}
        if op == "prefix":
            return str(actual).startswith(str(expected))
        if op == "suffix":
            return str(actual).endswith(str(expected))
        raise ValueError(f"unsupported operator: {operator}")
'''


NEW_COMPARE = '''    def _compare(self, actual: Any, operator: str, expected: Any) -> bool:
        op = operator.strip().lower()
        if op in {"eq", "=="}:
            return actual == expected
        if op in {"ne", "!="}:
            return actual != expected
        if op in {"gt", ">"}:
            return self._as_number(actual) > self._as_number(expected)
        if op in {"ge", ">="}:
            return self._as_number(actual) >= self._as_number(expected)
        if op in {"lt", "<"}:
            return self._as_number(actual) < self._as_number(expected)
        if op in {"le", "<="}:
            return self._as_number(actual) <= self._as_number(expected)
        if op == "between":
            low, high = expected
            value = self._as_number(actual)
            return self._as_number(low) <= value <= self._as_number(high)
        if op == "contains_any":
            if actual is None:
                return False

            def _items(value: Any) -> tuple[Any, ...]:
                if isinstance(value, str):
                    return (value,)
                return tuple(value)

            return bool(set(_items(actual)) & set(_items(expected)))
        if op == "in":
            return actual in expected
        if op == "not_in":
            return actual not in expected
        if op == "has_tag":
            return str(expected).lower() in {str(tag).lower() for tag in actual or ()}
        if op == "prefix":
            return str(actual).startswith(str(expected))
        if op == "suffix":
            return str(actual).endswith(str(expected))
        raise ValueError(f"unsupported operator: {operator}")
'''


TEST_SOURCE = '''import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reference_pipeline_patched import ConditionEvaluator, Event, Rule


class TestReferencePipelineOperators(unittest.TestCase):
    def setUp(self):
        self.evaluator = ConditionEvaluator()
        self.event = Event(
            event_id="evt-test",
            kind="payment",
            tenant="alpha",
            user_id="u-1",
            value=75,
            tags=("vip", "review"),
            metadata={"risk": {"score": 42}, "channels": ["web", "mobile"]},
        )

    def test_between_true(self):
        rule = Rule("value-window", "value", "between", (50, 100))
        self.assertTrue(self.evaluator.evaluate(self.event, rule))

    def test_between_false(self):
        rule = Rule("value-window", "value", "between", (80, 100))
        self.assertFalse(self.evaluator.evaluate(self.event, rule))

    def test_between_metadata_value(self):
        rule = Rule("risk-window", "risk.score", "between", (40, 50))
        self.assertTrue(self.evaluator.evaluate(self.event, rule))

    def test_contains_any_tags_true(self):
        rule = Rule("tag-overlap", "tags", "contains_any", ("manual", "vip"))
        self.assertTrue(self.evaluator.evaluate(self.event, rule))

    def test_contains_any_tags_false(self):
        rule = Rule("tag-overlap", "tags", "contains_any", ("manual", "blocked"))
        self.assertFalse(self.evaluator.evaluate(self.event, rule))

    def test_contains_any_string_is_single_value(self):
        rule = Rule("kind-overlap", "kind", "contains_any", ("refund", "payment"))
        self.assertTrue(self.evaluator.evaluate(self.event, rule))

    def test_unknown_operator_still_raises(self):
        rule = Rule("unknown-op", "kind", "unknown", "payment")
        with self.assertRaises(ValueError):
            self.evaluator.evaluate(self.event, rule)


if __name__ == "__main__":
    unittest.main()
'''


def main() -> None:
    if not TARGET.exists():
        raise FileNotFoundError(f"missing target: {TARGET}")

    source = TARGET.read_text(encoding="utf-8")
    if NEW_COMPARE in source:
        patched = source
    else:
        if OLD_COMPARE not in source:
            raise RuntimeError("patch target not found; reset the workspace copy first")
        patched = source.replace(OLD_COMPARE, NEW_COMPARE, 1)

    TARGET.write_text(patched, encoding="utf-8")
    TEST_FILE.write_text(TEST_SOURCE, encoding="utf-8")
    print("PATCHED")


if __name__ == "__main__":
    main()
