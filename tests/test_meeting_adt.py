"""meeting_adt 测试：随目标链 G1–G14 每颗一个分区，分区内覆盖边界与失败路径。"""

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
        # 模块暂时只导出流名常量：防止意外把内部名当公共 API
        assert set(adt.__all__) == {
            "SCREEN_STREAM_NAME",
            "CHAT_STREAM_NAME",
            "SCORE_STREAM_NAME",
            "KEYSTROKE_STREAM_NAME",
            "TERMINAL_CONTROL_STREAM_NAME",
            "STREAM_NAMES",
        }
