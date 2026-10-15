"""Regenerate magic.ico (white bold M on a black rounded square, like web/favicon.svg). Needs Pillow; the .ico is committed."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZES = [16, 24, 32, 48, 64, 256]
S = 1024  # draw large, then downsample


def render():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S * 7 // 32, fill=(0, 0, 0, 255))
    font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", S * 21 // 32)
    d.text((S / 2, S * 23.5 / 32), "M", font=font, fill="white", anchor="ms")
    return img


if __name__ == "__main__":
    big = render()
    out = Path(__file__).with_name("magic.ico")
    big.save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print("wrote", out)
