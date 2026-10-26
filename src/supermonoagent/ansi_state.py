"""Bounded rendition, cursor visibility and input modes for cropped history."""

from __future__ import annotations


class SgrState:
    MAX_CSI = 256

    def __init__(self) -> None:
        self.attributes: dict[str, str] = {}
        self.mode = "text"
        self.csi = bytearray()
        self.cursor_visible: bool | None = None
        self.application_cursor = False
        self.bracketed_paste = False

    def copy(self) -> SgrState:
        other = SgrState()
        other.attributes = self.attributes.copy()
        other.mode = self.mode
        other.csi = self.csi.copy()
        other.cursor_visible = self.cursor_visible
        other.application_cursor = self.application_cursor
        other.bracketed_paste = self.bracketed_paste
        return other

    def feed(self, data: bytes) -> None:
        position = 0
        while position < len(data):
            if self.mode == "text":
                position = data.find(b"\x1b", position)
                if position < 0:
                    return
                self.mode = "escape"
                position += 1
                continue
            byte = data[position]
            position += 1
            if byte in (0x18, 0x1a):
                self.mode = "text"
                self.csi.clear()
            elif self.mode == "escape":
                if byte == ord("["):
                    self.mode = "csi"
                    self.csi.clear()
                elif byte == ord("]"):
                    self.mode = "osc"
                elif byte in b"PX^_":
                    self.mode = "string"
                elif byte == ord("c"):
                    self._replay_reset(False)
                    self.attributes.clear()
                    self.application_cursor = self.bracketed_paste = False
                    # Bundled xterm's RIS preserves core cursor visibility.
                    # DECSTR below explicitly restores the visible default.
                    self.mode = "text"
                elif 0x20 <= byte <= 0x2f:
                    self.mode = "escape_intermediate"
                else:
                    self.mode = "escape" if byte == 0x1b else "text"
            elif self.mode == "escape_intermediate":
                if byte == 0x1b:
                    self.mode = "escape"
                elif 0x30 <= byte <= 0x7e:
                    self.mode = "text"
            elif self.mode in {"osc", "osc_escape", "string", "string_escape"}:
                osc = self.mode.startswith("osc")
                if byte == 7 and osc or byte == ord("\\") and self.mode.endswith("_escape"):
                    self.mode = "text"
                elif byte == 0x1b:
                    self.mode = "osc_escape" if osc else "string_escape"
                else:
                    self.mode = "osc" if osc else "string"
            elif self.mode in {"csi", "csi_ignore"}:
                if byte == 0x1b:
                    self.mode = "escape"
                    self.csi.clear()
                elif 0x40 <= byte <= 0x7e:
                    if self.mode == "csi":
                        self._replay_csi(byte, bytes(self.csi))
                        if byte == ord("m"):
                            self._sgr(bytes(self.csi))
                        elif byte == ord("p") and self.csi == b"!":
                            self.attributes.clear()  # DECSTR soft reset.
                            self.cursor_visible = True
                            self.application_cursor = self.bracketed_paste = False
                        elif byte in (ord("h"), ord("l")) and self.csi.startswith(b"?"):
                            modes = bytes(self.csi[1:]).split(b";")[:32]  # Bundled xterm parser limit.
                            if all(not mode or mode.isdigit() for mode in modes):
                                enabled = byte == ord("h")
                                for mode in modes:
                                    code = int(mode or b"0")
                                    if code == 25:
                                        self.cursor_visible = enabled
                                    elif code == 1:
                                        self.application_cursor = enabled
                                    elif code == 2004:
                                        self.bracketed_paste = enabled
                    self.mode = "text"
                    self.csi.clear()
                elif self.mode == "csi" and 0x20 <= byte <= 0x3f:
                    if len(self.csi) < self.MAX_CSI:
                        self.csi.append(byte)
                    else:
                        self.csi.clear()
                        self.mode = "csi_ignore"

    def _replay_csi(self, final: int, raw: bytes) -> None:
        """Optional context hook; legacy rendition output stays unchanged."""

    def _replay_reset(self, soft: bool) -> None:
        """Optional hook for replay state beyond rendition."""

    def _sgr(self, raw: bytes) -> None:
        if any(byte not in b"0123456789;:" for byte in raw):
            return
        parameters = raw.decode("ascii").split(";")
        index = 0
        while index < len(parameters):
            item = parameters[index]
            components = item.split(":")
            code = int(components[0] or "0")
            index += 1
            if code in {38, 48, 58}:
                if len(components) > 1:
                    color = components[1:]
                    if color[0] == "2" and len(color) == 5 and color[1] in {"", "0"}:
                        color = [color[0], *color[2:]]
                else:
                    if index >= len(parameters):
                        break
                    mode = parameters[index]
                    count = 2 if mode == "5" else 4 if mode == "2" else 1
                    color = parameters[index:index + count]
                    index += count
                if ((len(color) == 2 and color[0] == "5") or (len(color) == 4 and color[0] == "2")):
                    if all(channel.isdigit() and 0 <= int(channel) <= 255 for channel in color[1:]):
                        self.attributes[{38: "fg", 48: "bg", 58: "underline_color"}[code]] = ";".join([str(code), *(str(int(value)) for value in color)])
                continue
            if code == 0:
                self.attributes.clear()
            elif code in {1, 2, 3, 7, 8, 9, 53}:
                self.attributes[str(code)] = str(code)
            elif code == 4:
                if len(components) == 1 or len(components) == 2 and components[1] in {"0", "1", "2", "3", "4", "5"}:
                    if components[-1] == "0":
                        self.attributes.pop("underline", None)
                    else:
                        self.attributes["underline"] = item
            elif code == 21:
                self.attributes["underline"] = "4:2"
            elif code in {5, 6}:
                self.attributes["blink"] = str(code)
            elif code in {10, 11, 12, 13, 14, 15, 16, 17, 18, 19}:
                if code == 10:
                    self.attributes.pop("font", None)
                else:
                    self.attributes["font"] = str(code)
            elif code in {22, 23, 24, 25, 27, 28, 29, 54, 55, 59, 65, 75}:
                groups = {22: ("1", "2"), 23: ("3",), 24: ("underline",), 25: ("blink",),
                          27: ("7",), 28: ("8",), 29: ("9",), 54: ("frame",), 55: ("53",),
                          59: ("underline_color",), 65: ("ideogram",), 75: ("script",)}
                for group in groups[code]:
                    self.attributes.pop(group, None)
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.attributes["fg"] = str(code)
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self.attributes["bg"] = str(code)
            elif code in {39, 49}:
                self.attributes.pop("fg" if code == 39 else "bg", None)
            elif code in {51, 52}:
                self.attributes["frame"] = str(code)
            elif 60 <= code <= 64:
                self.attributes["ideogram"] = str(code)
            elif code in {73, 74}:
                self.attributes["script"] = str(code)

    def prefix(self) -> bytes:
        rendition = ("\x1b[" + ";".join(["0", *(self.attributes[key] for key in sorted(self.attributes))]) + "m").encode("ascii")
        # Self-drawn TUI inputs often leave the hardware cursor on a blank row.
        # Restore DECTCEM if its command aged out; never guess a prompt position.
        visibility = b"" if self.cursor_visible is None else (b"\x1b[?25h" if self.cursor_visible else b"\x1b[?25l")
        input_modes = (b"\x1b[?1h" if self.application_cursor else b"") + (b"\x1b[?2004h" if self.bracketed_paste else b"")
        return rendition + visibility + input_modes


def bounded_snapshot(data: bytes, limit: int, initial: SgrState | None = None, *, cropped: bool = False) -> bytes:
    """Keep a safe tail with its rendition, even if the cut crosses CSI/OSC/UTF-8."""
    if len(data) <= limit and not cropped:
        return data
    state = initial.copy() if initial is not None else SgrState()
    cut = max(0, len(data) - limit)
    state.feed(data[:cut])
    while True:
        while cut < len(data) and state.mode != "text":
            state.feed(data[cut:cut + 1])
            cut += 1
        while cut < len(data) and data[cut] & 0xc0 == 0x80:
            cut += 1
        prefix = state.prefix()
        extra = len(prefix) + len(data) - cut - limit
        if extra <= 0:
            return prefix + data[cut:]
        if cut == len(data):
            return b""  # A tiny frame cannot hold even the rendition itself.
        end = min(len(data), cut + extra)
        state.feed(data[cut:end])
        cut = end
