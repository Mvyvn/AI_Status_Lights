# -*- coding: utf-8 -*-
"""上位机离线自测: 用假串口/假设备跑真实源码, 不需要板子.

    python test_host_tools.py

覆盖:
  1. 三种帧的字节与固件 README 里列出的示例完全一致
  2. 校验和/帧头/帧尾错误被丢弃, 并能重新同步
  3. CH340 识别(描述关键字 / VID:PID / 非 CH340)
  4. 自动查找: 第一个 CH340 装死不回应, 第二个回应 -> 选中第二个
  5. 自动查找: CH340 全都没反应 -> 回退扫描其它串口
  6. 自动查找: 只有非协议响应 -> 作为候选返回并标记
  7. 探测时冲掉开机残留数据、重试逻辑、应答不是本协议时的判定
"""

from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ail_protocol as proto
import ail_link
import ail_rainbow
from ail_link import PortInfo

PASS = 0
FAIL = 0
FAILURES = []


def check(cond: bool, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(label)
        print("  FAIL  %s" % label)


def check_eq(got, want, label: str) -> None:
    check(got == want, "%s   got=%r want=%r" % (label, got, want))


# ==========================================================================
# 假串口 / 假设备
# ==========================================================================

class VirtualBoard:
    """模拟固件的串口行为: 收帧 -> 回验证应答 / 记录灯效蜂鸣."""

    def __init__(self, alive: bool = True, reply: bytes = proto.VERIFY_REPLY,
                 boot_junk: bytes = b"", garbage: bytes = b""):
        self.alive = alive
        self.reply = reply
        self.garbage = garbage          # 不是本协议的响应(用于"有响应但协议不符")
        self.rx = bytearray()
        self.tx = bytearray()
        self.parser = proto.FrameParser()
        self.opened = 0
        self.closed = 0
        self.written = 0
        self.dtr_pulses = 0
        self.leds = None
        self.beeps = []
        if boot_junk:
            self.tx += boot_junk

    # -- 串口接口 ----------------------------------------------------------
    def write(self, data: bytes) -> int:
        self.written += 1
        self.rx += bytes(data)
        if not self.alive:
            return len(data)
        for mode, payload in self.parser.feed(bytes(data)):
            if mode == proto.MODE_VERIFY:
                self.tx += self.reply if self.reply else self.garbage
            elif mode == proto.MODE_LEDS:
                self.leds = proto.parse_led_payload(payload)
            elif mode == proto.MODE_BUZZER:
                self.beeps.append(proto.parse_buzzer_payload(payload))
        return len(data)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self.tx[:size])
        del self.tx[:len(chunk)]
        return chunk

    def reset_input_buffer(self) -> None:
        pass                                # 设备侧没有需要清的东西

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1

    @property
    def is_open(self) -> bool:
        return self.closed == 0


class FakePort:
    """给 list_ports 用的假端口信息."""

    def __init__(self, device, description="", hwid="", manufacturer="", product=""):
        self.device = device
        self.description = description
        self.hwid = hwid
        self.manufacturer = manufacturer
        self.product = product


class FakeOpener:
    """把 device 名字映射到 VirtualBoard 的串口工厂."""

    def __init__(self, boards: dict):
        self.boards = boards
        self.log = []

    def __call__(self, port, baud, timeout, reset_pulse=False, **kwargs):
        board = self.boards[port]
        board.opened += 1
        if reset_pulse:
            board.dtr_pulses += 1
        return board


def silent_log(_msg: str) -> None:
    pass


# ==========================================================================
# 1. 帧字节
# ==========================================================================

