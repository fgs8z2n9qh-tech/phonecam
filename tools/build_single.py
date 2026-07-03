"""Build PhoneCam-SingleFile.exe: the SingleExe.cs stub with PhoneCam-Portable.zip embedded.
Run scratchpad's build_zip.py (or any fresh zip build) first — this script refuses a stale zip.
"""
import os, subprocess, sys, time, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(ROOT, "PhoneCam-Portable.zip")
CS = os.path.join(ROOT, "SingleExe.cs")
OUT = os.path.join(ROOT, "PhoneCam-SingleFile.exe")
ICON = os.path.join(ROOT, "assets", "icon.ico")
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

if not os.path.exists(ZIP):
    sys.exit("ABORT: PhoneCam-Portable.zip not found — build the zip first.")
age_h = (time.time() - os.path.getmtime(ZIP)) / 3600
if age_h > 24:
    sys.exit("ABORT: PhoneCam-Portable.zip is %.0f hours old — rebuild it first." % age_h)

version = "1.7+" + datetime.datetime.now().strftime("%Y%m%d%H%M")
src = open(CS, encoding="utf-8").read().replace("__VERSION__", version)
tmp_cs = os.path.join(ROOT, "SingleExe.stamped.cs")
open(tmp_cs, "w", encoding="utf-8").write(src)

cmd = [CSC, "/nologo", "/target:winexe", "/out:" + OUT,
       "/win32icon:" + ICON,
       "/res:" + ZIP + ",payload.zip",
       "/r:System.IO.Compression.dll",
       "/r:System.IO.Compression.FileSystem.dll",   # ZipFileExtensions.ExtractToFile
       "/r:System.Windows.Forms.dll",
       tmp_cs]
t0 = time.time()
r = subprocess.run(cmd, capture_output=True, text=True)
os.remove(tmp_cs)
print(r.stdout, r.stderr)
if r.returncode:
    sys.exit("csc failed (%d)" % r.returncode)
mb = os.path.getsize(OUT) / (1024 * 1024)
print("SINGLE-EXE DONE  version=%s  size=%.0f MB  in %.0fs" % (version, mb, time.time() - t0))
