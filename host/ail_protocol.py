# -*- coding: utf-8 -*-
"""AI Status Lights 串口协议 (纯逻辑, 不依赖 pyserial / GUI).

帧格式 (与固件 User/protocol.h 完全一致):

    55 AA | MODE | PAYLOAD... | CHECKSUM | BB

CHECKSUM = (从帧头 0x55 起, 到 payload 最后一个字节为止的所有字节之和) & 0xFF

    MODE 0x00 VERIFY  无 payload               整帧 5 字节  -> 回固定串
    MODE 0x01 LEDS    21 字节 R0G0B0..R6G6B6   整帧 26 字节 -> 立即刷灯
    MODE 0x02 BUZZER  5 字节 频率(2,小端) 音量(1) 时长(2,小端)
                                               整帧 10 字节 -> 蜂鸣

上位机只做两件事: 按这个格式打包, 以及把目标板回读的字节按这个格式解开.
"""

from __future__ import annotations

from typing import Iterable, Iterator, List, Sequence, Tuple

HEAD1 = 0x55
HEAD2 = 0xAA
TAIL = 0xBB

MODE_VERIFY = 0x00
MODE_LEDS = 0x01
MODE_BUZZER = 0x02

LED_NUM = 7
LED_PAYLOAD_LEN = LED_NUM * 3          # 21
BUZZER_PAYLOAD_LEN = 5

# 固件对 MODE 0x00 的固定应答
VERIFY_REPLY = b"AI Status Lights OK\r\n"

# 固件解析半截帧的超时(ms), 上位机解析回包时用同样的思路丢弃残帧
FRAME_TIMEOUT_MS = 20

MODE_NAMES = {
    MODE_VERIFY: "VERIFY",
    MODE_LEDS: "LEDS",
    MODE_BUZZER: "BUZZER",
}

# 各模式的 payload 长度; 未知模式返回 None
_PAYLOAD_LEN = {
    MODE_VERIFY: 0,
    MODE_LEDS: LED_PAYLOAD_LEN,
    MODE_BUZZER: BUZZER_PAYLOAD_LEN,
}


class ProtocolError(ValueError):
    """打包参数不合法时抛出."""


# --------------------------------------------------------------------------
# 打包
# --------------------------------------------------------------------------

def checksum(data: bytes) -> int:
    """从 0x55 起累加取低 8 位."""
    return sum(data) & 0xFF


def build_frame(mode: int, payload: bytes = b"") -> bytes:
    """按协议拼一整帧."""
    if not 0 <= int(mode) <= 0xFF:
        raise ProtocolError("mode 必须在 0x00..0xFF 之间: %r" % (mode,))
    body = bytes([HEAD1, HEAD2, int(mode)]) + bytes(payload)
    return body + bytes([checksum(body), TAIL])


def build_verify() -> bytes:
    """MODE 0x00: 验证帧, 固定 55 AA 00 FF BB."""
    return build_frame(MODE_VERIFY)


def _clamp(value: int, low: int, high: int) -> int:
    return low if value < low else (high if value > high else value)


def build_leds(colors: Sequence[Sequence[int]]) -> bytes:
    """MODE 0x01: colors = 7 个 (R, G, B), 顺序为 LED1..LED7(离 MCU 最近的是第 1 颗)."""
    if len(colors) != LED_NUM:
        raise ProtocolError("需要 %d 个颜色, 收到 %d 个" % (LED_NUM, len(colors)))

    payload = bytearray()
    for i, rgb in enumerate(colors):
        if len(rgb) != 3:
            raise ProtocolError("第 %d 个颜色不是 (R,G,B): %r" % (i + 1, rgb))
        for comp in rgb:
            value = int(comp)
            if not 0 <= value <= 0xFF:
                raise ProtocolError("第 %d 个颜色的分量超出 0..255: %r" % (i + 1, rgb))
            payload.append(value)
    return build_frame(MODE_LEDS, bytes(payload))


def build_leds_all(color: Sequence[int]) -> bytes:
    """7 颗灯同一个颜色."""
    return build_leds([tuple(int(c) for c in color)] * LED_NUM)


def build_buzzer(freq_hz: int, volume: int, duration_ms: int) -> bytes:
    """MODE 0x02: 频率 20..20000Hz(自动夹紧), 音量 0..100, 时长 0..65535ms."""
    freq = _clamp(int(freq_hz), 0, 0xFFFF)
    vol = _clamp(int(volume), 0, 100)
    dur = _clamp(int(duration_ms), 0, 0xFFFF)
    payload = bytes([freq & 0xFF, (freq >> 8) & 0xFF, vol, dur & 0xFF, (dur >> 8) & 0xFF])
    return build_frame(MODE_BUZZER, payload)


# --------------------------------------------------------------------------
# 解包 (解析目标板回读的字节流)
# --------------------------------------------------------------------------

def parse_led_payload(payload: bytes) -> List[Tuple[int, int, int]]:
    if len(payload) != LED_PAYLOAD_LEN:
        raise ProtocolError("LED payload 长度应为 %d, 实际 %d" % (LED_PAYLOAD_LEN, len(payload)))
    return [(payload[i * 3], payload[i * 3 + 1], payload[i * 3 + 2]) for i in range(LED_NUM)]


def parse_buzzer_payload(payload: bytes) -> Tuple[int, int, int]:
    if len(payload) != BUZZER_PAYLOAD_LEN:
        raise ProtocolError("蜂鸣器 payload 长度应为 %d, 实际 %d" % (BUZZER_PAYLOAD_LEN, len(payload)))
    freq = payload[0] | (payload[1] << 8)
    volume = payload[2]
    duration = payload[3] | (payload[4] << 8)
    return freq, volume, duration


