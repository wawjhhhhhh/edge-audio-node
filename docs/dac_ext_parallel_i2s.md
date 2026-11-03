# 方案 B：把 I²S 复制一路到 40PIN 扩展口（给外接 ES9038Q2M）

> 状态：**已应用到 RTL/XDC 并重建**（2026-10-27 21:30）
> 实际引脚：`aud2_bclk=W11`、`aud2_lrc=Y13`、`aud2_dacdat=Y11`（全 BANK13、全空闲、全 3.3V）
> 前提事实（已查证，见 `材料库/02-硬件取证/2026-10-27_ES9038Q2M解码板_核验与上板指南.md` §10）：
> 领航者有 J3/J4 两个 40PIN 扩展口，每口 36 IO + 3.3V + 5V + 2GND；**BANK13/34/35 全接 3.3V**，IO 均为 LVCMOS33。
> 实拍丝印读出的标号全是 BANK13 球号 → **底部那排 40PIN 就是 J4**。

---

## 0. 为什么改动能这么小

`aud_bclk / aud_lrc / aud_dacdat` 这三个网已经在 `eth_audio_top.v` 的端口表里、
由 BD 里的 `audio_axis_core` 驱动。要做的只是**把同样的三个网再扇出一份到新引脚**——
纯 `assign`，不经过任何逻辑、不产生额外延迟、不碰 BD、不碰 `audio_axis_core`。

因此**不需要重新生成 BD**，用 `rebuild_audio_fast.tcl` 快路径重建即可（约 5 分钟），
也就不会踩 `create_project` 把旧工程删掉那个坑。

---

## 1. RTL 改动（`work/eth_audio/rtl/eth_audio_top.v`，共 6 行）

### 1.1 端口表：在 `aud_sda` 那一行后面加

```verilog
    output wire        aud_scl,        // E18
    inout  wire        aud_sda,        // F17

    // 复制给外接 DAC（方案 B：40PIN 扩展口）。与 aud_* 同一网络，纯扇出。
    output wire        aud2_bclk,      // 扩展口
    output wire        aud2_lrc,       // 扩展口
    output wire        aud2_dacdat     // 扩展口
);
```

### 1.2 模块末尾（`IOBUF u_mdio (...)` 之后、`endmodule` 之前）加

```verilog
    /* 外接 ES9038Q2M 用的 I2S 副本。三个信号与板载 ES8388 完全同源，
       所以两个 DAC 收到的是同一路数据 —— 这就是并联 A/B。
       纯 wire 扇出：不加寄存器、不改时序、对原有链路零影响。 */
    assign aud2_bclk   = aud_bclk;
    assign aud2_lrc    = aud_lrc;
    assign aud2_dacdat = aud_dacdat;
```

> 为什么可以直接 `assign` 一个 output：`aud_bclk` 等已经是模块内的 wire（由 `u_bd` 驱动），
> 再连一个 output 只是多一个负载，Vivado 会自动复制驱动，**不会**产生"多驱动"错误。

---

## 2. XDC 改动（`work/eth_audio/bd/eth_audio_pins.xdc`，**已加在文件末尾**）

```tcl
# ------------------------------------------------- ext DAC (parallel I2S out)
set DAC_BCK W11
set DAC_LRC Y13
set DAC_DAT Y11

set_property -dict [list PACKAGE_PIN $DAC_BCK IOSTANDARD LVCMOS33] [get_ports aud2_bclk]
set_property -dict [list PACKAGE_PIN $DAC_LRC IOSTANDARD LVCMOS33] [get_ports aud2_lrc]
set_property -dict [list PACKAGE_PIN $DAC_DAT IOSTANDARD LVCMOS33] [get_ports aud2_dacdat]
```

> **注释必须 ASCII**：本项目里 XDC/Verilog 一律不许有中文（Vivado 的 Tcl 解析会出问题）。
> 想换引脚只改 `set DAC_*` 三行的值，其它不用动。


---

## 3. 重建与验证

```bash
# 1) 重建（不重新生成 BD，约 5 分钟）
cd /d/vitis_tmp
python run_vivado.py "D:/vitis_tmp/rebuild_audio_fast.tcl"     # 注意：脚本在 vitis_tmp，不在 vivado_tmp

# 2) 看时序是否仍然收敛（脚本末尾有 WNS<0 就报错的检查，报错就是没收敛）
grep -i "WNS\|timing not met" /d/vivado_tmp/vlog.txt | tail -5

# 3) 烧写
python run_xsdb.py "D:/vitis_tmp/flash_ea_quick.tcl"

# 4) 发 440 Hz 自检音，耳机插 DAC 板
python "C:/Users/温先生/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe" D:/vitis_tmp/ctl.py t
```

> 注意：第 1 步用的 `rebuild_audio_fast.tcl` **不会**重新生成 BD，也不会 `file delete -force` 工程，
> 所以这次改动只能改 RTL/XDC —— 正好是我们要改的东西。

---

