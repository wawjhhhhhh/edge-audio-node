# 构建与烧写脚本

> 这里的脚本原本散落在 `D:\vitis_tmp\`（不在 git 里）。为了让「复现」不只是口号，全部收进仓库。
> **注意**：脚本里写死了本机路径（`D:\vitis_tmp`、`D:\xilinx\2026.1`），换机器按 `材料库/06-交付文档/01_复现手册.md` §8 的路径表逐条改。

## 依赖

- Vivado / Vitis 2026.1，安装在 `D:\xilinx\2026.1\2026.1\`
- Python（用于 `run_vivado.py` / `run_xsdb.py` 的子进程包装），任意 3.8+ 即可
- JTAG：Digilent JTAG-HS1（xsdb 会自己拉起 `hw_server`，端口 3121）

## 脚本清单

| 脚本 | 作用 | 怎么用 |
|---|---|---|
| `rebuild_audio_fast.tcl` | 重建 PL：`open_project` → 复位并重跑 synth → impl → `write_bitstream`，打印 `STATS.WNS/WHS` | `python run_vivado.py rebuild_audio_fast.tcl 1800` |
| `run_vivado.py` | 包装 `vivado -mode batch`，带超时与日志落盘 | 见上，第二个参数是超时秒数 |
| `flash_bit_fast.tcl` | JTAG 下载：`rst -system` → `ps7_init` → `fpga -file` → `dow` → `con` | `python run_xsdb.py flash_bit_fast.tcl` |
| `run_xsdb.py` | 包装 `xsdb`，把脚本喂进去并回显 | 同上 |
| `probe0.tcl` | 只做连通性检查：`connect` + `targets` | `python run_xsdb.py probe0.tcl` |
| `flash_ea_quick.tcl` | 早期版本的快速下载脚本（保留备查，已被 `flash_bit_fast.tcl` 取代） | — |
| `flash_vendor_bit.tcl` | 烧**厂商** bitstream（做硬件 A/B 用：证明耳机口本身是好的） | `python run_xsdb.py flash_vendor_bit.tcl` |
| `boot_ea.bif` | bootgen 输入：fsbl + bit + elf 三段 | `bootgen -image boot_ea.bif -arch zynq -o BOOT_xxx.bin -w on` |
| `build_app_ea.py` | 想用 Vitis 的 python API 编应用（**本机不可用**：绑定是 cp313，见复现手册 §4） | — |
| `serial_cap.ps1` | 抓串口日志到文件 | `powershell -f serial_cap.ps1` |

## 标准流程

```powershell
cd D:\vitis_tmp                                        # 脚本假设的工作目录
# 1) 改完 RTL → 重建
python "D:\deepseek herness\work\eth_audio\tools\run_vivado.py" ``
       "D:\deepseek herness\work\eth_audio\tools\rebuild_audio_fast.tcl" 1800
# 2) 拷 bit 到下载目录
Copy-Item D:\vivado_tmp\eth_audio_prj\eth_audio.runs\impl_1\eth_audio_top.bit D:\vitis_tmp\ -Force
# 3) 改完 main.c → 用 ninja 编（Vitis python API 在本机不可用）
cd D:\vitis_tmp\ws_ea\app_ea\build
$env:PATH = "D:\xilinx\2026.1\2026.1\Vitis\gnu\aarch32\nt\gcc-arm-none-eabi\bin;" + $env:PATH
& "D:\xilinx\2026.1\2026.1\Vitis\bin\ninja.exe"
Copy-Item app_ea.elf app_ea_fpu.elf -Force
# 4) 下载
cd D:\vitis_tmp
python "D:\deepseek herness\work\eth_audio\tools\run_xsdb.py" `
       "D:\deepseek herness\work\eth_audio\tools\flash_bit_fast.tcl"
```

## 掉电自启（QSPI）

```powershell
cd D:\vitis_tmp
& "D:\xilinx\2026.1\2026.1\Vitis\bin\bootgen.bat" -image boot_ea.bif -arch zynq -o BOOT_xxx.bin -w on
& "D:\xilinx\2026.1\2026.1\Vitis\bin\program_flash.bat" -f D:\vitis_tmp\BOOT_xxx.bin -offset 0 `
    -flash_type qspi-x4-single `
    -fsbl D:\vitis_tmp\ws_boot2\plat_ea\zynq_fsbl\build\fsbl.elf `
    -cable type xilinx_tcf url TCP:127.0.0.1:3121
```

- 2026.1 的合法 `-flash_type` 是 **`qspi-x4-single`** 或 `qspi-x1-single`；旧文档里的 `qspi_single` 不被认。
- **烧之前必须把开发板启动拨码拨到 QSPI 档**，否则 `sf probe` 读回浮空值 `40,dd,ff` 并报 `Error while detecting SPI flash device`（本机 10-29 实测）。
- 验收：不碰 JTAG 下载，上电后 `mrd -force 0x41200008` 读到 `0xA5xxxxxx`。
