# AI Status Lights

一个基于 **CH32V003F4P6** 的状态指示灯板：**7 颗 WS2812B** + **无源蜂鸣器**，只用一路串口
（`115200 8-N-1`）控制。协议很短：5～26 字节一帧，三种命令（验证 / 设置灯色 / 蜂鸣）。
仓库里同时带了一个 Python 上位机：自动识别 CH340、自动探测板子、图形界面、以及 100 Hz 的彩虹流水灯。

| | |
| --- | --- |
| MCU | CH32V003F4P6（RV32EC，16 KB Flash / 2 KB RAM，48 MHz HSI） |
| 灯 | 7 × WS2812B 串联，PD3；硬件 PWM + DMA 产生 800 kHz 波形，不占用 CPU |
| 蜂鸣器 | 无源，PC7（TIM1_CH2，TIM1 完全重映射）；频率 + 音量 + 时长 |
| 串口 | PD5 / PD6（USART1），115200 8-N-1，中断接收 + 128 字节环形缓冲 |
| 资源占用 | **Flash 6588 B / 16 KB（40.2%）、RAM 848 B / 2 KB（41.4%）** |
| 上位机 | Python + pyserial + tkinter，见 [`host/`](host/README.md) |

---

## 1. 硬件连接

| 引脚 | 功能 | 说明 |
| --- | --- | --- |
| PD3 | WS2812B 数据线 | TIM2_CH2，7 颗串联，800 kHz 单线归零码 |
| PC7 | 蜂鸣器 | TIM1_CH2（需 TIM1 完全重映射） |
| PD5 | USART1_TX | 115200 8-N-1 |
| PD6 | USART1_RX | 115200 8-N-1，中断接收 |

LED 编号：**索引 0 = 最靠近 MCU 的那颗**，依次往外。

---

## 2. 通信协议

### 2.1 帧格式

所有命令都是同一副骨架，**固定帧头、固定帧尾、字节序为小端**：

```
+--------+--------+--------+------------------+----------+--------+
| 0x55   | 0xAA   | MODE   | PAYLOAD (0..21B) | CHECKSUM | 0xBB   |
+--------+--------+--------+------------------+----------+--------+
   帧头     帧头     命令         数据            校验      帧尾
```

### 2.2 校验（CHECKSUM）

**从帧头 `0x55` 开始，累加到 payload 最后一个字节，取低 8 位。**

```python
checksum = (0x55 + 0xAA + MODE + sum(payload)) & 0xFF
```

校验**不包含**帧尾 `0xBB`。校验错、帧尾错、未知 MODE 一律丢弃整帧，解析器重新同步到下一个 `0x55`。

### 2.3 命令一览

| MODE | 功能 | PAYLOAD | 整帧长度 | 下位机动作 |
| --- | --- | --- | --- | --- |
| `0x00` | 验证握手 | 无 | **5 B** | 回固定 ASCII 串 `AI Status Lights OK\r\n` |
| `0x01` | 设置 7 颗灯颜色 | 21 B | **26 B** | 立即刷新灯效 |
| `0x02` | 蜂鸣器 | 5 B | **10 B** | 非阻塞响一声 |
| 其它 | — | — | — | 忽略（整帧丢弃并重新同步） |

### 2.4 MODE `0x00` — 验证握手

无 payload。收到后回 **`AI Status Lights OK\r\n`**（21 字节 ASCII，含 `\r\n`）。
用来确认"这个串口后面是不是本板子"，也是上位机自动找设备的依据。

```
发送: 55 AA 00 FF BB
回复: 41 49 20 53 74 61 74 75 73 20 4C 69 67 68 74 73 20 4F 4B 0D 0A   ("AI Status Lights OK\r\n")
```

### 2.5 MODE `0x01` — 设置 7 颗灯颜色

payload = 21 字节，按 **LED0 的 R,G,B → LED1 的 R,G,B → … → LED6 的 R,G,B** 排列（每颗灯顺序是
**RGB**，驱动内部自己转成 WS2812B 的 GRB）。收到后整条灯带立即刷新，亮度就是通道值本身。

| 偏移 | 0 | 1 | 2 | 3 | 4 | 5 | … | 18 | 19 | 20 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 含义 | R0 | G0 | B0 | R1 | G1 | B1 | … | R6 | G6 | B6 |

### 2.6 MODE `0x02` — 蜂鸣器

payload = 5 字节：

| 偏移 | 宽度 | 字节序 | 含义 | 范围 |
| --- | --- | --- | --- | --- |
| 0–1 | 2 B | 小端 | 频率 Hz | 20–20000（超出自动夹紧） |
| 2 | 1 B | — | 音量 | 0–100 |
| 3–4 | 2 B | 小端 | 时长 ms | 0–65535 |