def test_frames() -> None:
    print("[1] 帧字节")
    check_eq(proto.hex_str(proto.build_verify()), "55 AA 00 FF BB", "验证帧")

    frame = proto.build_leds_all((255, 0, 0))
    check_eq(proto.hex_str(frame), " ".join(["55 AA 01"] + ["FF 00 00"] * 7 + ["F9 BB"]), "7 灯全红")
    check_eq(len(frame), 26, "灯效帧长 26")

    # 固件开机自检那组彩虹色, 与 README 示例逐字节一致
    rainbow = [
        (0xFF, 0x00, 0x00), (0xFF, 0x60, 0x00), (0xFF, 0xFF, 0x00),
        (0x00, 0xFF, 0x00), (0x00, 0xFF, 0xFF), (0x00, 0x00, 0xFF),
        (0xA0, 0x00, 0xFF),
    ]
    rainbow_frame = proto.hex_str(proto.build_leds(rainbow))
    check_eq(
        rainbow_frame,
        "55 AA 01 FF 00 00 FF 60 00 FF FF 00 00 FF 00 00 FF FF 00 00 FF A0 00 FF F7 BB",
        "彩虹帧 (README 示例)",
    )
    # GUI 里的"彩虹"按钮必须产出同一帧
    import ai_status_lights_gui as gui
    check_eq(proto.hex_str(proto.build_leds(gui.RAINBOW)), rainbow_frame, "GUI 彩虹 = README 彩虹帧")

    check_eq(proto.hex_str(proto.build_buzzer(2000, 100, 500)), "55 AA 02 D0 07 64 F4 01 31 BB", "蜂鸣 2kHz/100/500ms")
    check_eq(proto.hex_str(proto.build_buzzer(1000, 80, 1000)), "55 AA 02 E8 03 50 E8 03 27 BB", "蜂鸣 1kHz/80/1000ms")
    check_eq(len(proto.build_buzzer(2000, 100, 500)), 10, "蜂鸣帧长 10")

    check_eq(proto.checksum(bytes([0x55, 0xAA, 0x00])), 0xFF, "校验和 = FF")

    # 夹紧
    check_eq(proto.build_buzzer(70000, 200, 70000)[3:8], bytes([0xFF, 0xFF, 100, 0xFF, 0xFF]), "频率/时长夹紧到 0xFFFF")
    check_eq(proto.parse_buzzer_payload(proto.build_buzzer(20, 0, 0)[3:-2]), (20, 0, 0), "音量 0 时长 0")
    check_eq(proto.parse_buzzer_payload(proto.build_buzzer(20000, 100, 65535)[3:-2]), (20000, 100, 65535), "边界值")

    # 参数错误要报错
    for bad, label in ((lambda: proto.build_leds([(0, 0, 0)]), "颜色个数不对"),
                       (lambda: proto.build_leds_all((0, 0, 300)), "分量超范围"),
                       (lambda: proto.parse_color("GG"), "颜色文本非法")):
        try:
            bad()
            check(False, label + " 应该抛异常")
        except proto.ProtocolError:
            check(True, label)

    check_eq(proto.parse_color("#12ab34"), (0x12, 0xAB, 0x34), "parse_color #RRGGBB")
    check_eq(proto.parse_color("1,2,3"), (1, 2, 3), "parse_color R,G,B")


# ==========================================================================
# 2. 解析器
# ==========================================================================

def test_parser() -> None:
    print("[2] 帧解析")
    p = proto.FrameParser()
    frames = list(p.feed(b"\x55\xAA\x00\xFF\xBB"))
    check_eq(frames, [(0x00, b"")], "解析验证帧")

    led_frame = proto.build_leds_all((1, 2, 3))
    p = proto.FrameParser()
    frames = list(p.feed(b"\x00\x11" + led_frame + b"\x22"))
    check_eq(len(frames), 1, "垃圾里夹一帧能解析出来")
    check_eq(proto.parse_led_payload(frames[0][1])[0], (1, 2, 3), "payload 正确")

    # 校验错
    bad = bytearray(proto.build_verify())
    bad[-2] ^= 0xFF
    p = proto.FrameParser()
    check_eq(list(p.feed(bytes(bad))), [], "校验错被丢弃")
    check_eq(p.bad_frames, 1, "错误计数 +1")

    # 帧尾错
    bad = bytearray(proto.build_verify())
    bad[-1] = 0x00
    p = proto.FrameParser()
    check_eq(list(p.feed(bytes(bad))), [], "帧尾错被丢弃")

    # 未知模式
    p = proto.FrameParser()
    check_eq(list(p.feed(proto.build_frame(0x7F, b""))), [], "未知模式被丢弃")
    check_eq(p.unknown_modes, 1, "未知模式计数")

    # 55 55 -> 第二个 55 当新帧头
    p = proto.FrameParser()
    check_eq(list(p.feed(b"\x55\x55\xAA\x00\xFF\xBB")), [(0x00, b"")], "55 55 重新同步")

    # 半截帧超时丢弃后还能收下一帧
    p = proto.FrameParser()
    check_eq(list(p.feed(b"\x55\xAA\x01\x11\x22")), [], "半截帧不吐")
    drained = p.tick(proto.FRAME_TIMEOUT_MS)
    check_eq(drained, 1, "超时丢弃半截帧")
    check_eq(list(p.feed(proto.build_verify())), [(0x00, b"")], "丢弃后能收新帧")

    # 校验和正确但模式是灯效, payload 不足 -> 不会误吐
    p = proto.FrameParser()
    check_eq(list(p.feed(b"\x55\xAA\x01\xFF")), [], "灯效帧不完整时不吐")


