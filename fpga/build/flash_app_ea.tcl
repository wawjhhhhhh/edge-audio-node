set bit "D:/vivado_tmp/eth_audio_prj/eth_audio.runs/impl_1/eth_audio_top.bit"
set elf "D:/vitis_tmp/ws_ea/app_ea/build/app_ea_fpu.elf"
if {![file exists $elf]} { set elf "D:/vitis_tmp/ws_ea/app_ea/build/app_ea.elf" }
set ps7 "D:/vitis_tmp/ws_ea/app_ea/_ide/psinit/ps7_init.tcl"
puts "ELF=$elf"
connect
targets -set -nocase -filter {name =~ "APU*"}
rst -system
after 3000
targets -set -nocase -filter {name =~ "APU*"}
source $ps7
ps7_init
ps7_post_config
puts "PS7INIT_OK"
fpga -file $bit
puts "FPGA_OK"
targets -set -nocase -filter {name =~ "ARM*#0"}
dow $elf
puts "DOW_OK"
rwr cpsr 0x1d3
con
puts "CON"
after 9000
puts "DONE"
exit