## 4. 可用引脚查表（BANK 13/34/35，已排除本工程占用的 24 个）

原始数据来源：`D:\xilinx\2026.1\2026.1\data\ip\xilinx\mig_7series_v4_2\data\fpga_tlib\zynq\7z\xc7z020clg400_pkg.xml`
（Vivado 自带的封装描述，含每个球的 bank 与 IOBLOC），解析结果存为
`材料库/02-硬件取证/xc7z020clg400_引脚bank表.csv`。

| BANK | 归属 | 空闲球号 |
|---|---|---|
| **13** | **J4 扩展口**（7020 独有） | `T5 T9 U5 U7 U8 U9 U10 V5 V6 V7 V8 V10 V11 W6 W8 W9 W10 W11 Y6 Y7 Y8 Y9 Y11 Y12 Y13` |
| **34** | J3 扩展口 | `N17 N18 N20 P14 P15 P16 P18 P19 P20 R14 R16 R17 R18 R19 T10 T11 T12 T14 T15 T16 T17 T20 U12 U13 U14 U15 U17 U18 U19 U20 V12 V13 V15 V16 V17 V18 V20 W13 W14 W15 W16 W18 W19 W20 Y14 Y16 Y17 Y18 Y19` |
| **35** | J3/J4 + 板载外设（eth/audio/uart 占 23 个） | `F16 G14 G18 G19 G20 H15 H18 H20 J14 J16 J18 J19 J20 K14 K16 K19 L14 L15 L16 L17 L19 M14 M15 M19 M20 N15 N16` |

**选脚三条建议：**
1. **优先 BANK 13（J4）**——它只被扩展口占用，板载外设一个都没用，冲突概率最低；
2. 三个信号**尽量选同一排、物理相邻**，方便扩展板上走等长短线；
3. 顺手在扩展板上给这三根**各串 33 Ω**（见核验文档 §10.3）。

---

## 5. 应用后我需要记录的三件事

1. 时序是否仍收敛（`WNS ≥ 0`），资源增量（应该只有 3 个 OBUF，LUT 不变）；
2. 三个引脚的实际球号（写进本文档和 XDC 注释）；
3. 440 Hz 验证结果（听感 + 是否需要改 `IOSTANDARD`）。

---

## 6. 上板实测结果（2026-10-27 22:35，已出声）

### 6.1 时序与资源
- 重建完成：`WNS = +0.357022 ns`，与改动前**完全一致** → 三根输出是纯扇出，零时序影响。
- 三个引脚均落 BANK13、`LVCMOS33`、`OUTPUT / FIXED`（`impl_1/eth_audio_top_io_placed.rpt` 实证）：
  `W11 → aud2_bclk` / `Y13 → aud2_lrc` / `Y11 → aud2_dacdat`。

### 6.2 关键发现：DAC 板丝印的 DATA / LRCK 与实际脚位相反

按丝印直连（`BCK→W11`、`LRCK→Y13`、`DATA→Y11`）**无声**；
把 **Y13 与 Y11 两根对调后立即出声**。

#### 证据分级（2026-10-28 02:10 修订）

**A. 已确定（器件级实据，不依赖听觉）** —— FPGA 侧送出的是什么：

`impl_1/eth_audio_top_io_placed.rpt` 是一张**按球号排序的完整封装表**（第一列含 G3/G4…/VCCPINT/GND
等全部球，第二列才是信号名，列序由"电源球那行为空"这一事实锁定，不是靠相邻猜测）：

| 球号 | 端口 | IO 站点 | BANK |
|---|---|---|---|
| `W11` | `aud2_bclk` | `IO_L18P_T2_13` | 13 |
| `Y11` | `aud2_dacdat` | `IO_L18N_T2_13` | 13 |
| `Y13` | `aud2_lrc` | `IO_L20N_T3_13` | 13 |

配合 `eth_audio_top.v:72-74` 的 `aud2_lrc=aud_lrc` / `aud2_dacdat=aud_dacdat`（无交叉），
可得：**Y13 上是 48 kHz 帧时钟，Y11 上是串行数据，W11 上是 3.072 MHz 位时钟。**

**B. 已确定（由 B 段推出的排他逻辑）** —— 有一侧的 LRCK/DATA 命名是反的：

把 DATA 与 LRCK 互换后仍能听到**干净的 440 Hz**。ES9038Q2M 不会自动交换"数据"与"帧时钟"：
帧时钟一旦接到伪随机码流，芯片连 I²S/LJ/RJ 都判不出来，结果是静音或乱码，不可能是一段干净的
正弦。所以"出声的那一刻，三个脚各自拿到了正确信号"这一条是硬的。

**C. 推断（未测量，可能错）** —— 反在哪一侧：

我当时据 B 段 + 现场接线报告，判成"**商家丝印印错了**"，并把 XDC 保持语义正确。
这一条**只是推断**：没有示波器/逻辑分析仪量过任何一个焊盘，而且它还有两个同样能解释现象的
替代原因：
1. **接触不良**：那次"A/B"是靠**移动两根线**完成的，动线本身就会修复冷焊点/松插针 →
   测到的可能是"接点好了"，而不是"线序对了"；
