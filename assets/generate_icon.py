"""Generate Halo icon -- teal/cyan ring symbol on dark background."""
from PIL import Image, ImageDraw, ImageFont
import os

sizes = [256, 128, 64, 48, 32, 16]
icon_path = os.path.join(os.path.dirname(__file__), "halo.ico")
png_path = os.path.join(os.path.dirname(__file__), "halo.png")

# Create the main 256x256 image
size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Dark circle background
bg_color = (18, 18, 24, 255)
draw.ellipse([8, 8, size - 8, size - 8], fill=bg_color)

# Outer ring -- teal glow
ring_color = (0, 210, 210, 200)
draw.ellipse([8, 8, size - 8, size - 8], outline=ring_color, width=4)

# Inner halo ring (thicker, brighter)
cx, cy = size // 2, size // 2
halo_r = 70
for r in range(halo_r, halo_r - 6, -1):
    t = (r - (halo_r - 6)) / 6
    c = (int(0 + 50 * t), int(200 + 30 * t), int(200 + 30 * t), int(180 + 75 * t))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=2)

# Center dot -- bright teal
dot_r = 18
for r in range(dot_r, 0, -1):
    t = r / dot_r
    c = (int(0 + 50 * (1 - t)), int(230 * t), int(230 * t), 255)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)

# Highlight dot
draw.ellipse([cx - 8, cy - 8, cx - 2, cy - 2], fill=(255, 255, 255, 200))

# "H" letter at bottom
try:
    font = ImageFont.truetype("arial.ttf", 28)
except Exception:
    font = ImageFont.load_default()
draw.text((cx - 9, cy + 55), "H", fill=ring_color, font=font)

# Save PNG
img.save(png_path, "PNG")
print(f"Saved {png_path}")

# Generate multi-size ICO
ico_images = []
for s in sizes:
    ico_images.append(img.resize((s, s), Image.LANCZOS))
ico_images[0].save(icon_path, format="ICO", sizes=[(s, s) for s in sizes],
                   append_images=ico_images[1:])
print(f"Saved {icon_path}")
