# build_eth_audio.tcl -- merged BD: TEMAC (axi_ethernet, RGMII) + audio core
# Path: MIC -> ES8388 -> I2S -> PL -> AXI DMA -> DDR -> PS -> UDP -> PC  (live monitor)
#       and later PC -> board (network as audio source).
#
# Clock domains:
#   FCLK0 100 MHz : AXI-Lite, TEMAC axis_clk, DMA MM side
#   FCLK1  50 MHz : audio_axis_core sys_clk (core MMCM makes 12.288 MHz MCLK)
#   clk_wiz      : 125 MHz (gtx_clk) + 200 MHz (ref_clk)
# DMA0 = ethernet (MM2S -> txgen -> TEMAC, TEMAC RX -> S2MM)
# DMA1 = audio    (MM2S -> core s_axis -> DAC, core m_axis -> S2MM -> PS)
#
# NOTE: keep this file pure ASCII. Generated Tcl/XDC must never carry non-ASCII
# text: reading it back with PowerShell/GBK has already corrupted this script once.

set PROJ   "D:/vivado_tmp/eth_audio_prj"
set RTL    "D:/deepseek herness/work/eth_audio/rtl"
set ARTL   "D:/deepseek herness/work/audio_axis/rtl"
set AXIRTL "D:/deepseek herness/work/eth_axi/rtl"
set XDC    "D:/deepseek herness/work/eth_audio/bd/eth_audio_pins.xdc"

file delete -force $PROJ
create_project eth_audio $PROJ -part xc7z020clg400-2 -force
# every .v under $RTL is added automatically: audio_axis_core pulls in
# slave_signal_gen / audio_config / audio_receive / afifo, and audio_config
# pulls in i2c_reg_cfg + i2c_dri. Globbing avoids re-breaking the build when
# another dependency is discovered.
add_files -norecurse [concat [list "$AXIRTL/eth_txctrl_gen.v" "$ARTL/afifo.v"] [glob -nocomplain "$RTL/*.v"]]
update_compile_order -fileset sources_1

# ---------------------------------------------------------------- block design
create_bd_design eth_audio

# ---- PS7 ----------------------------------------------------------------
set ps [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0]
set_property -dict [list \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_EN_CLK0_PORT {1} \
    CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ {50} \
    CONFIG.PCW_EN_CLK1_PORT {1} \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_S_AXI_HP0 {1} \
    CONFIG.PCW_UART0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_UART0_UART0_IO {EMIO} \
    CONFIG.PCW_SD0_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_SD0_SD0_IO {MIO 40 .. 45} \
] $ps
source "D:/deepseek herness/work/audio_axis/bd/ps7_ddr_props.tcl"

# ---- clocking: 100 MHz -> 125 MHz (gtx_clk) + 200 MHz (ref_clk) ----------
set cw [create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 clk_wiz_0]
set_property -dict [list \
    CONFIG.PRIM_IN_FREQ {100.000} \
    CONFIG.PRIM_SOURCE {No_buffer} \
    CONFIG.USE_LOCKED {false} \
    CONFIG.USE_RESET {false} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {125.000} \
    CONFIG.CLKOUT2_USED {true} \
    CONFIG.CLKOUT2_REQUESTED_OUT_FREQ {200.000} \
    CONFIG.NUM_OUT_CLKS {2} \
] $cw

# ---- TEMAC ---------------------------------------------------------------
set ae [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_ethernet:8.0 axi_ethernet_0]
set_property -dict [list \
    CONFIG.PHY_TYPE {RGMII} \
    CONFIG.gtrefclkrate {125} \
    CONFIG.axiliteclkrate {100.0} \
    CONFIG.axisclkrate {100.0} \
] $ae

# ---- AXI DMA 0: ethernet (32 bit MM <-> 32 bit stream) -------------------
set dma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 axi_dma_0]
set_property -dict [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_sg_include_stscntrl_strm {0} \
    CONFIG.c_include_mm2s {1} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_m_axi_mm2s_data_width {32} \
    CONFIG.c_m_axis_mm2s_tdata_width {32} \
    CONFIG.c_m_axi_s2mm_data_width {32} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
] $dma

