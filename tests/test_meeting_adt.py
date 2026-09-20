"""Tests for meeting_adt: one section per goal in the G1-G14 goal chain, covering edge cases and failure paths within each section."""

from datetime import datetime as _datetime, timezone as _dt_timezone
from typing import Optional

import pytest

import meeting_adt as adt


# =====================================================================
# G1: the four stream constants
# =====================================================================

class TestStreamNames:
    def test_order_is_fixed_per_adt(self) -> None:
        assert adt.STREAM_NAMES == (
            "screen",
            "chat",
            "score",
            "terminal_control",
        )

    def test_length_is_four(self) -> None:
        assert len(adt.STREAM_NAMES) == 4

    def test_constants_equal_positions_in_tuple(self) -> None:
        assert adt.SCREEN_STREAM_NAME == adt.STREAM_NAMES[0]
        assert adt.CHAT_STREAM_NAME == adt.STREAM_NAMES[1]
        assert adt.SCORE_STREAM_NAME == adt.STREAM_NAMES[2]
        assert adt.TERMINAL_CONTROL_STREAM_NAME == adt.STREAM_NAMES[3]

    def test_names_are_unique(self) -> None:
        assert len(set(adt.STREAM_NAMES)) == len(adt.STREAM_NAMES)

    def test_names_are_str_and_not_empty(self) -> None:
        for name in adt.STREAM_NAMES:
            assert isinstance(name, str)
            assert name

    def test_tuple_is_immutable(self) -> None:
        with pytest.raises(TypeError):
            adt.STREAM_NAMES[0] = "other"  # type: ignore[index]

    def test_public_exports_cover_every_stream_attr(self) -> None:
        for attr in (
            "SCREEN_STREAM_NAME",
            "CHAT_STREAM_NAME",
            "SCORE_STREAM_NAME",
            "TERMINAL_CONTROL_STREAM_NAME",
            "STREAM_NAMES",
        ):
            assert attr in adt.__all__
            assert getattr(adt, attr) is not None

    def test_public_exports_only_expected_names(self) -> None:
        assert set(adt.__all__) == {
            "SCREEN_STREAM_NAME",
            "CHAT_STREAM_NAME",
            "SCORE_STREAM_NAME",
            "TERMINAL_CONTROL_STREAM_NAME",
            "STREAM_NAMES",
            "HUMAN_USER_NAME",
            "is_human",
            "AgentRole",
            "agent_role_from_name",
            "ScreenCapture",
            "concat_text_field_of_list",
            "frame_rows",
            "row_plain",
            "plain_of",
            "assert_screen_plain_matches",
            "cell_width",
            "seg_attrs",
            "assert_screen_row_widths_fit",
            "assert_screen_rows_match_height",
            "assert_cursor_in_bounds",
            "assert_segments_valid",
            "STYLE_BOLD",
            "STYLE_UNDERLINE",
            "STYLE_ITALIC",
            "STYLE_REVERSE",
            "STYLE_BLINK",
            "STYLE_DIM",
            "STYLE_NAMES",
            "Rgb",
            "PaletteColor",
            "TerminalTextSegment",
            "RawMessage",
            "AtUserMessage",
            "ChatMessage",
            "ChatStreamChunk",
            "chat_part_to_dict",
            "chat_part_from_dict",
            "chat_message_to_dict",
            "chat_message_from_dict",
            "chat_stream_chunk_to_dict",
            "chat_stream_chunk_from_dict",
            "TextRegion",
            "ScreenReference",
            "ChatReference",
            "ScoreReference",
            "text_region_to_dict",
            "text_region_from_dict",
            "content_reference_to_dict",
            "content_reference_from_dict",
            "resolve_screen_reference",
            "resolve_chat_reference",
            "resolve_score_reference",
            "resolve_content_reference",
            "ScoreMessage",
            "ScoreStreamChunk",
            "score_message_to_dict",
            "score_message_from_dict",
            "score_stream_chunk_to_dict",
            "score_stream_chunk_from_dict",
            "score_summon_chained",
            "assert_score_summon_chained",
            "TextKey",
            "CtrlKey",
            "RestKey",
            "Keystroke",
            "TerminalControl",
            "TerminalControlStreamChunk",
            "keystroke_str_to_dict",
            "keystroke_str_from_dict",
            "keystroke_to_dict",
            "keystroke_from_dict",
            "terminal_control_to_dict",
            "terminal_control_from_dict",
            "terminal_control_stream_chunk_to_dict",
            "terminal_control_stream_chunk_from_dict",
            "owner_at",
            "terminal_capable",
            "assert_terminal_control_capable",
            "assert_keystroke_owner_matches",
            "ScreenStreamChunk",
            "StreamChunk",
            "terminal_text_segment_to_dict",
            "terminal_text_segment_from_dict",
            "screen_capture_to_dict",
            "screen_capture_from_dict",
            "screen_stream_chunk_to_dict",
            "screen_stream_chunk_from_dict",
            "stream_chunk_to_dict",
            "stream_chunk_from_dict",
            "MeetingData",
            "user_names_of_meeting_data",
            "assert_role_map_keys_in_user_names",
            "meeting_data_to_dict",
            "meeting_data_from_dict",
            "assert_file_desktop_member_order",
            "assert_file_desktop_unique_names",
            "assert_file_desktop_distinct_file_ids",
            "assert_file_id_in_store",
            "data_status_from_name",
            "DataStatus",
            "desktop_member_order_from_dict",
            "desktop_member_order_to_dict",
            "DesktopMemberOrder",
            "file_desktop_from_dict",
            "file_desktop_to_dict",
            "file_fold_from_dict",
            "file_fold_to_dict",
            "file_id_from_dict",
            "file_id_to_dict",
            "file_ids_of_desktop",
            "file_kv_store_from_dict",
            "file_kv_store_to_dict",
            "file_workspace_from_dict",
            "file_workspace_to_dict",
            "FileDesktop",
            "FileFold",
            "FileId",
            "FileEntry",
            "file_entry_to_dict",
            "file_entry_from_dict",
            "FileKvStore",
            "FileWorkspace",
            "fold_member_order_from_dict",
            "fold_member_order_to_dict",
            "FoldMemberOrder",
            "identity_desktop_order",
            "identity_workspace_order",
            "Versioned",
            "versioned_file_from_dict",
            "versioned_file_to_dict",
            "versioned_from_dict",
            "versioned_to_dict",
            "VersionedFile",
            "workspace_member_order_from_dict",
            "workspace_member_order_to_dict",
            "WorkspaceMemberOrder",
            "TerminalControlReference",
            "VersionedFileContentReference",
            "VersionedFileReference",
        }


# =====================================================================
# G2: UserName / AgentRole
# =====================================================================

class TestUserName:
    def test_human_sentinel_value(self) -> None:
        assert adt.HUMAN_USER_NAME == "$human"

    def test_is_human_true_for_sentinel(self) -> None:
        assert adt.is_human("$human") is True

    def test_is_human_false_for_agent_names(self) -> None:
        for name in ("alice", "agent-1", "Alice@provider", "x", "$humanity"):
            assert adt.is_human(name) is False

    def test_is_human_false_for_empty_and_role_names(self) -> None:
        assert adt.is_human("") is False
        assert adt.is_human("$generator") is False
        assert adt.is_human("$active_discriminator") is False
        assert adt.is_human("$passive_discriminator") is False


