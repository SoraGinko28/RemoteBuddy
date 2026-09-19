"""meeting_sdk_viba -- the data interface, in viba.

Every api of the data-interface chapter is written there with its return type
(``now[Ctx] := mc.Result[mc.datetime] <- $ctx Ctx``). This module reads those
return types and turns an api's result into the viba script the remote binding
answers with:

    entry := $ok ...

so a remote answer is a viba witness: the rule layer reads it with
viba.reflect, the same way meeting_core_reflect reads a local answer.

Three questions, one per function: what type does this api return
(``return_type``), what is a result of it written as (``script_for``), and what
does that script have to be a resident of (``definition_of``).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import meeting_core_reflect as _reflect
import meeting_local_sdk as _local
from viba import serialize as _serialize
from viba.type import Err, Ok, Result, custom_module

DESIGN_CHAPTER = "The data interface"
API_LINE = re.compile(r"(?m)^(\w+)\[Ctx\] :=[ \t]*\n?[ \t]*(.*?)(?=\n[ \t]*<-|\n\w|\n#|\n\n|\Z)",
                      re.DOTALL)

__all__ = ["return_type", "return_types", "script_for", "access_of",
           "definition_of", "is_a_valid_answer"]


@lru_cache(maxsize=1)
def _chapter_source() -> str:
    """The data-interface chapter of MEETING_ADT.md, as written."""
    text = Path(_reflect.DESIGN_PATH).read_text(encoding="utf-8").replace("\r\n", "\n")
    heading = re.search(r"(?m)^##\s*" + re.escape(DESIGN_CHAPTER), text)
    if heading is None:
        raise _reflect.VibaReflectError(
            f"{_reflect.DESIGN_FILE_NAME} has no chapter named {DESIGN_CHAPTER!r}")
    fence = text.index("```", heading.end())
    start = text.index("\n", fence) + 1
    return text[start:text.index("\n```", start)].rstrip("\n") + "\n"


@lru_cache(maxsize=1)
def return_types() -> dict:
    """api -> its return type, exactly as the chapter writes it."""
    found = {}
    source = _chapter_source()
    for match in API_LINE.finditer(source):
        name, written = match.group(1), match.group(2)
        if name not in _local.__all__:
            continue                      # the two hint definitions are prose
        written = written.split("#")[0].strip()
        found[name] = re.sub(r"\s+", " ", written)
    return found


def return_type(api: str) -> str:
    """The return type of one api; an api written as another api follows it."""
    written = return_types().get(api)
    if written is None:
        raise KeyError(f"the data interface has no api named {api!r}")
    alias = re.fullmatch(r"(\w+)\[Ctx\]", written)
    if alias:
        return return_type(alias.group(1))
    return written


def script_for(api: str, result) -> Result:
    """A result of that api -> ``entry := ...`` (the remote answer).

    The result is this layer's Python data, so binding it into a node happens
    here; viba.serialize writes the source from the node.
    """
    found = access_of(api)
    if isinstance(found, Err):
        return found
    access, definition = found.ok_value
    root = access.root(definition, _reflect._Record(result))
    if isinstance(root, Err):
        return root
    return _serialize.serialize("entry", access, root.ok_value)


@lru_cache(maxsize=None)
def access_of(api: str):
    """(access, definition) that reads a value of this api's return type."""
    return _reflect.access_for(return_type(api))


@lru_cache(maxsize=None)
def _entry_module(api: str):
    """(module, the SdkEntry definition body) for that api's return type."""
    written = re.sub(r"\bmc\.", "", return_type(api))
    source = _reflect._design_source() + f"\nSdkEntry := {written}\n"
    module = custom_module(source)
    definitions = {definition.name: definition for definition in module.module.body}
    return module, definitions["SdkEntry"].body


def definition_of(api: str):
    """(module, definition body) a script of that api has to be a resident of."""
    return _entry_module(api)


def is_a_valid_answer(api: str, script: str) -> Result:
    """Does this script parse, and is its ``entry`` a resident of the api's type?"""
    from viba import viba_ast
    from viba.rule.is_compliant import is_compliant
    from viba.type import AstNodeType

    try:
        parsed = viba_ast.parse(script)
    except SyntaxError as error:
        return Err(f"the answer is not viba code: {error}")
    entries = [definition for definition in parsed.body if definition.name == "entry"]
    if len(entries) != 1:
        return Err("the answer has no single entry := ... definition")
    module, definition = definition_of(api)
    judged = is_compliant(AstNodeType(entries[0].body, module),
                          AstNodeType(definition, module))
    if isinstance(judged, Err):
        return judged
    if judged.ok_value is not True:
        return Err(f"the entry is not a resident of {return_type(api)}")
    return Ok(entries[0].body)
