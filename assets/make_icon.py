"""Focal app icon generator -> assets/icon.ico  (run: python assets/make_icon.py)

An iPhone-camera-style icon: a brushed graphite-silver metal squircle with a
realistic camera lens on it — nested housing rings, deep blue glass (radial),
a cyan lens-coating flare and speculars. Glyph-only at every size so it stays
legible down to 16px in the taskbar (no wordmark).

The previous "PhoneCam" green-wordmark icon is kept at assets/icon_phonecam_backup.ico.
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ICO = os.path.join(HERE, "icon.ico")

# brushed graphite / cool-silver metal (top-left -> bottom-right)
M0 = (220, 226, 230)   # #DCE2E6  light
M1 = (170, 179, 186)   # #AAB3BA  mid
M2 = (134, 143, 151)   # #868F97  dark
# lens glass radial stops (bright blue reflection -> near-black)
G0 = (47, 88, 120)     # #2f5878
G1 = (10, 24, 38)      # #0a1826
G2 = (4, 7, 12)        # #04070c


def _lerp(c0, c1, f):
    c0 = np.array(c0, np.float32); c1 = np.array(c1, np.float32)
    return c0 * (1 - f) + c1 * f


def _metal(S):
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    t = ((xx + yy) / (2 * (S - 1)))[..., None]
    a = np.clip(t / 0.55, 0, 1)
    b = np.clip((t - 0.55) / 0.45, 0, 1)
    mid = _lerp(M0, M1, a)                                  # M0->M1 in the first half
    rgb = mid * (1 - b) + np.array(M2, np.float32) * b      # then M1->M2 in the second
    arr = np.concatenate([rgb, np.full((S, S, 1), 255.0)], axis=2).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _gloss(S):
    """soft white radial highlight near the top-left -> glossy metal."""
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    r = np.sqrt((xx - 0.30 * S) ** 2 + (yy - 0.20 * S) ** 2) / (0.95 * S)
    a = np.clip(1 - r, 0, 1) ** 1.7 * 90
    img = np.zeros((S, S, 4), np.uint8)
    img[..., :3] = 255
    img[..., 3] = a.astype(np.uint8)
    return Image.fromarray(img, "RGBA")


def _glass(S, cx, cy, Rg):
    """deep blue lens glass with an off-centre bright reflection, disc-masked."""
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    hx = cx - 0.16 * Rg                                     # reflection centre (upper-left of glass)
    hy = cy - 0.28 * Rg
    r = np.clip(np.sqrt((xx - hx) ** 2 + (yy - hy) ** 2) / (1.7 * Rg), 0, 1)[..., None]
    a = np.clip(r / 0.6, 0, 1)
    b = np.clip((r - 0.6) / 0.4, 0, 1)
    mid = _lerp(G0, G1, a)
    col = mid * (1 - b) + np.array(G2, np.float32) * b
    disc = ((xx - cx) ** 2 + (yy - cy) ** 2) <= Rg * Rg
    alpha = (disc * 255).astype(np.float32)[..., None]
    rgba = np.concatenate([col, alpha], axis=2).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def _circle(d, cx, cy, r, **kw):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], **kw)


def render(px):
    S = px * 4                                              # 4x supersample -> crisp edges
    inset = max(1, round(0.012 * S))
    rad = round(0.225 * S)
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, S - 1 - inset, S - 1 - inset],
                                           radius=rad, fill=255)
    base.paste(_metal(S), (0, 0), mask)
    base.alpha_composite(Image.composite(_gloss(S), Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))

    cx = cy = S // 2
    Rg = int(0.30 * S)                                      # glass radius
    d = ImageDraw.Draw(base)

    # lens drop shadow + nested metal housing rings
    off = round(0.012 * S)
    _circle(d, cx, cy + off, int(1.37 * Rg), fill=(8, 12, 16, 90))
    _circle(d, cx, cy, int(1.35 * Rg), fill=(58, 61, 66, 255))
    _circle(d, cx, cy, int(1.275 * Rg), fill=(23, 25, 28, 255))
    _circle(d, cx, cy, int(1.10 * Rg), fill=(10, 11, 13, 255))

    # glass
    base.alpha_composite(_glass(S, cx, cy, Rg))
    d = ImageDraw.Draw(base)

    # aperture pupil (darkest at centre)
    _circle(d, cx, cy, int(0.20 * Rg), fill=(5, 6, 10, 255))
    # cyan lens-coating flare ring
    _circle(d, cx, cy, int(0.75 * Rg), outline=(42, 216, 255, 95), width=max(1, round(0.02 * Rg)))

    # soft blurred highlight (upper-left)
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    hx = cx - int(0.32 * Rg); hy = cy - int(0.38 * Rg)
    hd.ellipse([hx - int(0.5 * Rg), hy - int(0.28 * Rg), hx + int(0.5 * Rg), hy + int(0.28 * Rg)],
               fill=(255, 255, 255, 72))
    base.alpha_composite(hl.filter(ImageFilter.GaussianBlur(radius=max(1, int(0.10 * Rg)))))
    d = ImageDraw.Draw(base)

    # sharp specular glint + cyan flare dots + pupil catchlight
    _circle(d, cx + int(0.30 * Rg), cy - int(0.35 * Rg), int(0.10 * Rg), fill=(255, 255, 255, 225))
    _circle(d, cx + int(0.50 * Rg), cy + int(0.50 * Rg), int(0.060 * Rg), fill=(42, 216, 255, 150))
    _circle(d, cx + int(0.62 * Rg), cy + int(0.58 * Rg), int(0.045 * Rg), fill=(120, 235, 255, 110))
    _circle(d, cx - int(0.10 * Rg), cy - int(0.10 * Rg), int(0.05 * Rg), fill=(106, 200, 224, 180))

    return base.resize((px, px), Image.LANCZOS)


def main():
    sizes = [256, 128, 64, 48, 32, 24, 16]
    imgs = [render(s) for s in sizes]
    imgs[0].save(ICO, format="ICO", append_images=imgs[1:])
    # previews for review
    render(256).save(os.path.join(HERE, "icon_preview_256.png"))
    strip_sizes = [256, 128, 64, 48, 32, 16]
    pad = 12; H = 256
    strip = Image.new("RGBA", (sum(strip_sizes) + pad * (len(strip_sizes) + 1), H + pad * 2), (32, 38, 46, 255))
    x = pad
    for s in strip_sizes:
        im = render(s)
        strip.alpha_composite(im, (x, pad + (H - s)))
        x += s + pad
    strip.save(os.path.join(HERE, "icon_preview_strip.png"))
    print("icon written:", ICO)


if __name__ == "__main__":
    main()
