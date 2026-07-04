"""Build Focal-Portable.zip from the Focal-Portable/ folder.

ABORTS (exit 2) if any secret material (*.key / *.pem / *.cer / *.crt / *.pfx / relay_key.txt)
is present anywhere in the tree — the local CA private key and certificates must NEVER ship.
Run this before tools/build_single.py.
"""
import os, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "Focal-Portable")
OUT = os.path.join(ROOT, "Focal-Portable.zip")
ARCROOT = "Focal-Portable"                 # top-level folder inside the zip (SingleExe.cs Prefix must match)

SECRET_EXT = {".key", ".pem", ".cer", ".crt", ".pfx"}
SECRET_NAMES = {"relay_key.txt"}
SKIP_DIRS = {"__pycache__"}
SKIP_EXT = {".pyc", ".pyo"}

if not os.path.isdir(SRC):
    sys.exit("ABORT: Focal-Portable/ folder not found — nothing to zip.")

# 1) fail-closed secret scan
offenders = []
for dp, dns, fns in os.walk(SRC):
    for fn in fns:
        ext = os.path.splitext(fn)[1].lower()
        if ext in SECRET_EXT or fn.lower() in SECRET_NAMES:
            offenders.append(os.path.join(dp, fn))
if offenders:
    print("ABORT: secret/cert material present — refusing to build a shippable zip:")
    for o in offenders:
        print("   ", o)
    sys.exit(2)

# 2) build the zip
if os.path.exists(OUT):
    os.remove(OUT)
n = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for dp, dns, fns in os.walk(SRC):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, SRC)
            arc = ARCROOT + "/" + rel.replace(os.sep, "/")
            z.write(full, arc)
            n += 1
mb = os.path.getsize(OUT) / (1024 * 1024)
print("ZIP DONE  %s  (%d files, %.0f MB)" % (OUT, n, mb))