# ==========================================================================
# 3. 串口识别
# ==========================================================================

def test_port_identify() -> None:
    print("[3] CH340 识别")
    ports = [
        FakePort("COM1", "通讯端口 (COM1)", "ACPI\\PNP0501"),
        FakePort("COM5", "WCH-Link SERIAL (COM5)", "USB VID:PID=1A86:8010 SER=B8DF8F063E11", "wch.cn"),
        FakePort("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", "wch.cn"),
        FakePort("COM20", "USB Single Serial", "USB VID:PID=1A86:7523 LOCATION=1-2", ""),
        FakePort("COM30", "CP2102 USB to UART Bridge", "USB VID:PID=10C4:EA60", "Silicon Labs"),
    ]
    infos = ail_link.list_ports(lambda: ports)
    check_eq([p.device for p in infos], ["COM11", "COM20", "COM1", "COM5", "COM30"],
             "CH340 排前面, 同组自然排序(COM5 在 COM30 前)")
    ch = [p.device for p in ail_link.ch340_ports(infos)]
    check_eq(sorted(ch), ["COM11", "COM20"], "只留 CH340")
    check(not next(p for p in infos if p.device == "COM30").is_ch340, "CP2102 不算 CH340")
    check(next(p for p in infos if p.device == "COM5").is_ch340 is False, "WCH-Link 不算 CH340")


# ==========================================================================
# 4/5/6. 自动查找
# ==========================================================================

def test_probe_replies() -> None:
    print("[4] 单个串口探测")
    board = VirtualBoard()
    opener = FakeOpener({"COM11": board})
    res = ail_link.probe_port("COM11", retries=3, settle=0.0, opener=opener, log=silent_log)
    check(res.opened and res.responded and res.matched, "正常应答 -> matched")
    check_eq(res.reply, proto.VERIFY_REPLY, "回包内容")
    check_eq(board.written, 1, "一次就成功, 不重试")

    # 装死: 三次重试都发
    dead = VirtualBoard(alive=False)
    res = ail_link.probe_port("COM11", retries=3, settle=0.0, opener=FakeOpener({"COM11": dead}), log=silent_log)
    check(not res.responded and not res.matched, "装死 -> 未命中")
    check_eq(dead.written, 3, "重试 3 次")
    check_eq(dead.closed, 1, "结束后关闭串口")

    # 有响应但不是本协议
    other = VirtualBoard(reply=b"HELLO\r\n")
    res = ail_link.probe_port("COM11", retries=2, settle=0.0, opener=FakeOpener({"COM11": other}), log=silent_log)
    check(res.responded and not res.matched, "非协议响应 -> 有响应但不匹配")

    # 开机残留先冲掉, 残留里的伪应答不能算命中
    junk = VirtualBoard(boot_junk=proto.VERIFY_REPLY + b"\x00" * 10)
    junk.alive = False                      # 之后不再应答, 只有残留
    res = ail_link.probe_port("COM11", retries=1, settle=0.05, opener=FakeOpener({"COM11": junk}), log=silent_log)
    check(not res.matched, "开机残留的旧应答不算命中")
    check(any("旧数据" in n for n in res.notes), "记录丢弃的残留")

    # 打不开
    def boom(port, baud, timeout, **kw):
        raise OSError("拒绝访问")
    res = ail_link.probe_port("COM99", opener=boom, log=silent_log)
    check(not res.opened and res.error, "打不开时记录错误")

    # DTR/RTS 复位脉冲
    b = VirtualBoard()
    res = ail_link.probe_port("COM11", retries=1, settle=0.0, reset_pulse=True,
                              opener=FakeOpener({"COM11": b}), log=silent_log)
    check_eq(b.dtr_pulses, 1, "reset_pulse 传给了串口")

    # 不匹配时也把"有响应"的原始字节带回来
    o = VirtualBoard(reply=b"XY")
    res = ail_link.probe_port("COM11", retries=2, settle=0.0, opener=FakeOpener({"COM11": o}), log=silent_log)
    check_eq(res.reply, b"XY", "带回原始响应")