# ---- AXI DMA 1: audio (MM side 100 MHz, stream side converted) -----------
set dma_a [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 axi_dma_a]
set_property -dict [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_sg_include_stscntrl_strm {0} \
    CONFIG.c_include_mm2s {1} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_m_axi_mm2s_data_width {32} \
    CONFIG.c_m_axis_mm2s_tdata_width {32} \
    CONFIG.c_m_axi_s2mm_data_width {32} \
    CONFIG.c_s_axis_s2mm_tdata_width {32} \
] $dma_a

# ---- audio core (module reference) --------------------------------------
set acore [create_bd_cell -type module -reference audio_axis_core audio_axis_core_0]
# A module reference keeps the RTL defaults unless they are set here:
#   BEATS_PER_PKT defaults to 48 and LOOPBACK to 1, i.e. 1 ms packets and the
#   PL self-loop, so the DMA never sees a 10 ms frame (measured 250 "frames"
#   per second instead of 100) and the RX FIFO is drained by the internal TX
#   path instead of the DMA.
# These are exactly the values the working audio_axis project used.
# AW sets the async FIFO depth (2**AW words).  AW 4 = 16 words = 0.33 ms at
# 48 kHz, far too little to cover the CPU time spent refilling the DAC through
# AXI DMA: one 1030 B UDP frame alone keeps the core busy for ~250 us, and each
# re-arm that overshoots the FIFO drains it so the codec plays a hole.  That is
# the periodic "beep" heard even with nothing but the 440 Hz self-test tone.
# AW 9 = 512 words = 10.7 ms of slack (~2 KB BRAM).
foreach {p v} [list WL 16 BEATS_PER_PKT 480 AW 9 LOOPBACK 0] {
    if {[catch {set_property CONFIG.$p $v $acore} err]} {
        puts ">>> WARN: CONFIG.$p=$v not applied: $err"
    }
}
# LOOPBACK must really be 0, otherwise synthesis trims s_axis/m_axis and the
# DMA silently receives nothing (no error, just no audio).
if {([catch {set_property CONFIG.LOOPBACK 0 $acore}] == 0) &&
    ([get_property CONFIG.LOOPBACK $acore] != 0)} {
    error "LOOPBACK is not 0 after set_property; module parameter mapping broken"
}
puts ">>> audio_axis_core: BEATS_PER_PKT=[get_property CONFIG.BEATS_PER_PKT $acore] LOOPBACK=[get_property CONFIG.LOOPBACK $acore] AW=[get_property CONFIG.AW $acore]"
# module-reference AXIS interfaces default to 100 MHz; the core runs at 50 MHz (FCLK1),
# and the clock converters are 50<->100 MHz, so declare the core side explicitly.
set_property CONFIG.FREQ_HZ {50000000} [get_bd_intf_pins audio_axis_core_0/s_axis]
set_property CONFIG.FREQ_HZ {50000000} [get_bd_intf_pins audio_axis_core_0/m_axis]

# ---- GPIO: ch1 output control, ch2 input status -------------------------
set gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 axi_gpio_0]
set_property -dict [list \
    CONFIG.C_GPIO_WIDTH {32} CONFIG.C_GPIO2_WIDTH {32} \
    CONFIG.C_IS_DUAL {1} \
    CONFIG.C_ALL_INPUTS {0} CONFIG.C_ALL_INPUTS_2 {1} \
] $gpio

# ---- read-only debug bus (txgen dbg) ------------------------------------
set g1 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 axi_gpio_dbg]
set_property -dict [list CONFIG.C_GPIO_WIDTH {32} CONFIG.C_IS_DUAL {0} CONFIG.C_ALL_INPUTS {1}] $g1

