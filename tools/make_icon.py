"""生成应用图标: assets/app_icon.ico (多尺寸) + assets/app_icon.png (预览)。

设计语义 (图片压缩工具):
  - 蓝色圆角底 (主题蓝 #378ADD)
  - 白色照片卡 + 山峰 + 太阳 = 图片
  - 下方向下箭头 + 收拢底线 = 体积压缩变小

用法: python tools/make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

BRAND = (55, 138, 221, 255)      # #378ADD 主题蓝
BRAND_DARK = (31, 95, 168, 255)  # 山 (深)
BRAND_MID = (62, 134, 196, 255)  # 山 (浅)
SUN = (245, 194, 74, 255)        # 太阳
WHITE = (255, 255, 255, 255)

SS = 4  # 超采样倍数 (先画大图再缩小, 获得平滑边缘)


def _draw(size: int, simple: bool = False) -> Image.Image:
    s = size * SS
    u = s / 256.0  # 1 单位 = 256 坐标系下的 1px
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景 (圆角方形)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(58 * u), fill=BRAND)

    # 照片卡
    card = [56 * u, 44 * u, 200 * u, 152 * u]
    d.rounded_rectangle(card, radius=int(14 * u), fill=WHITE)

    if not simple:
        # 太阳
        r = 13 * u
        cx, cy = 172 * u, 72 * u
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SUN)
        # 山峰 (底边贴卡片底边, 天然不溢出)
        d.polygon([(58 * u, 152 * u), (110 * u, 88 * u), (162 * u, 152 * u)],
                  fill=BRAND_DARK)
        d.polygon([(126 * u, 152 * u), (164 * u, 114 * u), (202 * u, 152 * u)],
                  fill=BRAND_MID)

    # 压缩箭头 (竖干 + 三角 + 底线)
    d.rectangle([120 * u, 162 * u, 136 * u, 200 * u], fill=WHITE)
    d.polygon([(104 * u, 192 * u), (152 * u, 192 * u), (128 * u, 224 * u)],
              fill=WHITE)
    d.rounded_rectangle([92 * u, 230 * u, 164 * u, 238 * u],
                        radius=int(4 * u), fill=WHITE)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    big = _draw(256)
    big.save(ASSETS / "app_icon.png")

    # ICO: 256/128/64/48/32/24/16; 小尺寸用简化版 (去掉山与太阳细节)
    frames = []
    for size in (256, 128, 64, 48, 32, 24, 16):
        frames.append(_draw(size, simple=(size <= 24)))

    ico_path = ASSETS / "app_icon.ico"
    frames[0].save(
        ico_path, format="ICO",
        sizes=[(f.width, f.height) for f in frames],
        append_images=frames[1:],
    )
    print(f"saved: {ico_path} ({ico_path.stat().st_size} bytes)")
    print(f"saved: {ASSETS / 'app_icon.png'}")


if __name__ == "__main__":
    main()