def find_with(boards_cfg, fallback=True):
    """boards_cfg: [(device, description, hwid, alive, reply)]"""
    fake_ports = []
    boards = {}
    for device, desc, hwid, alive, reply in boards_cfg:
        fake_ports.append(FakePort(device, desc, hwid))
        boards[device] = VirtualBoard(alive=alive, reply=reply)
    res = ail_link.find_device(
        retries=1, timeout=0.05, fallback_all=fallback,
        port_lister=lambda: fake_ports, opener=FakeOpener(boards), log=silent_log,
    )
    return res, boards


def test_find_device() -> None:
    print("[5] 自动查找")

    # CH340 与 WCH-Link 都在, 第一个 CH340 装死, 第二个应答
    res, _ = find_with([
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", False, proto.VERIFY_REPLY),
        ("COM12", "USB-SERIAL CH340 (COM12)", "USB VID:PID=1A86:7523", True, proto.VERIFY_REPLY),
        ("COM5", "WCH-Link SERIAL (COM5)", "USB VID:PID=1A86:8010", True, proto.VERIFY_REPLY),
    ])
    check_eq(res.port, "COM12", "跳过装死的 CH340, 命中第二个")
    check_eq([p.port for p in res.probes], ["COM11", "COM12"], "命中即停, 没去碰 WCH-Link")

    # 第一个 CH340 直接应答
    res, _ = find_with([
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", True, proto.VERIFY_REPLY),
        ("COM12", "USB-SERIAL CH340 (COM12)", "USB VID:PID=1A86:7523", True, proto.VERIFY_REPLY),
    ])
    check_eq(res.port, "COM11", "第一个就命中")
    check(res.best() is not None and res.best().matched, "best() 是命中的那个")

    # CH340 全装死 -> 回退到 WCH-Link 口
    res, _ = find_with([
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", False, proto.VERIFY_REPLY),
        ("COM5", "WCH-Link SERIAL (COM5)", "USB VID:PID=1A86:8010", True, proto.VERIFY_REPLY),
    ])
    check_eq(res.port, "COM5", "CH340 全无应答 -> 回退其它串口")

    # 关掉回退
    res, _ = find_with([
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", False, proto.VERIFY_REPLY),
        ("COM5", "WCH-Link SERIAL (COM5)", "USB VID:PID=1A86:8010", True, proto.VERIFY_REPLY),
    ], fallback=False)
    check(not res.found, "不回退时找不到就返回未找到")

    # 谁都不应答
    res, _ = find_with([
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", False, proto.VERIFY_REPLY),
        ("COM12", "USB-SERIAL CH340 (COM12)", "USB VID:PID=1A86:7523", False, proto.VERIFY_REPLY),
    ])
    check(not res.found, "都没应答 -> 未找到")
    check_eq(len(res.probes), 2, "两个都探过")

    # 只有非协议响应: 作为候选返回
    res, _ = find_with([
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", True, b"HELLO\r\n"),
        ("COM12", "USB-SERIAL CH340 (COM12)", "USB VID:PID=1A86:7523", False, proto.VERIFY_REPLY),
    ])
    check_eq(res.port, "COM11", "非协议响应仍作为候选返回(但不算 matched)")
    check(res.best() is None, "best() 为空表示内容不符")

    # 一个串口都没有
    res = ail_link.find_device(port_lister=lambda: [], log=silent_log)
    check(not res.found and "没有找到任何串口" in res.log[0], "无串口时明确报出")

    # 全部打不开 -> 提示端口被占用(真实机器上 COM11 被串口助手占住时就是这个)
    def boom(port, baud, timeout, **kw):
        raise PermissionError(13, "拒绝访问。")
    res = ail_link.find_device(
        port_lister=lambda: [FakePort("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523")],
        opener=boom, retries=1, timeout=0.05, log=silent_log)
    check(not res.found and "占用" in res.hint, "打不开时提示端口被占用")

    # 能打开但都不应答 -> 提示查接线/固件
    res, _ = find_with([
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", False, proto.VERIFY_REPLY),
    ])
    check(not res.found and "应答" in res.hint and "占用" not in res.hint, "无人应答时提示查接线/固件")

    # 真实机器上的混合情况: 一个口被占用打不开, 另一个能打开但不应答
    mixed_ports = [FakePort("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523"),
                   FakePort("COM12", "USB-SERIAL CH340 (COM12)", "USB VID:PID=1A86:7523")]
    mixed_boards = {"COM12": VirtualBoard(alive=False)}

    def mixed_opener(port, baud, timeout, **kw):
        if port == "COM11":
            raise PermissionError(13, "拒绝访问。")
        return mixed_boards[port]

    res = ail_link.find_device(port_lister=lambda: mixed_ports, opener=mixed_opener,
                               retries=1, timeout=0.05, log=silent_log)
    check(not res.found and "打不开" in res.hint and "COM11" in res.hint,
          "部分端口被占用时指名给出提示")

    # 只认 CH340 时不碰其它口 (prefer_ch340 且 CH340 命中)
    res, _ = find_with([
        ("COM30", "CP2102 USB to UART Bridge", "USB VID:PID=10C4:EA60", True, proto.VERIFY_REPLY),
        ("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", True, proto.VERIFY_REPLY),
    ])
    check_eq(res.port, "COM11", "优先 CH340, 即使 CP2102 排在前面")


