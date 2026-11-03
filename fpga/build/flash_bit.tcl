set bit "D:/vivado_tmp/eth_audio_prj/eth_audio.runs/impl_1/eth_audio_top.bit"
set elf "D:/vitis_tmp/ws_ea/app_ea/build/app_ea_fpu.elf"
set ps7 "D:/vitis_tmp/ws_ea/app_ea/_ide/psinit/ps7_init.tcl"
connect
targets -set -nocase -filter {name =~ "APU*"}
rst -system
after 1500
targets -set -nocase -filter {name =~ "APU*"}
source $ps7
ps7_init
ps7_post_config
fpga -file $bit
puts "BIT_IN_NOW"
targets -set -nocase -filter {name =~ "ARM*#0"}
dow $elf
rwr cpsr 0x1d3
con
puts "ALL_DONE"
exit
