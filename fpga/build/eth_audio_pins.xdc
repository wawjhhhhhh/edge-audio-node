# eth_audio pins -- from NAVIGATOR_ZYNQ_IO.xdc (ATK ZYNQ7020)
set_property -dict [list PACKAGE_PIN K17 IOSTANDARD LVCMOS33] [get_ports eth_rxc]
set_property -dict [list PACKAGE_PIN E17 IOSTANDARD LVCMOS33] [get_ports eth_rx_ctl]
set_property -dict [list PACKAGE_PIN B20 IOSTANDARD LVCMOS33] [get_ports eth_txc]
set_property -dict [list PACKAGE_PIN K18 IOSTANDARD LVCMOS33] [get_ports eth_tx_ctl]
set_property -dict [list PACKAGE_PIN F20 IOSTANDARD LVCMOS33] [get_ports eth_mdc]
set_property -dict [list PACKAGE_PIN F19 IOSTANDARD LVCMOS33] [get_ports eth_mdio]
set_property -dict [list PACKAGE_PIN G15 IOSTANDARD LVCMOS33] [get_ports eth_rst_n]
set_property -dict [list PACKAGE_PIN J15 IOSTANDARD LVCMOS33] [get_ports uart_txd]
set_property -dict [list PACKAGE_PIN T19 IOSTANDARD LVCMOS33] [get_ports uart_rxd]
set_property -dict [list PACKAGE_PIN L20 IOSTANDARD LVCMOS33] [get_ports aud_lrc]
set_property -dict [list PACKAGE_PIN M18 IOSTANDARD LVCMOS33] [get_ports aud_bclk]
set_property -dict [list PACKAGE_PIN E19 IOSTANDARD LVCMOS33] [get_ports aud_mclk]
set_property -dict [list PACKAGE_PIN G17 IOSTANDARD LVCMOS33] [get_ports aud_dacdat]
set_property -dict [list PACKAGE_PIN M17 IOSTANDARD LVCMOS33] [get_ports aud_adcdat]
set_property -dict [list PACKAGE_PIN E18 IOSTANDARD LVCMOS33] [get_ports aud_scl]
set_property -dict [list PACKAGE_PIN F17 IOSTANDARD LVCMOS33] [get_ports aud_sda]
set_property -dict [list PACKAGE_PIN B19 IOSTANDARD LVCMOS33] [get_ports {eth_rxd[0]}]
set_property -dict [list PACKAGE_PIN A20 IOSTANDARD LVCMOS33] [get_ports {eth_rxd[1]}]
set_property -dict [list PACKAGE_PIN H17 IOSTANDARD LVCMOS33] [get_ports {eth_rxd[2]}]
set_property -dict [list PACKAGE_PIN H16 IOSTANDARD LVCMOS33] [get_ports {eth_rxd[3]}]
set_property -dict [list PACKAGE_PIN D18 IOSTANDARD LVCMOS33] [get_ports {eth_txd[0]}]
set_property -dict [list PACKAGE_PIN C20 IOSTANDARD LVCMOS33] [get_ports {eth_txd[1]}]
set_property -dict [list PACKAGE_PIN D19 IOSTANDARD LVCMOS33] [get_ports {eth_txd[2]}]
set_property -dict [list PACKAGE_PIN D20 IOSTANDARD LVCMOS33] [get_ports {eth_txd[3]}]
set_property PULLUP true [get_ports eth_mdio]
set_property PULLUP true [get_ports aud_sda]

# ---- clock domains ----------------------------------------------------
# The audio core's I2S side runs on its own MMCM output (aud_mclk,
# 12.288 MHz / 81.354 ns) and its feedback output clkfb_out (50 MHz).
# They are unrelated to the PS FCLK domains and to the MAC internals.
# Without this, Vivado checks the reset that reaches the I2S flops as
# async_default Recovery/Removal against clk_fpga_1 and reports
# -3.096 ns on .../u_slave_signal_gen/aud_lrc_reg/CLR.
# 2026-11-03：把 aud2_mclk_OBUF 加回组里。上一版被 build 脚本重写时丢掉了这一项，
# 结果 `clk_fpga_1 -> aud2_mclk_OBUF` 的 recovery/removal 变成真检查（15 个端点、
# WNS -2.858 ns），I2S 域的复位释放被排进建立时间窗口里 —— 采集侧就这样被"优化"坏了。
set_clock_groups -asynchronous -group [get_clocks {clk_fpga_0 clk_fpga_1}] -group [get_clocks {aud_mclk_OBUF aud2_mclk_OBUF clkfb_out}]

# 2026-11-03: aud2_* / led * 引脚约束回填（build 脚本会重写这份 XDC）
set_property -dict [list PACKAGE_PIN W11 IOSTANDARD LVCMOS33] [get_ports aud2_bclk]
set_property -dict [list PACKAGE_PIN Y13 IOSTANDARD LVCMOS33] [get_ports aud2_lrc]
set_property -dict [list PACKAGE_PIN Y11 IOSTANDARD LVCMOS33] [get_ports aud2_dacdat]
set_property -dict [list PACKAGE_PIN W10 IOSTANDARD LVCMOS33] [get_ports aud2_mclk]
set_property -dict [list PACKAGE_PIN W9 IOSTANDARD LVCMOS33] [get_ports aud2_dacdat_w9]
set_property -dict [list PACKAGE_PIN W8 IOSTANDARD LVCMOS33] [get_ports aud2_dacdat_w8]
set_property -dict [list PACKAGE_PIN H15 IOSTANDARD LVCMOS33] [get_ports led0]
set_property -dict [list PACKAGE_PIN L15 IOSTANDARD LVCMOS33] [get_ports led1]
