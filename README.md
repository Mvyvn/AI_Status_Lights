# AI Status Lights

基于 **CH32V003F4P6** 的状态指示灯：**7 颗 WS2812B** + **无源蜂鸣器**，只用一路串口
（`115200 8-N-1`）控制。协议很短：5～26 字节一帧，三种命令（验证 / 设置灯色 / 蜂鸣）。
仓库里还带了一个 Python 上位机：自动识别 CH340、自动探测板子、图形界面，以及 100 Hz 的彩虹流水灯。

| | |
| --- | --- |
| MCU | CH32V003F4P6（RV32EC，16 KB Flash / 2 KB RAM，48 MHz HSI） |
| 灯 | 7 × WS2812B 串联，PD3；硬件 PWM + DMA 产生 800 kHz 波形，不占用 CPU |
| 蜂鸣器 | 无源，PC7（TIM1_CH2，TIM1 完全重映射）；频率 + 音量 + 时长 |
| 串口 | PD5 / PD6（USART1），115200 8-N-1，中断接收 + 128 字节环形缓冲 |

## 硬件连接

| 引脚 | 功能 | 说明 |
| --- | --- | --- |
| PD3 | WS2812B 数据线 | TIM2_CH2，7 颗串联，800 kHz 单线归零码 |
| PC7 | 蜂鸣器 | TIM1_CH2（需 TIM1 完全重映射） |
| PD5 | USART1_TX | 115200 8-N-1 |
| PD6 | USART1_RX | 115200 8-N-1，中断接收 |

LED 编号：**索引 0 = 最靠近 MCU 的那颗**，依次往外。

## 通信协议

### 帧格式

所有命令共用同一副骨架，**固定帧头、固定帧尾、字节序小端**：

```
+--------+--------+--------+------------------+----------+--------+
| 0x55   | 0xAA   | MODE   | PAYLOAD (0..21B) | CHECKSUM | 0xBB   |
+--------+--------+--------+------------------+----------+--------+
   帧头     帧头     命令         数据            校验      帧尾
```

### 校验（CHECKSUM）

**从帧头 `0x55` 开始，累加到 payload 最后一个字节，取低 8 位**（不含帧尾 `0xBB`）：

```python
checksum = (0x55 + 0xAA + MODE + sum(payload)) & 0xFF
```

### 命令一览

| MODE | 功能 | PAYLOAD | 整帧长度 | 下位机动作 |
| --- | --- | --- | --- | --- |
| `0x00` | 验证握手 | 无 | **5 B** | 回固定 ASCII 串 `AI Status Lights OK\r\n` |
| `0x01` | 设置 7 颗灯颜色 | 21 B | **26 B** | 立即刷新灯效 |
| `0x02` | 蜂鸣器 | 5 B | **10 B** | 非阻塞响一声 |
| 其它 | — | — | — | 忽略（整帧丢弃并重新同步） |

### MODE `0x00` — 验证握手

无 payload，收到后回 **`AI Status Lights OK\r\n`**（21 字节 ASCII，含 `\r\n`）。用来确认
"这个串口后面是不是本板子"，也是上位机自动找设备的依据。

```
发送: 55 AA 00 FF BB
回复: 41 49 20 53 74 61 74 75 73 20 4C 69 67 68 74 73 20 4F 4B 0D 0A   ("AI Status Lights OK\r\n")
```

### MODE `0x01` — 设置 7 颗灯颜色

payload = 21 字节，按 **LED0 的 R,G,B → LED1 的 R,G,B → … → LED6 的 R,G,B** 排列
（每颗灯顺序是 **RGB**，驱动内部自己转成 WS2812B 的 GRB）。收到后整条灯带立即刷新，亮度就是通道值本身。

| 偏移 | 0 | 1 | 2 | 3 | 4 | 5 | … | 18 | 19 | 20 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 含义 | R0 | G0 | B0 | R1 | G1 | B1 | … | R6 | G6 | B6 |

### MODE `0x02` — 蜂鸣器

| 偏移 | 宽度 | 字节序 | 含义 | 范围 |
| --- | --- | --- | --- | --- |
| 0–1 | 2 B | 小端 | 频率 Hz | 20–20000（超出自动夹紧） |
| 2 | 1 B | — | 音量 | 0–100 |
| 3–4 | 2 B | 小端 | 时长 ms | 0–65535 |

音量 0–100 线性映射到 PWM 占空比 0–50%（无源蜂鸣器在 50% 占空比时最响），音量 0 就是静音，
时长 0 就是立刻停。响铃是**非阻塞**的，长响铃不会卡住串口收帧。

### 示例帧（校验位已算好，可直接复制）

```text
验证握手
55 AA 00 FF BB

7 灯全红
55 AA 01 FF 00 00 FF 00 00 FF 00 00 FF 00 00 FF 00 00 FF 00 00 FF 00 00 F9 BB

7 灯彩虹（LED0 红 → 橙 → 黄 → 绿 → 青 → 蓝 → 紫）
55 AA 01 FF 00 00 FF 60 00 FF FF 00 00 FF 00 00 FF FF 00 00 FF A0 00 FF F7 BB

蜂鸣 2 kHz 音量100 500ms
55 AA 02 D0 07 64 F4 01 31 BB

蜂鸣 1 kHz 音量 80 1000ms
55 AA 02 E8 03 50 E8 03 27 BB
```

### 时序与容错

- 一帧 26 字节约 2.3 ms；整帧一次发出或慢速逐字节发都可以，**帧内相邻字节间隔小于 20 ms 就不会被丢**。
- 半截帧看门狗：一帧收到一半后超过 **20 ms** 没有新字节，该半截帧丢弃，解析器回到等待帧头状态。
- 重同步：任意时刻出现 `55 AA` 就能重新对齐，`55 55 AA` 也能正确识别（第二个 `55` 当新帧头）。
- 校验错 / 帧尾不是 `0xBB` / 未知 MODE：整帧丢弃，不影响后续帧。

### 生成帧（Python）

```python
def frame(mode: int, payload: bytes = b"") -> bytes:
    body = bytes([0x55, 0xAA, mode]) + payload
    return body + bytes([sum(body) & 0xFF]) + b"\xBB"

frame(0x00)                                              # 验证
frame(0x01, bytes([255, 0, 0] * 7))                      # 7 灯全红
frame(0x02, (2000).to_bytes(2, "little") + bytes([100]) + (500).to_bytes(2, "little"))
```

### 扩展约定

新命令用没被占用的 MODE（`0x03` 起），保持"帧头 `55 AA` + 帧尾 `BB` + 累加和校验"不变。
下位机的 payload 长度表在 [`User/protocol.c`](User/protocol.c) 的 `protocol_payload_len()` 里，
未知 MODE 会被安全丢弃，所以给旧固件发新命令不会出错，只是没反应。

## 上位机

[`host/`](host/README.md)：Python + pyserial + tkinter。先扫串口（优先 CH340），再逐个发验证帧
自动找到板子，然后就能发灯效 / 蜂鸣 / 手动帧，并带一个 100 Hz 的彩虹流水灯（可调颜色间隔与流速）。

```bash
pip install pyserial
python host/ai_status_lights_gui.py            # 或双击 host/运行.bat
python host/ai_status_lights_gui.py --port COM11 --leds-all FF0000
python host/ai_status_lights_gui.py --port COM11 --rainbow 40 120 100 --duration 10
```