# ---- interconnect -------------------------------------------------------
set smc_gp [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi_smc_gp]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {5} CONFIG.NUM_CLKS {1}] $smc_gp
set smc_hp [create_bd_cell -type ip -vlnv xilinx.com:ip:smartconnect:1.0 axi_smc_hp]
set_property -dict [list CONFIG.NUM_SI {4} CONFIG.NUM_MI {1} CONFIG.NUM_CLKS {1}] $smc_hp

set rst [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_0]
set lck [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 xlconstant_0]
set_property -dict [list CONFIG.CONST_VAL {1}] $lck

# 50 MHz domain reset (audio core)
set rst1 [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_1]
set lck1 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 xlconstant_1]
set_property -dict [list CONFIG.CONST_VAL {1}] $lck1

# ---- connections: clocks and resets ------------------------------------
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins clk_wiz_0/clk_in1]
connect_bd_net [get_bd_pins clk_wiz_0/clk_out1]  [get_bd_pins axi_ethernet_0/gtx_clk]
connect_bd_net [get_bd_pins clk_wiz_0/clk_out2]  [get_bd_pins axi_ethernet_0/ref_clk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_ethernet_0/axis_clk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_ethernet_0/s_axi_lite_clk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins proc_sys_reset_0/slowest_sync_clk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_dma_0/s_axi_lite_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_dma_0/m_axi_mm2s_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_dma_0/m_axi_s2mm_aclk]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_dma_0/axi_resetn]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_gpio_0/s_axi_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_smc_gp/aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_smc_hp/aclk]

connect_bd_net [get_bd_pins processing_system7_0/FCLK_RESET0_N] [get_bd_pins proc_sys_reset_0/ext_reset_in]
connect_bd_net [get_bd_pins xlconstant_0/dout] [get_bd_pins proc_sys_reset_0/dcm_locked]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_gpio_0/s_axi_aresetn]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_ethernet_0/s_axi_lite_resetn]

# TEMAC AXIS resets (active low)
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_ethernet_0/axi_txd_arstn]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_ethernet_0/axi_txc_arstn]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_ethernet_0/axi_rxd_arstn]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_ethernet_0/axi_rxs_arstn]

# 50 MHz domain: audio core
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK1] [get_bd_pins proc_sys_reset_1/slowest_sync_clk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_RESET0_N] [get_bd_pins proc_sys_reset_1/ext_reset_in]
connect_bd_net [get_bd_pins xlconstant_1/dout] [get_bd_pins proc_sys_reset_1/dcm_locked]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK1] [get_bd_pins audio_axis_core_0/sys_clk]
connect_bd_net [get_bd_pins proc_sys_reset_1/peripheral_aresetn] [get_bd_pins audio_axis_core_0/sys_rst_n]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_dma_a/s_axi_lite_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_dma_a/m_axi_mm2s_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_dma_a/m_axi_s2mm_aclk]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_dma_a/axi_resetn]

# ---- connections: AXI --------------------------------------------------
connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] [get_bd_intf_pins axi_smc_gp/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_smc_gp/M00_AXI] [get_bd_intf_pins axi_ethernet_0/s_axi]
connect_bd_intf_net [get_bd_intf_pins axi_smc_gp/M01_AXI] [get_bd_intf_pins axi_dma_0/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins axi_smc_gp/M02_AXI] [get_bd_intf_pins axi_gpio_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_smc_gp/M03_AXI] [get_bd_intf_pins axi_gpio_dbg/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_smc_gp/M04_AXI] [get_bd_intf_pins axi_dma_a/S_AXI_LITE]

connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXI_MM2S] [get_bd_intf_pins axi_smc_hp/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXI_S2MM] [get_bd_intf_pins axi_smc_hp/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_dma_a/M_AXI_MM2S] [get_bd_intf_pins axi_smc_hp/S02_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_dma_a/M_AXI_S2MM] [get_bd_intf_pins axi_smc_hp/S03_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_smc_hp/M00_AXI] [get_bd_intf_pins processing_system7_0/S_AXI_HP0]

connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins processing_system7_0/M_AXI_GP0_ACLK]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins processing_system7_0/S_AXI_HP0_ACLK]

# ---- connections: ethernet data path -----------------------------------
set zg [create_bd_cell -type module -reference eth_txctrl_gen txgen]
connect_bd_intf_net [get_bd_intf_pins axi_dma_0/M_AXIS_MM2S] [get_bd_intf_pins txgen/s_axis]
connect_bd_intf_net [get_bd_intf_pins txgen/m_axis] [get_bd_intf_pins axi_ethernet_0/s_axis_txd]
connect_bd_intf_net [get_bd_intf_pins txgen/c_axis] [get_bd_intf_pins axi_ethernet_0/s_axis_txc]
connect_bd_net [get_bd_pins axi_ethernet_0/axis_clk] [get_bd_pins txgen/clk]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins txgen/rstn]
connect_bd_intf_net [get_bd_intf_pins axi_ethernet_0/m_axis_rxd] [get_bd_intf_pins axi_dma_0/S_AXIS_S2MM]
set zr [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 xlconst_one]
set_property -dict [list CONFIG.CONST_VAL {1} CONFIG.CONST_WIDTH {1}] $zr
connect_bd_net [get_bd_pins xlconst_one/dout] [get_bd_pins axi_ethernet_0/m_axis_rxs_tready]

# ---- connections: audio data path --------------------------------------
# MIC: core m_axis -> acc_rx -> DMA1 S2MM
# DAC: DMA1 MM2S -> acc_tx -> core s_axis
# AXI DMA 7.1 has no separate stream clock pins (only 3 aclk), so the audio core
# (50 MHz FCLK1) and the DMA stream side (100 MHz FCLK0) need clock converters.
set acc_rx [create_bd_cell -type ip -vlnv xilinx.com:ip:axis_clock_converter:1.1 acc_rx]
set acc_tx [create_bd_cell -type ip -vlnv xilinx.com:ip:axis_clock_converter:1.1 acc_tx]
connect_bd_intf_net [get_bd_intf_pins audio_axis_core_0/m_axis] [get_bd_intf_pins acc_rx/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins acc_rx/M_AXIS] [get_bd_intf_pins axi_dma_a/S_AXIS_S2MM]
connect_bd_intf_net [get_bd_intf_pins axi_dma_a/M_AXIS_MM2S] [get_bd_intf_pins acc_tx/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins acc_tx/M_AXIS] [get_bd_intf_pins audio_axis_core_0/s_axis]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK1] [get_bd_pins acc_rx/s_axis_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins acc_rx/m_axis_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins acc_tx/s_axis_aclk]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK1] [get_bd_pins acc_tx/m_axis_aclk]
connect_bd_net [get_bd_pins proc_sys_reset_1/peripheral_aresetn] [get_bd_pins acc_rx/s_axis_aresetn]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins acc_rx/m_axis_aresetn]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins acc_tx/s_axis_aresetn]
connect_bd_net [get_bd_pins proc_sys_reset_1/peripheral_aresetn] [get_bd_pins acc_tx/m_axis_aresetn]

# key: no button function here, hold released (high)
set keyc [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 xlconst_key]
set_property -dict [list CONFIG.CONST_VAL {1} CONFIG.CONST_WIDTH {1}] $keyc
connect_bd_net [get_bd_pins xlconst_key/dout] [get_bd_pins audio_axis_core_0/key]

# ---- audio core bring-up counters -> GPIO ch2 (0x41200008) ----
connect_bd_net [get_bd_pins audio_axis_core_0/dbg_audio] [get_bd_pins axi_gpio_0/gpio2_io_i]

# ---- MAC irq to GPIO ch2 ------------------------------------------------
connect_bd_net [get_bd_pins axi_ethernet_0/mac_irq] [get_bd_pins axi_gpio_0/gpio_io_i]