音量的含义：**0–100 线性映射到 PWM 占空比 0–50%**。无源蜂鸣器在 50% 占空比时辐射能量最大，
所以音量 100 = 50% 占空比 = 最响；音量 0 直接把输出拉低（静音）。
`时长 = 0` 就是"立刻停"。

响铃是**非阻塞**的：`MODE 0x02` 只是启动音调，主循环里的 `Buzzer_Task()` 计时到期后关掉，
所以长响铃不会卡住串口收帧（响铃期间还能继续收灯效帧）。

### 2.7 示例帧（校验位已算好，可直接复制）

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

### 2.8 时序与容错

- **波特率 115200 8-N-1**，无流控。一帧 26 字节约 2.3 ms。
- 帧内字节可以**连续发送**（一次 `write` 整帧即可），也可以慢速逐字节发；**帧内相邻字节间隔小于 20 ms 就不会被丢掉**。
- 半截帧看门狗：一帧收到一半后**超过 20 ms 没有新字节**，该半截帧被丢弃，解析器回到等待帧头状态 ——
  这样上位机中途重启/拔插也不会把后续帧当成 payload 吞掉。
- 重同步：任意时刻只要出现 `55 AA` 就能重新对齐；`55 55 AA` 也能正确识别（第二个 `55` 当作新帧头）。
- 帧尾不是 `0xBB`：整帧丢弃（但 `0xBB` 只影响这一帧，解析器已经回到等待帧头状态）。
- unknown MODE：立即丢弃并重新同步，不会把它的"payload"误当成数据。

### 2.9 参考实现

**Python（上位机/脚本）**

```python
def frame(mode: int, payload: bytes = b"") -> bytes:
    body = bytes([0x55, 0xAA, mode]) + payload
    return body + bytes([sum(body) & 0xFF]) + b"\xBB"

frame(0x00)                                              # 验证
frame(0x01, bytes([255, 0, 0] * 7))                      # 7 灯全红
frame(0x02, (2000).to_bytes(2, "little") + bytes([100]) + (500).to_bytes(2, "little"))
```

**C**

```c
static void send_frame(uint8_t mode, const uint8_t *payload, uint8_t len)
{
    uint8_t buf[26], i, sum;

    buf[0] = 0x55; buf[1] = 0xAA; buf[2] = mode; sum = 0x55 + 0xAA + mode;
    for(i = 0; i < len; i++) { buf[3 + i] = payload[i]; sum += payload[i]; }
    buf[3 + len] = sum; buf[4 + len] = 0xBB;
    uart_send(buf, 5 + len);
}
```

### 2.10 扩展约定

- 新命令一律用**没被占用的 MODE**（`0x03` 起），保持"帧头 55 AA + 帧尾 BB + 累加和校验"不变；
- 下位机的 payload 长度表在 [`User/protocol.c`](User/protocol.c) 的 `protocol_payload_len()` 里，
  未知 MODE 会被安全丢弃，所以**给旧固件发新命令不会出错**，只是没反应。

---

## 3. 快速上手

### 3.1 编译

**MounRiver Studio 2**：打开 `Project/AIStatusLights` 工程直接 Build。

**命令行**（已在本机验证，WCH RISC-V GCC 8.2.0 + Make）：

```powershell
$tc = "D:\mountriver\MounRiver_Studio2\resources\app\resources\win32\components\WCH\Toolchain\RISC-V Embedded GCC\bin"
$mk = "D:\mountriver\MounRiver_Studio2\resources\app\resources\win32\others\Build_Tools\Make\bin"
$env:PATH = "$tc;$mk;$env:PATH"
make -C obj all
```

产物：`obj/AIStatusLights.hex` / `.elf` / `.lst` / `.map`。

> 必须带目标名 `all`：直接 `make -C obj` 的默认目标会落到某个 `.o` 上，不会完整构建。

### 3.2 烧录

用 WCH-Link 在 MounRiver Studio 里下载，或直接把 `obj/AIStatusLights.hex` 烧进去。
上电后串口会打印：

```
SystemClk:48000000
ChipID:xxxxxxxx
AI Status Lights: 7 x WS2812B on PD3, buzzer on PC7, UART1 115200 8-N-1 (PD5/PD6)
Frame sent
```

### 3.3 上位机（可选）

```bash
pip install pyserial
python host/ai_status_lights_gui.py          # 或者双击 host/运行.bat
```

界面里的 ①②③ 就是流程：**① 扫描串口**（描述里带 CH340/CH341 的排最前）→
**② 自动查找设备**（在这些口上逐个发 `55 AA 00 FF BB`，收到 `AI Status Lights OK` 的就是板子，命中即停）→
**③ 打开串口**。