# ==========================================================================
# 8. 端到端: 探测 + 发命令
# ==========================================================================

def test_end_to_end() -> None:
    print("[6] 端到端: 找到板子 -> 发灯效/蜂鸣")
    board = VirtualBoard()
    res, _ = find_with([("COM11", "USB-SERIAL CH340 (COM11)", "USB VID:PID=1A86:7523", True, proto.VERIFY_REPLY)])
    check_eq(res.port, "COM11", "找到板子")

    board.write(proto.build_leds([(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12), (13, 14, 15), (16, 17, 18), (19, 20, 21)]))
    check_eq(board.leds[0], (1, 2, 3), "第 1 颗灯")
    check_eq(board.leds[6], (19, 20, 21), "第 7 颗灯")

    board.write(proto.build_buzzer(2000, 100, 500))
    check_eq(board.beeps[-1], (2000, 100, 500), "蜂鸣参数")

    # 半截帧 + 超时后仍能正常执行下一帧 (对应上位机串口抖动)
    board.write(b"\x55\xAA\x02\xD0")
    board.parser.tick(proto.FRAME_TIMEOUT_MS)
    board.write(proto.build_buzzer(1000, 50, 100))
    check_eq(board.beeps[-1], (1000, 50, 100), "残帧不会吃掉后面的命令")


# ==========================================================================
# 7. 彩虹流水的颜色数学
# ==========================================================================