2. **标签与焊盘的对应看错一格**：那排是 `3V3_OUT / GND / DATA / LRCK / BCK / MCK` 六个焊盘，
   丝印是整排印的，人工数格容易错位。

**D. 结论的落点不变，但措辞要降级**：`§6.3` 的接线表是**实测有效**的接法（照它接能出声），
但"错在商家丝印"这句话应写成"**错在某一侧的 LRCK/DATA 对应关系，待用仪器定案**"。

#### 6.2b 定案办法（按代价从低到高）

1. **示波器/逻辑分析仪（10 秒，最硬）**：量 DAC 板丝印 `BCK` 与 `LRCK` 两个焊盘。
   丝印 `BCK` 上应是 3.072 MHz、丝印 `LRCK` 上应是 48 kHz 方波。都对 → 丝印正确，**错在我们**；
   若两者对调 → 丝印真的反了。
2. **灯语三拔法（不用仪器，2026-10-28 02:22 由现场观察校准过，见 §6.2c）**：
   依次拔掉一根线、记一次灯态，再插回。原理是"锁定需要两根时钟、不需要数据线"，
   所以**对灯不起作用的那一根就是 DATA**。
3. **引脚身份证固件（5 分钟快路径重建，最不依赖运气）**：把 `aud2_bclk/lrc/dacdat` 分别改成
   1 Hz 方波 / 2 Hz 方波 / 常低，用万用表 DC 挡或一颗 LED 就能把每个焊盘认出来，一次定案。
   缺点是会中断正常音频，需重编重烧。

#### 6.2c 那颗灯不能当判据 —— 它的语义在同一晚被推翻三次

时间线（全部来自现场观察，不是推测）：

| 时刻 | 观察 | 我当时的结论 |
|---|---|---|
| 10-27 01:47 | 一推流灯就灭 | "灭 = 有音频" |
| 10-28 02:00 | "灯亮的时候板子才工作" | 推翻上一条 → "亮 = 正常" |
| 10-28 02:22 | 静音时插上 BCK 灯亮、拔掉变暗 | "灯 = 锁定指示" |
| 10-28 02:31 | **一出声灯变暗，一停声灯变亮** | 上面又都不对了 |

把这四行放一起看，唯一自洽的结论是：**"灯暗"至少有两种成因** ——
① 正在出声（02:31）；② 根本没锁上（02:22）。两种状态外观相同，
所以任何"靠灯判断"的推理都会在某个时刻翻车。今晚翻了三次，根子在这里。

**因此：把灯从判据里剔除。** §6.2b 的"灯语三拔法"只有在**完全静音**（不推流、自检音关闭）
且**先确认灯亮=锁定**的前提下才可用；做不到这两点就不要用，改用示波器或下方固件法。

顺便一个不能排除的物理解释：亮度跟的是**电流**，而不是逻辑。出声时耳放（TPA6138）开始
驱动负载 → 某路电源轻微下降 → 灯变暗；停声回升。若真是这样，那说明**耳放是好的、在驱动**，
问题落在耳机/插孔/音量，而不是数字侧。这条可以立刻用"换一副 16~32 Ω 的低阻耳机"来试。



### 6.3 由此定稿的接线表（以实测为准）

| DAC 板丝印 | 接扩展口球号 | FPGA 侧真实信号 |
|---|---|---|
| `GND` | 任意 `GND`（建议两根） | —— |
| `BCK` | `W11` | `aud2_bclk` |
| `LRCK` | **`Y11`** | `aud2_dacdat`（即 FPGA 的 DATA） |
| `DATA` | **`Y13`** | `aud2_lrc`（即 FPGA 的 LRCK） |
| `VCC` | 不接（DAC 板自带 Type-C 供电） | —— |

> 画扩展板时**按"真实信号"走线**：`Y13` 要连到 DAC 板丝印 `DATA` 的位置、
> `Y11` 要连到丝印 `LRCK` 的位置。**不要按丝印名字直连。**

### 6.4 并联 A/B 已成立
同一条 `aud_dacdat` 同时喂板载 ES8388 与外接 ES9038Q2M，**两个耳机口同时出声，RTL 零改动**。
做 A/B 对比时要一次只戴/插一个口，否则听到的是两路混合，判断不可靠。

### 6.5 用户复核（2026-10-27 22:38）

- DAC 板上标着 **`LRCK`** 的那根线，实测插在 **`Y11`**（= `aud2_dacdat`）→ 与 §6.3 表一致；
- 听到的是**干净的连续 440 Hz**，无沙沙声、无断续 → 数字链路与模拟侧均正常，不存在其它错位。

→ **本文档 §6.3 的接线表即为定稿。** 详细版（含丝印错位成因、状态灯当探针的用法）
见 `材料库/02-硬件取证/2026-10-27_ES9038Q2M解码板_核验与上板指南.md` §11。
