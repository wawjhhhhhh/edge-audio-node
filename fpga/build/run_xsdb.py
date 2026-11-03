import subprocess, sys, os
for k in ("http_proxy","HTTP_PROXY","https_proxy","HTTPS_PROXY"):
    os.environ.pop(k, None)
os.environ["PROCESSOR_ARCHITECTURE"] = "AMD64"
os.environ["PROCESSOR_ARCHITEW6432"] = "AMD64"
tcl = sys.argv[1] if len(sys.argv) > 1 else r"D:/vitis_tmp/probe.tcl"
cmd = r'call "D:/xilinx/2026.1/2026.1/Vitis/bin/xsdb.bat" ' + tcl
print("RUN:", cmd, flush=True)
try:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=240, errors="replace")
    print("RC=", p.returncode, flush=True)
    out = (p.stdout or "") + (p.stderr or "")
    open(r"D:/vitis_tmp/xsdb_py.log", "w", encoding="utf-8", errors="replace").write(out)
    print("--- captured %d chars ---" % len(out))
    print(out[-4000:])
except subprocess.TimeoutExpired:
    print("TIMEOUT")
