# This machine is 8 cores / 16 threads, but Vivado reports
# get_param general.maxThreads == 2 and every phase runs on 2 CPUs
# (synth/place/route all say "maximum of 2"). Measured with a plain
# multiprocessing test: 8 workers scale 4.7x, so the cores are really
# there -- it is Vivado's own cap, not the OS. Raise it to the physical
# core count; going above physical cores makes place/route SLOWER.
set_param general.maxThreads 8

set PROJ "D:/vivado_tmp/eth_audio_prj/eth_audio.xpr"
set t0 [clock seconds]
puts ">>> opening $PROJ"
open_project $PROJ

puts ">>> resetting synth_1"
reset_run synth_1
launch_runs synth_1 -jobs 8
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] != "100%"} {
    error "synth failed: [get_property STATUS [get_runs synth_1]]"
}

puts ">>> launching impl_1"
launch_runs impl_1 -to_step write_bitstream -jobs 8
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "impl failed: [get_property STATUS [get_runs impl_1]]"
}

set wns [get_property STATS.WNS [get_runs impl_1]]
set whs [get_property STATS.WHS [get_runs impl_1]]
puts ">>> STATS.WNS = $wns"
puts ">>> STATS.WHS = $whs"
puts ">>> BIT = [get_property DIRECTORY [get_runs impl_1]]/eth_audio_top.bit"

if {$wns < 0} {
    error "timing not met: WNS=$wns"
}
puts ">>> elapsed = [expr {[clock seconds] - $t0}] s"
