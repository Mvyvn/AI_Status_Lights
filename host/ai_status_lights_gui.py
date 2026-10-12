#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Status Lights 上位机 (Tkinter GUI + 命令行).

打开串口时先找描述里带 CH340 的, 在它们中间逐个发验证帧
(55 AA 00 FF BB), 谁回 "AI Status Lights OK" 谁就是板子.

用法:
    python ai_status_lights_gui.py                      启动界面
    python ai_status_lights_gui.py --scan               只列串口
    python ai_status_lights_gui.py --find               自动查找板子(命令行)
    python ai_status_lights_gui.py --port COM11 --verify 发验证帧
    python ai_status_lights_gui.py --port COM11 --leds FF0000,00FF00,0000FF,FFFF00,00FFFF,FF00FF,FFFFFF
    python ai_status_lights_gui.py --port COM11 --leds-all 00FF00
    python ai_status_lights_gui.py --port COM11 --beep 2000 100 500
    python ai_status_lights_gui.py --port COM11 --monitor 5

依赖: pyserial (pip install pyserial); 界面需要 Python 自带 tkinter.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ail_protocol as proto                     # noqa: E402
import ail_link                                  # noqa: E402
import ail_rainbow                               # noqa: E402
from ail_protocol import (                        # noqa: E402
    LED_NUM,
    VERIFY_REPLY,
    color_hex,
    hex_str,
    parse_color,
)

APP_TITLE = "AI Status Lights 上位机"
# 固件开机自检用的那组彩虹色 (与 README 里的示例帧一致)
RAINBOW = [
    (0xFF, 0x00, 0x00),
    (0xFF, 0x60, 0x00),
    (0xFF, 0xFF, 0x00),
    (0x00, 0xFF, 0x00),
    (0x00, 0xFF, 0xFF),
    (0x00, 0x00, 0xFF),
    (0xA0, 0x00, 0xFF),
]


def timestamp() -> str:
    return time.strftime("%H:%M:%S")


# ==========================================================================
# 串口连接 (带后台读线程)
# ==========================================================================

