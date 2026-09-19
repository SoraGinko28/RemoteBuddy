"""MEETING_ADT.md 里的两份设计是不是完备的（viba 的 is_complete）。

`meeting_core.viba` = the code block of the "Stable types" chapter; `meeting_sdk.viba` = the "The data interface" chapter,
要拿前者当库（`import meeting_core as mc`）。

完备 = 从每个定义出发，图上每个坐标都走得到叶子；走不进去的只有 CodeBlock，
The three prose words (`Assert` / `Appendix` / `Hint`) are defined as units, so a
code block is never reached and no terminator is needed.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# viba 是子模块，包在它自己的根目录下面；加进 sys.path 才 import 得到
sys.path.insert(0, str(REPO_ROOT / "viba"))

from viba.is_complete import is_complete  # noqa: E402

DESIGN_PATH = REPO_ROOT / "MEETING_ADT.md"
TERMINATORS = {"Assert", "Appendix", "Hint"}
CORE_CHAPTER = "Stable types"
SDK_CHAPTER = "The data interface"


def _chapter_source(title: str) -> str:
    """章标题下面第一个代码块里的原文，就是那份 .viba。"""
    text = DESIGN_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    heading = re.search(r"(?m)^##\s*" + re.escape(title), text)
    assert heading is not None, f"MEETING_ADT.md 里没有「{title}」这一章"
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


def test_no_terminators_are_needed() -> None:
    """Assert / Appendix / Hint are units: the walk never reaches a code block."""
    assert is_complete(_core_source(), (), (), set()) is True
