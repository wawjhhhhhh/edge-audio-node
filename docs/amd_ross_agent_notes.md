# 用 AMD Ross 智能体辅助设计：踩过的坑、根因与规避动作

> 本文记录本作品开发过程中，使用 **AMD Ross™ Agentic AI Assistant**（智能体辅助设计套件）时实际遇到的问题。
> 每条都给出：**现象 → 根因 → 规避动作 → 本仓库里的证据**。证据列的文件都在本工程包内，可逐条复核。
> 读这份文档不需要懂我们的电路，只需要知道"哪些结论不能凭工具口头通过就相信"。

## 0. Ross 是什么，我们是怎么用的

Ross 是 AMD 2026-09-30 发布的嵌入式开发智能体助手，由四个部件组成：

| 部件 | 作用 | 我们实际用到的方式 |
|---|---|---|
| MCP servers | 把 AI 客户端接到 AMD 工具上（Vivado / ChipScope / Power Design Manager / Vitis / Vitis HLS / Vitis AI / ROCm / Ryzen AI） | 让智能体直接 `start` Vivado、跑 Tcl、读报告、起 xsdb 烧板 |
| AMD Knowledge Base | AMD 校验过的向量库（UG/PG/白皮书/应用笔记/问答） | 查 7 系列器件手册、AXI DMA、时序方法论（UG906） |
| Agent skills | 专家写的 markdown 工作流（时序优化、HLS 改造等） | 用时序方法论检查、RTL lint、IP 配置类技能 |
| Design examples | 可直接跑的完整例子 | 对照 BD 结构与约束写法 |

Ross 的客户端是开放可替换的：我们这一轮把它接在 **DeepSeek Harness** 上（也就是这套会话），
由它承担"读日志 → 下判断 → 改文件 → 重跑工具"的循环。

⚠️ AMD 在发布说明里明确写过一条，也是本文的前提：
**Ross 会建议、会执行，但输出与建议必须由人复核后才能使**（原文 footnote：users remain responsible for reviewing generated outputs, recommendations, and proposed actions）。

> 出处：AMD Newsroom, *AMD Brings the Power of Agentic AI to Embedded Design and Development Cycle*, 2026-09-30
> <https://newsroom.amd.com/news/amd-ross-agentic-ai-embedded-design-development/>
> 产品页：<https://www.amd.com/en/products/software/ross-agentic-ai.html>

---

## 1. 工具链与环境类（5 条）

### P1　`xsct` 在 Vitis 2026.1 里已经没有了，脚本直接报"找不到命令"

- **现象**：老的"xsct 烧板"写法在新装环境里第一条命令就失败，而且报错里不提示替代品。
- **根因**：Vitis 2026.1 移除了 `xsct`，命令行调试统一走 **`xsdb`**。很多网上教程（包括我们自己的旧脚本）还是 `xsct` 版本。
- **规避**：烧写/调试脚本一律改用 `xsdb.bat`；在脚本里把解释器路径写成一个变量，换机器只改一处。
- **证据**：本仓库 `fpga/build/run_xsdb.py` 与 `fpga/build/flash_bit.tcl`；`host/firmware/README_firmware.md` 里写了这条。

### P2　`rrd pc` 报 `no register match: pc`

- **现象**：只想读一下 PC 看板子是不是还活着，命令报没有匹配的寄存器。
- **根因**：`targets APU*` 选中的是 APU 这个组，不是一个具体的 ARM 核，寄存器读取需要落到具体 target 上。
- **规避**：先 `targets -set -nocase -filter {name =~ "ARM*#0"}`，再 `rrd pc`。
- **证据**：`fpga/build/run_xsdb.py` 里的 JTAG 体检流程（选 target → 读 pc → 读固定地址）。

### P3　同一份 Tcl，在 GUI 模式和 TCL 模式下的输出不一样

- **现象**：`puts` 打的东西在 GUI 会话里抓不到；`report_*` 不加 `-return_string` 就"什么都没返回"。
- **根因**：GUI 会话经 webserver 走 HTTP，只回最后一个表达式的值；非 GUI 走 stdin/stdout，`puts` 与返回值都抓得到。
- **规避**：脚本里不要依赖 `puts` 传结果——要么让最后一行是表达式，要么 `report_* -return_string`，要么干脆把报告写成文件再读文件。
- **证据**：`fpga/build/run_vivado.py`（统一用 `-notrace -mode batch -source`，输出全部进日志文件）。

