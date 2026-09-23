"""The four entity surfaces in MEETING_ENTITIES.md: concepts and apis in viba.

Each entity exports two things — the concepts it owns and the api it offers — and
each one is a `.viba` chapter of that document, compiled and checked here the way
`tests/test_meeting_design.py` checks the two chapters of MEETING_ADT.md:

* it parses (viba's syntax),
* it is complete once the `{ ... }` hints are dropped: every name and every tag
  resolves to a leaf, so the chapter describes something an implementation can be
  written from (a hint is the one place a design may stop, and viba's own
  `is_complete` reads a bare code block as an unfinished address),
* all four together are one consistent pool: one tag holds one member, and no
  inlined chain comes back to itself,

and one check that is about writing the language rather than reading it: in
viba `**` binds tighter than `*`, and `*` binds tighter than `|`, so a union
written where a field is expected without grouping —

    * $content ChatMessage | ScoreMessage | ScreenContent

— is not a field holding a union. It is a sum whose second branch is a whole
`ScoreMessage`: the record loses its author and its moment. The last test reads
both documents of the ADT and the rule files and refuses that spelling.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# viba is a submodule nested under its own root directory; add it to sys.path
# to be able to import it
sys.path.insert(0, str(REPO_ROOT / "viba"))

from viba import viba_ast  # noqa: E402
from viba.check_tag_and_inline import check_tag_and_inline  # noqa: E402
from viba.is_complete import is_complete  # noqa: E402
from viba.type import Ok  # noqa: E402
from viba.viba_type_descriptor import (  # noqa: E402
    empty_pool, parse_viba_file, pool_add_file)

ENTITIES_PATH = REPO_ROOT / "MEETING_ENTITIES.md"
ADT_PATH = REPO_ROOT / "MEETING_ADT.md"
CORE_CHAPTER = "Stable types"
TERMINATORS = {"Assert", "Appendix", "Hint"}
ENTITIES = ("meeting_server", "actor", "assistant", "critic")

# A unit head is the head of a multi-line block, not a member of the sum.
BLOCK_HEADS = {"Object", "Oneof"}
# Assert / Appendix / Hint carry a code block and are units: a sum branch may be
# one of them without a tag of its own.
UNIT_WRAPPERS = {"Assert", "Appendix", "Hint"}


# ----------------------------------------------------------------------
# reading the two documents
# ----------------------------------------------------------------------


def _chapter_source(path: Path, title: str) -> str:
    """The first code block under the chapter heading is that .viba."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    heading = re.search(r"(?m)^##\s*" + re.escape(title), text)
    assert heading is not None, f"{path.name} has no chapter named {title!r}"
    fence = text.index("```", heading.end())
    start = text.index("\n", fence) + 1
    return text[start:text.index("\n```", start)].rstrip("\n") + "\n"


