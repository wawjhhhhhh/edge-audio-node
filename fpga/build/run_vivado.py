"""Run a Vivado batch TCL script and capture its output.

Why this exists: the handoff notes keep saying `python run_vivado.py <tcl>`,
but only run_xsdb.py was ever committed, so the command silently failed with
"can't open file".  Same pattern as run_xsdb.py: strip proxy vars (they make
Vivado's licence/telemetry calls hang), pin the architecture, capture stdout
into a log so the long synth/impl output does not flood the terminal.

Usage:  python run_vivado.py D:/vivado_tmp/rebuild_audio_fast.tcl [timeout_s]
"""
import os
import subprocess
import sys

for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(k, None)
os.environ["PROCESSOR_ARCHITECTURE"] = "AMD64"
os.environ["PROCESSOR_ARCHITEW6432"] = "AMD64"

VIVADO = r"D:/xilinx/2026.1/2026.1/Vivado/bin/vivado.bat"
tcl = sys.argv[1] if len(sys.argv) > 1 else r"D:/vivado_tmp/rebuild_audio_fast.tcl"
timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 1800
log = r"D:/vitis_tmp/vivado_batch.log"

cmd = [VIVADO, "-mode", "batch", "-notrace", "-source", tcl,
       "-log", r"D:/vitis_tmp/vivado_batch_vivado.log",
       "-journal", r"D:/vitis_tmp/vivado_batch.jou"]
print("RUN:", " ".join(cmd), flush=True)
try:
    p = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout, errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    open(log, "w", encoding="utf-8", errors="replace").write(out)
    print("RC=", p.returncode, "| captured %d chars -> %s" % (len(out), log), flush=True)
    for line in out.splitlines():
        if line.startswith(">>>") or "ERROR" in line or "CRITICAL" in line:
            print(line, flush=True)
except subprocess.TimeoutExpired:
    print("TIMEOUT after %d s" % timeout, flush=True)