### P4　构建脚本里写死了绝对路径，换台电脑就编不过

- **现象**：换机器后 PS 侧编译直接失败，报找不到工程目录。
- **根因**：`build_app_ea.py` 里是 `D:\vitis_tmp\ws_ea` 这样的绝对路径；这在"自己机器上能跑"的语境里毫无问题，但在别人机器上必挂。
- **规避**：路径一律走参数或环境变量；交付文档里把这条单独标出来。
- **证据**：`host/firmware/README_firmware.md` 明确写了"换机要改这一行"；包内同时提供已验证的 `app_ea_fpu.elf`，评审可以完全不编译就上板。

### P5　"命令跑不起来"其实是 Python 运行时选错了

- **现象**：控制台一起来就 `ModuleNotFoundError: No module named 'numpy'`。
- **根因**：机器上装了多个 Python，默认那个是 3.8 且没装科学计算包。
- **规避**：脚本里先校验解释器（版本 + 关键包），失败时打印"请用哪个解释器"；交付包带 `requirements.txt` 和一键启动脚本。
- **证据**：`host/deploy/run_console.cmd`（先验 numpy 再起服务）、`host/deploy/requirements.txt`。

---

## 2. 综合与实现类（6 条）——最贵的一类：工具说"成功"，板子是死的

### P6　WNS 是正的、时序"全过"，但功能完全不对

- **现象**：实现报告 `WNS +0.357 ns`、`0 failing endpoint`、布线 100% 通过，烧进板子后麦克风一个包都没有。
- **根因**：设计里有一整个时钟域**没有 `create_clock`**（PL 产生的 `aud_bclk`）。Vivado 不会为此报错，只是把那 16923 个触发器当作"无时钟、不用分析"，于是这块逻辑的时序**根本没被检查**。它过不过，取决于布线抽签。
- **规避**（这条现在是我们所有项目的红线）：
  1. 看 WNS 之前先看 `check_timing`：`no_clock` 必须为 **0**；
  2. 自己产生的时钟（分频、门控、PL 生成的 I2S 时钟）必须显式 `create_clock`，并写进约束文件而不是"临时补一下"；
  3. `report_clock_utilization` 里出现"驱动了上千个引脚却没有时钟定义"的管脚，直接当缺陷处理。
- **证据**：`metrics/logs/timing_axis.log`、`docs/known_issues.md` 第 1 条（16923 pins under `aud_bclk_reg/Q`）、`fpga/report/` 下的 `check_timing` 与 `clock_utilization` 报告。

### P7　TIMING-17 只显示 1000 条，别把它当"只有 1000 个问题"

- **现象**：方法论报告里 `TIMING-17 Non-clocked sequential cell` 正好是 1000。
- **根因**：DRC 输出有截断上限，1000 是上限不是真值；真实数量要看百分比列。
- **规避**：读 DRC 一律"看百分比 + 看被截断标记"，并回到 `check_timing` 去数实际受影响的引脚数。
- **证据**：`fpga/report/eth_audio_top_methodology_drc_routed.rpt`（`1000` 条 + `33.33%` 一列）。

### P8　`Synth 8-4767` / `8-13159`：RAM 没进 BRAM，被拆成 16384 个触发器

- **现象**：综合日志里两条 WARNING，综合"成功"，但资源表里 Block RAM = 0、触发器暴涨到 33700（占 31.67%）。
- **根因**：我们那个异步 FIFO 的存储体写在 `always @(posedge wclk or negedge wrst_n)` 里。**带异步复位的 always 块里的数组，综合器不会推断成 RAM**，只能展开成寄存器。`8-13159` 那条 `dissolved into (16384) registers bits` 就是明说。
- **规避**：
  1. 存储器写块里**不要**出现复位（复位只作用于指针）；
  2. 加 `(* ram_style = "block" *)` 并核对资源表确实出现 BRAM；
  3. 综合日志做关键词扫描：`8-4767`、`8-13159`、`dissolved into` 出现即视为失败。
- **证据**：`metrics/logs/audio_core_ooc_synth.log`（原文两条 WARNING）、`fpga/src/rtl/afifo.v`、`skill/scripts/check_timing_health.py`（自动扫这两条）。

### P9　Slice 用到 99.40% 还能过，但"能过"不等于"能复现"