# ---- external ports ----------------------------------------------------
foreach {p dir w} {
    eth_rxc        I 1
    eth_rxd        I 4
    eth_rx_ctl     I 1
    eth_txd        O 4
    eth_tx_ctl     O 1
    eth_txc        O 1
    eth_mdc        O 1
    eth_mdio_i     I 1
    eth_mdio_o     O 1
    eth_mdio_t     O 1
    eth_rst_n      O 1
    uart_txd       O 1
    uart_rxd       I 1
    aud_mclk       O 1
    aud_bclk       O 1
    aud_lrc        O 1
    aud_dacdat     O 1
    aud_adcdat     I 1
    aud_scl        O 1
} {
    if {$w > 1} {
        set port [create_bd_port -dir $dir -from [expr {$w-1}] -to 0 $p]
    } else {
        set port [create_bd_port -dir $dir $p]
    }
}
set port [create_bd_port -dir IO aud_sda]

connect_bd_net [get_bd_pins axi_ethernet_0/rgmii_rxc]     [get_bd_ports eth_rxc]
connect_bd_net [get_bd_pins axi_ethernet_0/rgmii_rx_ctl]  [get_bd_ports eth_rx_ctl]
connect_bd_net [get_bd_pins axi_ethernet_0/rgmii_rd]      [get_bd_ports eth_rxd]
connect_bd_net [get_bd_pins axi_ethernet_0/rgmii_txc]     [get_bd_ports eth_txc]
connect_bd_net [get_bd_pins axi_ethernet_0/rgmii_tx_ctl]  [get_bd_ports eth_tx_ctl]
connect_bd_net [get_bd_pins axi_ethernet_0/rgmii_td]      [get_bd_ports eth_txd]
connect_bd_net [get_bd_pins axi_ethernet_0/mdio_mdc]      [get_bd_ports eth_mdc]
connect_bd_net [get_bd_pins axi_ethernet_0/mdio_mdio_i]   [get_bd_ports eth_mdio_i]
connect_bd_net [get_bd_pins axi_ethernet_0/mdio_mdio_o]   [get_bd_ports eth_mdio_o]
connect_bd_net [get_bd_pins axi_ethernet_0/mdio_mdio_t]   [get_bd_ports eth_mdio_t]
connect_bd_net [get_bd_pins axi_ethernet_0/phy_rst_n]     [get_bd_ports eth_rst_n]
connect_bd_net [get_bd_pins processing_system7_0/UART0_TX] [get_bd_ports uart_txd]
connect_bd_net [get_bd_pins processing_system7_0/UART0_RX] [get_bd_ports uart_rxd]

connect_bd_net [get_bd_pins audio_axis_core_0/aud_mclk]   [get_bd_ports aud_mclk]
connect_bd_net [get_bd_pins audio_axis_core_0/aud_bclk]   [get_bd_ports aud_bclk]
connect_bd_net [get_bd_pins audio_axis_core_0/aud_lrc]    [get_bd_ports aud_lrc]
connect_bd_net [get_bd_pins audio_axis_core_0/aud_dacdat] [get_bd_ports aud_dacdat]
connect_bd_net [get_bd_pins audio_axis_core_0/aud_adcdat] [get_bd_ports aud_adcdat]
connect_bd_net [get_bd_pins audio_axis_core_0/aud_scl]    [get_bd_ports aud_scl]
connect_bd_net [get_bd_pins audio_axis_core_0/aud_sda]    [get_bd_ports aud_sda]

# ---- debug bus ---------------------------------------------------------
connect_bd_net [get_bd_pins txgen/dbg] [get_bd_pins axi_gpio_dbg/gpio_io_i]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins axi_gpio_dbg/s_axi_aclk]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] [get_bd_pins axi_gpio_dbg/s_axi_aresetn]

assign_bd_address
validate_bd_design
save_bd_design

# ---- wrapper + sources -------------------------------------------------
set bd_file [get_files eth_audio.bd]
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper
add_files -norecurse [list "$RTL/eth_audio_top.v"]
set_property top eth_audio_top [current_fileset]
update_compile_order -fileset sources_1

