"""Parse a tmux ``capture-pane -e`` frame into ScreenCapture segments.

Mirrors the Meeting ADT:

- ``TerminalTextSegment``: text + optional fg/bg color + styles set
- ``plain_of(segments) == screen_data``: the plain projection is the row
  right-trimmed text of the concatenated segments.
- Adjacent segments with identical attributes are merged (canonical form).
"""
from __future__ import annotations

from typing import Any, Dict, List

STYLE_CODES = {1: "bold", 2: "dim", 3: "italic", 4: "underline", 5: "blink", 7: "reverse"}
RESET_STYLES = {22: {"bold", "dim"}, 23: {"italic"}, 24: {"underline"}, 25: {"blink"}, 27: {"reverse"}}


def _apply_style(attributes: Dict[str, Any], number: int) -> None:
    if number == 0:
        attributes["fg"] = None
        attributes["bg"] = None
        attributes["styles"] = set()
    elif number == 39:
        attributes["fg"] = None
    elif number == 49:
        attributes["bg"] = None
    elif 30 <= number <= 37:
        attributes["fg"] = {"palette": number - 30}
    elif 90 <= number <= 97:
        attributes["fg"] = {"palette": 8 + number - 90}
    elif 40 <= number <= 47:
        attributes["bg"] = {"palette": number - 40}
    elif 100 <= number <= 107:
        attributes["bg"] = {"palette": 8 + number - 100}
    elif number in STYLE_CODES:
        attributes["styles"].add(STYLE_CODES[number])
    elif number in RESET_STYLES:
        attributes["styles"].difference_update(RESET_STYLES[number])


def _extended_color(codes: List[str], offset: int) -> Dict[str, Any]:
    """Resolve the 38/48 sub-parameters starting at ``offset``."""
    palette256 = offset + 1 < len(codes) and codes[offset + 1] == "5" and offset + 2 < len(codes)
    if palette256:
        try:
            index = int(codes[offset + 2])
        except ValueError:
            index = -1
        if 0 <= index <= 255:
            return {"palette": index}
        return {}
    truecolor = offset + 1 < len(codes) and codes[offset + 1] == "2" and offset + 3 < len(codes)
    if truecolor:
        try:
            red, green, blue = (int(value) for value in codes[offset + 2 : offset + 5])
        except ValueError:
            return {}
        if all(0 <= value <= 255 for value in (red, green, blue)):
            return {"truecolor": {"r": red, "g": green, "b": blue}}
    return {}


def _apply_sgr_codes(attributes: Dict[str, Any], codes: List[str]) -> None:
    offset = 0
    while offset < len(codes):
        code = codes[offset]
        if code in {"38", "48"}:
            target = "fg" if code == "38" else "bg"
            color = _extended_color(codes, offset)
            attributes[target] = color or None
            if color:
                # 5;n or 2;r;g;b consumes the following codes.
                offset += 1
                if offset < len(codes) and codes[offset] in {"5", "2"}:
                    offset += 1 if codes[offset] == "5" else 3
            offset += 1
            continue
        try:
            _apply_style(attributes, int(code))
        except ValueError:
            pass
        offset += 1