class SerialLink:
    """一个打开的串口 + 后台读线程; 收到的数据丢进 Queue."""

    def __init__(self) -> None:
        self.ser = None
        self.rx_queue: "queue.Queue[bytes]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._tx_lock = threading.Lock()
        self.port = ""
        self.baud = 0

    @property
    def is_open(self) -> bool:
        return self.ser is not None and getattr(self.ser, "is_open", False)

    def open(self, port: str, baud: int) -> None:
        if not ail_link.PYSERIAL_AVAILABLE:
            raise RuntimeError("没有安装 pyserial, 请先 pip install pyserial")
        self.close()
        self.ser = ail_link.serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=ail_link.serial.EIGHTBITS,
            parity=ail_link.serial.PARITY_NONE,
            stopbits=ail_link.serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=1.0,
        )
        self.port, self.baud = port, baud
        self._running = True
        self._thread = threading.Thread(target=self._reader, name="ail-rx", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        while self._running and self.ser is not None:
            try:
                chunk = self.ser.read(256)
            except Exception:
                break
            if chunk:
                self.rx_queue.put(chunk)

    def send(self, data: bytes) -> int:
        with self._tx_lock:
            if not self.is_open:
                raise RuntimeError("串口没有打开")
            return self.ser.write(data)

    def close(self) -> None:
        with self._tx_lock:
            self._running = False
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
                thread.join(timeout=0.5)
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None


# ==========================================================================
# GUI
# ==========================================================================

class App:
    def __init__(self, initial_port: Optional[str] = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.link = SerialLink()
        self.parser = proto.FrameParser()
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.port_map = {}                        # 下拉框显示文本 -> PortInfo
        self.led_vars: List["tk.StringVar"] = []
        self.led_buttons: List["tk.Button"] = []
        self.busy = False
        # 彩虹流水: 上位机以 100 Hz 不停下发 MODE 0x01 帧
        self.rainbow = ail_rainbow.RainbowStreamer(send=self._stream_send)
        self.rainbow_canvas = None
        self.rainbow_dots: List[int] = []
        self._rainbow_ui_ms = 0
        self._rainbow_was_running = False

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("880x680")
        self.root.minsize(800, 600)

        self._build_port_bar()
        self._build_notebook()
        self._build_log()

        self.refresh_ports(silent=True)

        if initial_port:
            for text, info in self.port_map.items():
                if info.device.upper() == initial_port.upper():
                    self.port_combo.set(text)
                    break
            self.open_port(initial_port)

        self.root.after(30, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ 顶部
    def _build_port_bar(self) -> None:
        tk, ttk = self.tk, self.ttk

        bar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        bar.pack(fill="x")

        ttk.Label(bar, text="串口:").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(bar, width=38, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="w", padx=(4, 8))

        ttk.Label(bar, text="波特率:").grid(row=0, column=2, sticky="w")
        self.baud_var = tk.StringVar(value=str(ail_link.DEFAULT_BAUD))
        ttk.Combobox(
            bar, width=9, textvariable=self.baud_var,
            values=("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"),
        ).grid(row=0, column=3, sticky="w", padx=(4, 8))

        ttk.Label(bar, text="超时(s):").grid(row=0, column=4, sticky="w")
        self.timeout_var = tk.StringVar(value="0.3")
        ttk.Entry(bar, width=6, textvariable=self.timeout_var).grid(row=0, column=5, sticky="w", padx=(4, 8))

        ttk.Label(bar, text="重试:").grid(row=0, column=6, sticky="w")
        self.retry_var = tk.StringVar(value="3")
        ttk.Entry(bar, width=4, textvariable=self.retry_var).grid(row=0, column=7, sticky="w", padx=(4, 8))

        bar2 = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        bar2.pack(fill="x")

        self.btn_scan = ttk.Button(bar2, text="① 扫描串口", command=self.refresh_ports)
        self.btn_scan.pack(side="left")
        self.btn_find = ttk.Button(bar2, text="② 自动查找设备", command=self.auto_find)
        self.btn_find.pack(side="left", padx=(6, 0))
        self.btn_open = ttk.Button(bar2, text="③ 打开串口", command=self.open_port)
        self.btn_open.pack(side="left", padx=(6, 0))
        self.btn_close = ttk.Button(bar2, text="关闭串口", command=self.close_port, state="disabled")
        self.btn_close.pack(side="left", padx=(6, 0))

        self.fallback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar2, text="CH340 无应答时回退扫描全部串口", variable=self.fallback_var).pack(
            side="left", padx=(12, 0)
        )
        self.resetpulse_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar2, text="探测前脉冲 DTR/RTS 复位", variable=self.resetpulse_var).pack(
            side="left", padx=(12, 0)
        )

        self.status_var = tk.StringVar(value="就绪")
        self.status = ttk.Label(self.root, textvariable=self.status_var, anchor="w",
                                relief="sunken", padding=(6, 3))
        self.status.pack(fill="x", side="bottom")

    # ------------------------------------------------------------- 三个页签
    def _build_notebook(self) -> None:
        tk, ttk = self.tk, self.ttk
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        self._build_led_tab(nb)
        self._build_rainbow_tab(nb)
        self._build_buzzer_tab(nb)
        self._build_manual_tab(nb)

    def _build_led_tab(self, nb) -> None:
        tk, ttk = self.tk, self.ttk
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  灯效 (MODE 01)  ")

        ttk.Label(tab, text="点色块选颜色, 或直接改右边的 #RRGGBB. LED1 是离单片机最近的那颗.",
                  foreground="#555").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        for i in range(LED_NUM):
            var = tk.StringVar(value=color_hex(RAINBOW[i]))
            self.led_vars.append(var)
            r = i + 1
            ttk.Label(tab, text="LED%d" % (i + 1), width=6).grid(row=r, column=0, sticky="w")
            btn = tk.Button(tab, width=6, bg=color_hex(RAINBOW[i]), relief="raised",
                            command=lambda idx=i: self.pick_color(idx))
            btn.grid(row=r, column=1, padx=(0, 8), pady=2)
            self.led_buttons.append(btn)
            entry = ttk.Entry(tab, width=12, textvariable=var)
            entry.grid(row=r, column=2, sticky="w", pady=2)
            var.trace_add("write", lambda *_a, idx=i: self._sync_swatch(idx))

        tools = ttk.Frame(tab)
        tools.grid(row=LED_NUM + 2, column=0, columnspan=4, sticky="w", pady=(12, 0))
        ttk.Button(tools, text="发送灯效", command=self.send_leds).pack(side="left")
        ttk.Button(tools, text="全灭", command=lambda: self.set_all_colors((0, 0, 0))).pack(side="left", padx=(6, 0))
        ttk.Button(tools, text="全亮(白)", command=lambda: self.set_all_colors((255, 255, 255))).pack(
            side="left", padx=(6, 0))
        ttk.Button(tools, text="彩虹", command=lambda: self.set_all_colors(list(RAINBOW))).pack(
            side="left", padx=(6, 0))

        pick = ttk.Frame(tab)
        pick.grid(row=LED_NUM + 3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(pick, text="全部设为:").pack(side="left")
        self.bulk_var = tk.StringVar(value="#00FF00")
        ttk.Entry(pick, width=10, textvariable=self.bulk_var).pack(side="left", padx=(4, 6))
        ttk.Button(pick, text="应用", command=self.apply_bulk).pack(side="left")

        self.led_preview = ttk.Label(tab, text="待发送: (未生成)", foreground="#666")
        self.led_preview.grid(row=LED_NUM + 4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.led_vars[0].trace_add("write", lambda *_a: self._update_led_preview())
        self._update_led_preview()

    def _build_rainbow_tab(self, nb) -> None:
        tk, ttk = self.tk, self.ttk
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  彩虹流水 (100Hz)  ")

        ttk.Label(tab, foreground="#555", justify="left",
                  text=("上位机以固定 %d Hz 不停下发 MODE 0x01 灯效帧（下位机不用改，也不需要新命令）。\n"
                        "第 i 颗灯色相 = 基准 + i×颜色间隔，基准按流动速率推进；拖滑块实时生效。"
                        % int(ail_rainbow.FRAME_HZ))).grid(row=0, column=0, columnspan=3, sticky="w",
                                                           pady=(0, 10))

        ttk.Label(tab, text="颜色间隔").grid(row=1, column=0, sticky="w")
        self.rb_step_var = tk.IntVar(value=ail_rainbow.DEFAULT_STEP_DEG)
        tk.Scale(tab, from_=ail_rainbow.STEP_MIN, to=ail_rainbow.STEP_MAX, orient="horizontal",
                 length=320, showvalue=False, variable=self.rb_step_var,
                 command=lambda _v: self._on_rainbow_change()).grid(row=1, column=1, sticky="w", pady=2)
        self.rb_step_label = ttk.Label(tab, width=14, text="")
        self.rb_step_label.grid(row=1, column=2, sticky="w")

        ttk.Label(tab, text="流动速率").grid(row=2, column=0, sticky="w")
        self.rb_speed_var = tk.DoubleVar(value=ail_rainbow.DEFAULT_SPEED_DEG_S)
        tk.Scale(tab, from_=ail_rainbow.SPEED_MIN, to=ail_rainbow.SPEED_MAX, orient="horizontal",
                 length=320, showvalue=False, resolution=1, variable=self.rb_speed_var,
                 command=lambda _v: self._on_rainbow_change()).grid(row=2, column=1, sticky="w", pady=2)
        self.rb_speed_label = ttk.Label(tab, width=14, text="")
        self.rb_speed_label.grid(row=2, column=2, sticky="w")

        ttk.Label(tab, text="亮度").grid(row=3, column=0, sticky="w")
        self.rb_bright_var = tk.IntVar(value=ail_rainbow.DEFAULT_BRIGHTNESS)
        tk.Scale(tab, from_=ail_rainbow.BRIGHTNESS_MIN, to=ail_rainbow.BRIGHTNESS_MAX,
                 orient="horizontal", length=320, showvalue=False, variable=self.rb_bright_var,
                 command=lambda _v: self._on_rainbow_change()).grid(row=3, column=1, sticky="w", pady=2)
        self.rb_bright_label = ttk.Label(tab, width=14, text="")
        self.rb_bright_label.grid(row=3, column=2, sticky="w")

        buttons = ttk.Frame(tab)
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 6))
        self.rb_start_btn = ttk.Button(buttons, text="开始流水", command=self.rainbow_start)
        self.rb_start_btn.pack(side="left")
        self.rb_stop_btn = ttk.Button(buttons, text="停止", command=self.rainbow_stop, state="disabled")
        self.rb_stop_btn.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="正向", command=lambda: self.rb_speed_var.set(
            abs(float(self.rb_speed_var.get())) or ail_rainbow.DEFAULT_SPEED_DEG_S)).pack(side="left", padx=(12, 0))
        ttk.Button(buttons, text="反向", command=lambda: self.rb_speed_var.set(
            -abs(float(self.rb_speed_var.get())) or -ail_rainbow.DEFAULT_SPEED_DEG_S)).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="静止(0)", command=lambda: self.rb_speed_var.set(0)).pack(side="left", padx=(6, 0))

        self.rb_rate_label = ttk.Label(tab, text="", foreground="#333")
        self.rb_rate_label.grid(row=5, column=0, columnspan=3, sticky="w")

        self.rainbow_canvas = tk.Canvas(tab, width=560, height=76, highlightthickness=1,
                                        highlightbackground="#bbb")
        self.rainbow_canvas.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 4))
        for i in range(LED_NUM):
            x0 = 20 + i * 76
            self.rainbow_dots.append(self.rainbow_canvas.create_oval(
                x0, 14, x0 + 52, 66, fill="#000000", outline="#666"))

        self.rb_frame_label = ttk.Label(tab, text="", foreground="#666", font=("Consolas", 8))
        self.rb_frame_label.grid(row=7, column=0, columnspan=3, sticky="w")

        self._on_rainbow_change()

    def _build_buzzer_tab(self, nb) -> None:
        tk, ttk = self.tk, self.ttk
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  蜂鸣器 (MODE 02)  ")

        ttk.Label(tab, text="无源蜂鸣器: 频率决定音调, 音量映射到占空比(0-100 -> 0-50%).",
                  foreground="#555").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(tab, text="频率 (Hz)").grid(row=1, column=0, sticky="w")
        self.freq_var = tk.StringVar(value="2000")
        tk.Spinbox(tab, from_=20, to=20000, increment=100, width=10, textvariable=self.freq_var).grid(
            row=1, column=1, sticky="w", pady=3)

        ttk.Label(tab, text="音量 (0-100)").grid(row=2, column=0, sticky="w")
        self.vol_var = tk.IntVar(value=100)
        self.vol_scale = tk.Scale(tab, from_=0, to=100, orient="horizontal", length=260, showvalue=False,
                                  variable=self.vol_var, command=lambda _v: self._update_buzzer_preview())
        self.vol_scale.grid(row=2, column=1, sticky="w", pady=3)
        self.vol_label = ttk.Label(tab, text="100")
        self.vol_label.grid(row=2, column=2, sticky="w")

        ttk.Label(tab, text="时长 (ms)").grid(row=3, column=0, sticky="w")
        self.dur_var = tk.StringVar(value="200")
        tk.Spinbox(tab, from_=0, to=60000, increment=50, width=10, textvariable=self.dur_var).grid(
            row=3, column=1, sticky="w", pady=3)

        tools = ttk.Frame(tab)
        tools.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(tools, text="响一声", command=self.send_beep).pack(side="left")
        ttk.Button(tools, text="静音/停止 (时长0)", command=self.send_silence).pack(side="left", padx=(6, 0))

        presets = ttk.Frame(tab)
        presets.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(presets, text="快捷:").pack(side="left")
        for text, freq, vol, dur in (("嘀 2kHz/100ms", 2000, 100, 100),
                                     ("长响 1kHz/1s", 1000, 100, 1000),
                                     ("低音 500Hz/300ms", 500, 60, 300),
                                     ("高音 4kHz/80ms", 4000, 80, 80)):
            ttk.Button(presets, text=text, command=lambda f=freq, v=vol, d=dur: self._apply_preset(f, v, d)).pack(
                side="left", padx=(6, 0))

        self.buzzer_preview = ttk.Label(tab, text="待发送: (未生成)", foreground="#666")
        self.buzzer_preview.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.freq_var.trace_add("write", lambda *_a: self._update_buzzer_preview())
        self.dur_var.trace_add("write", lambda *_a: self._update_buzzer_preview())
        self._update_buzzer_preview()

    def _build_manual_tab(self, nb) -> None:
        tk, ttk = self.tk, self.ttk
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  手动 / 监视  ")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Label(row, text="发送(HEX):").pack(side="left")
        self.manual_var = tk.StringVar(value="55 AA 00 FF BB")
        ttk.Entry(row, textvariable=self.manual_var).pack(side="left", fill="x", expand=True, padx=(6, 6))
        ttk.Button(row, text="发送", command=self.send_manual).pack(side="left")
        ttk.Button(row, text="发验证帧", command=self.send_verify).pack(side="left", padx=(6, 0))

        opts = ttk.Frame(tab)
        opts.pack(fill="x", pady=(8, 4))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="自动滚动", variable=self.autoscroll_var).pack(side="left")
        self.show_hex_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="显示 HEX", variable=self.show_hex_var).pack(side="left", padx=(10, 0))
        ttk.Button(opts, text="清空接收", command=self.clear_monitor).pack(side="right")

        self.monitor = tk.Text(tab, height=18, wrap="none", font=("Consolas", 9))
        yscroll = ttk.Scrollbar(tab, orient="vertical", command=self.monitor.yview)
        self.monitor.configure(yscrollcommand=yscroll.set)
        self.monitor.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

    def _build_log(self) -> None:
        tk, ttk = self.tk, self.ttk
        frame = ttk.LabelFrame(self.root, text="日志", padding=4)
        frame.pack(fill="both", padx=8, pady=(0, 6))
        self.log_text = tk.Text(frame, height=9, wrap="none", font=("Consolas", 9), state="disabled")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ------------------------------------------------------------- 日志/状态
    def log(self, message: str) -> None:
        self.log_queue.put("%s  %s" % (timestamp(), message))

    def _flush_log(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_text.configure(state="normal")
        for line in lines:
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    # ------------------------------------------------------------------ 串口
    def refresh_ports(self, silent: bool = False) -> None:
        try:
            ports = ail_link.list_ports()
        except Exception as exc:
            self.log("扫描串口失败: %s" % exc)
            return

        self.port_map = {}
        values = []
        for info in ports:
            text = "%s  |  %s" % (info.device, info.description or info.hwid or "?")
            if info.is_ch340:
                text = "★ " + text
            self.port_map[text] = info
            values.append(text)

        self.port_combo["values"] = values
        if values and not self.port_combo.get():
            self.port_combo.set(values[0])

        if not silent:
            n_ch = len(ail_link.ch340_ports(ports))
            self.log("扫描到 %d 个串口, 其中 CH340 %d 个" % (len(ports), n_ch))
            for info in ports:
                self.log("   " + str(info))
            self.set_status("扫描到 %d 个串口 (CH340: %d)" % (len(ports), n_ch))

    def selected_port(self) -> Optional[str]:
        text = self.port_combo.get()
        info = self.port_map.get(text)
        if info is not None:
            return info.device
        return text.split("|")[0].strip() if text else None

    def _baud(self) -> int:
        try:
            return int(self.baud_var.get())
        except ValueError:
            return ail_link.DEFAULT_BAUD

    def open_port(self, port: Optional[str] = None) -> bool:
        port = port or self.selected_port()
        if not port:
            self.log("没有选择串口")
            return False
        try:
            self.link.open(port, self._baud())
        except Exception as exc:
            self.log("打开 %s 失败: %s" % (port, exc))
            self.set_status("打开失败: %s" % exc)
            return False

        self.parser.reset()
        self.btn_open.configure(state="disabled")
        self.btn_close.configure(state="normal")
        self.log("已打开 %s @ %d" % (port, self._baud()))
        self.set_status("已连接 %s @ %d 8-N-1" % (port, self._baud()))
        return True

    def close_port(self) -> None:
        if self.rainbow.running:
            self.rainbow_stop(quiet=True)
            self.log("彩虹流水随串口关闭一起停了")
        if self.link.is_open:
            self.log("已关闭 %s" % self.link.port)
        self.link.close()
        self.btn_open.configure(state="normal")
        self.btn_close.configure(state="disabled")
        self.set_status("串口已关闭")

    def auto_find(self) -> None:
        """后台线程里逐个探测, 不卡界面."""
        if self.busy:
            return
        self.busy = True
        self.btn_find.configure(state="disabled")
        self.btn_scan.configure(state="disabled")

        try:
            timeout = float(self.timeout_var.get())
        except ValueError:
            timeout = 0.3
        try:
            retries = int(self.retry_var.get())
        except ValueError:
            retries = 3

        baud = self._baud()
        fallback = bool(self.fallback_var.get())
        pulse = bool(self.resetpulse_var.get())
        self.log("=== 开始自动查找: 先扫 CH340, 再逐个发验证帧 ===")

        def worker() -> None:
            try:
                result = ail_link.find_device(
                    baud=baud, timeout=timeout, retries=retries, fallback_all=fallback,
                    reset_pulse=pulse, log=self.log,
                )
            except Exception as exc:
                self.log("查找出错: %s" % exc)
                result = None
            self.root.after(0, lambda: self._on_found(result))

        threading.Thread(target=worker, name="ail-find", daemon=True).start()

    def _on_found(self, result) -> None:
        self.busy = False
        self.btn_find.configure(state="normal")
        self.btn_scan.configure(state="normal")

        if result is None or not result.found:
            self.log("=== 没有找到应答的设备 ===")
            if result is not None and result.hint:
                self.log("提示: " + result.hint)
                self.set_status(result.hint)
            else:
                self.set_status("未找到设备")
            return

        port = result.port
        for text, info in self.port_map.items():
            if info.device == port:
                self.port_combo.set(text)
                break
        probe = result.best()
        if probe is not None and probe.matched:
            self.log("=== 找到设备: %s (应答 %r) ===" % (port, probe.reply.decode("ascii", "replace")))
            self.set_status("找到设备 %s" % port)
        else:
            self.log("=== 只有 %s 有响应(内容不符协议) ===" % port)
            self.set_status("%s 有响应, 请人工确认" % port)
        self.open_port(port)

    # ------------------------------------------------------------------ 发送
    def _require_open(self) -> bool:
        if not self.link.is_open:
            self.log("串口未打开, 请先 ① 扫描 -> ② 查找 -> ③ 打开")
            self.set_status("串口未打开")
            return False
        return True

    def _send(self, frame: bytes, note: str) -> bool:
        if not self._require_open():
            return False
        try:
            self.link.send(frame)
        except Exception as exc:
            self.log("发送失败: %s" % exc)
            return False
        self.log("发送 %-4s %s   (%s)" % ("->", hex_str(frame), note))
        return True

    # -------------------------------------------------------------- 灯效相关
    def _sync_swatch(self, idx: int) -> None:
        try:
            color = parse_color(self.led_vars[idx].get())
        except Exception:
            return
        self.led_buttons[idx].configure(bg=color_hex(color))
        self._update_led_preview()

    def pick_color(self, idx: int) -> None:
        from tkinter import colorchooser
        try:
            initial = parse_color(self.led_vars[idx].get())
        except Exception:
            initial = (255, 255, 255)
        rgb, _hex = colorchooser.askcolor(color=color_hex(initial), title="选择 LED%d 颜色" % (idx + 1))
        if rgb:
            self.led_vars[idx].set(color_hex((int(rgb[0]), int(rgb[1]), int(rgb[2]))))

    def _current_colors(self) -> Optional[List[Tuple[int, int, int]]]:
        colors = []
        for i, var in enumerate(self.led_vars):
            try:
                colors.append(parse_color(var.get()))
            except Exception:
                self.log("LED%d 颜色不合法: %r" % (i + 1, var.get()))
                return None
        return colors

    def _update_led_preview(self) -> None:
        try:
            frame = proto.build_leds(self._current_colors() or [])
        except Exception:
            self.led_preview.configure(text="待发送: (参数不合法)")
            return
        self.led_preview.configure(text="待发送(%d 字节): %s" % (len(frame), hex_str(frame)))

    def set_all_colors(self, color) -> None:
        if isinstance(color[0], tuple):
            for var, col in zip(self.led_vars, color):
                var.set(color_hex(col))
        else:
            for var in self.led_vars:
                var.set(color_hex(color))

    def apply_bulk(self) -> None:
        try:
            color = parse_color(self.bulk_var.get())
        except Exception as exc:
            self.log("颜色解析失败: %s" % exc)
            return
        self.set_all_colors(color)

    def send_leds(self) -> None:
        colors = self._current_colors()
        if colors is None:
            return
        if self.rainbow.running:
            self.rainbow_stop()
            self.log("发送静态灯效前先停掉了彩虹流水")
        self._send(proto.build_leds(colors), "灯效 %s" % " ".join(color_hex(c) for c in colors))

    # ---------------------------------------------------------- 彩虹流水相关
    def _rainbow_params_from_ui(self) -> "ail_rainbow.RainbowParams":
        return ail_rainbow.RainbowParams(
            step_deg=int(round(float(self.rb_step_var.get()))),
            speed_deg_s=float(self.rb_speed_var.get()),
            brightness=int(round(float(self.rb_bright_var.get()))),
        ).clamped()

    def _on_rainbow_change(self) -> None:
        p = self._rainbow_params_from_ui()
        self.rainbow.set_params(p.step_deg, p.speed_deg_s, p.brightness)
        self.rb_step_label.configure(text="%d° / 颗" % p.step_deg)
        self.rb_speed_label.configure(text="%+.0f °/s" % p.speed_deg_s)
        self.rb_bright_label.configure(text="%d %%" % p.brightness)
        if not self.rainbow.running:
            self._draw_rainbow(self.rainbow.colors())
            self.rb_frame_label.configure(
                text="待下发(26 字节): %s" % hex_str(proto.build_leds(self.rainbow.colors())))

    def _draw_rainbow(self, colors) -> None:
        if self.rainbow_canvas is None:
            return
        for dot, color in zip(self.rainbow_dots, colors):
            self.rainbow_canvas.itemconfig(dot, fill=color_hex(color))

    def _stream_send(self, frame: bytes) -> None:
        """由彩虹流水线程调用; 串口关了就抛异常, 线程会自己停下."""
        self.link.send(frame)

    def rainbow_start(self) -> None:
        if not self._require_open():
            return
        p = self._rainbow_params_from_ui()
        self.rainbow.start(p)
        self.rb_start_btn.configure(state="disabled")
        self.rb_stop_btn.configure(state="normal")
        self.log("彩虹流水开始: %s, 帧率 %d Hz, 每帧 26 字节 (约 %d B/s)"
                 % (p.describe(), int(ail_rainbow.FRAME_HZ), int(26 * ail_rainbow.FRAME_HZ)))
        self.set_status("彩虹流水运行中: %d Hz" % int(ail_rainbow.FRAME_HZ))

    def rainbow_stop(self, quiet: bool = False) -> None:
        was_running = self.rainbow.running
        stats = self.rainbow.stats()
        self.rainbow.stop()
        self._rainbow_was_running = False
        self.rb_start_btn.configure(state="normal")
        self.rb_stop_btn.configure(state="disabled")
        if was_running and not quiet:
            self.log("彩虹流水停止: 共 %d 帧, 实测 %.1f Hz (%.2f ms/帧)"
                     % (stats["frames"], stats["fps"], stats["interval_ms"]))
        self._on_rainbow_change()
        if not quiet:
            self.set_status("彩虹流水已停止, 灯停在最后一帧")

    def _update_rainbow_readout(self) -> None:
        stats = self.rainbow.stats()
        self._draw_rainbow(self.rainbow.colors())

        if stats["running"]:
            self.rb_rate_label.configure(
                text="帧率: 目标 %d Hz, 实测 %.1f Hz (%.2f ms/帧) | 已发 %d 帧 | 约 %d B/s"
                     % (int(ail_rainbow.FRAME_HZ), stats["fps"], stats["interval_ms"], stats["frames"],
                        int(round(stats["fps"] * 26))))
            self.rb_frame_label.configure(
                text="刚才那帧: %s" % hex_str(proto.build_leds(self.rainbow.colors())))
        else:
            self.rb_rate_label.configure(
                text="帧率: 目标 %d Hz（未运行）| 上次共 %d 帧%s"
                     % (int(ail_rainbow.FRAME_HZ), stats["frames"],
                        "" if not stats["last_error"] else " | 错误: " + stats["last_error"]))

        # 线程因为连续发送失败自己停了, 这里同步一下界面并提示一次
        if (not stats["running"]) and self._rainbow_was_running:
            self.rb_start_btn.configure(state="normal")
            self.rb_stop_btn.configure(state="disabled")
            if stats["last_error"]:
                self.log("彩虹流水已停止: %s" % stats["last_error"])
                self.set_status("彩虹流水已停止: %s" % stats["last_error"])
        self._rainbow_was_running = stats["running"]

    # ------------------------------------------------------------ 蜂鸣器相关
    def _buzzer_params(self) -> Optional[Tuple[int, int, int]]:
        try:
            freq = int(float(self.freq_var.get()))
        except ValueError:
            self.log("频率不合法: %r" % self.freq_var.get())
            return None
        try:
            dur = int(float(self.dur_var.get()))
        except ValueError:
            self.log("时长不合法: %r" % self.dur_var.get())
            return None
        return freq, int(self.vol_var.get()), dur

    def _update_buzzer_preview(self) -> None:
        params = self._buzzer_params()
        self.vol_label.configure(text=str(int(self.vol_var.get())))
        if params is None:
            self.buzzer_preview.configure(text="待发送: (参数不合法)")
            return
        frame = proto.build_buzzer(*params)
        self.buzzer_preview.configure(
            text="待发送(%d 字节): %s   -> 频率夹紧后 %dHz" % (
                len(frame), hex_str(frame),
                proto.parse_buzzer_payload(frame[3:-2])[0],
            )
        )

    def _apply_preset(self, freq: int, vol: int, dur: int) -> None:
        self.freq_var.set(str(freq))
        self.vol_var.set(vol)
        self.dur_var.set(str(dur))
        self._update_buzzer_preview()

    def send_beep(self) -> None:
        params = self._buzzer_params()
        if params is None:
            return
        self._send(proto.build_buzzer(*params), "蜂鸣 %dHz 音量%d %dms" % params)

    def send_silence(self) -> None:
        params = self._buzzer_params()
        if params is None:
            return
        self._send(proto.build_buzzer(params[0], 0, 0), "静音(音量0/时长0)")

    # -------------------------------------------------------------- 手动发送
    def send_manual(self) -> None:
        text = self.manual_var.get().strip()
        try:
            data = bytes.fromhex(text.replace(",", " ").replace("0x", " "))
        except ValueError:
            self.log("HEX 解析失败: %r (示例: 55 AA 00 FF BB)" % text)
            return
        self._send(data, "手动")

    def send_verify(self) -> None:
        self.manual_var.set(hex_str(proto.build_verify()))
        self._send(proto.build_verify(), "验证帧")

    def clear_monitor(self) -> None:
        self.monitor.delete("1.0", "end")

    # ------------------------------------------------------------------ 轮询
    def _pump(self) -> None:
        self._flush_log()

        chunks = []
        while True:
            try:
                chunks.append(self.link.rx_queue.get_nowait())
            except queue.Empty:
                break

        if chunks:
            received = b"".join(chunks)
            self._show_rx(received)
            for mode, payload in self.parser.feed(received):
                try:
                    self.log("接收 <- 帧 %s" % proto.describe_frame(mode, payload))
                except Exception:
                    self.log("接收 <- 帧 MODE 0x%02X %s" % (mode, hex_str(payload)))

        # 彩虹流水的读数/预览每 ~150 ms 刷一次 (10 Hz 刷界面就够, 帧率是 100 Hz)
        self._rainbow_ui_ms += 30
        if self._rainbow_ui_ms >= 150:
            self._rainbow_ui_ms = 0
            self._update_rainbow_readout()

        self.root.after(30, self._pump)

    def _show_rx(self, data: bytes) -> None:
        text = hex_str(data) if self.show_hex_var.get() else data.decode("utf-8", "replace")
        self.monitor.insert("end", "%s  <-  %s\n" % (timestamp(), text))
        if self.autoscroll_var.get():
            self.monitor.see("end")

    def on_close(self) -> None:
        self.rainbow.stop()
        self.link.close()
        self.root.destroy()

    def run(self) -> None:
        self.log("%s 启动" % APP_TITLE)
        self.log("流程: ① 扫描串口(优先 CH340) -> ② 自动查找设备 -> ③ 打开串口 -> 发命令")
        self.log("彩虹流水页: 上位机以 %d Hz 下发 MODE 0x01 帧, 下位机不用改" % int(ail_rainbow.FRAME_HZ))
        self.root.mainloop()


# ==========================================================================
# 命令行模式
# ==========================================================================

def cli_scan() -> int:
    ports = ail_link.list_ports()
    if not ports:
        print("没有找到任何串口")
        return 1
    print("共 %d 个串口:" % len(ports))
    for p in ports:
        print("  " + str(p))
    ch = ail_link.ch340_ports(ports)
    print("\nCH340 串口: %s" % (", ".join(p.device for p in ch) if ch else "无"))
    return 0


def cli_find(args) -> int:
    result = ail_link.find_device(
        baud=args.baud, timeout=args.timeout, retries=args.retries,
        fallback_all=not args.no_fallback, reset_pulse=args.reset_pulse, log=print,
    )
    if result.found:
        print("\n结果: %s" % result.port)
        if not (result.best() and result.best().matched):
            print("注意: 该口有响应, 但内容与协议不符")
        return 0
    print("\n没有找到应答的设备")
    if result.hint:
        print("提示: " + result.hint)
    return 1


def cli_command(args) -> int:
    port = args.port
    if not port:
        result = ail_link.find_device(
            baud=args.baud, timeout=args.timeout, retries=args.retries,
            fallback_all=not args.no_fallback, reset_pulse=args.reset_pulse, log=print,
        )
        if not result.found:
            print("没有找到设备, 也没有指定 --port")
            return 1
        port = result.port

    try:
        ser = ail_link.serial.Serial(port=port, baudrate=args.baud, timeout=0.4, write_timeout=1.0)
    except Exception as exc:
        print("打开 %s 失败: %s" % (port, exc))
        return 1

    print("已打开 %s @ %d" % (port, args.baud))
    rc = 0
    try:
        def exchange(frame: bytes, wait: float = 0.4) -> bytes:
            ser.reset_input_buffer()
            print("发送 -> %s" % hex_str(frame))
            ser.write(frame)
            ser.flush()
            deadline = time.monotonic() + wait
            got = bytearray()
            while time.monotonic() < deadline:
                chunk = ser.read(256)
                if chunk:
                    got += chunk
                    deadline = time.monotonic() + 0.15
            if got:
                print("接收 <- %s   %r" % (hex_str(bytes(got)), bytes(got).decode("ascii", "replace")))
            else:
                print("接收 <- (无)")
            return bytes(got)

        if args.verify or (args.monitor is None and not (args.leds or args.leds_all or args.beep or args.rainbow)):
            got = exchange(proto.build_verify())
            if VERIFY_REPLY in got:
                print("验证通过: 收到固定应答 %r" % VERIFY_REPLY.decode())
            else:
                print("验证失败: 没有收到 %r" % VERIFY_REPLY.decode())
                rc = 1

        if args.leds:
            colors = [parse_color(c) for c in args.leds.replace(" ", "").split(",") if c]
            if len(colors) == 1:
                frame = proto.build_leds_all(colors[0])
            else:
                frame = proto.build_leds(colors)
            exchange(frame)

        if args.leds_all:
            exchange(proto.build_leds_all(parse_color(args.leds_all)))

        if args.beep:
            freq, vol, dur = (int(float(x)) for x in args.beep)
            exchange(proto.build_buzzer(freq, vol, dur), wait=0.2)

        if args.rainbow:
            step, speed, bright = (float(x) for x in args.rainbow)
            params = ail_rainbow.RainbowParams(int(step), speed, int(bright)).clamped()
            duration = float(args.duration)
            print("彩虹流水: %s | 帧率 %d Hz | 跑 %.1f 秒 (每帧 26 字节, 约 %d B/s)"
                  % (params.describe(), int(ail_rainbow.FRAME_HZ), duration, int(26 * ail_rainbow.FRAME_HZ)))
            streamer = ail_rainbow.RainbowStreamer(send=lambda f: ser.write(f))
            streamer.start(params)
            deadline = time.monotonic() + duration
            try:
                while streamer.running and time.monotonic() < deadline:
                    time.sleep(0.5)
                    st = streamer.stats()
                    print("  已发 %5d 帧, 实测 %5.1f Hz (%5.2f ms/帧)"
                          % (st["frames"], st["fps"], st["interval_ms"]))
            except KeyboardInterrupt:
                print("\n已中断")
            finally:
                streamer.stop()
            st = streamer.stats()
            print("结束: 共 %d 帧, 实测 %.1f Hz%s"
                  % (st["frames"], st["fps"], "" if not st["last_error"] else " | 错误: " + st["last_error"]))
            if st["last_error"]:
                rc = 1

        if args.monitor is not None:
            print("监视 %s 秒 (Ctrl+C 退出)..." % args.monitor)
            end = time.monotonic() + args.monitor
            while time.monotonic() < end:
                chunk = ser.read(256)
                if chunk:
                    print("%s  <-  %s" % (timestamp(), hex_str(chunk)))
    except KeyboardInterrupt:
        print("\n已中断")
    finally:
        ser.close()
    return rc


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="AI Status Lights 上位机 (不传参数 = 打开界面)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python ai_status_lights_gui.py --find\n"
               "  python ai_status_lights_gui.py --port COM11 --verify\n"
               "  python ai_status_lights_gui.py --port COM11 --leds-all FF0000\n"
               "  python ai_status_lights_gui.py --port COM11 --beep 2000 100 500\n"
               "  python ai_status_lights_gui.py --port COM11 --rainbow 40 120 100 --duration 10\n",
    )
    ap.add_argument("--scan", action="store_true", help="只列出串口")
    ap.add_argument("--find", action="store_true", help="自动查找板子(命令行)")
    ap.add_argument("--port", help="直接指定串口, 跳过自动查找")
    ap.add_argument("--baud", type=int, default=ail_link.DEFAULT_BAUD, help="波特率, 默认 115200")
    ap.add_argument("--timeout", type=float, default=0.3, help="每次探测等待时间(s), 默认 0.3")
    ap.add_argument("--retries", type=int, default=3, help="每个串口重试次数, 默认 3")
    ap.add_argument("--no-fallback", action="store_true", help="CH340 没应答时不去扫其它串口")
    ap.add_argument("--reset-pulse", action="store_true", help="探测前用 DTR/RTS 复位一下")
    ap.add_argument("--verify", action="store_true", help="发验证帧")
    ap.add_argument("--leds", help="7 个颜色, 逗号分隔, 每个 RRGGBB")
    ap.add_argument("--leds-all", help="7 颗灯同一个颜色, RRGGBB")
    ap.add_argument("--beep", nargs=3, metavar=("FREQ", "VOL", "MS"), help="蜂鸣: 频率 音量 时长ms")
    ap.add_argument("--rainbow", nargs=3, metavar=("STEP", "SPEED", "BRIGHT"),
                    help="彩虹流水: 颜色间隔(度) 流动速率(度/秒, 可负) 亮度(0-100), 跑 --duration 秒")
    ap.add_argument("--duration", type=float, default=5.0, help="彩虹流水持续秒数, 默认 5")
    ap.add_argument("--monitor", type=float, help="单纯监视串口 N 秒")
    return ap


def main() -> int:
    args = build_argparser().parse_args()

    if args.scan:
        return cli_scan()
    if args.find:
        return cli_find(args)
    if args.leds or args.leds_all or args.beep or args.rainbow or args.verify or args.monitor is not None:
        return cli_command(args)

    if not ail_link.PYSERIAL_AVAILABLE:
        print("缺少 pyserial: pip install pyserial")
        return 1
    try:
        App(initial_port=args.port).run()
    except Exception as exc:                       # 没有显示环境时别静默死掉
        print("界面启动失败: %s" % exc)
        print("可以用命令行模式: --scan / --find / --port COM11 --verify")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