def _chapters(path: Path):
    """(title, source) for every chapter of the document that carries a block."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    found = []
    for heading in re.finditer(r"(?m)^##\s*(.+)$", text):
        title = heading.group(1).strip()
        following = re.search(r"(?m)^##\s", text[heading.end():])
        end = heading.end() + following.start() if following else len(text)
        fence = text.find("```", heading.end())
        if fence < 0 or fence > end:
            continue
        start = text.index("\n", fence) + 1
        found.append((title, text[start:text.index("\n```", start)].rstrip("\n") + "\n"))
    return found


def _entity_modules():
    """(module name, title, source) for the entity chapters, in document order."""
    modules = []
    for title, source in _chapters(ENTITIES_PATH):
        named = re.match(r"^The (.+) — ([a-z_]+)\.viba$", title)
        if named is None:
            continue
        modules.append((named.group(2), title, source))
    return modules


def _core_source() -> str:
    return _chapter_source(ADT_PATH, CORE_CHAPTER)


# ----------------------------------------------------------------------
# the checks
# ----------------------------------------------------------------------


def _drop_hints(source: str) -> str:
    """The chapter with its `{ ... }` hints gone: what is left must be complete."""
    class _Drop(viba_ast.NodeTransformer):
        def visit_ExponentChain(self, node):
            node.elements = [element for element in node.elements
                             if not isinstance(element, viba_ast.CodeBlock)]
            return self.generic_visit(node)

    tree = _Drop().visit(viba_ast.canonical(viba_ast.parse(source)))
    return viba_ast.unparse_module(tree)


def _definitions(source: str):
    return [node for node in viba_ast.parse(source).body
            if isinstance(node, (viba_ast.TypeDefinition, viba_ast.GenericDefinition))]


def _is_api(definition) -> bool:
    """An api is a chain — a result and its arguments; a concept is a type."""
    body = viba_ast.canonical(viba_ast.Module([definition])).body[0].body
    return isinstance(body, viba_ast.ExponentChain) and len(body.elements) > 1


# ----------------------------------------------------------------------
# the four surfaces
# ----------------------------------------------------------------------


def test_the_four_entities_have_one_chapter_each() -> None:
    names = tuple(module for module, _, _ in _entity_modules())
    assert names == ENTITIES, names


def test_every_entity_chapter_parses() -> None:
    for module, title, source in _entity_modules():
        viba_ast.parse(source)  # SyntaxError names the line when it does not


def test_every_entity_chapter_is_complete_without_its_hints() -> None:
    """A chapter's hints are the work left for an implementation; everything else
    in it has to resolve: every tag, every name, down to a leaf."""
    library = [("meeting_core.viba", _core_source())]
    for module, title, source in _entity_modules():
        assert is_complete(_drop_hints(source), list(library), (), TERMINATORS), title
        library.append((f"{module}.viba", source))


def test_the_entity_chapters_share_one_pool() -> None:
    """All four chapters together: one tag holds one member, and an untagged
    member that is a product does not come back to itself."""
    pool = empty_pool()
    library = [("meeting_core.viba", "meeting_core", _core_source())]
    library += [(f"{module}.viba", module, source)
                for module, _, source in _entity_modules()]
    for file_name, module_name, source in library:
        parsed = parse_viba_file(pool, source, file_name, module_name)
        assert isinstance(parsed, Ok), (file_name, parsed)
        added = pool_add_file(pool, parsed.ok_value)
        assert isinstance(added, Ok), (file_name, added)
        pool = added.ok_value
    given = check_tag_and_inline(pool)
    assert isinstance(given, Ok) and given.ok_value is None, given


def test_every_entity_exports_concepts_and_an_api() -> None:
    """Both halves are the surface: the types the others talk about it with, and
    the functions that make it work. One without the other is not an interface."""
    for module, title, source in _entity_modules():
        definitions = _definitions(source)
        concepts = [node.name for node in definitions if not _is_api(node)]
        api = [node.name for node in definitions if _is_api(node)]
        assert concepts, f"{module} exports no concept"
        assert api, f"{module} exports no api"


# ----------------------------------------------------------------------
# writing the language: a union stays inside its field
# ----------------------------------------------------------------------


def _leaked_branches(definition):
    """The branches a union grew out of a field position, or None.

    Written where a field was expected and without grouping, a union's tail ends
    up as branches of the whole definition; the first branch then holds a field
    chain that stops before the union, and the rest are bare names where the sum
    wants a tag, a literal or `nil`.
    """
    body = viba_ast.canonical(viba_ast.Module([definition])).body[0].body
    if not isinstance(body, viba_ast.SumChain) or len(body.elements) < 2:
        return None
    leaked = []
    for index, element in enumerate(body.elements):
        if index == 0:
            if isinstance(element, viba_ast.ProductChain):
                head = element.elements[0]
                if not (isinstance(head, viba_ast.TypeRef) and head.name in BLOCK_HEADS):
                    leaked.append((index, viba_ast.unparse_type(element)))
            continue
        if isinstance(element, (viba_ast.Tagged, viba_ast.Constant, viba_ast.Nil)):
            continue
        if (isinstance(element, viba_ast.TypeApp)
                and isinstance(element.constructor, viba_ast.TypeRef)
                and element.constructor.name in UNIT_WRAPPERS):
            continue
        leaked.append((index, viba_ast.unparse_type(element)))
    return leaked or None


def _viba_sources():
    """Every viba the repository keeps: the ADT documents' chapters, and the
    rules the reviewer layers are written as."""
    sources = [(path.name, title, source)
               for path in (ADT_PATH, ENTITIES_PATH)
               for title, source in _chapters(path)]
    for pattern in ("critic/*.viba", "assistant/*.viba", "critic/design/*.viba"):
        sources += [(path.name, "(whole file)", path.read_text(encoding="utf-8"))
                    for path in sorted(REPO_ROOT.glob(pattern))]
    return sources


def test_no_union_leaks_out_of_a_field_position() -> None:
    """`A * $x B | C` is `(A * $x B) | C`, never `A * $x (B | C)`."""
    leaked = []
    for file_name, title, source in _viba_sources():
        for definition in _definitions(source):
            branches = _leaked_branches(definition)
            if branches:
                leaked.append((file_name, title, definition.name, branches))
    assert leaked == [], leaked