def test_rainbow_math() -> None:
    print("[7] 彩虹流水颜色数学")
    check_eq([ail_rainbow.brightness_value(b) for b in (0, 1, 50, 99, 100, 200)],
             [0, 3, 128, 252, 255, 255], "亮度 0..100 -> 0..255")

    # 六个纯色 + 首尾相接
    check_eq(ail_rainbow.hsv_to_rgb(0, 255), (255, 0, 0), "hue 0 -> 红")
    check_eq(ail_rainbow.hsv_to_rgb(60, 255), (255, 255, 0), "hue 60 -> 黄")
    check_eq(ail_rainbow.hsv_to_rgb(120, 255), (0, 255, 0), "hue 120 -> 绿")
    check_eq(ail_rainbow.hsv_to_rgb(180, 255), (0, 255, 255), "hue 180 -> 青")
    check_eq(ail_rainbow.hsv_to_rgb(240, 255), (0, 0, 255), "hue 240 -> 蓝")
    check_eq(ail_rainbow.hsv_to_rgb(300, 255), (255, 0, 255), "hue 300 -> 品红")
    check_eq(ail_rainbow.hsv_to_rgb(360, 255), ail_rainbow.hsv_to_rgb(0, 255), "hue 360 回到 0")
    check_eq(ail_rainbow.hsv_to_rgb(-60, 255), ail_rainbow.hsv_to_rgb(300, 255), "负色相回绕")
    check_eq(ail_rainbow.hsv_to_rgb(200, 0), (0, 0, 0), "亮度 0 = 全黑")
    check_eq(ail_rainbow.hsv_to_rgb(30, 128), (128, 63, 0), "亮度减半, 整数运算")
    # 中间值: hue 30 -> f=127, t=255*127/255=127, q=255*128/255=128
    check_eq(ail_rainbow.hsv_to_rgb(30, 255), (255, 127, 0), "hue 30 -> 橙")

    # 60° 间隔 = 经典彩虹
    colors = ail_rainbow.frame_colors(60, 255, 0)
    check_eq(colors[:6], [(255, 0, 0), (255, 255, 0), (0, 255, 0),
                          (0, 255, 255), (0, 0, 255), (255, 0, 255)], "60° 间隔彩虹")
    check_eq(len(colors), ail_rainbow.LED_NUM, "7 颗灯")

    # 基准色相推进一整圈要精确回到原样
    check_eq(ail_rainbow.frame_colors(40, 255, 0), ail_rainbow.frame_colors(40, 255, 360000), "色相走满 360°")
    check_eq(ail_rainbow.frame_colors(40, 255, -1000), ail_rainbow.frame_colors(40, 255, 359000), "负基准回绕")

    # 帧字节仍然是合法的 MODE 0x01 帧
    frame = ail_rainbow.frame_bytes(60, 255, 0)
    check_eq(len(frame), 26, "整帧 26 字节")
    mode, payload = proto.decode_frame(frame)
    check_eq(mode, proto.MODE_LEDS, "MODE 0x01")
    check_eq(proto.parse_led_payload(payload)[0], (255, 0, 0), "payload 第 1 颗灯")

    # 亮度 0 -> 全黑帧
    frame = ail_rainbow.frame_bytes(40, 0, 0)
    check_eq(proto.parse_led_payload(proto.decode_frame(frame)[1]),
             [(0, 0, 0)] * ail_rainbow.LED_NUM, "亮度 0 全黑")

    # 参数夹紧
    p = ail_rainbow.RainbowParams(999, 9999, 999).clamped()
    check_eq((p.step_deg, p.speed_deg_s, p.brightness), (180, 720, 100), "参数上限夹紧")
    p = ail_rainbow.RainbowParams(-5, -9999, -5).clamped()
    check_eq((p.step_deg, p.speed_deg_s, p.brightness), (0, -720, 0), "参数下限夹紧")


# ==========================================================================
# 8. 100 Hz 流式发送
# ==========================================================================

class FakeWriter:
    """假的发送回调, 记录每帧的时间戳和内容."""

    def __init__(self, fail_after=None):
        self.frames = []
        self.times = []
        self.fail_after = fail_after
        self.calls = 0

    def __call__(self, frame: bytes) -> None:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OSError("串口被拔了")
        self.frames.append(frame)
        self.times.append(time.perf_counter())


