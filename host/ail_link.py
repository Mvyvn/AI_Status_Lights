# -*- coding: utf-8 -*-
"""串口发现: 先挑出 CH340, 再一个个发验证帧, 谁应答谁就是板子.

对上层只暴露两个函数:

    list_ports()                  -> [PortInfo, ...]        列出所有串口
    find_device(...)              -> FindResult             自动找到板子
    probe_port(port, ...)         -> ProbeResult            单独探测一个口

串口读写全部走 pyserial; 但 `opener` / `port_lister` 都能注入, 方便离线测试.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import ail_protocol as proto
from ail_protocol import VERIFY_REPLY, hex_str

try:                                              # 允许在没有 pyserial 的机器上 import 本模块
    import serial
    import serial.tools.list_ports as list_ports_tool
    PYSERIAL_AVAILABLE = True
    PYSERIAL_ERROR = ""
except Exception as exc:                          # pragma: no cover
    serial = None                                 # type: ignore[assignment]
    list_ports_tool = None                        # type: ignore[assignment]
    PYSERIAL_AVAILABLE = False
    PYSERIAL_ERROR = str(exc)

# CH340/CH341 系列: 描述里通常写 "USB-SERIAL CH340", 厂商 wch.cn, VID:PID = 1A86:7523
CH340_KEYWORDS = ("ch340", "ch341", "ch34x", "ch343", "ch9102")
CH340_VID_PIDS = ("1a86:7523", "1a86:5523", "1a86:7522", "1a86:55d4", "1a86:55d3")

DEFAULT_BAUD = 115200


@dataclass
class PortInfo:
    """一个串口."""

    device: str
    description: str = ""
    hwid: str = ""
    manufacturer: str = ""
    product: str = ""

    @property
    def blob(self) -> str:
        """用于关键字匹配的一整串信息."""
        return " ".join(
            x for x in (self.device, self.description, self.hwid, self.manufacturer, self.product or "") if x
        ).lower()

    @property
    def is_ch340(self) -> bool:
        blob = self.blob
        if any(k in blob for k in CH340_KEYWORDS):
            return True
        return any(v in blob for v in CH340_VID_PIDS)

    def __str__(self) -> str:
        tag = "CH340" if self.is_ch340 else "其它 "
        return "%s  %s  [%s]" % (tag, self.device, self.description or self.hwid or "?")


@dataclass
class ProbeResult:
    """一次探测的结果."""

    port: str
    opened: bool = False
    responded: bool = False          # 有没有收到任何字节
    matched: bool = False            # 收到的是不是协议的固定应答
    reply: bytes = b""
    elapsed_ms: int = 0
    error: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """能判定"这就是板子": 应答内容匹配协议; 或者应答了但不认识(仍算候选)."""
        return self.matched

    def summary(self) -> str:
        if self.error:
            return "%-6s 打开失败: %s" % (self.port, self.error)
        if self.matched:
            return "%-6s 命中! 收到 %r (%d ms)" % (self.port, self.reply.decode("ascii", "replace"), self.elapsed_ms)
        if self.responded:
            return "%-6s 有响应但不是本协议: %s" % (self.port, hex_str(self.reply[:32]))
        return "%-6s 无响应" % self.port


@dataclass
class FindResult:
    """自动查找的最终结果."""

    port: Optional[str] = None
    info: Optional[PortInfo] = None
    probes: List[ProbeResult] = field(default_factory=list)
    candidates: List[PortInfo] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    hint: str = ""                    # 给人看的下一步建议

    @property
    def found(self) -> bool:
        return self.port is not None

    def best(self) -> Optional[ProbeResult]:
        """命中的那个探测结果."""
        for p in self.probes:
            if p.matched and p.port == self.port:
                return p
        return None


# --------------------------------------------------------------------------
# 列串口
# --------------------------------------------------------------------------

def _natural_key(device: str):
    """COM5 排在 COM30 前面, 而不是按字典序."""
    import re

    parts = re.split(r"(\d+)", device.upper())
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts if p != ""]


def list_ports(port_lister: Optional[Callable[[], Iterable]] = None) -> List[PortInfo]:
    """列出本机所有串口, CH340 排在前面, 同组按串口号自然排序."""
    if port_lister is None:
        if not PYSERIAL_AVAILABLE:
            raise RuntimeError("没有安装 pyserial: pip install pyserial (%s)" % PYSERIAL_ERROR)
        port_lister = list_ports_tool.comports

    infos: List[PortInfo] = []
    for p in port_lister():
        infos.append(
            PortInfo(
                device=getattr(p, "device", "") or "",
                description=getattr(p, "description", "") or "",
                hwid=getattr(p, "hwid", "") or "",
                manufacturer=getattr(p, "manufacturer", "") or "",
                product=getattr(p, "product", "") or "",
            )
        )
    infos.sort(key=lambda i: (not i.is_ch340, _natural_key(i.device)))
    return infos


def ch340_ports(ports: Optional[Sequence[PortInfo]] = None) -> List[PortInfo]:
    """只留 CH340. 没有 description 的虚拟串口也可能被 hwid 里的 1A86:7523 捞进来."""
    if ports is None:
        ports = list_ports()
    return [p for p in ports if p.is_ch340]


# --------------------------------------------------------------------------
# 默认串口工厂
# --------------------------------------------------------------------------

def default_opener(port: str, baud: int, timeout: float, reset_pulse: bool = False, **kwargs):
    """打开串口; 注入点, 测试时可替换成假串口."""
    if not PYSERIAL_AVAILABLE:
        raise RuntimeError("没有安装 pyserial: pip install pyserial")
    return serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
        write_timeout=1.0,
    )


def _drain(ser, seconds: float) -> bytes:
    """把缓冲区里残留的旧数据读掉, 返回读到的内容."""
    end = time.monotonic() + seconds
    data = bytearray()
    while time.monotonic() < end:
        chunk = ser.read(256)
        if chunk:
            data += chunk
        else:
            time.sleep(0.01)
    return bytes(data)


# --------------------------------------------------------------------------
# 探测单个串口
# --------------------------------------------------------------------------

def probe_port(
    port: str,
    baud: int = DEFAULT_BAUD,
    timeout: float = 0.3,
    retries: int = 3,
    settle: float = 0.15,
    opener: Optional[Callable] = None,
    reset_pulse: bool = False,
    expect: bytes = VERIFY_REPLY,
    log: Optional[Callable[[str], None]] = None,
) -> ProbeResult:
    """往一个串口发验证帧, 看有没有应答.

    流程: 打开 -> 冲掉残留 -> 发 55 AA 00 FF BB -> 读回包; 没读够就重试 retries 次.
    `reset_pulse=True` 时先用 DTR/RTS 拉一下(板子上有复位电路才有用).
    """
    result = ProbeResult(port=port)
    say = log or (lambda _msg: None)
    open_fn = opener or default_opener
    started = time.monotonic()
    ser = None

    try:
        ser = open_fn(port, baud, timeout, reset_pulse=reset_pulse)
        result.opened = True
    except Exception as exc:
        result.error = "%s: %s" % (type(exc).__name__, exc)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        say("%-6s 打开失败: %s" % (port, result.error))
        return result

    try:
        if reset_pulse:
            try:
                ser.dtr = False
                ser.rts = True
                time.sleep(0.05)
                ser.rts = False
                time.sleep(0.05)
                result.notes.append("已脉冲 RTS 复位")
            except Exception as exc:
                result.notes.append("RTS 复位失败: %s" % exc)

        try:
            ser.reset_input_buffer()
        except Exception:
            pass

        junk = _drain(ser, settle)
        if junk:
            result.notes.append("打开时丢弃 %d 字节旧数据" % len(junk))
            say("%-6s 丢弃开机残留 %d 字节: %s" % (port, len(junk), hex_str(junk[:16])))

        frame = proto.build_verify()
        got = bytearray()

        for attempt in range(1, max(1, retries) + 1):
            say("%-6s 第%d次发送 %s" % (port, attempt, hex_str(frame)))
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write(frame)
            try:
                ser.flush()
            except Exception:
                pass

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                chunk = ser.read(256)
                if chunk:
                    got += chunk
                    if expect and expect in got:
                        break
                    deadline = time.monotonic() + timeout   # 收到数据就再等一小会儿
                else:
                    break
            if got:
                break

        result.reply = bytes(got)
        result.responded = len(got) > 0
        result.matched = bool(expect) and expect in got
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        say("%-6s -> %s" % (port, result.summary()))
        return result

    except Exception as exc:
        result.error = "%s: %s" % (type(exc).__name__, exc)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        say("%-6s 通信异常: %s" % (port, result.error))
        return result

    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 自动查找
# --------------------------------------------------------------------------

def find_device(
    baud: int = DEFAULT_BAUD,
    timeout: float = 0.3,
    retries: int = 3,
    fallback_all: bool = True,
    prefer_ch340: bool = True,
    port_lister: Optional[Callable[[], Iterable]] = None,
    opener: Optional[Callable] = None,
    reset_pulse: bool = False,
    log: Optional[Callable[[str], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
) -> FindResult:
    """在一堆串口里找出真正连着板子的那个.

    1. 列出串口, CH340 优先;
    2. 按顺序探测: 发 55 AA 00 FF BB, 收到 "AI Status Lights OK" 就算命中, 立即返回;
    3. 有响应但内容不是本协议的记为候选, 继续找完全匹配的;
    4. CH340 全都没命中且 fallback_all=True, 再扫剩下的串口.
    """
    say = log or (lambda _msg: None)
    outcome = FindResult()

    ports = list_ports(port_lister)
    if not ports:
        outcome.log.append("没有找到任何串口")
        say("没有找到任何串口")
        return outcome

    say("发现 %d 个串口:" % len(ports))
    for p in ports:
        say("   " + str(p))

    first = ch340_ports(ports) if prefer_ch340 else list(ports)
    if prefer_ch340:
        if first:
            say("CH340 串口 %d 个, 开始逐个探测" % len(first))
        else:
            say("没有 CH340 串口")

    second = [p for p in ports if p not in first] if fallback_all else []

    outcome.candidates = list(first) + list(second)
    unmatched: List[ProbeResult] = []

    for group, label in ((first, "CH340"), (second, "其它串口")):
        if not group:
            continue
        if label == "其它串口":
            say("CH340 均无应答, 回退扫描其它串口")

        for info in group:
            if stop_flag is not None and stop_flag():
                outcome.log.append("已取消")
                say("已取消")
                return outcome

            probe = probe_port(
                info.device,
                baud=baud,
                timeout=timeout,
                retries=retries,
                opener=opener,
                reset_pulse=reset_pulse,
                log=log,
            )
            outcome.probes.append(probe)
            outcome.log.append(probe.summary())

            if probe.matched:
                outcome.port = info.device
                outcome.info = info
                say("=> 找到设备: %s (%s)" % (info.device, info.description))
                return outcome

            if probe.responded and not probe.error:
                unmatched.append(probe)
                say("%-6s 有响应但协议不符, 先记下继续找" % info.device)

    # 没有严格匹配: 退而求其次, 有响应就算
    if unmatched:
        outcome.port = unmatched[0].port
        outcome.info = next((p for p in ports if p.device == outcome.port), None)
        say("=> 只有 %s 有响应(协议不符), 作为候选返回" % outcome.port)
        outcome.log.append("只有 %s 有响应, 但内容不是本协议" % outcome.port)
        return outcome

    say("=> 所有串口都没有响应")
    outcome.log.append("没有设备应答")

    # 一个都打不开: 基本是端口被别的程序占着, 而不是板子没插
    failed = [p for p in outcome.probes if not p.opened]
    if outcome.probes and len(failed) == len(outcome.probes):
        outcome.hint = ("%s 都打不开, 多半被别的程序占用了 "
                        "(串口助手 / IDE 的串口监视器 / 上一次没关干净), 关掉再试"
                        % ", ".join(p.port for p in failed))
    elif failed:
        outcome.hint = ("%s 打不开(可能被别的程序占用); 其余串口没人应答, "
                        "确认固件已烧录、TX/RX 是否接反、板子有没有上电"
                        % ", ".join(p.port for p in failed))
    elif outcome.probes and not any(p.responded for p in outcome.probes):
        outcome.hint = "串口都能打开但没人应答: 确认固件已烧录、TX/RX 是否接反、板子有没有上电"

    if outcome.hint:
        outcome.log.append(outcome.hint)
        say(outcome.hint)

    return outcome


if __name__ == "__main__":                        # python ail_link.py 快速看一眼
    def _print(msg: str) -> None:
        print(msg)

    res = find_device(log=_print)
    print()
    print("结果:", res.port or "未找到")
