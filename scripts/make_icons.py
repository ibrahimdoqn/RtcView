#!/usr/bin/env python3
"""Generate RtcView app icons (SVG + PNGs) — Apple-style camera lens on rounded square."""
from PIL import Image, ImageDraw, ImageFilter, ImageChops
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
OUT.mkdir(parents=True, exist_ok=True)


def lerp(a, b, t):
    return int(a * (1 - t) + b * t)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def vertical_gradient(size, c1, c2):
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        col = (lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t), 255)
        for x in range(size):
            px[x, y] = col
    return img


def radial_disc(size, cx, cy, r_outer, c_center, c_edge):
    """Draw a filled circle with radial gradient from center to edge."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for r in range(r_outer, 0, -1):
        t = 1 - r / r_outer
        col = (
            lerp(c_edge[0], c_center[0], t),
            lerp(c_edge[1], c_center[1], t),
            lerp(c_edge[2], c_center[2], t),
            255,
        )
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    return layer


def soft_glow(size, cx, cy, r, color, max_alpha=180):
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(r, 0, -1):
        t = 1 - i / r
        a = int(max_alpha * t * t)
        d.ellipse([cx - i, cy - i, cx + i, cy + i], fill=(color[0], color[1], color[2], a))
    return layer


def make_icon(size: int) -> Image.Image:
    S = size
    # 1) Rounded-square background with vertical gradient (deep navy → black)
    bg = vertical_gradient(S, (36, 46, 80), (8, 12, 22))
    mask = rounded_mask(S, int(S * 0.225))
    bg.putalpha(mask)
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    canvas.alpha_composite(bg)

    # 2) Subtle top highlight (glassy sheen)
    sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sheen)
    for y in range(S // 2):
        t = 1 - y / (S / 2)
        sd.line([(0, y), (S, y)], fill=(255, 255, 255, int(28 * t)))
    sheen_alpha = ImageChops.multiply(sheen.split()[3], mask)
    sheen.putalpha(sheen_alpha)
    canvas.alpha_composite(sheen)

    cx, cy = S // 2, int(S * 0.52)

    # 3) Lens shadow (soft ring under lens)
    shadow = soft_glow(S, cx, cy + int(S * 0.02), int(S * 0.36), (0, 0, 0), max_alpha=110)
    canvas.alpha_composite(shadow)

    # 4) Outer bezel (metallic ring)
    bezel_r = int(S * 0.32)
    bezel = radial_disc(S, cx, cy, bezel_r, (140, 150, 168), (60, 70, 85))
    canvas.alpha_composite(bezel)

    # Thin dark ring around inside
    d = ImageDraw.Draw(canvas)
    inr = int(S * 0.285)
    d.ellipse([cx - inr, cy - inr, cx + inr, cy + inr], fill=(10, 14, 22, 255))

    # 5) Blue glass lens (radial gradient, bright rim → deep center)
    glass_r = int(S * 0.26)
    glass = radial_disc(S, cx, cy, glass_r, (2, 12, 40), (30, 130, 255))
    canvas.alpha_composite(glass)

    # 6) Very inner dark
    innermost = int(S * 0.13)
    d = ImageDraw.Draw(canvas)
    d.ellipse([cx - innermost, cy - innermost, cx + innermost, cy + innermost], fill=(2, 6, 20, 255))

    # 7) Big soft glint (top-left of lens)
    gx = cx - int(S * 0.09)
    gy = cy - int(S * 0.10)
    glint = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glint)
    gr = int(S * 0.14)
    for r in range(gr, 0, -1):
        t = 1 - r / gr
        a = int(210 * t * t * t)
        gd.ellipse([gx - r, gy - r * 0.75, gx + r, gy + r * 0.75], fill=(255, 255, 255, a))
    # Clip glint to inside the lens
    lens_clip = Image.new("L", (S, S), 0)
    ImageDraw.Draw(lens_clip).ellipse([cx - glass_r, cy - glass_r, cx + glass_r, cy + glass_r], fill=255)
    glint.putalpha(ImageChops.multiply(glint.split()[3], lens_clip))
    canvas.alpha_composite(glint)

    # 8) Tiny second highlight (bottom-right, cool blue reflection)
    tiny = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tiny)
    tx, ty = cx + int(S * 0.11), cy + int(S * 0.12)
    tr = int(S * 0.045)
    for r in range(tr, 0, -1):
        t = 1 - r / tr
        a = int(140 * t * t)
        td.ellipse([tx - r, ty - r, tx + r, ty + r], fill=(160, 200, 255, a))
    tiny.putalpha(ImageChops.multiply(tiny.split()[3], lens_clip))
    canvas.alpha_composite(tiny)

    # 9) Red REC LED (top-right corner)
    lx, ly = int(S * 0.78), int(S * 0.20)
    led_halo = soft_glow(S, lx, ly, int(S * 0.075), (255, 80, 80), max_alpha=140)
    canvas.alpha_composite(led_halo)
    d = ImageDraw.Draw(canvas)
    lr = int(S * 0.028)
    d.ellipse([lx - lr, ly - lr, lx + lr, ly + lr], fill=(255, 69, 58, 255))
    # Small highlight on LED
    d.ellipse([lx - lr // 2, ly - lr, lx, ly - lr // 2], fill=(255, 200, 200, 200))

    # 10) Final rounded clip (safety)
    final_mask = rounded_mask(S, int(S * 0.225))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), final_mask)
    return out


SVG_LOGO = """<svg viewBox="0 0 200 200" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="RtcView">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#242e50"/><stop offset="1" stop-color="#080c16"/>
    </linearGradient>
    <radialGradient id="lens" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="#020a28"/><stop offset="0.55" stop-color="#0d3aa8"/><stop offset="1" stop-color="#1e82ff"/>
    </radialGradient>
    <linearGradient id="bezel" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#8c96a8"/><stop offset="1" stop-color="#3c4655"/>
    </linearGradient>
    <radialGradient id="glint" cx="0.35" cy="0.32" r="0.35">
      <stop offset="0" stop-color="rgba(255,255,255,0.85)"/><stop offset="1" stop-color="rgba(255,255,255,0)"/>
    </radialGradient>
    <radialGradient id="ledHalo" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="rgba(255,80,80,0.6)"/><stop offset="1" stop-color="rgba(255,80,80,0)"/>
    </radialGradient>
    <clipPath id="app"><rect x="0" y="0" width="200" height="200" rx="45"/></clipPath>
  </defs>
  <g clip-path="url(#app)">
    <rect x="0" y="0" width="200" height="200" fill="url(#bg)"/>
    <rect x="0" y="0" width="200" height="100" fill="rgba(255,255,255,0.06)"/>
    <circle cx="100" cy="104" r="66" fill="rgba(0,0,0,0.4)"/>
    <circle cx="100" cy="104" r="64" fill="url(#bezel)"/>
    <circle cx="100" cy="104" r="57" fill="#0a0e18"/>
    <circle cx="100" cy="104" r="52" fill="url(#lens)"/>
    <circle cx="100" cy="104" r="26" fill="#02061a"/>
    <ellipse cx="82" cy="86" rx="24" ry="18" fill="url(#glint)"/>
    <circle cx="156" cy="40" r="15" fill="url(#ledHalo)"/>
    <circle cx="156" cy="40" r="6" fill="#ff453a"/>
    <circle cx="154" cy="38" r="1.8" fill="rgba(255,220,220,0.9)"/>
  </g>
</svg>"""


def main():
    for size, name in [(192, "icon-192.png"), (512, "icon-512.png"), (64, "icon-64.png"), (32, "icon-32.png")]:
        img = make_icon(size)
        img.save(OUT / name, "PNG", optimize=True)
        print("wrote", OUT / name)
    (OUT / "icon.svg").write_text(SVG_LOGO, encoding="utf-8")
    print("wrote", OUT / "icon.svg")


if __name__ == "__main__":
    main()