def test_rainbow_streamer() -> None:
    print("[8] 彩虹流水 100 Hz 流式发送")

    writer = FakeWriter()
    streamer = ail_rainbow.RainbowStreamer(send=writer)
    streamer.start(ail_rainbow.RainbowParams(step_deg=40, speed_deg_s=120, brightness=100))
    time.sleep(0.6)
    stats = streamer.stats()
    streamer.stop()

    check(not streamer.running, "stop() 之后线程结束")
    check(stats["errors"] == 0, "没有发送错误")
    # 100 Hz 跑 0.6 秒 -> 大约 60 帧; 给调度留些余量
    check(55 <= stats["frames"] <= 65, "0.6 秒发了 %d 帧 (期望约 60)" % stats["frames"])
    check(97.0 <= stats["fps"] <= 103.0, "实测帧率 %.1f Hz (目标 100)" % stats["fps"])

    intervals = [b - a for a, b in zip(writer.times, writer.times[1:])]
    mean_ms = 1000.0 * sum(intervals) / len(intervals)
    print("      实测: %d 帧, %.2f Hz, 平均间隔 %.3f ms, 间隔范围 %.2f..%.2f ms"
          % (stats["frames"], stats["fps"], mean_ms, 1000.0 * min(intervals), 1000.0 * max(intervals)))
    check(9.4 <= mean_ms <= 10.6, "平均帧间隔 %.2f ms (目标 10)" % mean_ms)
    # 绝对期限节奏: 个别帧被系统调度挤晚, 平均必须仍是 10 ms (不累积漂移)
    worst = max(abs(1000.0 * i - 10.0) for i in intervals)
    check(worst <= 6.0, "单帧最大偏差 %.2f ms (Windows 调度抖动, 不累积)" % worst)

    # 每一帧都必须是合法的 MODE 0x01 帧
    parser = proto.FrameParser()
    decoded = list(parser.feed(b"".join(writer.frames)))
    check_eq(len(decoded), len(writer.frames), "所有帧都能被协议解析")
    check(all(m == proto.MODE_LEDS for m, _ in decoded), "全是 MODE 0x01")

    # 颜色确实在流动 (第一帧和最后一帧不同), 而且起步时第 1 颗灯接近红色
    first = proto.parse_led_payload(decoded[0][1])
    last = proto.parse_led_payload(decoded[-1][1])
    check(first != last, "颜色随时间变化")
    check(first[0] in [ail_rainbow.hsv_to_rgb(h, 255) for h in range(0, 30)],
          "起步时第 1 颗灯接近红色")

    # 静止 (speed=0) 时颜色不变
    w2 = FakeWriter()
    s2 = ail_rainbow.RainbowStreamer(send=w2)
    s2.start(ail_rainbow.RainbowParams(step_deg=60, speed_deg_s=0, brightness=100))
    time.sleep(0.25)
    s2.stop()
    colors = {proto.parse_led_payload(proto.decode_frame(f)[1])[0] for f in w2.frames}
    check_eq(len(colors), 1, "流速 0 = 图案静止")
    check_eq(list(colors)[0], (255, 0, 0), "静止时基准色相停在红色")

    # 运行中改参数不会中断动画, 且立即生效
    w3 = FakeWriter()
    s3 = ail_rainbow.RainbowStreamer(send=w3)
    s3.start(ail_rainbow.RainbowParams(step_deg=0, speed_deg_s=0, brightness=100))
    time.sleep(0.15)
    s3.set_params(step_deg=180, speed_deg_s=0, brightness=100)
    time.sleep(0.15)
    s3.stop()
    after = proto.parse_led_payload(proto.decode_frame(w3.frames[-1])[1])
    check_eq(after[0], (255, 0, 0), "间隔改 180° 后第 1 颗灯仍是红色")
    check_eq(after[1], (0, 255, 255), "间隔 180° 后第 2 颗灯是对面色 (青)")

    # 亮度 0 -> 全黑
    w4 = FakeWriter()
    s4 = ail_rainbow.RainbowStreamer(send=w4)
    s4.start(ail_rainbow.RainbowParams(step_deg=40, speed_deg_s=60, brightness=0))
    time.sleep(0.12)
    s4.stop()
    check_eq(proto.parse_led_payload(proto.decode_frame(w4.frames[-1])[1]), [(0, 0, 0)] * 7, "亮度 0 全黑")

    # 串口没了要自己停下并报错, 不能一直刷屏
    w5 = FakeWriter(fail_after=3)
    s5 = ail_rainbow.RainbowStreamer(send=w5)
    s5.start()
    for _ in range(60):
        if not s5.running:
            break
        time.sleep(0.02)
    st5 = s5.stats()
    check(not s5.running, "连续发送失败后线程自动停止")
    check(st5["errors"] >= ail_rainbow.MAX_SEND_ERRORS, "记录了 %d 次错误" % st5["errors"])
    check("串口被拔了" in st5["last_error"], "错误信息带上了原因: %r" % st5["last_error"])
    s5.stop()

    # 未运行时也能给出预览颜色
    s6 = ail_rainbow.RainbowStreamer(send=FakeWriter())
    s6.set_params(step_deg=60, speed_deg_s=60, brightness=100)
    check_eq(s6.colors()[:2], [(255, 0, 0), (255, 255, 0)], "未运行时 colors() 也能算出来")