- **现象**：`Slice 13220 / 13300 = 99.40%`，布线依然成功、时序依然收敛。
- **根因**：布局器在极限占用下仍可能凑出可行解，但换一次综合种子、换一版工具，结果就可能完全不同；再叠加上面 P6（无时钟域不参与分析），就成了"同一个工程反复重建、有时好有时坏"。
- **规避**：把资源红线写进流程——**Slice > 90% 就要停下来**（要么降逻辑，要么换器件），不要靠"这次过了"交付。
- **证据**：`fpga/report/eth_audio_top_utilization_placed.rpt`。

### P10　BD 的参数会覆盖 RTL 的默认值，只看 `.v` 会读出错误结论

- **现象**：`audio_axis_core.v` 里明明写着 `AW=4 / LOOPBACK=1 / BEATS_PER_PKT=48`，实际构建出来是 `AW=9 / LOOPBACK=0 / BEATS=480`。
- **根因**：该模块在 Block Design 里被当作 module reference 实例化，BD 侧的 `CONFIG.*` 属性覆盖了 RTL 默认值。
- **规避**：写文档、算资源、判时序之前，先回读**实际生效**的参数（BD 属性或综合日志里的 `Parameter X bound to: Y`），不要引用源码默认值。
- **证据**：`fpga/build/build_eth_audio.tcl`（`foreach {p v} [list WL 16 BEATS_PER_PKT 480 AW 9 LOOPBACK 0]`）。

### P11　构建脚本会整份重写约束文件，手改的约束下次就没了

- **现象**：手补进 `.xdc` 的时钟组约束，下次跑构建脚本后"又不见了"。
- **根因**：构建脚本里有一段把 XDC 整个重写成固定内容（`build_eth_audio.tcl` 第 322–350 行），任何手改都会被覆盖。
- **规避**：约束只能改**脚本里的模板**，不能改产物；改完立刻重新构建一次并回读约束文件确认生效。
- **证据**：`fpga/build/build_eth_audio.tcl` 与 `fpga/build/eth_audio_pins.xdc`。

---

## 3. 判据与取证类（4 条）

### P12　"板子说了算"：寄存器回读比任何推断都硬

- **现象**：智能体根据代码推断"这个功能应该在跑"，但读回来的值完全对不上。
- **根因**：PL 里的构建版本、PS 里的固件版本、电脑上的源码，三者可以互不一致；只有板上回读是真相。
- **规避**：每个功能都设计一个**回读判据**（例如"把状态字的固定魔数读回来对上，才认为这一版 bit 生效"），并写进验收步骤。
- **证据**：`docs/known_issues.md` 第 5 条（读回 `0xA5B16D1E` 说明板上 bit 与源码不是同一版）；`docs/interface.md` 的 GPIO 寄存器表。

### P13　文档里的数字也要回源码核，别直接抄

- **现象**：交接文档里写着"BCLK 3.072 MHz / 周期 325.521 ns"，按它去写约束就是错的。
- **根因**：真实值是 1.536 MHz / 650.8 ns（`bclk_div_cnt == 8'd3`，8 × 81.354 ns）。文档作者当时按"12.288 MHz ÷ 4"写了，源码写的是"÷ 8"。
- **规避**：**任何要写进约束、写进报告的数字，必须回到产生它的那行代码**；文档只是线索，不是证据。
- **证据**：`fpga/src/rtl/slave_signal_gen.v`；`docs/architecture.md` 的时钟域表。

### P14　烧写前必须核对"烧的是哪一个文件"

- **现象**：烧完发现行为跟预期不同，白查半小时，最后发现脚本里 `set bit` 指向的是另一份 bit。
- **根因**：机器上同时存在"最新构建"和"已验证版本"两套烧写脚本，文件名很像。
- **规避**：烧写脚本开头打印/校验目标文件路径 + 大小 + SHA256 前 12 位，与验收记录对上再烧。
- **证据**：`fpga/build/flash_bit.tcl`；交付包内 bit 的 SHA256 前 12 位记录在 `docs/manifest.tsv`。

### P15　状态字段会"过期"，别拿旧字段当活读数

- **现象**：按文档里的字段布局去解析板子回来的状态，解析出一堆恒定的怪值。
- **根因**：固件升级过，状态字里的 `res[]` 保留区早就不更新了；照旧布局解读就会得出"板子卡住了"的错误结论。
- **规避**：协议字段与固件同版本管理；交付文档里对废弃字段显式标注"已废弃，勿用"。
- **证据**：`docs/interface.md` 状态包逐字节表（标注了哪些是保留/已废弃）。

