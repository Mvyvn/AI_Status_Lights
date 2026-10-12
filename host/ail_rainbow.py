# -*- coding: utf-8 -*-
"""彩虹流水灯: 颜色数学 + 100 Hz 流式发送线程 (纯上位机实现, MCU 不用改).

原理
----
下位机只需要原来的 MODE 0x01 (静态灯效) 命令, 上位机以固定 100 Hz 不停地下发
26 字节的灯效帧, 帧内容按时间推进色相, 视觉上就是彩虹在流动.

    第 i 颗灯色相 = (base + i * step_deg) mod 360
    base 每秒推进 speed 度 (可以为负 = 反向)

时间推进用的是 *实测* 经过的时间 (perf_counter), 所以流动速度与真实时间一致;
帧节奏用绝对期限 (deadline) 控制, 不会因为写串口耗时或系统调度而累积漂移.

色相 -> RGB 用整数运算 (S 固定 255), 与 MCU 上 WS2812 的 8 位/通道一致.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

import ail_protocol as proto

LED_NUM = proto.LED_NUM
FRAME_HZ = 100.0
FRAME_PERIOD = 1.0 / FRAME_HZ

# 参数范围 (与界面滑块一致)
STEP_MIN, STEP_MAX = 0, 180
SPEED_MIN, SPEED_MAX = -720, 720
BRIGHTNESS_MIN, BRIGHTNESS_MAX = 0, 100

DEFAULT_STEP_DEG = 40
DEFAULT_SPEED_DEG_S = 120.0
DEFAULT_BRIGHTNESS = 100

# 连续写失败多少次就自动停止 (串口被拔掉之类)
MAX_SEND_ERRORS = 5

# Windows 默认定时器精度 ~15.6 ms, 会把 100 Hz 的节奏拖成锯齿. 用
# timeBeginPeriod(1) 把它提到 1 ms, 让每帧间隔稳在 10 ms 附近.
_timer_period_depth = 0


def _raise_timer_resolution() -> None:
    global _timer_period_depth
    try:
        import ctypes
        if _timer_period_depth == 0:
            ctypes.windll.winmm.timeBeginPeriod(1)
        _timer_period_depth += 1
    except Exception:
        pass


def _restore_timer_resolution() -> None:
    global _timer_period_depth
    try:
        import ctypes
        if _timer_period_depth > 0:
            _timer_period_depth -= 1
            if _timer_period_depth == 0:
                ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


# --------------------------------------------------------------------------
# 颜色
# --------------------------------------------------------------------------

def brightness_value(brightness: int) -> int:
    """0..100 -> 0..255 (四舍五入), 与蜂鸣器音量的量纲一致."""
    b = int(clamp(brightness, 0, 100))
    if b >= 100:
        return 255
    return (b * 255 + 50) // 100


def hsv_to_rgb(hue_deg: int, value: int) -> Tuple[int, int, int]:
    """整数 HSV -> RGB, 饱和度固定 255.

    与固件里 WS2812 的 8 位/通道一致, 全部整数运算, 方便测试逐值比对.
    """
    hue = int(hue_deg) % 360
    v = max(0, min(255, int(value)))

    region = hue // 60
    rem = hue % 60

    f = (rem * 255) // 60          # 区域内位置 0..255
    t = (v * f) // 255             # 上升沿
    q = (v * (255 - f)) // 255     # 下降沿

    if region == 0:
        return (v, t, 0)
    if region == 1:
        return (q, v, 0)
    if region == 2:
        return (0, v, t)
    if region == 3:
        return (0, q, v)
    if region == 4:
        return (t, 0, v)
    return (v, 0, q)


def frame_colors(step_deg: int, value: int, base_millideg: int) -> List[Tuple[int, int, int]]:
    """整条灯带的颜色; base_millideg 是第 1 颗灯的色相 (1/1000 度, 可为负)."""
    base = base_millideg % 360000
    hue0 = base // 1000
    return [hsv_to_rgb(hue0 + i * int(step_deg), value) for i in range(LED_NUM)]


def frame_bytes(step_deg: int, value: int, base_millideg: int) -> bytes:
    """直接给出要下发的 MODE 0x01 整帧 (26 字节)."""
    return proto.build_leds(frame_colors(step_deg, value, base_millideg))


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

@dataclass
class RainbowParams:
    step_deg: int = DEFAULT_STEP_DEG
    speed_deg_s: float = DEFAULT_SPEED_DEG_S
    brightness: int = DEFAULT_BRIGHTNESS

    def clamped(self) -> "RainbowParams":
        return RainbowParams(
            step_deg=int(clamp(self.step_deg, STEP_MIN, STEP_MAX)),
            speed_deg_s=float(clamp(self.speed_deg_s, SPEED_MIN, SPEED_MAX)),
            brightness=int(clamp(self.brightness, BRIGHTNESS_MIN, BRIGHTNESS_MAX)),
        )

    def describe(self) -> str:
        return "间隔 %d° / 流速 %+.0f°/s / 亮度 %d" % (
            self.step_deg, self.speed_deg_s, self.brightness)


# --------------------------------------------------------------------------
# 100 Hz 发送线程
# --------------------------------------------------------------------------

class RainbowStreamer:
    """以固定帧率把 MODE 0x01 灯效帧写出去.

    send 是一个回调: `send(frame: bytes) -> None`, 抛异常表示发送失败.
    参数可以随时用 set_params() 改, 动画不会中断 (色相连续, 不跳变).
    """

    def __init__(self, send: Callable[[bytes], None], fps: float = FRAME_HZ,
                 params: Optional[RainbowParams] = None) -> None:
        self._send = send
        self.fps = float(fps)
        self._lock = threading.Lock()
        self._params = (params or RainbowParams()).clamped()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._base_millideg = 0
        self._colors: List[Tuple[int, int, int]] = frame_colors(
            self._params.step_deg, brightness_value(self._params.brightness), 0)
        self._frames = 0
        self._errors = 0
        self._last_error = ""
        self._fps_measured = 0.0
        self._interval_ms = 0.0
        self._started_at = 0.0
        self._running = False

    # -- 状态 --------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def params(self) -> RainbowParams:
        with self._lock:
            return self._params

    def colors(self) -> List[Tuple[int, int, int]]:
        with self._lock:
            return list(self._colors)

    def current_frame(self) -> bytes:
        return proto.build_leds(self.colors())

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "frames": self._frames,
                "errors": self._errors,
                "last_error": self._last_error,
                "fps": self._fps_measured,
                "interval_ms": self._interval_ms,
                "base_millideg": self._base_millideg,
            }

    # -- 控制 --------------------------------------------------------------
    def set_params(self, step_deg: Optional[int] = None, speed_deg_s: Optional[float] = None,
                   brightness: Optional[int] = None) -> RainbowParams:
        with self._lock:
            p = self._params
            if step_deg is not None:
                p = RainbowParams(p.step_deg, p.speed_deg_s, p.brightness)
                p.step_deg = int(clamp(step_deg, STEP_MIN, STEP_MAX))
            if speed_deg_s is not None:
                p.speed_deg_s = float(clamp(speed_deg_s, SPEED_MIN, SPEED_MAX))
            if brightness is not None:
                p.brightness = int(clamp(brightness, BRIGHTNESS_MIN, BRIGHTNESS_MAX))
            self._params = p
            # 停着的时候也要让预览跟着动
            if not self._running:
                self._colors = frame_colors(p.step_deg, brightness_value(p.brightness), self._base_millideg)
            return p

    def start(self, params: Optional[RainbowParams] = None) -> None:
        if self._running:
            if params is not None:
                self.set_params(params.step_deg, params.speed_deg_s, params.brightness)
            return
        if params is not None:
            self.set_params(params.step_deg, params.speed_deg_s, params.brightness)
        self._stop.clear()
        self._errors = 0
        self._last_error = ""
        self._frames = 0
        self._started_at = time.perf_counter()
        self._running = True
        _raise_timer_resolution()
        self._thread = threading.Thread(target=self._run, name="ail-rainbow", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=timeout)
        if self._running:
            _restore_timer_resolution()
        self._running = False

    # -- 线程主体 ----------------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / self.fps
        last = time.perf_counter()
        next_deadline = last
        window_start = last
        window_frames = 0

        while not self._stop.is_set():
            now = time.perf_counter()

            with self._lock:
                p = self._params

            # 色相按"实测"经过的时间推进, 流速与真实时间一致
            elapsed = now - last
            last = now
            self._base_millideg = int(
                (self._base_millideg + elapsed * 1000.0 * p.speed_deg_s) % 360000.0)

            value = brightness_value(p.brightness)
            colors = frame_colors(p.step_deg, value, self._base_millideg)
            frame = proto.build_leds(colors)

            with self._lock:
                self._colors = colors
                self._frames += 1
                window_frames += 1

            try:
                self._send(frame)
            except Exception as exc:
                with self._lock:
                    self._errors += 1
                    self._last_error = "%s: %s" % (type(exc).__name__, exc)
                    errors = self._errors
                if errors >= MAX_SEND_ERRORS:
                    with self._lock:
                        self._last_error = "连续 %d 次发送失败, 已停止: %s" % (errors, exc)
                        self._running = False
                    self._stop.set()
                    break
                # 发送失败也别丢帧节奏, 继续跑

            # 统计实测帧率 (每 ~0.5 秒更新一次)
            now = time.perf_counter()
            if now - window_start >= 0.5:
                with self._lock:
                    self._fps_measured = window_frames / (now - window_start)
                    self._interval_ms = 1000.0 * (now - window_start) / max(1, window_frames)
                window_start = now
                window_frames = 0

            # 绝对期限节奏: 慢一次不会累积漂移
            next_deadline += period
            if next_deadline < now - period:
                next_deadline = now + period        # 落后超过一帧, 重新对齐
            sleep_left = next_deadline - now
            if sleep_left > 0.002:
                time.sleep(sleep_left - 0.0015)
            while time.perf_counter() < next_deadline:
                if self._stop.is_set():
                    break

        self._running = False
        _restore_timer_resolution()
        if self._thread is threading.current_thread():
            self._thread = None


# --------------------------------------------------------------------------
# 单帧自检 (不启动线程)
# --------------------------------------------------------------------------

def one_frame(params: RainbowParams, base_millideg: int = 0) -> bytes:
    p = params.clamped()
    return frame_bytes(p.step_deg, brightness_value(p.brightness), base_millideg)


def static_colors(params: RainbowParams, base_millideg: int = 0) -> Sequence[Tuple[int, int, int]]:
    p = params.clamped()
    return frame_colors(p.step_deg, brightness_value(p.brightness), base_millideg)
