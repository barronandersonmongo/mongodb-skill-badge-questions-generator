"""Meta-tests: enforce the documentation convention on the suite itself.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
REQUIRED_SECTIONS = ("Intent:", "Success:", "Feature:")


def _test_functions() -> list[tuple[str, ast.FunctionDef]]:
    found: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found.append((f"{path.name}::{node.name}", node))
    return found


ALL_TESTS = _test_functions()


def test_the_suite_was_discovered():
    """
    Intent: The checks below iterate over discovered test functions; if discovery
        silently found nothing they would all vacuously pass, hiding a broken
        convention.
    Success: A substantial number of test functions is discovered.
    Feature: Test suite — documentation convention enforcement.
    """
    assert len(ALL_TESTS) > 50


@pytest.mark.parametrize("name,node", ALL_TESTS, ids=[n for n, _ in ALL_TESTS])
def test_every_test_documents_intent_success_and_feature(name, node):
    """
    Intent: Each test must record why it exists, what counts as passing, and which
        business feature it protects, so a future reader can tell a real
        requirement from an incidental assertion — and cannot weaken a test without
        confronting the requirement it encodes.
    Success: Every test function has a docstring containing Intent:, Success:, and
        Feature: sections.
    Feature: Test suite — documentation convention enforcement.
    """
    docstring = ast.get_docstring(node)
    assert docstring, f"{name} has no docstring"
    missing = [s for s in REQUIRED_SECTIONS if s not in docstring]
    assert not missing, f"{name} is missing section(s): {', '.join(missing)}"


@pytest.mark.parametrize("name,node", ALL_TESTS, ids=[n for n, _ in ALL_TESTS])
def test_documentation_sections_are_not_left_blank(name, node):
    """
    Intent: A present-but-empty section satisfies the letter of the convention while
        recording nothing, which is worse than no convention at all.
    Success: Each of Intent:, Success:, and Feature: is followed by text.
    Feature: Test suite — documentation convention enforcement.
    """
    docstring = ast.get_docstring(node) or ""
    for section in REQUIRED_SECTIONS:
        _, _, remainder = docstring.partition(section)
        first_line = remainder.split("\n", 1)[0].strip()
        assert first_line, f"{name} has an empty {section} section"
