"""Are the two designs in MEETING_ADT.md complete (viba's is_complete).

`meeting_core.viba` = the code block of the "Stable types" chapter; `meeting_sdk.viba` = the data-interface chapter,
the former is used as a library (`import meeting_core as mc`).

Complete = starting from every definition, every coordinate on the graph can be
walked to a leaf; the only one that cannot be entered is CodeBlock.
`Assert` / `Appendix` / `Hint` are defined as units, so a code block is never
reached and no terminator is needed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# viba is a submodule nested under its own root directory; add it to sys.path
# to be able to import it
sys.path.insert(0, str(REPO_ROOT / "viba"))

from viba.check_tag_and_inline import check_tag_and_inline  # noqa: E402
from viba.is_complete import is_complete  # noqa: E402
from viba.type import Ok  # noqa: E402

import meeting_core_reflect as reflect  # noqa: E402

DESIGN_PATH = REPO_ROOT / "MEETING_ADT.md"
TERMINATORS = {"Assert", "Appendix", "Hint"}
CORE_CHAPTER = "Stable types"
SDK_CHAPTER = "The data interface"


def _chapter_source(title: str) -> str:
    """The source in the first code block under the chapter heading is that .viba."""
    text = DESIGN_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    heading = re.search(r"(?m)^##\s*" + re.escape(title), text)
    assert heading is not None, f"MEETING_ADT.md has no chapter named {title!r}"
    fence = text.index("```", heading.end())
    start = text.index("\n", fence) + 1
    return text[start:text.index("\n```", start)].rstrip("\n") + "\n"


def _core_source() -> str:
    return _chapter_source(CORE_CHAPTER)


def test_meeting_core_is_complete() -> None:
    assert is_complete(_core_source(), (), (), TERMINATORS) is True


def test_meeting_sdk_is_complete() -> None:
    library = [("meeting_core.viba", _core_source())]
    assert is_complete(_chapter_source(SDK_CHAPTER), library, (), TERMINATORS) is True


def test_the_stable_types_have_no_repeated_tag_and_no_inline_cycle() -> None:
    """viba's own check over the chapter: one tag holds one member, and an
    untagged member that is a product does not come back to itself."""
    given = check_tag_and_inline(reflect._design_pool())
    assert isinstance(given, Ok) and given.ok_value is None, given


def test_no_terminators_are_needed() -> None:
    """Assert / Appendix / Hint are units: the walk never reaches a code block."""
    assert is_complete(_core_source(), (), (), set()) is True