功能：`灯效`（7 行取色器 + 待发帧预览）、`彩虹流水`（100 Hz 流式下发 MODE `0x01`，
可调颜色间隔 / 流动速率 / 亮度）、`蜂鸣器`（频率 / 音量 / 时长 + 快捷档位）、
`手动 / 监视`（任意 HEX 收发、带时间戳的接收区、按协议解析）。命令行用法见 [`host/README.md`](host/README.md)。

```bash
python host/ai_status_lights_gui.py --find                      # 自动找板子
python host/ai_status_lights_gui.py --port COM11 --verify       # 发验证帧
python host/ai_status_lights_gui.py --port COM11 --leds-all FF0000
python host/ai_status_lights_gui.py --port COM11 --rainbow 40 120 100 --duration 10
```

上位机自带的离线自测（不需要板子，用假串口 + 虚拟板子跑真实源码，**125 项检查**）：

```bash
python host/test_host_tools.py
```

---

## 4. 代码结构

```text
Project/AIStatusLights/
├─ User/                  应用层
│  ├─ main.c              初始化 + 主循环（1 ms 节拍）
│  ├─ ws2812.c / .h       WS2812B 驱动（TIM2_CH2 PWM + DMA1_Channel2）
│  ├─ buzzer.c / .h       蜂鸣器驱动（PC7 / TIM1_CH2）
│  ├─ uart.c / .h         USART1 驱动（中断接收环形缓冲）
│  └─ protocol.c / .h     协议解析与分发  ← 帧格式见 §2
├─ host/                  Python 上位机（GUI + 命令行 + 自测）
├─ Core/ Peripheral/      WCH 官方的核心头文件与外设库
├─ Startup/ Ld/ Debug/    启动文件、链接脚本、printf/延时
├─ obj/                   MRS 构建目录（makefile + 产物）
└─ README.md
```

### 对外接口

```c
/* 灯 */
void    WS2812_Init(void);
void    WS2812_SetPixel(uint8_t index, uint8_t r, uint8_t g, uint8_t b);
void    WS2812_SetAll(uint8_t r, uint8_t g, uint8_t b);
void    WS2812_Clear(void);
uint8_t WS2812_Refresh(void);        /* WS2812_OK / WS2812_ERR_TIMEOUT */

/* 蜂鸣器 */
void    Buzzer_Init(void);
void    Buzzer_Start(uint16_t freq_hz, uint8_t volume);
void    Buzzer_Stop(void);
void    Buzzer_Beep(uint16_t freq_hz, uint8_t volume, uint16_t duration_ms);  /* 非阻塞 */
void    Buzzer_Task(void);           /* 主循环约 1 ms 调用一次 */
uint8_t Buzzer_IsBusy(void);

/* 串口 */
void     UART_Init(uint32_t baudrate);
void     UART_SendBuffer(const uint8_t *data, uint16_t len);
void     UART_SendString(const char *str);
int      UART_ReadByte(uint8_t *data);

/* 协议 */
void     Protocol_Init(void);
void     Protocol_Task(void);        /* 主循环反复调用 */
```

---

## 5. 实现要点

### WS2812B（PD3，TIM2_CH2 + DMA）

用纯硬件产生 800 kHz 波形，不受中断和 CPU 执行时间影响：

- TIM2_CH2 PWM 模式 1，PSC = 0，ARR = 59 → 位周期 60 / 48 MHz = **1.25 µs**
- 每个位的高电平时间由 DMA 写入 CCR：`19` → 0 码（396 ns 高），`38` → 1 码（792 ns 高）
- DMA 通道 **DMA1_Channel2**（CH32V003 上就是 TIM2 更新事件 TIM2_UP 的 DMA 请求线），MSB 先行、GRB 顺序
- 缓冲区比数据多一项 `0`：帧结束后 CCR = 0，数据线保持低电平，天然形成下一帧的复位时间
- 每帧 7×24+1 = **169 个位单元 ≈ 211 µs**，CPU 只等一个完成标志

时序保护：启动 DMA 前先**停表 + 清 UIF**（`TIM_TimeBaseInit()` 和上一帧的更新事件都会置位 UIF，
若在 UIF 挂起时使能 DMA 请求会立即多搬一次，整帧位移 1 位、收尾项搬不出去）；DMA 完成等待有上限，
超时返回 `WS2812_ERR_TIMEOUT` 并安全停机。

### 蜂鸣器（PC7，TIM1_CH2）

```
频率 = 48 MHz / ((PSC + 1) × (ARR + 1))        音量 → 占空比
```