def test_gui_smoke() -> None:
    print("[9] GUI 冒烟 (真的建一遍窗口, 再演练各按钮处理函数)")
    try:
        import ai_status_lights_gui as gui
        app = gui.App()
    except Exception as exc:                        # 无显示环境时跳过
        print("  跳过(没有图形环境?): %s" % exc)
        return

    try:
        app.root.update_idletasks()
        app.root.update()

        # 串口没打开时各按钮只能报错, 不能崩
        app.send_leds()
        app.send_beep()
        app.send_silence()
        app.send_manual()
        app.send_verify()
        app.close_port()
        check(True, "未连接时各按钮不崩")

        # 查找回调的两条路径 (不真去动串口)
        app._on_found(None)
        result = ail_link.FindResult(port="COM404", info=None)
        result.probes.append(ail_link.ProbeResult(port="COM404", opened=True, responded=True,
                                                  matched=True, reply=proto.VERIFY_REPLY))
        app._on_found(result)                       # 打不开 COM404, 但必须优雅失败
        app._on_found(ail_link.FindResult(port="COM404", info=None,
                                         probes=[ail_link.ProbeResult(port="COM404", responded=True)]))
        check(True, "查找回调(命中/候选/未找到)不崩")

        # 颜色/预览联动
        app.set_all_colors((0x11, 0x22, 0x33))
        app.root.update_idletasks()
        check_eq([v.get() for v in app.led_vars], ["#112233"] * 7, "全部设为一种颜色")
        check_eq(app.led_buttons[0].cget("bg"), "#112233", "色块跟着更新")
        check("55 AA 01" in app.led_preview.cget("text"), "灯效预览显示了帧内容")

        app.bulk_var.set("#0A0B0C")
        app.apply_bulk()
        check_eq(app.led_vars[6].get(), "#0A0B0C", "批量应用颜色")

        # 非法输入要有提示, 不抛异常
        app.led_vars[2].set("ZZZ")
        app.send_leds()
        app.led_vars[2].set("#00FF00")
        app.freq_var.set("abc")
        app._update_buzzer_preview()
        app.freq_var.set("2000")
        app.manual_var.set("55 AA 0G")
        app.send_manual()
        check(True, "非法输入不崩")

        # 蜂鸣器预览/预设
        app._apply_preset(1000, 80, 1000)
        app.root.update_idletasks()
        check("E8 03 50 E8 03" in app.buzzer_preview.cget("text"), "蜂鸣预览帧正确")
        check_eq(app.vol_label.cget("text"), "80", "音量标签跟随")

        # 扫描/查找的 UI 路径(后台线程真实跑一次, 不加超时会很久, 只验证能启动)
        check(app.port_combo["values"] is not None, "串口下拉框已填充")

        # 彩虹流水页: 滑块 => 预览 + 参数; 没开串口时开始要被拦住
        app.rb_step_var.set(90)
        app.rb_speed_var.set(-240)
        app.rb_bright_var.set(50)
        app._on_rainbow_change()
        app.root.update_idletasks()
        p = app.rainbow.params()
        check_eq((p.step_deg, int(p.speed_deg_s), p.brightness), (90, -240, 50), "滑块写进参数")
        check_eq(app.rb_step_label.cget("text"), "90° / 颗", "间隔标签")
        check_eq(app.rb_speed_label.cget("text"), "-240 °/s", "流速标签")
        check_eq(app.rainbow_canvas.itemcget(app.rainbow_dots[0], "fill"), "#800000",
                 "预览第一个色块 = 亮度 50 的红 (通道值减半)")
        app.rainbow_start()                      # 未打开串口 -> 只记日志
        check(not app.rainbow.running, "串口没开时不允许开始流水")
        app._update_rainbow_readout()
        check("未运行" in app.rb_rate_label.cget("text"), "状态显示未运行")

        # 停止按钮在没跑的时候也不能崩
        app.rainbow_stop()
        check(True, "停止按钮安全")
    finally:
        app.root.destroy()

    check(True, "窗口销毁正常")


def main() -> int:
    print("AI Status Lights 上位机自测   python=%s" % sys.version.split()[0])
    print("pyserial 可用: %s\n" % ail_link.PYSERIAL_AVAILABLE)
    test_frames()
    test_parser()
    test_port_identify()
    test_probe_replies()
    test_find_device()
    test_end_to_end()
    test_rainbow_math()
    test_rainbow_streamer()
    test_gui_smoke()
    print("\n检查项: %d 通过, %d 失败" % (PASS, FAIL))
    if FAIL:
        for f in FAILURES:
            print("  - " + f)
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
