"""Colors, fonts and the app icon (drawn with Pillow, so no image files are needed)."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = "#0a0e0b"        # window
PANEL = "#0f1611"     # cards
FIELD = "#060906"     # inputs / log
LINE = "#1d3324"      # borders
SEL = "#12281a"       # selected card
HOVER = "#13201a"
FG = "#c9f2d4"        # body text
DIM = "#5e8a6b"       # secondary text
GREEN = "#3dff7a"     # phosphor accent
AMBER = "#ffbf3d"
RED = "#ff6262"
BLACK = "#000000"

MONO_CHOICES = ("Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas", "Courier New")


def pick_mono(families):
    return next((f for f in MONO_CHOICES if f in families), "Courier")


def _font(size):
    for name in ("CascadiaMono.ttf", "CascadiaCode.ttf", "consolab.ttf", "consola.ttf"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                pass
    return ImageFont.load_default()


def make_icon(size=256, active=True):
    """A rounded terminal tile with a `>_` prompt. Dimmed when Even Terminal isn't running."""
    s = 4  # supersample for smooth edges
    big = size * s
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    green = (61, 255, 122) if active else (94, 138, 107)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=big // 5, fill=(10, 14, 11, 255),
                        outline=green, width=max(big // 22, 2))
    font = _font(int(big * 0.52))
    text = ">_"
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((big - w) / 2 - box[0], (big - h) / 2 - box[1] - big * 0.02), text, font=font, fill=green)
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path):
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    make_icon(256).save(path, format="ICO", sizes=[(n, n) for n in sizes])


if __name__ == "__main__":
    write_ico(Path(__file__).with_name("g2switcher.ico"))
    print("wrote g2switcher.ico")
