"""Generate high-DPI PWA icons and favicons for VaultSwap from the VaultSwap logo."""

import os
from PIL import Image, ImageDraw, ImageFilter

BASE_DIR = os.path.dirname(__file__)
LOGO_PATH = os.path.join(BASE_DIR, "static", "img", "vaultswap-logo.jpg")
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "img", "icons")
os.makedirs(OUTPUT_DIR, exist_ok=True)

BG_COLOR = (2, 6, 23, 255)  # #020617 (matches manifest background_color & theme_color)


def _load_logo_rgba() -> Image.Image:
    if not os.path.exists(LOGO_PATH):
        raise FileNotFoundError(f"VaultSwap logo not found at {LOGO_PATH}")
    return Image.open(LOGO_PATH).convert("RGBA")


def draw_master_icon(size: int, is_maskable: bool = False, flatten_bg: bool = False) -> Image.Image:
    """Render a square PWA/favicon asset from vaultswap-logo.jpg."""
    scale = 4
    render_size = size * scale
    canvas = Image.new("RGBA", (render_size, render_size), BG_COLOR if (is_maskable or flatten_bg) else (0, 0, 0, 0))

    logo = _load_logo_rgba()
    lw, lh = logo.size

    if size <= 48:
        # At tiny favicon sizes, center-crop to square so the circular emblem fills the icon
        side = min(lw, lh)
        left = (lw - side) // 2
        top = (lh - side) // 2
        cropped = logo.crop((left, top, left + side, top + side))
        resized = cropped.resize((render_size, render_size), Image.Resampling.LANCZOS)

        # Rounded-square mask for crisp favicon edges
        mask = Image.new("L", (render_size, render_size), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle([0, 0, render_size, render_size], radius=int(render_size * 0.18), fill=255)
        canvas.paste(resized, (0, 0), mask)
        return canvas.resize((size, size), Image.Resampling.LANCZOS)

    # For app icons (180, 192, 512): preserve full logo width (including VAULTSWAP banner)
    # Maskable icons keep the emblem inside the central 80% safe zone.
    target_ratio = 0.78 if is_maskable else 0.90
    target_w = int(render_size * target_ratio)
    target_h = int(target_w * (lh / lw))
    resized = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # Soften outer edges with a rounded-rectangle feather so it blends into #020617
    corner_rad = int(min(target_w, target_h) * 0.14)
    edge_mask = Image.new("L", (target_w, target_h), 0)
    edraw = ImageDraw.Draw(edge_mask)
    inset = max(int(4 * scale), 2)
    edraw.rounded_rectangle(
        [inset, inset, target_w - inset, target_h - inset],
        radius=corner_rad,
        fill=255,
    )
    edge_mask = edge_mask.filter(ImageFilter.GaussianBlur(radius=max(int(4 * scale), 2)))

    # If non-maskable transparent-friendly icon, still give it a solid #020617 rounded badge
    # so it looks great on both light and dark OS launchers and on the #020617 splash screen.
    if not is_maskable and not flatten_bg:
        badge_mask = Image.new("L", (render_size, render_size), 0)
        bdraw = ImageDraw.Draw(badge_mask)
        bdraw.rounded_rectangle(
            [0, 0, render_size, render_size],
            radius=int(render_size * 0.22),
            fill=255,
        )
        badge_bg = Image.new("RGBA", (render_size, render_size), BG_COLOR)
        canvas.paste(badge_bg, (0, 0), badge_mask)

    ox = (render_size - target_w) // 2
    oy = (render_size - target_h) // 2
    canvas.paste(resized, (ox, oy), edge_mask)

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def main():
    print("Generating VaultSwap PWA icons from static/img/vaultswap-logo.jpg...")
    targets = [
        ("icon-512.png", 512, False, False),
        ("icon-192.png", 192, False, False),
        ("icon-maskable-512.png", 512, True, True),
        ("icon-maskable-192.png", 192, True, True),
        ("apple-touch-icon.png", 180, False, True),
        ("favicon-32x32.png", 32, False, False),
        ("favicon-16x16.png", 16, False, False),
    ]

    for filename, size, maskable, flatten in targets:
        path = os.path.join(OUTPUT_DIR, filename)
        icon = draw_master_icon(size, is_maskable=maskable, flatten_bg=flatten)
        icon.save(path, "PNG", optimize=True)
        print(f"  [OK] Saved {filename} ({size}x{size}, maskable={maskable})")

    # Generate multi-size favicon.ico
    ico_path = os.path.join(BASE_DIR, "static", "favicon.ico")
    icon_16 = draw_master_icon(16, is_maskable=False)
    icon_32 = draw_master_icon(32, is_maskable=False)
    icon_48 = draw_master_icon(48, is_maskable=False)
    icon_32.save(
        ico_path,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
        append_images=[icon_16, icon_48],
    )
    print("  [OK] Saved favicon.ico (16, 32, 48px)")


if __name__ == "__main__":
    main()
