"""Generate high-DPI PWA icons and favicons for VaultSwap."""

import os
from PIL import Image, ImageDraw, ImageFilter

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "static", "img", "icons")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def draw_master_icon(size: int, is_maskable: bool = False) -> Image.Image:
    # Render at 4x resolution for pristine anti-aliasing
    scale = 4
    render_size = size * scale
    img = Image.new("RGBA", (render_size, render_size), (2, 6, 23, 255))
    draw = ImageDraw.Draw(img)

    s = render_size / 512.0
    center = render_size / 2.0

    # Ambient radial glow in emerald (#10b981)
    glow_steps = 30
    max_radius = 230 * s
    for i in range(glow_steps):
        r = max_radius * (1 - i / glow_steps)
        alpha = int(18 * (1 - (r / max_radius)))
        bbox = [center - r, center - r, center + r, center + r]
        draw.ellipse(bbox, fill=(16, 185, 129, alpha))

    # Badge geometry
    # Maskable safe-zone is central 80% circle, so badge fits comfortably inside safe zone
    badge_size = 280 * s if is_maskable else 348 * s
    badge_rad = 68 * s if is_maskable else 82 * s
    x0 = (render_size - badge_size) / 2.0
    y0 = (render_size - badge_size) / 2.0
    x1 = x0 + badge_size
    y1 = y0 + badge_size

    # Soft badge shadow
    if not is_maskable:
        shadow = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sdraw.rounded_rectangle(
            [x0, y0 + 10 * s, x1, y1 + 10 * s],
            radius=int(badge_rad),
            fill=(0, 0, 0, 140),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(16 * s))
        img = Image.alpha_composite(img, shadow)

    # Emerald-to-teal gradient badge
    badge = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(badge)
    bdraw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=int(badge_rad),
        fill=(16, 185, 129, 255),  # emerald-500
    )

    # Gradient overlay (teal-600 towards bottom-right)
    grad_mask = Image.new("L", (render_size, render_size), 0)
    gmdraw = ImageDraw.Draw(grad_mask)
    for step in range(int(badge_size)):
        ratio = step / badge_size
        alpha = int(ratio * 160)
        gmdraw.line([(x0, y0 + step), (x1, y0 + step)], fill=alpha)
    
    teal_layer = Image.new("RGBA", (render_size, render_size), (13, 148, 136, 255))
    badge.paste(teal_layer, (0, 0), grad_mask)

    # Add inner border highlight for subtle tactile depth
    bdraw = ImageDraw.Draw(badge)
    bdraw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=int(badge_rad),
        outline=(255, 255, 255, 55),
        width=max(int(3 * s), 1),
    )

    img = Image.alpha_composite(img, badge)
    draw = ImageDraw.Draw(img)

    # Dual-arrow trade icon in dark slate (#020617)
    glyph_color = (2, 6, 23, 255)
    line_w = max(int(26 * s if not is_maskable else 21 * s), 2)

    cx, cy = center, center
    offset_x = 44 * s if not is_maskable else 35 * s
    arrow_len = 68 * s if not is_maskable else 54 * s
    wing_w = 34 * s if not is_maskable else 27 * s
    wing_h = 34 * s if not is_maskable else 27 * s

    # Left upward arrow (sending card)
    lx = cx - offset_x
    draw.line([(lx, cy + arrow_len), (lx, cy - arrow_len)], fill=glyph_color, width=line_w)
    draw.line([(lx, cy - arrow_len), (lx - wing_w, cy - arrow_len + wing_h)], fill=glyph_color, width=line_w)
    draw.line([(lx, cy - arrow_len), (lx + wing_w, cy - arrow_len + wing_h)], fill=glyph_color, width=line_w)

    # Right downward arrow (receiving card)
    rx = cx + offset_x
    draw.line([(rx, cy - arrow_len), (rx, cy + arrow_len)], fill=glyph_color, width=line_w)
    draw.line([(rx, cy + arrow_len), (rx - wing_w, cy + arrow_len - wing_h)], fill=glyph_color, width=line_w)
    draw.line([(rx, cy + arrow_len), (rx + wing_w, cy + arrow_len - wing_h)], fill=glyph_color, width=line_w)

    # Downsample with high-fidelity Lanczos
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main():
    print("Generating VaultSwap PWA icons...")
    targets = [
        ("icon-512.png", 512, False),
        ("icon-192.png", 192, False),
        ("icon-maskable-512.png", 512, True),
        ("icon-maskable-192.png", 192, True),
        ("apple-touch-icon.png", 180, False),
        ("favicon-32x32.png", 32, False),
        ("favicon-16x16.png", 16, False),
    ]

    for filename, size, maskable in targets:
        path = os.path.join(OUTPUT_DIR, filename)
        icon = draw_master_icon(size, is_maskable=maskable)
        icon.save(path, "PNG", optimize=True)
        print(f"  [OK] Saved {filename} ({size}x{size}, maskable={maskable})")

    # Generate multi-size favicon.ico
    ico_path = os.path.join(os.path.dirname(__file__), "static", "favicon.ico")
    icon_16 = draw_master_icon(16, is_maskable=False)
    icon_32 = draw_master_icon(32, is_maskable=False)
    icon_48 = draw_master_icon(48, is_maskable=False)
    icon_32.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
        append_images=[icon_16, icon_48],
    )
    print(f"  [OK] Saved favicon.ico (16, 32, 48px)")


if __name__ == "__main__":
    main()