def parse_segments(frame: str) -> List[Dict[str, Any]]:
    """Convert an ANSI frame into a canonical TerminalTextSegment list."""
    segments: List[Dict[str, Any]] = []
    buffer: List[str] = []
    attributes: Dict[str, Any] = {"fg": None, "bg": None, "styles": set()}
    index = 0
    length = len(frame)

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        fg = attributes["fg"]
        bg = attributes["bg"]
        styles = frozenset(attributes["styles"])
        text = "".join(buffer)
        buffer = []
        if segments and segments[-1]["fg"] == fg and segments[-1]["bg"] == bg and segments[-1]["styles"] == styles:
            segments[-1]["text"] += text
        else:
            segments.append({"text": text, "fg": fg, "bg": bg, "styles": sorted(styles)})

    while index < length:
        character = frame[index]
        if character != "\x1b":
            buffer.append(character)
            index += 1
            continue
        flush()
        if index + 1 >= length:
            break
        introducer = frame[index + 1]
        if introducer == "[":
            cursor = index + 2
            parameters: List[str] = []
            has_intermediate = False
            final_byte = ""
            while cursor < length:
                token = frame[cursor]
                if token.isdigit() or token in {";", ":", "?"}:
                    parameters.append(token)
                elif " " <= token <= "/":
                    has_intermediate = True
                    parameters.append(token)
                elif 0x40 <= ord(token) <= 0x7E:
                    final_byte = token
                    cursor += 1
                    break
                else:
                    break
                cursor += 1
            if final_byte == "m" and not has_intermediate:
                codes = "".join(parameters).split(";")
                if codes == [""]:
                    codes = ["0"]
                _apply_sgr_codes(attributes, codes)
            index = cursor
            continue
        if introducer == "]":
            cursor = index + 2
            while cursor < length:
                if frame[cursor] == "\x07":
                    cursor += 1
                    break
                if frame[cursor] == "\x1b" and cursor + 1 < length and frame[cursor + 1] == "\\":
                    cursor += 2
                    break
                cursor += 1
            index = cursor
            continue
        if introducer in "PX^_":
            cursor = index + 2
            while cursor < length:
                if frame[cursor] == "\x1b" and cursor + 1 < length and frame[cursor + 1] == "\\":
                    cursor += 2
                    break
                cursor += 1
            index = cursor
            continue
        # Two-character escapes: charset selection, keypad, single-char codes.
        index += 2
    flush()
    # Second pass: an interleaved control sequence (e.g. an OSC link or a
    # no-op SGR) may have split text of identical attributes; merge again so
    # the canonical form (adjacent segments differ) always holds.
    merged: List[Dict[str, Any]] = []
    for segment in segments:
        if (
            merged
            and merged[-1]["fg"] == segment["fg"]
            and merged[-1]["bg"] == segment["bg"]
            and merged[-1]["styles"] == segment["styles"]
        ):
            merged[-1]["text"] += segment["text"]
        else:
            merged.append(segment)
    return merged


def plain_of(segments: List[Dict[str, Any]]) -> str:
    """ADT ``plain_of``: concatenated text with rows right-trimmed."""
    text = "".join(segment["text"] for segment in segments)
    return "\n".join(row.rstrip(" ") for row in text.split("\n"))


_STYLE_TO_CODE = {name: code for code, name in STYLE_CODES.items()}


def _color_codes(color: Optional[Dict[str, Any]], foreground: bool) -> List[str]:
    if color is None:
        return []
    palette = color.get("palette") if isinstance(color, dict) else None
    if isinstance(palette, int) and 0 <= palette <= 255:
        if palette < 8:
            return [str(30 + palette if foreground else 40 + palette)]
        if palette < 16:
            return [str(90 + palette - 8 if foreground else 100 + palette - 8)]
        return [("38" if foreground else "48"), "5", str(palette)]
    truecolor = color.get("truecolor") if isinstance(color, dict) else None
    if isinstance(truecolor, dict):
        red, green, blue = (truecolor.get("r"), truecolor.get("g"), truecolor.get("b"))
        if all(isinstance(value, int) and 0 <= value <= 255 for value in (red, green, blue)):
            return [
                ("38" if foreground else "48"),
                "2",
                str(red),
                str(green),
                str(blue),
            ]
    return []


def _segment_sgr(segment: Dict[str, Any]) -> str:
    codes: List[str] = []
    for name in ("bold", "dim", "italic", "underline", "blink", "reverse"):
        if name in segment.get("styles", []):
            codes.append(str(_STYLE_TO_CODE[name]))
    codes.extend(_color_codes(segment.get("fg"), foreground=True))
    codes.extend(_color_codes(segment.get("bg"), foreground=False))
    if not codes:
        return ""
    return "\x1b[" + ";".join(codes) + "m"


def encode_segments(segments: List[Dict[str, Any]]) -> str:
    """Encode canonical segments back into SGR/ANSI text.

    Every segment starts with a full reset so the encoding is
    self-contained (each run is independent of previous state), which makes
    it safe to write into a terminal emulator after a clear and round-trips
    back to the same canonical segment list.
    """
    encoded: List[str] = []
    for segment in segments:
        encoded.append("\x1b[0m")
        sgr = _segment_sgr(segment)
        if sgr:
            encoded.append(sgr)
        encoded.append(segment.get("text", ""))
    return "".join(encoded)