# ---- XDC ---------------------------------------------------------------
set fh [open $XDC w]
puts $fh "# eth_audio pins -- from NAVIGATOR_ZYNQ_IO.xdc (ATK ZYNQ7020)"
foreach {p pin} {
    eth_rxc K17  eth_rx_ctl E17  eth_txc B20  eth_tx_ctl K18
    eth_mdc F20  eth_mdio F19  eth_rst_n G15
    uart_txd J15  uart_rxd T19
    aud_lrc L20  aud_bclk M18  aud_mclk E19  aud_dacdat G17  aud_adcdat M17
    aud_scl E18  aud_sda F17
} {
    puts $fh "set_property -dict \[list PACKAGE_PIN $pin IOSTANDARD LVCMOS33\] \[get_ports $p\]"
}
foreach {p pin} {0 B19 1 A20 2 H17 3 H16} {
    puts $fh "set_property -dict \[list PACKAGE_PIN $pin IOSTANDARD LVCMOS33\] \[get_ports {eth_rxd\[$p\]}\]"
}
foreach {p pin} {0 D18 1 C20 2 D19 3 D20} {
    puts $fh "set_property -dict \[list PACKAGE_PIN $pin IOSTANDARD LVCMOS33\] \[get_ports {eth_txd\[$p\]}\]"
}
puts $fh "set_property PULLUP true \[get_ports eth_mdio\]"
puts $fh "set_property PULLUP true \[get_ports aud_sda\]"
puts $fh ""
puts $fh "# ---- clock domains ----------------------------------------------------"
puts $fh "# The audio core's I2S side runs on its own MMCM output (aud_mclk,"
puts $fh "# 12.288 MHz / 81.354 ns) and its feedback output clkfb_out (50 MHz)."
puts $fh "# They are unrelated to the PS FCLK domains and to the MAC internals."
puts $fh "# Without this, Vivado checks the reset that reaches the I2S flops as"
puts $fh "# async_default Recovery/Removal against clk_fpga_1 and reports"
puts $fh "# -3.096 ns on .../u_slave_signal_gen/aud_lrc_reg/CLR."
puts $fh "set_clock_groups -asynchronous -group \[get_clocks \{clk_fpga_0 clk_fpga_1\}\] -group \[get_clocks \{aud_mclk_OBUF clkfb_out\}\]"
close $fh
add_files -fileset constrs_1 -norecurse [list $XDC]
# clocks referenced by name must exist first: run our XDC after the IP/BD XDCs
# (get_files wants the basename here -- a full path matches nothing)
if {[catch {set_property PROCESSING_ORDER LATE [get_files -quiet eth_audio_pins.xdc]} err]} {
    puts ">>> WARN: PROCESSING_ORDER LATE failed: $err"
} else {
    puts ">>> eth_audio_pins.xdc PROCESSING_ORDER = LATE"
}
puts ">>> BD + wrapper done"

# ---------------------------------------------------------------- build
puts ">>> launching synth_1"
launch_runs synth_1 -jobs 8
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} { error "synth failed: [get_property STATUS [get_runs synth_1]]" }
puts ">>> launching impl_1"
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} { error "impl failed: [get_property STATUS [get_runs impl_1]]" }
puts ">>> STATS.WNS = [get_property STATS.WNS [get_runs impl_1]]"
puts ">>> STATS.WHS = [get_property STATS.WHS [get_runs impl_1]]"
# a violated design must not be treated as a success (the audio async-reset
# recovery failure once slipped through because only PROGRESS was checked)
if {[get_property STATS.WNS [get_runs impl_1]] < 0} {
    error "timing not met: WNS=[get_property STATS.WNS [get_runs impl_1]]"
}
write_hw_platform -fixed -include_bit -force -file $PROJ/eth_audio.xsa
puts ">>> XSA = $PROJ/eth_audio.xsa"