def decode_frame(frame: bytes) -> Tuple[int, bytes]:
    """校验一整帧, 返回 (mode, payload); 不合格抛 ProtocolError."""
    if len(frame) < 5:
        raise ProtocolError("帧太短: %d 字节" % len(frame))
    if frame[0] != HEAD1 or frame[1] != HEAD2:
        raise ProtocolError("帧头不对: %s" % frame[:2].hex(" ").upper())
    if frame[-1] != TAIL:
        raise ProtocolError("帧尾不是 0x%02X: 0x%02X" % (TAIL, frame[-1]))
    body, chk = frame[:-2], frame[-2]
    want = checksum(body)
    if chk != want:
        raise ProtocolError("校验错: 收到 0x%02X, 应为 0x%02X" % (chk, want))
    mode = frame[2]
    return mode, bytes(frame[3:-2])


class FrameParser:
    """按字节喂入, 吐出完整帧; 状态机与固件 Protocol_Task() 一致.

    用法::

        p = FrameParser()
        for mode, payload in p.feed(ser.read(64)):
            ...

    校验错/帧尾错/未知模式都会丢弃并重新找帧头; `tick(ms)` 用于丢弃半截帧
    (对应固件的 20ms 看门狗).
    """

    ST_HEAD1, ST_HEAD2, ST_MODE, ST_PAYLOAD, ST_CHECKSUM, ST_TAIL = range(6)

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._state = self.ST_HEAD1
        self._mode = 0
        self._payload = bytearray()
        self._expected = 0
        self._checksum = 0
        self._idle_ms = 0
        self.bad_frames = 0          # 校验/帧尾错误计数
        self.unknown_modes = 0       # 未知模式计数
        self.dropped_partial = 0     # 超时丢弃的半截帧计数

    # -- 兼容旧名 ----------------------------------------------------------
    @property
    def state(self) -> int:
        return self._state

    @property
    def bad_checksums(self) -> int:
        return self.bad_frames

    def feed(self, data: bytes) -> Iterator[Tuple[int, bytes]]:
        for byte in data:
            frame = self._feed_byte(byte)
            if frame is not None:
                yield frame

    def _feed_byte(self, byte: int):
        st = self._state

        if st == self.ST_HEAD1:
            if byte == HEAD1:
                self._checksum = byte
                self._state = self.ST_HEAD2

        elif st == self.ST_HEAD2:
            if byte == HEAD2:
                self._checksum = (self._checksum + byte) & 0xFF
                self._state = self.ST_MODE
            elif byte == HEAD1:
                # 55 55 -> 后一个 0x55 当新的帧头
                self._checksum = byte
            else:
                self._state = self.ST_HEAD1

        elif st == self.ST_MODE:
            self._mode = byte
            self._checksum = (self._checksum + byte) & 0xFF
            self._payload = bytearray()
            self._expected = _PAYLOAD_LEN.get(byte, -1)
            if self._expected < 0:
                self.unknown_modes += 1
                self._state = self.ST_HEAD1
            elif self._expected == 0:
                self._state = self.ST_CHECKSUM
            else:
                self._state = self.ST_PAYLOAD

        elif st == self.ST_PAYLOAD:
            self._payload.append(byte)
            self._checksum = (self._checksum + byte) & 0xFF
            if len(self._payload) >= self._expected:
                self._state = self.ST_CHECKSUM

        elif st == self.ST_CHECKSUM:
            if byte == self._checksum:
                self._state = self.ST_TAIL
            else:
                self.bad_frames += 1
                self._state = self.ST_HEAD1

        elif st == self.ST_TAIL:
            self._state = self.ST_HEAD1
            if byte == TAIL:
                return self._mode, bytes(self._payload)

        else:                                    # 理论到不了
            self._state = self.ST_HEAD1

        return None

    def tick(self, elapsed_ms: int = 1) -> int:
        """时间推进, 返回被丢弃的半截帧数量."""
        dropped = 0
        if self._state != self.ST_HEAD1:
            self._idle_ms += elapsed_ms
            while self._idle_ms >= FRAME_TIMEOUT_MS:
                self._idle_ms -= FRAME_TIMEOUT_MS
                dropped += 1
                self.dropped_partial += 1
                self._state = self.ST_HEAD1
                self._idle_ms = 0
        else:
            self._idle_ms = 0
        return dropped


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def hex_str(data: bytes, upper: bool = True) -> str:
    """55 AA 00 FF BB 风格的空格分隔十六进制."""
    text = data.hex(" ").upper() if upper else data.hex(" ")
    return text


def describe_frame(mode: int, payload: bytes) -> str:
    """把一帧翻译成人话, 用于日志."""
    name = MODE_NAMES.get(mode, "MODE_0x%02X" % mode)
    if mode == MODE_VERIFY:
        return "%s (无 payload)" % name
    if mode == MODE_LEDS:
        return "%s %s" % (name, " ".join("#%02X%02X%02X" % c for c in parse_led_payload(payload)))
    if mode == MODE_BUZZER:
        freq, vol, dur = parse_buzzer_payload(payload)
        return "%s %dHz 音量%d %dms" % (name, freq, vol, dur)
    return "%s payload=%s" % (name, hex_str(payload))


def parse_color(text: str) -> Tuple[int, int, int]:
    """接受 '#RRGGBB' / 'RRGGBB' / 'R,G,B' 三种写法."""
    raw = text.strip().lstrip("#")
    if "," in raw:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            raise ProtocolError("颜色应为 R,G,B: %r" % text)
        return tuple(_clamp(int(p), 0, 255) for p in parts)  # type: ignore[return-value]
    if len(raw) != 6:
        raise ProtocolError("颜色应为 RRGGBB: %r" % text)
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def color_hex(color: Iterable[int]) -> str:
    r, g, b = color
    return "#%02X%02X%02X" % (r, g, b)