class TestAgentRole:
    def test_members_exactly_three(self) -> None:
        assert set(adt.AgentRole) == {
            adt.AgentRole.GENERATOR,
            adt.AgentRole.ACTIVE_DISCRIMINATOR,
            adt.AgentRole.PASSIVE_DISCRIMINATOR,
        }

    def test_json_values_match_adt_names(self) -> None:
        assert adt.AgentRole.GENERATOR.value == "$generator"
        assert adt.AgentRole.ACTIVE_DISCRIMINATOR.value == "$active_discriminator"
        assert adt.AgentRole.PASSIVE_DISCRIMINATOR.value == "$passive_discriminator"

    def test_role_names_never_collide_with_human_sentinel(self) -> None:
        for role in adt.AgentRole:
            assert role.value != adt.HUMAN_USER_NAME

    def test_from_name_roundtrip_every_member(self) -> None:
        for role in adt.AgentRole:
            assert adt.agent_role_from_name(role.value) is role
            # value -> enum -> value round-trips losslessly
            assert adt.agent_role_from_name(adt.agent_role_from_name(role.value).value) is role

    @pytest.mark.parametrize("bad", [
        "", "$root", "generator", "GENERATOR", "$generator ", " $generator",
        "$active", "$passive", "active_discriminator",
    ])
    def test_from_name_rejects_unknown_strings(self, bad: str) -> None:
        with pytest.raises(ValueError):
            adt.agent_role_from_name(bad)

    @pytest.mark.parametrize("bad", [None, 3, 3.5, ["$generator"], {"$generator"}])
    def test_from_name_rejects_non_strings(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            adt.agent_role_from_name(bad)  # type: ignore[arg-type]


# =====================================================================
# G3: ScreenCapture pure field layer
# =====================================================================

class TestScreenCapture:
    def test_defaults(self) -> None:
        cap = adt.ScreenCapture()
        assert cap.screen_data == ""
        assert cap.terminal_text_content == []
        assert cap.cursor_x == 0
        assert cap.cursor_y == 0
        assert cap.screen_width == 0
        assert cap.screen_height == 0
        assert cap.captured_at is None

    def test_holds_all_fields(self) -> None:
        moment = _datetime(2026, 9, 2, 7, 35, 37, 684000)
        cap = adt.ScreenCapture(
            screen_data="ab\nc",
            terminal_text_content=[_seg("ab\nc")],
            cursor_x=3,
            cursor_y=2,
            screen_width=80,
            screen_height=24,
            captured_at=moment,
        )
        assert cap.screen_data == "ab\nc"
        assert cap.terminal_text_content[0].text == "ab\nc"
        assert (cap.cursor_x, cap.cursor_y) == (3, 2)
        assert (cap.screen_width, cap.screen_height) == (80, 24)
        assert cap.captured_at == moment

    def test_default_list_independent_per_instance(self) -> None:
        a, b = adt.ScreenCapture(), adt.ScreenCapture()
        a.terminal_text_content.append(_seg("x"))
        assert len(a.terminal_text_content) == 1
        assert a.terminal_text_content[0].text == "x"
        assert b.terminal_text_content == []

    def test_zero_geometry_is_allowed_at_field_level(self) -> None:
        # zero geometry / cursor is legal at this layer; staying in bounds is the G5 asserts' job
        cap = adt.ScreenCapture()
        assert (cap.cursor_x, cap.cursor_y) == (0, 0)
        assert (cap.screen_width, cap.screen_height) == (0, 0)

    def test_is_dataclass_with_equality(self) -> None:
        a = adt.ScreenCapture(screen_data="s", screen_width=80)
        b = adt.ScreenCapture(screen_data="s", screen_width=80)
        assert a == b
        c = adt.ScreenCapture(screen_data="t", screen_width=80)
        assert a != c

    def test_repr_contains_fields(self) -> None:
        assert "screen_data" in repr(adt.ScreenCapture())


# minimal styled-segment object used by tests (the real TerminalTextSegment type is only defined in G6)
class _Seg:
    def __init__(
        self,
        text: str,
        fg: object = None,
        bg: object = None,
        styles: frozenset = frozenset(),
    ) -> None:
        self.text = text
        self.fg = fg
        self.bg = bg
        self.styles = styles


def _seg(text: str) -> _Seg:
    return _Seg(text)


def _styled(text: str, fg: object = None, bg: object = None,
            styles: frozenset = frozenset()) -> _Seg:
    return _Seg(text, fg=fg, bg=bg, styles=styles)



# =====================================================================
# G4: ScreenCapture assertion #1 -- plain_of == screen_data
# =====================================================================

class TestPlainHelpers:
    def test_concat_empty(self) -> None:
        assert adt.concat_text_field_of_list([]) == ""

    def test_concat_single_and_many(self) -> None:
        assert adt.concat_text_field_of_list([_seg("ab")]) == "ab"
        assert adt.concat_text_field_of_list([_seg("ab"), _seg("cd")]) == "abcd"

    def test_concat_preserves_newlines_inside_segment(self) -> None:
        # one segment may contain \n (background color spans rows without breaking)
        assert adt.concat_text_field_of_list([_seg("a\nb")]) == "a\nb"

    def test_frame_rows_splits_on_newline(self) -> None:
        assert adt.frame_rows("") == [""]
        assert adt.frame_rows("ab\nc") == ["ab", "c"]
        assert adt.frame_rows("ab\nc\n") == ["ab", "c", ""]

    def test_row_plain_strips_only_trailing_spaces(self) -> None:
        assert adt.row_plain("ab  ") == "ab"
        assert adt.row_plain("  ab") == "  ab"   # leading spaces are preserved
        assert adt.row_plain("  ab  ") == "  ab"
        assert adt.row_plain("   ") == ""
        assert adt.row_plain("") == ""

    def test_plain_of_basic(self) -> None:
        assert adt.plain_of([]) == ""
        assert adt.plain_of([_seg("hello")]) == "hello"

    def test_plain_of_trims_row_ends_across_segments(self) -> None:
        assert adt.plain_of([_seg("ab  \nc ")]) == "ab\nc"

    def test_plain_of_segments_across_newline_boundary(self) -> None:
        # segment 1 ends with spaces, segment 2 starts with \n -> after joining on the row, trailing spaces are right-trimmed
        assert adt.plain_of([_seg("ab  "), _seg("\nc ")]) == "ab\nc"


class TestAssertScreenPlainMatches:
    def test_pass_simple(self) -> None:
        cap = adt.ScreenCapture(screen_data="hello", terminal_text_content=[_seg("hello")])
        assert adt.assert_screen_plain_matches(cap) is None

    def test_pass_multirow_multiseg(self) -> None:
        cap = adt.ScreenCapture(
            screen_data="ab\nc",
            terminal_text_content=[_seg("ab"), _seg("\nc")],
        )
        adt.assert_screen_plain_matches(cap)

    def test_pass_when_segment_has_trailing_space_trimmed(self) -> None:
        # screen_data is already right-trimmed, so trailing spaces inside the segment must still pass
        cap = adt.ScreenCapture(screen_data="hi", terminal_text_content=[_seg("hi  ")])
        adt.assert_screen_plain_matches(cap)

    def test_pass_empty_both(self) -> None:
        adt.assert_screen_plain_matches(adt.ScreenCapture())

    def test_fail_text_differs(self) -> None:
        cap = adt.ScreenCapture(screen_data="hello", terminal_text_content=[_seg("helo")])
        with pytest.raises(AssertionError, match="plain_of"):
            adt.assert_screen_plain_matches(cap)

    def test_fail_missing_newline(self) -> None:
        cap = adt.ScreenCapture(screen_data="a\nb", terminal_text_content=[_seg("ab")])
        with pytest.raises(AssertionError):
            adt.assert_screen_plain_matches(cap)

    def test_fail_empty_segments_but_data_present(self) -> None:
        cap = adt.ScreenCapture(screen_data="a", terminal_text_content=[])
        with pytest.raises(AssertionError):
            adt.assert_screen_plain_matches(cap)

    def test_fail_extra_trailing_newline(self) -> None:
        # screen_data has no trailing blank row: one extra trailing \n in the segment text must be a violation
        cap = adt.ScreenCapture(screen_data="a", terminal_text_content=[_seg("a\n")])
        with pytest.raises(AssertionError):
            adt.assert_screen_plain_matches(cap)

    def test_error_message_carries_timestamp(self) -> None:
        moment = _datetime(2026, 9, 2, 7, 35, 37, 684000)
        cap = adt.ScreenCapture(
            screen_data="a", terminal_text_content=[_seg("b")], captured_at=moment
        )
        with pytest.raises(AssertionError, match="2026-09-02 07:35:37"):
            adt.assert_screen_plain_matches(cap)


# =====================================================================
# G5: ScreenCapture assertions #2-#5 -- width / row count / cursor in bounds / segment validity
# =====================================================================

class TestCellWidth:
    def test_ascii_width_one(self) -> None:
        assert adt.cell_width("hello") == 5

    def test_cjk_width_two(self) -> None:
        assert adt.cell_width("\u4e2d\u6587") == 4
        assert adt.cell_width("a\u4e2db") == 4

    def test_wide_and_fullwidth(self) -> None:
        assert adt.cell_width("\uff21") == 2   # fullwidth A
        assert adt.cell_width("\uff71") == 1    # halfwidth katakana takes 1 cell
        assert adt.cell_width("あ") == 2

    def test_control_and_combining_zero_width(self) -> None:
        assert adt.cell_width("\x00\x1b") == 0
        assert adt.cell_width("e\u0301") == 1  # combining accent takes no cell

    def test_empty_row_zero(self) -> None:
        assert adt.cell_width("") == 0

    def test_space_width_one(self) -> None:
        assert adt.cell_width("a b") == 3


class TestAssertScreenRowWidthsFit:
    def _cap(self, text: str, width: int) -> adt.ScreenCapture:
        return adt.ScreenCapture(screen_data=text, terminal_text_content=[_seg(text)],
                                 screen_width=width, screen_height=1)

    def test_pass_when_fits_exactly(self) -> None:
        adt.assert_screen_row_widths_fit(self._cap("ab", 2))

    def test_pass_empty_row_with_width(self) -> None:
        adt.assert_screen_row_widths_fit(self._cap("", 1))

    def test_fail_overflow_ascii(self) -> None:
        with pytest.raises(AssertionError, match="screen_width"):
            adt.assert_screen_row_widths_fit(self._cap("abc", 2))

    def test_fail_cjk_double_counted(self) -> None:
        with pytest.raises(AssertionError, match="width 4"):
            adt.assert_screen_row_widths_fit(self._cap("\u4e2d\u6587", 3))

    def test_fail_one_bad_row_among_many(self) -> None:
        cap = adt.ScreenCapture(
            screen_data="ab\ncd\nxyz",
            terminal_text_content=[_seg("ab\ncd\nxyz")],
            screen_width=2,
            screen_height=3,
        )
        with pytest.raises(AssertionError):
            adt.assert_screen_row_widths_fit(cap)


class TestAssertScreenRowsMatchHeight:
    def test_pass_single_row(self) -> None:
        cap = adt.ScreenCapture(screen_data="ab", screen_height=1)
        adt.assert_screen_rows_match_height(cap)

    def test_pass_three_rows(self) -> None:
        cap = adt.ScreenCapture(screen_data="a\nb\nc", screen_height=3)
        adt.assert_screen_rows_match_height(cap)

    def test_pass_blank_rows_kept(self) -> None:
        # blank rows are "" and survive split -> the row count still equals height
        cap = adt.ScreenCapture(screen_data="a\n\nb", screen_height=3)
        adt.assert_screen_rows_match_height(cap)

    def test_fail_height_mismatch(self) -> None:
        cap = adt.ScreenCapture(screen_data="a\nb", screen_height=3)
        with pytest.raises(AssertionError, match="rows 2 != screen_height 3"):
            adt.assert_screen_rows_match_height(cap)

    def test_fail_screen_data_trailing_newline_adds_row(self) -> None:
        # a trailing \n splits into an extra blank row -> count +1 (exactly the motivation behind the ADT's "no trailing blank row" rule)
        cap = adt.ScreenCapture(screen_data="a\nb\n", screen_height=2)
        with pytest.raises(AssertionError):
            adt.assert_screen_rows_match_height(cap)


class TestAssertCursorInBounds:
    def test_pass_top_left(self) -> None:
        cap = adt.ScreenCapture(cursor_x=0, cursor_y=0, screen_width=80, screen_height=24)
        adt.assert_cursor_in_bounds(cap)

    def test_pass_bottom_right_corner(self) -> None:
        cap = adt.ScreenCapture(cursor_x=79, cursor_y=23, screen_width=80, screen_height=24)
        adt.assert_cursor_in_bounds(cap)

    def test_fail_x_equal_width(self) -> None:
        cap = adt.ScreenCapture(cursor_x=80, cursor_y=0, screen_width=80, screen_height=24)
        with pytest.raises(AssertionError, match="out of bounds"):
            adt.assert_cursor_in_bounds(cap)

    def test_fail_y_equal_height(self) -> None:
        cap = adt.ScreenCapture(cursor_x=0, cursor_y=24, screen_width=80, screen_height=24)
        with pytest.raises(AssertionError):
            adt.assert_cursor_in_bounds(cap)

    def test_fail_negative(self) -> None:
        cap = adt.ScreenCapture(cursor_x=-1, cursor_y=0, screen_width=80, screen_height=24)
        with pytest.raises(AssertionError):
            adt.assert_cursor_in_bounds(cap)

    def test_fail_zero_sized_screen_never_holds_cursor(self) -> None:
        cap = adt.ScreenCapture(screen_width=0, screen_height=0)
        with pytest.raises(AssertionError):
            adt.assert_cursor_in_bounds(cap)


class TestAssertSegmentsValid:
    def test_pass_single_segment(self) -> None:
        cap = adt.ScreenCapture(terminal_text_content=[_seg("a")])
        adt.assert_segments_valid(cap)

    def test_pass_adjacent_different_attrs(self) -> None:
        segs = [_styled("a", fg="red"), _styled("b", fg="blue")]
        adt.assert_segments_valid(adt.ScreenCapture(terminal_text_content=segs))

    def test_pass_adjacent_diff_in_styles_only(self) -> None:
        segs = [_styled("a", styles=frozenset({"bold"})),
                _styled("b", styles=frozenset({"dim"}))]
        adt.assert_segments_valid(adt.ScreenCapture(terminal_text_content=segs))

    def test_pass_empty_segment_list(self) -> None:
        adt.assert_segments_valid(adt.ScreenCapture(terminal_text_content=[]))

    def test_fail_empty_text_segment(self) -> None:
        segs = [_seg("a"), _seg("")]
        with pytest.raises(AssertionError, match="empty segment at index 1"):
            adt.assert_segments_valid(adt.ScreenCapture(terminal_text_content=segs))

    def test_fail_adjacent_same_attrs(self) -> None:
        segs = [_styled("a", fg="red"), _styled("b", fg="red")]
        with pytest.raises(AssertionError, match="share attrs"):
            adt.assert_segments_valid(adt.ScreenCapture(terminal_text_content=segs))

    def test_fail_adjacent_same_attrs_bg_ignored_none_vs_none(self) -> None:
        segs = [_styled("a"), _styled("b")]
        with pytest.raises(AssertionError):
            adt.assert_segments_valid(adt.ScreenCapture(terminal_text_content=segs))


# =====================================================================
# G6: TerminalTextSegment / Color / Rgb / Style
# =====================================================================

class TestStyle:
    def test_style_names_exact_set(self) -> None:
        assert adt.STYLE_NAMES == frozenset({
            "bold", "underline", "italic", "reverse", "blink", "dim",
        })

    def test_style_constants_match_names(self) -> None:
        assert adt.STYLE_BOLD == "bold"
        assert adt.STYLE_UNDERLINE == "underline"
        assert adt.STYLE_ITALIC == "italic"
        assert adt.STYLE_REVERSE == "reverse"
        assert adt.STYLE_BLINK == "blink"
        assert adt.STYLE_DIM == "dim"


class TestRgb:
    def test_bounds_ok(self) -> None:
        assert adt.Rgb(0, 0, 0) == adt.Rgb(0, 0, 0)
        assert adt.Rgb(255, 255, 255).r == 255

    @pytest.mark.parametrize("r,g,b", [
        (-1, 0, 0), (256, 0, 0), (0, -1, 0), (0, 256, 0),
        (0, 0, -1), (0, 0, 256), (1000, 0, 0),
    ])
    def test_out_of_range_raises(self, r: int, g: int, b: int) -> None:
        with pytest.raises(ValueError, match="out of range"):
            adt.Rgb(r, g, b)

    def test_is_frozen(self) -> None:
        rgb = adt.Rgb(1, 2, 3)
        with pytest.raises(AttributeError):
            rgb.r = 9  # type: ignore[misc]


class TestPaletteColor:
    def test_bounds_ok(self) -> None:
        assert adt.PaletteColor(0).index == 0
        assert adt.PaletteColor(255).index == 255

    @pytest.mark.parametrize("index", [-1, 256, 1000])
    def test_out_of_range_raises(self, index: int) -> None:
        with pytest.raises(ValueError, match="palette index"):
            adt.PaletteColor(index)


class TestTerminalTextSegment:
    def test_defaults_none_and_empty_styles(self) -> None:
        seg = adt.TerminalTextSegment(text="hi")
        assert seg.fg is None
        assert seg.bg is None
        assert seg.styles == frozenset()

    def test_holds_colors(self) -> None:
        fg = adt.PaletteColor(7)
        bg = adt.Rgb(0, 0, 0)
        seg = adt.TerminalTextSegment(text="x", fg=fg, bg=bg,
                                      styles=frozenset({"bold", "dim"}))
        assert seg.fg == fg
        assert seg.bg == bg
        assert seg.styles == frozenset({"dim", "bold"})  # order-independent

    def test_text_may_contain_newline(self) -> None:
        seg = adt.TerminalTextSegment(text="a\nb")
        assert seg.text == "a\nb"

    def test_unknown_style_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown styles"):
            adt.TerminalTextSegment(text="x", styles=frozenset({"neon"}))

    def test_bad_color_type_raises(self) -> None:
        with pytest.raises(TypeError, match="fg must be"):
            adt.TerminalTextSegment(text="x", fg="red")  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        seg = adt.TerminalTextSegment(text="x")
        with pytest.raises(AttributeError):
            seg.text = "y"  # type: ignore[misc]

    def test_equality_by_value(self) -> None:
        a = adt.TerminalTextSegment(text="x", styles=frozenset({"bold"}))
        b = adt.TerminalTextSegment(text="x", styles=frozenset({"bold"}))
        assert a == b

    def test_fits_screen_capture_annotation(self) -> None:
        # the real type can be placed directly into ScreenCapture.terminal_text_content
        cap = adt.ScreenCapture(
            screen_data="ab",
            terminal_text_content=[adt.TerminalTextSegment(text="ab")],
            screen_width=2,
            screen_height=1,
        )
        adt.assert_screen_plain_matches(cap)
        adt.assert_segments_valid(cap)


# =====================================================================
# G7: ChatMessage / ChatStreamChunk (incl. persisted-JSON round-trips)
# =====================================================================

class TestTimeCodec:
    def test_iso_parse_z_suffix(self) -> None:
        moment = adt._iso_parse("2026-09-02T07:35:37.684Z")
        assert moment.year == 2026
        assert moment.microsecond == 684000
        assert moment.tzinfo is not None

    def test_iso_format_roundtrip_utc(self) -> None:
        moment = adt._iso_parse("2026-09-02T07:35:37.684Z")
        assert adt._iso_format(moment) == "2026-09-02T07:35:37.684000Z"

    def test_iso_format_naive_becomes_utc(self) -> None:
        moment = _datetime(2026, 9, 2, 7, 35, 37)
        assert adt._iso_format(moment).endswith("Z")

    def test_iso_roundtrip_preserves_microseconds(self) -> None:
        moment = _datetime(2026, 9, 2, 7, 35, 37, 123456, tzinfo=_dt_timezone.utc)
        assert adt._iso_parse(adt._iso_format(moment)) == moment


class TestChatPartCodec:
    def test_raw_roundtrip(self) -> None:
        part = adt.RawMessage(text="hello")
        assert adt.chat_part_from_dict(adt.chat_part_to_dict(part)) == part

    def test_at_roundtrip_human(self) -> None:
        part = adt.AtUserMessage(user_name=adt.HUMAN_USER_NAME)
        assert adt.chat_part_from_dict(adt.chat_part_to_dict(part)) == part

    def test_at_roundtrip_agent(self) -> None:
        part = adt.AtUserMessage(user_name="bob")
        assert adt.chat_part_from_dict(adt.chat_part_to_dict(part)) == part

    def test_at_json_key(self) -> None:
        assert adt.chat_part_to_dict(adt.AtUserMessage(user_name="bob")) == {
            "at_user_name": "bob",
        }

    def test_from_dict_unknown_part_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown chat part"):
            adt.chat_part_from_dict({"mention": "bob"})

    def test_to_dict_unknown_object_raises(self) -> None:
        with pytest.raises(TypeError, match="unknown chat part"):
            adt.chat_part_to_dict("raw-string")  # type: ignore[arg-type]


class TestChatMessageCodec:
    def _message(self) -> adt.ChatMessage:
        return adt.ChatMessage(
            user_name="alice",
            time=_datetime(2026, 9, 2, 7, 35, 41, 328000, tzinfo=_dt_timezone.utc),
            message=[
                adt.RawMessage(text="hi "),
                adt.AtUserMessage(user_name="bob"),
                adt.RawMessage(text=" look here"),
            ],
        )

    def test_roundtrip_full(self) -> None:
        msg = self._message()
        assert adt.chat_message_from_dict(adt.chat_message_to_dict(msg)) == msg

    def test_json_shape(self) -> None:
        payload = adt.chat_message_to_dict(self._message())
        assert payload["user_name"] == "alice"
        assert payload["time"].startswith("2026-09-02T07:35:41.328")
        assert payload["time"].endswith("Z")
        assert payload["message"] == [
            {"raw": "hi "},
            {"at_user_name": "bob"},
            {"raw": " look here"},
        ]

    def test_human_user_name_encoded(self) -> None:
        msg = adt.ChatMessage(user_name=adt.HUMAN_USER_NAME, time=_dt_now(),
                              message=[adt.RawMessage(text="x")])
        assert adt.chat_message_to_dict(msg)["user_name"] == "$human"

    def test_empty_message_list(self) -> None:
        msg = adt.ChatMessage(user_name="a", time=_dt_now())
        assert adt.chat_message_from_dict(adt.chat_message_to_dict(msg)) == msg

    def test_missing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing key"):
            adt.chat_message_from_dict({"user_name": "a", "time": "2026-09-02T07:35:37Z"})

    def test_bad_time_raises(self) -> None:
        with pytest.raises(ValueError):
            adt.chat_message_from_dict(
                {"user_name": "a", "time": "not-a-time", "message": []}
            )


class TestChatStreamChunkCodec:
    def _chunk(self) -> adt.ChatStreamChunk:
        return adt.ChatStreamChunk(
            stream_name=adt.CHAT_STREAM_NAME,
            chunks=[
                adt.ChatMessage(user_name="alice", time=_dt_now(),
                                message=[adt.RawMessage(text="a")]),
                adt.ChatMessage(user_name=adt.HUMAN_USER_NAME, time=_dt_now(),
                                message=[adt.AtUserMessage(user_name="alice")]),
            ],
        )

    def test_roundtrip(self) -> None:
        chunk = self._chunk()
        assert adt.chat_stream_chunk_from_dict(adt.chat_stream_chunk_to_dict(chunk)) == chunk

    def test_json_shape(self) -> None:
        payload = adt.chat_stream_chunk_to_dict(self._chunk())
        assert payload["stream_name"] == "chat"
        assert len(payload["chunks"]) == 2

    def test_from_dict_rejects_wrong_stream_name(self) -> None:
        with pytest.raises(ValueError, match="not a chat chunk"):
            adt.chat_stream_chunk_from_dict({"stream_name": "screen", "chunks": []})

    def test_default_stream_name_is_chat(self) -> None:
        assert adt.ChatStreamChunk().stream_name == "chat"


# =====================================================================
# G8: the three ContentReference variants + resolve()
# =====================================================================

class TestTextRegion:
    def test_basic(self) -> None:
        assert adt.TextRegion(0, 5) == adt.TextRegion(0, 5)

    def test_empty_region_ok(self) -> None:
        assert adt.TextRegion(3, 3).start == 3

    def test_negative_start_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid region"):
            adt.TextRegion(-1, 5)

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(ValueError):
            adt.TextRegion(5, 2)

    def test_roundtrip_dict(self) -> None:
        assert adt.text_region_from_dict(adt.text_region_to_dict(adt.TextRegion(1, 9))) \
            == adt.TextRegion(1, 9)


class TestContentReferenceCodec:
    def _screen_ref(self) -> adt.ScreenReference:
        return adt.ScreenReference(
            capture_time=_datetime(2026, 9, 2, 7, 35, 37, 684000, tzinfo=_dt_timezone.utc),
            stream_name="screen",
            region=adt.TextRegion(0, 5),
        )

    def _chat_ref(self) -> adt.ChatReference:
        return adt.ChatReference(
            msg_time=_datetime(2026, 9, 2, 7, 35, 41, 328000, tzinfo=_dt_timezone.utc),
            user_name="alice",
            region=adt.TextRegion(2, 8),
        )

    def _score_ref(self) -> adt.ScoreReference:
        return adt.ScoreReference(
            judge_time=_datetime(2026, 9, 2, 7, 35, 41, 328000, tzinfo=_dt_timezone.utc),
            judge_name="bob",
        )

    def test_screen_roundtrip(self) -> None:
        ref = self._screen_ref()
        assert adt.content_reference_from_dict(adt.content_reference_to_dict(ref)) == ref

    def test_chat_roundtrip(self) -> None:
        ref = self._chat_ref()
        assert adt.content_reference_from_dict(adt.content_reference_to_dict(ref)) == ref

    def test_score_roundtrip(self) -> None:
        ref = self._score_ref()
        assert adt.content_reference_from_dict(adt.content_reference_to_dict(ref)) == ref

    def test_screen_json_envelope(self) -> None:
        payload = adt.content_reference_to_dict(self._screen_ref())
        assert set(payload) == {"screen_ref"}
        assert payload["screen_ref"]["region"] == {"start": 0, "end": 5}
        assert payload["screen_ref"]["stream_name"] == "screen"

    def test_chat_json_envelope(self) -> None:
        payload = adt.content_reference_to_dict(self._chat_ref())
        assert set(payload) == {"chat_ref"}
        assert payload["chat_ref"]["user_name"] == "alice"

    def test_score_json_envelope(self) -> None:
        payload = adt.content_reference_to_dict(self._score_ref())
        assert set(payload) == {"score_ref"}
        assert payload["score_ref"]["judge_name"] == "bob"

    def test_from_dict_no_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown content reference"):
            adt.content_reference_from_dict({"foo": {}})

    def test_to_dict_unknown_type_raises(self) -> None:
        with pytest.raises(TypeError, match="unknown content reference"):
            adt.content_reference_to_dict("nope")  # type: ignore[arg-type]

    def test_score_ref_has_no_region_field(self) -> None:
        # ADT: score_ref carries only judge_time/judge_name
        assert not hasattr(self._score_ref(), "region")


class TestResolveScreen:
    def _capture(self, moment: _datetime) -> adt.ScreenCapture:
        return adt.ScreenCapture(screen_data="ab\ncd", screen_width=2, screen_height=2,
                                 captured_at=moment)

    def test_found(self) -> None:
        t = _datetime(2026, 9, 2, 7, 35, 37, 684000, tzinfo=_dt_timezone.utc)
        ref = adt.ScreenReference(capture_time=t, stream_name="screen",
                                  region=adt.TextRegion(0, 1))
        cap = self._capture(t)
        assert adt.resolve_screen_reference(ref, [self._capture(_dt_now()), cap]) is cap

    def test_not_found_raises_lookup(self) -> None:
        ref = adt.ScreenReference(capture_time=_dt_now(), stream_name="screen",
                                  region=adt.TextRegion(0, 1))
        with pytest.raises(LookupError, match="dangling"):
            adt.resolve_screen_reference(ref, [])

    def test_duplicate_timestamps_raise_ambiguous(self) -> None:
        t = _dt_now()
        ref = adt.ScreenReference(capture_time=t, stream_name="screen",
                                  region=adt.TextRegion(0, 1))
        with pytest.raises(ValueError, match="ambiguous"):
            adt.resolve_screen_reference(ref, [self._capture(t), self._capture(t)])


class TestResolveChat:
    def test_found(self) -> None:
        t = _datetime(2026, 9, 2, 7, 35, 41, 328000, tzinfo=_dt_timezone.utc)
        msg = adt.ChatMessage(user_name="alice", time=t, message=[adt.RawMessage(text="x")])
        ref = adt.ChatReference(msg_time=t, user_name="alice", region=adt.TextRegion(0, 1))
        assert adt.resolve_chat_reference(ref, [msg]) is msg

    def test_wrong_user_does_not_match(self) -> None:
        t = _dt_now()
        msg = adt.ChatMessage(user_name="alice", time=t)
        ref = adt.ChatReference(msg_time=t, user_name="bob", region=adt.TextRegion(0, 1))
        with pytest.raises(LookupError):
            adt.resolve_chat_reference(ref, [msg])

    def test_wrong_time_does_not_match(self) -> None:
        msg = adt.ChatMessage(user_name="alice", time=_dt_now())
        ref = adt.ChatReference(msg_time=_dt_now(), user_name="alice",
                                region=adt.TextRegion(0, 1))
        with pytest.raises(LookupError):
            adt.resolve_chat_reference(ref, [msg])


class TestResolveScore:
    def _stub_score(self, judge_name: str, moment: _datetime) -> object:
        # ScoreMessage is only defined in G9: use a minimal duck-typed object here
        return type("ScoreStub", (), {"judge_name": judge_name, "time": moment})()

    def test_found(self) -> None:
        t = _dt_now()
        ref = adt.ScoreReference(judge_time=t, judge_name="bob")
        stub = self._stub_score("bob", t)
        assert adt.resolve_score_reference(ref, [stub]) is stub

    def test_wrong_name_raises_lookup(self) -> None:
        ref = adt.ScoreReference(judge_time=_dt_now(), judge_name="bob")
        with pytest.raises(LookupError):
            adt.resolve_score_reference(ref, [self._stub_score("carol", ref.judge_time)])


class TestResolveDispatch:
    def test_dispatches_screen(self) -> None:
        t = _dt_now()
        ref = adt.ScreenReference(capture_time=t, stream_name="screen",
                                  region=adt.TextRegion(0, 1))
        cap = adt.ScreenCapture(captured_at=t)
        result = adt.resolve_content_reference(ref, [cap], [], [])
        assert result is cap

    def test_dispatches_chat(self) -> None:
        t = _dt_now()
        msg = adt.ChatMessage(user_name="u", time=t)
        ref = adt.ChatReference(msg_time=t, user_name="u", region=adt.TextRegion(0, 1))
        assert adt.resolve_content_reference(ref, [], [msg], []) is msg

    def test_rejects_unknown(self) -> None:
        with pytest.raises(TypeError, match="unknown content reference"):
            adt.resolve_content_reference("x", [], [], [])  # type: ignore[arg-type]


# =====================================================================
# G9: ScoreMessage / ScoreStreamChunk / summon_chained
# =====================================================================

def _score_ref_into(judge: str, moment: _datetime) -> adt.ScoreReference:
    return adt.ScoreReference(judge_time=moment, judge_name=judge)


def _score(tag: str = "correctness", judge: str = "bob",
           moment: Optional[_datetime] = None,
           reference: Optional[adt.ContentReference] = None) -> adt.ScoreMessage:
    return adt.ScoreMessage(
        tag=tag, score=0.8, comment="ok", judge_name=judge,
        time=moment or _dt_now(), reference=reference,
    )


class TestScoreMessageCodec:
    def test_roundtrip_full(self) -> None:
        msg = _score(reference=_score_ref_into("bob", _dt_now()))
        assert adt.score_message_from_dict(adt.score_message_to_dict(msg)) == msg

    def test_roundtrip_without_optional_parts(self) -> None:
        msg = adt.ScoreMessage(tag="style")
        assert adt.score_message_from_dict(adt.score_message_to_dict(msg)) == msg

    def test_json_shape(self) -> None:
        moment = _datetime(2026, 9, 2, 8, 0, 0, tzinfo=_dt_timezone.utc)
        payload = adt.score_message_to_dict(_score(judge="bob", moment=moment))
        assert payload["tag"] == "correctness"
        assert payload["score"] == 0.8
        assert payload["comment"] == "ok"
        assert payload["judge_name"] == "bob"
        assert payload["time"] == "2026-09-02T08:00:00Z"
        assert "reference" not in payload  # key omitted when there is no reference

    def test_json_none_score(self) -> None:
        payload = adt.score_message_to_dict(adt.ScoreMessage(tag="t"))
        assert payload["score"] is None
        assert payload["time"] is None

    def test_missing_tag_raises(self) -> None:
        with pytest.raises(ValueError, match="missing tag"):
            adt.score_message_from_dict({"score": 0.5})

    def test_human_judge_roundtrip(self) -> None:
        msg = adt.ScoreMessage(tag="t", judge_name=adt.HUMAN_USER_NAME)
        assert adt.score_message_from_dict(adt.score_message_to_dict(msg)) == msg


class TestScoreStreamChunkCodec:
    def test_empty_chunk_is_the_empty_stream_repr(self) -> None:
        # the real journal has no score events -> empty chunks are the empty-stream representation
        chunk = adt.ScoreStreamChunk()
        assert adt.score_stream_chunk_from_dict(adt.score_stream_chunk_to_dict(chunk)) == chunk
        assert chunk.chunks == []

    def test_roundtrip_nonempty(self) -> None:
        chunk = adt.ScoreStreamChunk(chunks=[_score(), _score(tag="clarity")])
        assert adt.score_stream_chunk_from_dict(adt.score_stream_chunk_to_dict(chunk)) == chunk

    def test_default_stream_name_is_score(self) -> None:
        assert adt.ScoreStreamChunk().stream_name == "score"

    def test_rejects_wrong_stream_name(self) -> None:
        with pytest.raises(ValueError, match="not a score chunk"):
            adt.score_stream_chunk_from_dict({"stream_name": "chat", "chunks": []})


class TestSummonChained:
    def _role_map(self) -> dict:
        return {
            "generator_bot": adt.AgentRole.GENERATOR,
            "active_bot": adt.AgentRole.ACTIVE_DISCRIMINATOR,
            "passive_bot": adt.AgentRole.PASSIVE_DISCRIMINATOR,
        }

    def _role_of(self, role_map: dict):
        return lambda name: role_map.get(name) if name in role_map else None

    def _mention_msg(self, judge: str) -> adt.ChatMessage:
        return adt.ChatMessage(
            user_name="someone", time=_dt_now(),
            message=[adt.RawMessage(text="please review "),
                     adt.AtUserMessage(user_name=judge)],
        )

    def test_generator_never_needs_summon(self) -> None:
        msg = _score(judge="generator_bot", reference=None)
        assert adt.score_summon_chained(msg, self._role_of(self._role_map()), None) is True

    def test_active_never_needs_summon(self) -> None:
        msg = _score(judge="active_bot", reference=None)
        assert adt.score_summon_chained(msg, self._role_of(self._role_map()), None) is True

    def test_human_judge_not_passive_so_chained(self) -> None:
        msg = _score(judge=adt.HUMAN_USER_NAME)
        assert adt.score_summon_chained(msg, self._role_of(self._role_map()), None) is True

    def test_passive_with_resolvable_summon_mention_chained(self) -> None:
        msg = _score(judge="passive_bot")
        mention = self._mention_msg("passive_bot")
        assert adt.score_summon_chained(msg, self._role_of(self._role_map()), mention) is True

    def test_passive_no_reference_not_chained(self) -> None:
        msg = _score(judge="passive_bot", reference=None)
        assert adt.score_summon_chained(msg, self._role_of(self._role_map()), None) is False

    def test_passive_reference_to_non_summon_message_not_chained(self) -> None:
        msg = _score(judge="passive_bot")
        other = adt.ChatMessage(user_name="x", time=_dt_now(),
                                message=[adt.RawMessage(text="no mention")])
        assert adt.score_summon_chained(msg, self._role_of(self._role_map()), other) is False

    def test_passive_reference_to_mention_of_other_judge_not_chained(self) -> None:
        msg = _score(judge="passive_bot")
        mention_other = self._mention_msg("generator_bot")
        assert adt.score_summon_chained(msg, self._role_of(self._role_map()),
                                        mention_other) is False

    def test_assert_passes_for_generator(self) -> None:
        msg = _score(judge="generator_bot")
        assert adt.assert_score_summon_chained(msg, self._role_of(self._role_map()), None) is None

    def test_assert_fails_for_unsummoned_passive(self) -> None:
        msg = _score(judge="passive_bot")
        with pytest.raises(AssertionError, match="not summon_chained"):
            adt.assert_score_summon_chained(msg, self._role_of(self._role_map()), None)


# =====================================================================
# G10: Keystroke types (harness-internal) / TerminalControl
# =====================================================================

class TestKeystrokeStrCodec:
    @pytest.mark.parametrize("key,json", [
        (adt.TextKey("a"), {"text": "a"}),
        (adt.CtrlKey("^C"), {"ctrl": "^C"}),
        (adt.RestKey(120), {"rest": {"millisecond": 120}}),
    ])
    def test_roundtrip(self, key: adt.KeystrokeStr, json: dict) -> None:
        assert adt.keystroke_str_to_dict(key) == json
        assert adt.keystroke_str_from_dict(json) == key

    def test_from_dict_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown keystroke str"):
            adt.keystroke_str_from_dict({"key": "x"})

    def test_to_dict_unknown_raises(self) -> None:
        with pytest.raises(TypeError, match="unknown keystroke str"):
            adt.keystroke_str_to_dict("a")  # type: ignore[arg-type]


class TestKeystrokeCodec:
    def _key(self) -> adt.Keystroke:
        return adt.Keystroke(
            data=adt.TextKey("h"),
            user_name="agent-1",
            stroke_time=_datetime(2026, 9, 2, 7, 35, 40, tzinfo=_dt_timezone.utc),
        )

    def test_roundtrip(self) -> None:
        assert adt.keystroke_from_dict(adt.keystroke_to_dict(self._key())) == self._key()

    def test_json_shape(self) -> None:
        payload = adt.keystroke_to_dict(self._key())
        assert payload == {
            "data": {"text": "h"},
            "user_name": "agent-1",
            "stroke_time": "2026-09-02T07:35:40Z",
        }

    def test_human_keystroke_roundtrip(self) -> None:
        key = adt.Keystroke(data=adt.RestKey(50), user_name=adt.HUMAN_USER_NAME,
                            stroke_time=_dt_now())
        assert adt.keystroke_from_dict(adt.keystroke_to_dict(key)) == key


class TestTerminalControlCodec:
    def _control(self) -> adt.TerminalControl:
        return adt.TerminalControl(
            tmux_window_name="w0",
            tmux_pane=0,
            user_name=adt.HUMAN_USER_NAME,
            start_time=_datetime(2026, 9, 2, 7, 35, 40, tzinfo=_dt_timezone.utc),
        )

    def test_roundtrip(self) -> None:
        ctl = self._control()
        assert adt.terminal_control_from_dict(adt.terminal_control_to_dict(ctl)) == ctl

    def test_json_shape(self) -> None:
        payload = adt.terminal_control_to_dict(self._control())
        assert payload["tmux_window_name"] == "w0"
        assert payload["tmux_pane"] == 0
        assert payload["user_name"] == "$human"

    def test_empty_start_time_roundtrip(self) -> None:
        ctl = adt.TerminalControl(user_name="a")
        assert adt.terminal_control_from_dict(adt.terminal_control_to_dict(ctl)) == ctl


class TestChunkCodecs:
    def test_terminal_control_chunk_roundtrip(self) -> None:
        chunk = adt.TerminalControlStreamChunk(chunks=[
            adt.TerminalControl(user_name="a", start_time=_dt_now()),
        ])
        assert adt.terminal_control_stream_chunk_from_dict(
            adt.terminal_control_stream_chunk_to_dict(chunk)) == chunk

    def test_default_stream_names(self) -> None:
        assert adt.TerminalControlStreamChunk().stream_name == "terminal_control"

    def test_wrong_stream_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a terminal_control chunk"):
            adt.terminal_control_stream_chunk_from_dict(
                {"stream_name": "chat", "chunks": []})


class TestOwnerAt:
    def _control(self, t: _datetime, user: str) -> adt.TerminalControl:
        return adt.TerminalControl(user_name=user, start_time=t)

    def test_returns_owner_of_covering_interval(self) -> None:
        t1 = _datetime(2026, 9, 2, 7, 0, 0, tzinfo=_dt_timezone.utc)
        t2 = _datetime(2026, 9, 2, 8, 0, 0, tzinfo=_dt_timezone.utc)
        controls = [self._control(t1, "alice"), self._control(t2, adt.HUMAN_USER_NAME)]
        mid = _datetime(2026, 9, 2, 7, 30, 0, tzinfo=_dt_timezone.utc)
        assert adt.owner_at(mid, controls) == "alice"
        late = _datetime(2026, 9, 2, 8, 30, 0, tzinfo=_dt_timezone.utc)
        assert adt.owner_at(late, controls) == "$human"

    def test_before_first_interval_returns_none(self) -> None:
        t1 = _datetime(2026, 9, 2, 7, 0, 0, tzinfo=_dt_timezone.utc)
        early = _datetime(2026, 9, 2, 6, 0, 0, tzinfo=_dt_timezone.utc)
        assert adt.owner_at(early, [self._control(t1, "a")]) is None

    def test_exact_start_time_is_included(self) -> None:
        t1 = _datetime(2026, 9, 2, 7, 0, 0, tzinfo=_dt_timezone.utc)
        assert adt.owner_at(t1, [self._control(t1, "a")]) == "a"

    def test_no_controls_returns_none(self) -> None:
        assert adt.owner_at(_dt_now(), []) is None

    def test_unsorted_input_still_works(self) -> None:
        t1 = _datetime(2026, 9, 2, 7, 0, 0, tzinfo=_dt_timezone.utc)
        t2 = _datetime(2026, 9, 2, 8, 0, 0, tzinfo=_dt_timezone.utc)
        controls = [self._control(t2, "b"), self._control(t1, "a")]
        mid = _datetime(2026, 9, 2, 7, 30, 0, tzinfo=_dt_timezone.utc)
        assert adt.owner_at(mid, controls) == "a"


class TestTerminalCapable:
    def _role_of(self):
        return lambda name: {
            "gen": adt.AgentRole.GENERATOR,
            "act": adt.AgentRole.ACTIVE_DISCRIMINATOR,
            "pas": adt.AgentRole.PASSIVE_DISCRIMINATOR,
        }.get(name)

    def test_human_capable(self) -> None:
        assert adt.terminal_capable(adt.HUMAN_USER_NAME, self._role_of()) is True

    def test_generator_capable(self) -> None:
        assert adt.terminal_capable("gen", self._role_of()) is True

    def test_discriminators_not_capable(self) -> None:
        assert adt.terminal_capable("act", self._role_of()) is False
        assert adt.terminal_capable("pas", self._role_of()) is False

    def test_unknown_agent_not_capable(self) -> None:
        assert adt.terminal_capable("stranger", self._role_of()) is False

    def test_assert_passes_and_fails(self) -> None:
        role_of = self._role_of()
        adt.assert_terminal_control_capable(
            adt.TerminalControl(user_name="gen"), role_of)
        with pytest.raises(AssertionError, match="not terminal_capable"):
            adt.assert_terminal_control_capable(
                adt.TerminalControl(user_name="act"), role_of)


class TestAssertKeystrokeOwnerMatches:
    def test_pass_within_interval(self) -> None:
        t0 = _datetime(2026, 9, 2, 7, 0, 0, tzinfo=_dt_timezone.utc)
        t1 = _datetime(2026, 9, 2, 7, 0, 5, tzinfo=_dt_timezone.utc)
        controls = [adt.TerminalControl(user_name="alice", start_time=t0)]
        key = adt.Keystroke(data=adt.TextKey("x"), user_name="alice", stroke_time=t1)
        adt.assert_keystroke_owner_matches(key, controls)

    def test_fail_without_interval(self) -> None:
        key = adt.Keystroke(data=adt.TextKey("x"), user_name="alice",
                            stroke_time=_dt_now())
        with pytest.raises(AssertionError, match="owner_at None"):
            adt.assert_keystroke_owner_matches(key, [])

    def test_fail_owner_mismatch(self) -> None:
        t0 = _datetime(2026, 9, 2, 7, 0, 0, tzinfo=_dt_timezone.utc)
        controls = [adt.TerminalControl(user_name="bob", start_time=t0)]
        key = adt.Keystroke(data=adt.TextKey("x"), user_name="alice", stroke_time=t0)
        with pytest.raises(AssertionError, match="bob"):
            adt.assert_keystroke_owner_matches(key, controls)


# =====================================================================
# G11: ScreenStreamChunk + MeetingData (the meeting record proper)
# =====================================================================

class TestScreenCaptureCodec:
    def _capture(self) -> adt.ScreenCapture:
        return adt.ScreenCapture(
            screen_data="ab",
            terminal_text_content=[
                adt.TerminalTextSegment(text="a", fg=adt.PaletteColor(7)),
                adt.TerminalTextSegment(
                    text="b", fg=adt.Rgb(1, 2, 3), styles=frozenset({"bold", "dim"})),
            ],
            cursor_x=1, cursor_y=0, screen_width=2, screen_height=1,
            captured_at=_datetime(2026, 9, 2, 7, 35, 37, 684000, tzinfo=_dt_timezone.utc),
        )

    def test_roundtrip_full(self) -> None:
        cap = self._capture()
        assert adt.screen_capture_from_dict(adt.screen_capture_to_dict(cap)) == cap

    def test_json_shape(self) -> None:
        payload = adt.screen_capture_to_dict(self._capture())
        assert payload["screen_data"] == "ab"
        assert payload["terminal_text_content"][0]["fg"] == {"palette": 7}
        assert payload["terminal_text_content"][1]["fg"] == {"r": 1, "g": 2, "b": 3}
        assert payload["terminal_text_content"][1]["styles"] == ["bold", "dim"]
        assert payload["cursor_x"] == 1
        assert payload["captured_at"].endswith("Z")

    def test_roundtrip_empty_segments_and_no_time(self) -> None:
        cap = adt.ScreenCapture(screen_data="")
        assert adt.screen_capture_from_dict(adt.screen_capture_to_dict(cap)) == cap


class TestTerminalTextSegmentCodec:
    def test_roundtrip_all_color_variants(self) -> None:
        for fg in (None, adt.PaletteColor(0), adt.PaletteColor(255),
                   adt.Rgb(0, 0, 0), adt.Rgb(255, 128, 1)):
            seg = adt.TerminalTextSegment(text="x", fg=fg, bg=None,
                                          styles=frozenset({"reverse"}))
            assert adt.terminal_text_segment_from_dict(
                adt.terminal_text_segment_to_dict(seg)) == seg

    def test_color_codec_unknown_raises(self) -> None:
        with pytest.raises(KeyError):
            adt._color_from_dict({"hue": 3})


class TestScreenStreamChunkCodec:
    def test_roundtrip(self) -> None:
        chunk = adt.ScreenStreamChunk(
            stream_name="screen",
            owner_user_name="agent-1",
            chunks=[adt.ScreenCapture(screen_data="a", captured_at=_dt_now())],
        )
        assert adt.screen_stream_chunk_from_dict(
            adt.screen_stream_chunk_to_dict(chunk)) == chunk

    def test_default_stream_name_is_screen(self) -> None:
        assert adt.ScreenStreamChunk().stream_name == "screen"

    def test_rejects_wrong_stream_name(self) -> None:
        with pytest.raises(ValueError, match="not a screen chunk"):
            adt.screen_stream_chunk_from_dict({"stream_name": "chat", "chunks": []})


class TestStreamChunkDispatch:
    def _sample_chunk_for(self, stream_name: str) -> adt.StreamChunk:
        now = _dt_now()
        return {
            "screen": adt.ScreenStreamChunk(owner_user_name="a",
                                            chunks=[adt.ScreenCapture(captured_at=now)]),
            "chat": adt.ChatStreamChunk(chunks=[adt.ChatMessage(user_name="a", time=now)]),
            "score": adt.ScoreStreamChunk(chunks=[adt.ScoreMessage(tag="t")]),
            "terminal_control": adt.TerminalControlStreamChunk(chunks=[
                adt.TerminalControl(user_name="a", start_time=now)]),
        }[stream_name]

    @pytest.mark.parametrize("stream_name", adt.STREAM_NAMES)
    def test_each_stream_roundtrips_through_generic_codec(
            self, stream_name: str) -> None:
        chunk = self._sample_chunk_for(stream_name)
        restored = adt.stream_chunk_from_dict(adt.stream_chunk_to_dict(chunk))
        assert type(restored) is type(chunk)
        assert restored == chunk

    def test_unknown_stream_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown stream_name"):
            adt.stream_chunk_from_dict({"stream_name": "brainwaves", "chunks": []})

    def test_to_dict_unknown_type_raises(self) -> None:
        with pytest.raises(TypeError, match="unknown stream chunk"):
            adt.stream_chunk_to_dict("nope")  # type: ignore[arg-type]


class TestUserNames:
    def test_collects_from_all_four_streams(self) -> None:
        now = _dt_now()
        data = adt.MeetingData(
            sorted_stream_chunks=[
                adt.ScreenStreamChunk(owner_user_name="screen-owner"),
                adt.ChatStreamChunk(chunks=[adt.ChatMessage(user_name="chat-user",
                                                            time=now)]),
                adt.ScoreStreamChunk(chunks=[adt.ScoreMessage(tag="t",
                                                              judge_name="judge")]),
                adt.TerminalControlStreamChunk(chunks=[adt.TerminalControl(
                    user_name=adt.HUMAN_USER_NAME, start_time=now)]),
            ],
        )
        assert adt.user_names_of_meeting_data(data) == {
            "screen-owner", "chat-user", "judge", "$human",
        }

    def test_empty_meeting_has_no_names(self) -> None:
        assert adt.user_names_of_meeting_data(adt.MeetingData()) == set()


class TestMeetingData:
    def _sample(self) -> adt.MeetingData:
        now = _dt_now()
        return adt.MeetingData(
            meeting_uid="m-1",
            meeting_name="demo",
            agent_name2role={
                "alice": adt.AgentRole.GENERATOR,
                "bob": adt.AgentRole.PASSIVE_DISCRIMINATOR,
            },
            sorted_stream_chunks=[
                adt.ScreenStreamChunk(owner_user_name="alice",
                                      chunks=[adt.ScreenCapture(captured_at=now)]),
                adt.ChatStreamChunk(chunks=[
                    adt.ChatMessage(user_name="alice", time=now,
                                    message=[adt.RawMessage(text="hi")]),
                    adt.ChatMessage(user_name="bob", time=now,
                                    message=[adt.RawMessage(text="ok")]),
                ]),
            ],
        )

    def test_roundtrip_full(self) -> None:
        data = self._sample()
        assert adt.meeting_data_from_dict(adt.meeting_data_to_dict(data)) == data

    def test_json_shape(self) -> None:
        payload = adt.meeting_data_to_dict(self._sample())
        assert payload["meeting_uid"] == "m-1"
        assert payload["agent_name2role"] == {
            "alice": "$generator",
            "bob": "$passive_discriminator",
        }
        assert [c["stream_name"] for c in payload["sorted_stream_chunks"]] == \
            ["screen", "chat"]

    def test_missing_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing key"):
            adt.meeting_data_from_dict({"meeting_uid": "m-1"})

    def test_empty_meeting_roundtrip(self) -> None:
        data = adt.MeetingData(meeting_uid="m0", meeting_name="")
        assert adt.meeting_data_from_dict(adt.meeting_data_to_dict(data)) == data

    def test_role_map_assert_passes(self) -> None:
        adt.assert_role_map_keys_in_user_names(self._sample())

    def test_role_map_assert_fails_for_unknown_agent(self) -> None:
        data = self._sample()
        data.agent_name2role["ghost"] = adt.AgentRole.GENERATOR
        with pytest.raises(AssertionError, match="ghost"):
            adt.assert_role_map_keys_in_user_names(data)

    def test_human_never_needs_role_entry(self) -> None:
        # humans are identified by is_human and never need an agent_name2role entry
        data = self._sample()
        adt.assert_role_map_keys_in_user_names(data)






# test helper: a UTC-aware now
def _dt_now() -> _datetime:
    return _datetime.now(tz=_dt_timezone.utc)


# =====================================================================
# G15: file layer (FileKvStore / FileDesktop)
# =====================================================================

def _file_id(file_id: str) -> adt.FileId:
    return adt.FileId(file_id=file_id)


def _entry(file_id: str, entry_name: str) -> adt.FileEntry:
    """One name in one place: what a workspace or a fold holds."""
    return adt.FileEntry(entry_name=entry_name, file_id=_file_id(file_id))


def _versioned(data, moment=None, author: str = "") -> adt.Versioned:
    return adt.Versioned(data=data, time=moment, author=author)


def _sample_desktop() -> adt.FileDesktop:
    fold = adt.FileFold(fold_name="pkg", member_file_ids=[_entry("f2", "b.py")])
    workspace = adt.FileWorkspace(
        member_files=[_entry("f1", "a.py"), fold],
        workspace_name="ws",
        workspace_status=adt.DataStatus.READ_ONLY,
    )
    desktop = adt.FileDesktop(workspaces=[workspace])
    desktop.reorder_indexes = _versioned(adt.identity_desktop_order(desktop),
                                         author="alice")
    return desktop


def _sample_store() -> adt.FileKvStore:
    return adt.FileKvStore(file_id2file={
        "f1": adt.VersionedFile(
            file_name=[_versioned("a.py", author="alice")],
            file_meta=[_versioned(adt.DataStatus.READ_ONLY, author="alice")],
            file_content=[_versioned("print(1)", author="bob")],
        ),
        "f2": adt.VersionedFile(
            file_name=[_versioned("b.py", author="bob")],
            file_meta=[_versioned(None, author="bob")],
            file_content=[_versioned("", author="bob")],
        ),
    })


class TestDataStatusCodec:
    def test_values(self) -> None:
        assert adt.DataStatus.DELETED.value == "deleted"
        assert adt.DataStatus.READ_ONLY.value == "read_only"
        assert adt.data_status_from_name("deleted") is adt.DataStatus.DELETED

    def test_unknown_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown data status"):
            adt.data_status_from_name("hidden")


class TestVersionedCodec:
    def test_roundtrip_with_time_and_author(self) -> None:
        value = _versioned("body", _dt_now(), "alice")
        payload = adt.versioned_to_dict(value, str)
        assert payload == {"data": "body", "time": adt._iso_format(value.time),
                           "author": "alice"}
        assert adt.versioned_from_dict(payload, str) == value

    def test_roundtrip_without_time(self) -> None:
        value = _versioned("body")
        assert adt.versioned_to_dict(value, str)["time"] is None
        assert adt.versioned_from_dict(adt.versioned_to_dict(value, str), str) == value

    def test_status_payload_encodes_none(self) -> None:
        value = _versioned(None)
        payload = adt.versioned_to_dict(value, adt._data_status_encode)
        assert payload["data"] is None
        assert adt.versioned_from_dict(payload, adt._data_status_decode).data is None

    def test_status_payload_decodes_name(self) -> None:
        value = _versioned(adt.DataStatus.DELETED)
        restored = adt.versioned_from_dict(
            adt.versioned_to_dict(value, adt._data_status_encode), adt._data_status_decode)
        assert restored.data is adt.DataStatus.DELETED


class TestFileIdCodec:
    def test_roundtrip(self) -> None:
        file_id = _file_id("f1")
        payload = adt.file_id_to_dict(file_id)
        assert payload == {"file_id": "f1"}
        assert adt.file_id_from_dict(payload) == file_id

    def test_defaults(self) -> None:
        assert adt.file_id_from_dict({}) == adt.FileId()


class TestFileEntryCodec:
    def test_roundtrip(self) -> None:
        entry = _entry("f1", "a.py")
        payload = adt.file_entry_to_dict(entry)
        assert payload == {"entry_name": "a.py", "file_id": {"file_id": "f1"}}
        assert adt.file_entry_from_dict(payload) == entry

    def test_defaults(self) -> None:
        assert adt.file_entry_from_dict({}) == adt.FileEntry()


class TestVersionedFileCodec:
    def test_roundtrip(self) -> None:
        versioned_file = _sample_store().file_id2file["f1"]
        restored = adt.versioned_file_from_dict(adt.versioned_file_to_dict(versioned_file))
        assert restored == versioned_file
        assert restored.file_meta[-1].data is adt.DataStatus.READ_ONLY

    def test_empty_payload_gives_empty_chains(self) -> None:
        restored = adt.versioned_file_from_dict({})
        assert restored.file_name == []
        assert restored.file_meta == []
        assert restored.file_content == []


class TestFileKvStoreCodec:
    def test_roundtrip(self) -> None:
        store = _sample_store()
        restored = adt.file_kv_store_from_dict(adt.file_kv_store_to_dict(store))
        assert restored == store


class TestFileDesktopCodec:
    def test_roundtrip(self) -> None:
        desktop = _sample_desktop()
        restored = adt.file_desktop_from_dict(adt.file_desktop_to_dict(desktop))
        assert restored == desktop

    def test_fold_member_and_direct_file_survive(self) -> None:
        restored = adt.file_desktop_from_dict(adt.file_desktop_to_dict(_sample_desktop()))
        workspace = restored.workspaces[0]
        assert workspace.workspace_status is adt.DataStatus.READ_ONLY
        assert isinstance(workspace.member_files[0], adt.FileEntry)
        assert isinstance(workspace.member_files[1], adt.FileFold)
        assert workspace.member_files[0].entry_name == "a.py"
        assert workspace.member_files[0].file_id.file_id == "f1"
        assert workspace.member_files[1].member_file_ids[0].entry_name == "b.py"

    def test_empty_payload(self) -> None:
        restored = adt.file_desktop_from_dict({})
        assert restored.workspaces == []
        assert restored.reorder_indexes.data.workspace_indexes == []


class TestFileDesktopUniqueNames:
    def test_sample_desktop_is_valid(self) -> None:
        adt.assert_file_desktop_unique_names(_sample_desktop())

    def test_duplicate_workspace_names_rejected(self) -> None:
        desktop = _sample_desktop()
        desktop.workspaces.append(adt.FileWorkspace(workspace_name="ws"))
        with pytest.raises(AssertionError, match="duplicate workspace names"):
            adt.assert_file_desktop_unique_names(desktop)

    def test_unnamed_workspaces_do_not_collide(self) -> None:
        desktop = adt.FileDesktop(workspaces=[adt.FileWorkspace(), adt.FileWorkspace()])
        adt.assert_file_desktop_unique_names(desktop)

    def test_fold_name_colliding_with_file_entry_name_rejected(self) -> None:
        desktop = adt.FileDesktop(workspaces=[adt.FileWorkspace(member_files=[
            _entry("f1", "pkg"),
            adt.FileFold(fold_name="pkg"),
        ], workspace_name="ws")])
        with pytest.raises(AssertionError, match="duplicate names under workspace"):
            adt.assert_file_desktop_unique_names(desktop)

    def test_duplicate_entry_names_inside_fold_rejected(self) -> None:
        desktop = adt.FileDesktop(workspaces=[adt.FileWorkspace(member_files=[
            adt.FileFold(fold_name="pkg", member_file_ids=[
                _entry("f1", "a.py"), _entry("f2", "a.py")]),
        ], workspace_name="ws")])
        with pytest.raises(AssertionError, match="duplicate entry names under fold"):
            adt.assert_file_desktop_unique_names(desktop)


class TestFileDesktopDistinctFileIds:
    def test_sample_desktop_is_valid(self) -> None:
        adt.assert_file_desktop_distinct_file_ids(_sample_desktop())

    def test_one_id_under_two_paths_rejected(self) -> None:
        desktop = adt.FileDesktop(workspaces=[
            adt.FileWorkspace(member_files=[
                _entry("f1", "a.py"),
                adt.FileFold(fold_name="pkg", member_file_ids=[_entry("f1", "again.py")]),
            ], workspace_name="ws"),
            adt.FileWorkspace(member_files=[_entry("f1", "third.py")], workspace_name="other"),
        ])
        with pytest.raises(AssertionError, match="one file id under two paths"):
            adt.assert_file_desktop_distinct_file_ids(desktop)


class TestFileDesktopMemberOrder:
    def test_identity_order_is_valid(self) -> None:
        adt.assert_file_desktop_member_order(_sample_desktop())

    def test_empty_desktop_is_valid(self) -> None:
        adt.assert_file_desktop_member_order(adt.FileDesktop())

    def test_missing_workspace_index_rejected(self) -> None:
        desktop = _sample_desktop()
        desktop.workspaces.append(adt.FileWorkspace(workspace_name="other"))
        with pytest.raises(AssertionError, match="workspace_indexes is not a permutation"):
            adt.assert_file_desktop_member_order(desktop)

    def test_duplicate_workspace_index_rejected(self) -> None:
        desktop = _sample_desktop()
        order = desktop.reorder_indexes.data
        order.workspace_indexes.append(order.workspace_indexes[0])
        with pytest.raises(AssertionError, match="not a permutation"):
            adt.assert_file_desktop_member_order(desktop)

    def test_file_index_not_a_permutation_rejected(self) -> None:
        desktop = _sample_desktop()
        desktop.reorder_indexes.data.workspace_indexes[0].file_indexes = [(0, None), (0, None)]
        with pytest.raises(AssertionError, match="file_indexes of workspace 0"):
            adt.assert_file_desktop_member_order(desktop)

    def test_fold_order_not_a_permutation_rejected(self) -> None:
        desktop = _sample_desktop()
        desktop.reorder_indexes.data.workspace_indexes[0].file_indexes[1] = (
            1, adt.FoldMemberOrder(member_indexes=[0, 0]))
        with pytest.raises(AssertionError, match="member_indexes of fold"):
            adt.assert_file_desktop_member_order(desktop)

    def test_fold_order_on_a_plain_file_rejected(self) -> None:
        desktop = _sample_desktop()
        desktop.reorder_indexes.data.workspace_indexes[0].file_indexes[0] = (
            0, adt.FoldMemberOrder(member_indexes=[]))
        with pytest.raises(AssertionError, match="carries a fold order but is not a fold"):
            adt.assert_file_desktop_member_order(desktop)


class TestFileIdInStore:
    def test_present_id_passes(self) -> None:
        store = _sample_store()
        for file_id in adt.file_ids_of_desktop(_sample_desktop()):
            adt.assert_file_id_in_store(file_id, store)

    def test_missing_id_rejected(self) -> None:
        with pytest.raises(AssertionError, match="not in"):
            adt.assert_file_id_in_store(_file_id("nope"), _sample_store())

    def test_walker_collects_direct_and_fold_members(self) -> None:
        ids = [f.file_id for f in adt.file_ids_of_desktop(_sample_desktop())]
        assert ids == ["f1", "f2"]


class TestFileLayerUserNames:
    def test_authors_are_collected(self) -> None:
        data = adt.MeetingData(file_kv_store=_sample_store(),
                               file_desktop=_sample_desktop())
        assert adt.user_names_of_meeting_data(data) == {"alice", "bob"}

    def test_empty_file_layer_adds_nothing(self) -> None:
        assert adt.user_names_of_meeting_data(adt.MeetingData()) == set()


class TestMeetingDataFileLayer:
    def test_roundtrip_keeps_the_file_layer(self) -> None:
        data = adt.MeetingData(meeting_uid="m", file_kv_store=_sample_store(),
                               file_desktop=_sample_desktop())
        restored = adt.meeting_data_from_dict(adt.meeting_data_to_dict(data))
        assert restored.file_kv_store == data.file_kv_store
        assert restored.file_desktop == data.file_desktop

    def test_old_payload_without_file_layer_still_loads(self) -> None:
        payload = {"meeting_uid": "m", "meeting_name": "x", "agent_name2role": {},
                   "sorted_stream_chunks": []}
        data = adt.meeting_data_from_dict(payload)
        assert data.file_kv_store.file_id2file == {}
        assert data.file_desktop.workspaces == []
