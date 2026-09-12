"""Tier-3 browser_eval must wrap statement bodies before page.evaluate().

Regression: the detector required `return` to be followed by a space, "(" or
";", so `return[1,2]`, "return`x`", `return!!el`, `return/re/.test(s)` and
`return{a:1}` reached patchright unwrapped and raised
"SyntaxError: Illegal return statement" mid-run. Verified against real
patchright: every case below fails bare and succeeds wrapped.
"""
import re
import unittest


def _needs_wrap(script: str) -> bool:
    """Mirror of the logic in interactive_session.evaluate()."""
    body = script.strip()
    starts_function = body.startswith(("(", "async ", "async(", "function", "=>"))
    has_top_return = re.search(r"(?:^|[\s;{])return\b", body) is not None
    if body and not starts_function:
        return has_top_return or ";" in body.rstrip(" \t\n;")
    if body.startswith("{") and has_top_return:
        return True
    return False


class T3EvalWrapTests(unittest.TestCase):
    def test_statement_bodies_are_wrapped(self):
        for s in ("return document.title", "return document.title;",
                  "return[1,2]", "return`ok`", "return!!document.body",
                  "return/re/.test('x')", "return{a:1}",
                  "const x=2; return x*3", "{ return 7 }"):
            self.assertTrue(_needs_wrap(s), f"must wrap: {s!r}")

    def test_expressions_and_functions_are_left_alone(self):
        for s in ("document.title", "{a:1}", "(() => { return 1 })()",
                  "async () => { return 1 }", "function f(){ return 1 }",
                  "() => 5", "window.innerWidth"):
            self.assertFalse(_needs_wrap(s), f"must NOT wrap: {s!r}")

    def test_identifiers_containing_return_are_not_mistaken(self):
        for s in ("returnValue", "window.returnFoo", "obj.returnable"):
            self.assertFalse(_needs_wrap(s), f"must NOT wrap: {s!r}")

    def test_matches_the_shipped_implementation(self):
        """Guard against the source drifting from this mirror."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "antibot" / "interactive_session.py"
        text = src.read_text()
        self.assertIn(r'(?:^|[\s;{])return\b', text,
                      "evaluate() no longer uses the fixed return-detection pattern")


if __name__ == "__main__":
    unittest.main()