自动选取**最小预分频**，在保证 ARR ≤ 65535 的前提下拿到最好的占空比分辨率；频率限制 20–20000 Hz
（20 Hz 时 PSC = 36、ARR = 64863）。

### 串口（PD5/PD6，USART1）

TX 阻塞发送，**RX 中断 + 128 字节环形缓冲**，主循环忙时也不丢字节；中断里先读 `STATR` 再读 `DATAR`，
同时清掉 RXNE 和溢出标志，缓冲区满时丢弃新字节。

---

## 6. 验证情况

- 交叉编译 clean 构建通过，**无警告**。
- **反汇编核对**：PD3 = AF_PP、TIM2 ARR = 59、CH2 = PWM1 且 CCR 预装载关闭、DMA1_Channel2 目的地址
  = `TIM2->CH2CVR`(0x40000038)、长度 169、半字/递增/正常模式/最高优先级；`USART1_IRQHandler`
  已正确填入中断向量表；`Buzzer_Init` 只对 `AFIO_PCFR1` 执行 `lw / ori 0xC0 / sw`，即只写 TIM1_REMAP 域。
- **WS2812**：用桩函数在 PC 上编译真实驱动源码并回读 DMA 位流，**215 项断言**通过，解出的 GRB 字节流与
  7 种颜色完全一致，19/38/60 tick 全部在 WS2812B 规格内（含 DMA 超时分支）。
- **协议 + 蜂鸣器**：用桩函数在 PC 上编译真实的 `protocol.c` / `buzzer.c`，逐字节喂帧验证了三种模式的
  解析与分发、校验和/帧尾错误丢弃、垃圾数据重同步、`55 55` 重同步、半截帧 20 ms 超时丢弃、
  连续慢速发送不被误丢，以及蜂鸣器边界（20 Hz / 20 kHz / 音量 0 / 时长 0）和 500 ms 倒计时精确关闭。
- **上位机**：125 项离线检查（三种帧的字节与 §2.7 示例逐字节一致、CH340 识别与排序、自动查找的各种
  分支、彩虹流水的颜色数学与 100 Hz 实测帧率、GUI 冒烟）。

---

## 7. 说明与已知取舍

1. **时钟用 HSI 48 MHz**：原工程是 `48MHz_HSE`。CH32V003 板子若没焊晶振，HSE 启动会超时并静默继续跑
   HSI，而 `SystemCoreClock` 仍报 48 MHz，所有定时慢一倍（WS2812B 会不亮或乱闪）。改 HSI 后有无晶振都正常。
2. **校验位取累加和低 8 位**（需求里只说"校验位"，未指定算法）；要用异或或 CRC8，改 `protocol.c` 里
   `ST_CHECKSUM` 分支一处即可。
3. **频率/时长为 16 位小端、音量为 8 位**（需求未指定宽度）。
4. **MODE `0x00` 的回复内容是自定义的** `AI Status Lights OK\r\n`。
5. **音量 → 占空比 0–50%**：想改成 `占空比 = 音量%`，把 `buzzer.h` 的 `BUZZER_DUTY_FULL_SCALE`
   从 `200` 改成 `100`（注意那样音量 100 会变成直流、反而无声）。
6. **蜂鸣器非阻塞**：需要主循环持续调用 `Buzzer_Task()`（`main.c` 已按约 1 ms 调用）。
7. **TIM1 重映射没用 `GPIO_PinRemapConfig()`**：WCH 的 `GPIO_FullRemap_TIM1` 助手除了写
   `AFIO_PCFR1[7:6]`（TIM1_REMAP），还会把 `AFIO_PCFR1[26:24]`（SWJ_CFG）置成保留值 `0b111`。
   官方例程也这么用且能正常下载，但那是未定义值；这里改成直接写 TIM1_REMAP 域。
8. **彩虹流水由上位机实现**（100 Hz 下发 MODE `0x01`，固件不用改）：PC 不在、卡住或退出时，灯就停在
   最后一帧。要脱离 PC 自己流动，得在固件里加一个动画 MODE，并且先做出一个真正的毫秒时基
   （现在的 `Delay_Ms()` 只能"等"，测不出已经过了多久）。

## 8. 下一步可扩展

- 状态映射：空闲 / 运行 / 报警 对应不同灯色、呼吸、闪烁
- 固件内动画：新增 MODE `0x03`（色相步进 + 流速 + 亮度），10 ms 节拍出帧，通过串口改参数
- 用 MODE 增加"整机状态"命令，把颜色/蜂鸣的映射交给固件

## License

未指定。如需开源，建议补一个 `LICENSE`（例如 MIT）。