---

## 4. 智能体工作方式类（5 条）

### P16　交接材料要写"判据"，不要只写"结论"

- **现象**：新的对话（或人）接手后，按文档结论去做，做出来的东西是错的。
- **根因**：结论脱离了产生它的读数。写"0 包，原因是 X"没用；要写"读这三个地址，值应该是 A/B/C，现在读到 A/0/0，所以是 X"。
- **规避**：交接文档统一格式：**现象 → 复现命令 → 期望读数 → 实际读数 → 目前判断（标注置信度）**。
- **证据**：本包 `docs/known_issues.md` 全篇按此格式写。

### P17　技能（skills）不是越多越好，塞爆会拖慢每一轮

- **现象**：装了上百个技能后，每轮对话都变慢，且模型容易挑错技能。
- **根因**：技能目录每轮都会进上下文，噪声与延迟同时上升。
- **规避**：只保留与本项目相关的少数技能，其余按需加载；把"项目自己的规矩"写成一份短文档，比装十个通用技能有用。
- **证据**：`skill/README.md`（我们最终只沉淀了 5 条 S1–S5）。

### P18　往 MCU 的 REPL 里 `exec` 长代码会掉字符

- **现象**：一长段 Python 通过串口送进 MicroPython，执行到中间就语法错误。
- **根因**：串口缓冲 + 行编辑限制，长行会被截断。
- **规避**：分块写文件（base64 分块 + 写完用 `os.stat` 核对大小），不要指望一次粘贴成功。
- **证据**：`host/app/xiaozhi/esp32.py` 的 `write_file()`；`host/app/xiaozhi/board/main.py`。

### P19　多个智能体工具会抢同一块板子

- **现象**：ESP32 的串口突然写不进去，板子像死了一样。
- **根因**：另一个智能体工具在后台跑 `esptool chip-id -p COM12`，把板子按进了下载模式并占着串口。
- **规避**：一台机器上跑多个 agent 工具时，串口/调试器属于**独占资源**；开工前先查有没有别的进程占着 COM/JTAG，事后要物理复位才能恢复。
- **证据**：`docs/hardware_setup.md` 的排障条目（"串口被占用"）。

### P20　沙箱/权限导致的"假故障"，别当产品 bug 去修

- **现象**：TTS 报"系统上未安装语音"、浏览器窗口起 10 秒就自己退。
- **根因**：受限环境（无命名管道、无 COM、无桌面会话）造成的现象，换到完全权限就恢复正常。
- **规避**：报错先分层——是**代码错**、**环境缺**、还是**权限挡**。判定办法：同一条命令换权限跑一次，或换机器跑一次。
- **证据**：`metrics/logs/console_smoke_20261104.log`（干净目录下冒烟通过，证明前面的失败是环境问题）。

---

## 5. 直接可用的规避清单

**开工前**
- [ ] 这套工具链的版本记下来（Vivado / Vitis / Python / Ross 客户端），写进 README
- [ ] 确认没有别的进程占着 JTAG / 串口
- [ ] 明确本次要验证的**回读判据**是什么

**智能体生成之后（人工必查）**
- [ ] `check_timing` 里 `no_clock = 0`
- [ ] 综合日志里没有 `8-4767` / `8-13159`
- [ ] 资源占用（尤其 Slice）在 90% 以内
- [ ] 引用的每个数字都能在源码里找到那一行

**上板前**
- [ ] bit / elf 的路径、大小、SHA 与验收记录一致
- [ ] 约束来自脚本模板，不是手改产物

**交付前**
- [ ] 交接文档按"现象 → 命令 → 期望 → 实际"写
- [ ] 已知问题单独成篇，含失败分析与下一步，不藏
- [ ] 换一台机器能起得来（把"能起"当作一个要测的功能）

---

## 6. 这份文档与 `skill/` 的对应关系

上面 20 条里，被复用到别的项目也不变形的，已经提炼成技能包里的 5 条：

| 技能 | 覆盖的坑 |
|---|---|
| S1　时序健康度先于 WNS | P6、P7、P9 |
| S2　存储器推断检查 | P8 |
| S3　参数以回读为准 | P10、P12、P15 |
| S4　烧写目标校验 | P14 |
| S5　交接与失败分析格式 | P16、P19、P20 |

其余（P1–P5、P11、P13、P17、P18）属于"这次遇到、下次换个工具就未必一样"的工具细节，留在本文档里备查。
