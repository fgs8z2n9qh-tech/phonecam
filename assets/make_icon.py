"""PhoneCam app icon generator -> assets/icon.ico  (run: python assets/make_icon.py)

A modern squircle app icon: diagonal green gradient + top-left gloss, a glossy
camera "lens" with broadcast arcs (wireless webcam), and the "PhoneCam" wordmark.
Small sizes (<=48px) are rendered glyph-only so the icon stays legible in the taskbar.
"""
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ICO = os.path.join(HERE, "icon.ico")

C0 = (78, 226, 138)   # light emerald (top-left)
C1 = (20, 120, 62)    # deep green   (bottom-right)
FONT_PATHS = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/seguisb.ttf",
              "C:/Windows/Fonts/arialbd.ttf"]


def _font(px):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _gradient(S):
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    t = ((xx + yy) / (2 * (S - 1)))[..., None]
    rgb = (np.array(C0, np.float32) * (1 - t) + np.array(C1, np.float32) * t)
    arr = np.concatenate([rgb, np.full((S, S, 1), 255.0)], axis=2).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _gloss(S):
    """soft white radial highlight near the top-left -> glossy look."""
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    r = np.sqrt((xx - 0.30 * S) ** 2 + (yy - 0.20 * S) ** 2) / (0.95 * S)
    a = np.clip(1 - r, 0, 1) ** 1.7 * 95
    img = np.zeros((S, S, 4), np.uint8)
    img[..., :3] = 255
    img[..., 3] = a.astype(np.uint8)
    return Image.fromarray(img, "RGBA")


def _wordmark(img, S):
    d = ImageDraw.Draw(img)
    p1, p2 = "Phone", "Cam"
    fs = int(0.145 * S)
    f = _font(fs)
    w1 = d.textlength(p1, font=f); w2 = d.textlength(p2, font=f)
    total = w1 + w2 or 1
    fs = int(fs * (0.82 * S) / total)          # scale to ~82% width
    f = _font(fs)
    w1 = d.textlength(p1, font=f); w2 = d.textlength(p2, font=f); total = w1 + w2
    bb = f.getbbox("PhoneCam")
    th = bb[3] - bb[1]
    x0 = (S - total) / 2
    y = 0.845 * S - th / 2 - bb[1]             # vertically centered in the lower band
    sh = max(1, round(0.006 * S))
    d.text((x0 + sh, y + sh), p1, font=f, fill=(6, 28, 15, 150))
    d.text((x0 + w1 + sh, y + sh), p2, font=f, fill=(6, 28, 15, 150))
    d.text((x0, y), p1, font=f, fill=(255, 255, 255, 255))
    d.text((x0 + w1, y), p2, font=f, fill=(206, 247, 222, 255))


def render(px, with_text):
    S = px * 4                                  # 4x supersample -> crisp edges
    inset = max(1, round(0.012 * S))
    rad = round(0.225 * S)
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, S - 1 - inset, S - 1 - inset],
                                           radius=rad, fill=255)
    base.paste(_gradient(S), (0, 0), mask)
    base.alpha_composite(Image.composite(_gloss(S), Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))

    d = ImageDraw.Draw(base)
    cx = S // 2
    cy = int((0.435 if with_text else 0.5) * S)
    R = int((0.205 if with_text else 0.285) * S)

    # broadcast arcs flanking the lens (wireless / live)
    aw = max(2, round(0.024 * S))
    for rr, (a0, a1) in [(int(R * 1.36), (148, 212)), (int(R * 1.36), (-32, 32)),
                         (int(R * 1.66), (156, 204)), (int(R * 1.66), (-24, 24))]:
        d.arc([cx - rr, cy - rr, cx + rr, cy + rr], a0, a1, fill=(255, 255, 255, 170), width=aw)

    # lens drop shadow
    off = round(0.014 * S); rs = int(R * 1.04)
    d.ellipse([cx - rs, cy - rs + off, cx + rs, cy + rs + off], fill=(6, 38, 22, 95))
    # white rim
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=(255, 255, 255, 255))
    # glass (nested rings -> depth)
    for rr, col in [(int(R * 0.80), (14, 52, 34)), (int(R * 0.62), (24, 84, 54)),
                    (int(R * 0.42), (44, 130, 84))]:
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=col + (255,))
    # speculars
    hr = int(R * 0.20); hx, hy = cx - int(R * 0.30), cy - int(R * 0.33)
    d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=(255, 255, 255, 215))
    hr2 = int(R * 0.085); sx, sy = cx + int(R * 0.20), cy + int(R * 0.22)
    d.ellipse([sx - hr2, sy - hr2, sx + hr2, sy + hr2], fill=(255, 255, 255, 150))

    if with_text:
        _wordmark(base, S)
    return base.resize((px, px), Image.LANCZOS)


def main():
    sizes = [256, 128, 64, 48, 32, 24, 16]
    imgs = [render(s, s >= 64) for s in sizes]      # wordmark only at >=64px
    imgs[0].save(ICO, format="ICO", append_images=imgs[1:])
    # previews for review
    render(256, True).save(os.path.join(HERE, "icon_preview_256.png"))
    strip_sizes = [256, 128, 64, 48, 32, 16]
    pad = 12; H = 256
    strip = Image.new("RGBA", (sum(strip_sizes) + pad * (len(strip_sizes) + 1), H + pad * 2), (32, 38, 46, 255))
    x = pad
    for s in strip_sizes:
        im = render(s, s >= 64)
        strip.alpha_composite(im, (x, pad + (H - s)))
        x += s + pad
    strip.save(os.path.join(HERE, "icon_preview_strip.png"))
    print("icon written:", ICO)


if __name__ == "__main__":
    main()
