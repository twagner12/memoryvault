"""Guard: no swallowed exceptions anywhere in memoryvault/.

Findings #7, #8 and #10 were all invisible for the same reason — the failure
was caught and discarded, so a no-op write reported success. The three-state
invariant (merged / deferred / failed) only holds if nothing can quietly exit
a metadata path, so this test forbids the construct outright rather than
trusting review to catch the next one.

`except Exception: log(...)` and `except OSError: return None` are fine; what
is banned is a handler whose entire body is `pass`, and the bare `except:`
that also swallows KeyboardInterrupt and SystemExit.
"""

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "memoryvault"

# `except <anything>:` followed by nothing but `pass` — across the line break,
# and also in the one-line `except Exception: pass` form.
SWALLOWED = re.compile(r"except[^\n:]*:\s*\n?\s*pass\b")
BARE_EXCEPT = re.compile(r"^\s*except\s*:", re.MULTILINE)


def _python_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_package_has_python_files():
    """Guard the guard: a bad path would make every assertion below vacuous."""
    assert _python_files(), f"no Python files found under {PACKAGE}"


@pytest.mark.parametrize("pattern,label", [
    (SWALLOWED, "except ...: pass"),
    (BARE_EXCEPT, "bare except:"),
])
def test_no_swallowed_exceptions(pattern, label):
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(PACKAGE.parent)}:{line}")

    assert not offenders, (
        f"{label} found — a silent failure is exactly what findings #7/#8/#10 "
        f"were made of. Record the outcome instead:\n  " + "\n  ".join(offenders)
    )
