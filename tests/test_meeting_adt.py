"""meeting_adt 测试：随目标链 G1–G14 每颗一个分区，分区内覆盖边界与失败路径。"""

from datetime import datetime as _datetime

import pytest

import meeting_adt as adt


# =====================================================================
# G1：五流常量
# =====================================================================

class TestStreamNames:
    def test_order_is_fixed_per_adt(self) -> None:
        assert adt.STREAM_NAMES == (
            "screen",
            "chat",
            "score",
            "keystroke",
            "terminal_control",
        )

    def test_length_is_five(self) -> None:
        assert len(adt.STREAM_NAMES) == 5

    def test_constants_equal_positions_in_tuple(self) -> None:
        assert adt.SCREEN_STREAM_NAME == adt.STREAM_NAMES[0]
        assert adt.CHAT_STREAM_NAME == adt.STREAM_NAMES[1]
        assert adt.SCORE_STREAM_NAME == adt.STREAM_NAMES[2]
        assert adt.KEYSTROKE_STREAM_NAME == adt.STREAM_NAMES[3]
        assert adt.TERMINAL_CONTROL_STREAM_NAME == adt.STREAM_NAMES[4]

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
            "KEYSTROKE_STREAM_NAME",
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
            "KEYSTROKE_STREAM_NAME",
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
        }


# =====================================================================
# G2：UserName / AgentRole
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
            # 值 -> 枚举 -> 值 无损
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
# G3：ScreenCapture 纯字段层
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
        # 0 尺寸/光标在此层合法；界内性由 G5 断言负责
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


# 测试内最小的带样式段对象（TerminalTextSegment 在 G6 才定义真类型）
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
# G4：ScreenCapture 断言① —— plain_of == screen_data
# =====================================================================

class TestPlainHelpers:
    def test_concat_empty(self) -> None:
        assert adt.concat_text_field_of_list([]) == ""

    def test_concat_single_and_many(self) -> None:
        assert adt.concat_text_field_of_list([_seg("ab")]) == "ab"
        assert adt.concat_text_field_of_list([_seg("ab"), _seg("cd")]) == "abcd"

    def test_concat_preserves_newlines_inside_segment(self) -> None:
        # 单段内可含 \n（跨行背景色不断开）
        assert adt.concat_text_field_of_list([_seg("a\nb")]) == "a\nb"

    def test_frame_rows_splits_on_newline(self) -> None:
        assert adt.frame_rows("") == [""]
        assert adt.frame_rows("ab\nc") == ["ab", "c"]
        assert adt.frame_rows("ab\nc\n") == ["ab", "c", ""]

    def test_row_plain_strips_only_trailing_spaces(self) -> None:
        assert adt.row_plain("ab  ") == "ab"
        assert adt.row_plain("  ab") == "  ab"   # 前导空格保留
        assert adt.row_plain("  ab  ") == "  ab"
        assert adt.row_plain("   ") == ""
        assert adt.row_plain("") == ""

    def test_plain_of_basic(self) -> None:
        assert adt.plain_of([]) == ""
        assert adt.plain_of([_seg("hello")]) == "hello"

    def test_plain_of_trims_row_ends_across_segments(self) -> None:
        assert adt.plain_of([_seg("ab  \nc ")]) == "ab\nc"

    def test_plain_of_segments_across_newline_boundary(self) -> None:
        # 段1 以空格收尾、段2 以 \n 开头 → 行内合并后该行右去尾空格
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
        # screen_data 已右去尾空格：段里有尾空格也须通过
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
        # screen_data 无尾随空行：段文本末尾多一个 \n 必须判违规
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
# G5：ScreenCapture 断言②-⑤ —— 宽度 / 行数 / 光标界内 / 段合法
# =====================================================================

class TestCellWidth:
    def test_ascii_width_one(self) -> None:
        assert adt.cell_width("hello") == 5

    def test_cjk_width_two(self) -> None:
        assert adt.cell_width("中文") == 4
        assert adt.cell_width("a中b") == 4

    def test_wide_and_fullwidth(self) -> None:
        assert adt.cell_width("Ａ") == 2   # fullwidth A
        assert adt.cell_width("ｱ") == 1    # halfwidth katakana 占 1 格
        assert adt.cell_width("あ") == 2

    def test_control_and_combining_zero_width(self) -> None:
        assert adt.cell_width("\x00\x1b") == 0
        assert adt.cell_width("e\u0301") == 1  # 组合重音不占格

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
            adt.assert_screen_row_widths_fit(self._cap("中文", 3))

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
        # 空行是 ""，split 后保留 → 行数仍等于 height
        cap = adt.ScreenCapture(screen_data="a\n\nb", screen_height=3)
        adt.assert_screen_rows_match_height(cap)

    def test_fail_height_mismatch(self) -> None:
        cap = adt.ScreenCapture(screen_data="a\nb", screen_height=3)
        with pytest.raises(AssertionError, match="rows 2 != screen_height 3"):
            adt.assert_screen_rows_match_height(cap)

    def test_fail_screen_data_trailing_newline_adds_row(self) -> None:
        # 尾随 \n 会 split 出空行 → 行数 +1（这正是 ADT "无尾随空行" 的约束动机）
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
# G6：TerminalTextSegment / Color / Rgb / Style
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
        assert seg.styles == frozenset({"dim", "bold"})  # 顺序无关

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
        # 真类型可直接放入 ScreenCapture.terminal_text_content
        cap = adt.ScreenCapture(
            screen_data="ab",
            terminal_text_content=[adt.TerminalTextSegment(text="ab")],
            screen_width=2,
            screen_height=1,
        )
        adt.assert_screen_plain_matches(cap)
        adt.assert_segments_valid(cap)





