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


# 测试内最小的带 .text 段对象（TerminalTextSegment 在 G6 才定义）
class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


def _seg(text: str) -> _Seg:
    return _Seg(text)


