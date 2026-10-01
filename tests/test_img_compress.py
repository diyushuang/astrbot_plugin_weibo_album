"""img_compress 离线单测：超大图重压的改名/原地覆盖/失败保留。

python tests/test_img_compress.py

main.py 依赖 astrbot 离线导不进来，所以压缩逻辑独立成模块，这里直接覆盖：
JPEG 原地覆盖并缩长边、PNG 换 .jpg 后缀并垫白底、压完反而更大或动图保留原图。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import ImageDraw  # noqa: E402

from img_compress import PILImage, recompress_image  # noqa: E402


def ok(msg):
    print(f"[ok] {msg}")


def main():
    if PILImage is None:
        print("[skip] 未安装 Pillow，跳过 img_compress 单测")
        return

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        # 超大 JPEG（长边超上限）：重压后原地覆盖，分辨率缩到长边内
        big = PILImage.effect_noise((6000, 4000), 64)
        src = d / "big.jpg"
        big.save(src, "JPEG", quality=100)
        assert recompress_image(src, long_side=5000, quality=85)
        with PILImage.open(src) as im:
            assert im.format == "JPEG" and im.size == (5000, 3333), im.size
        assert not list(d.glob("*.cp.jpg")), "压缩完不能留 .cp.jpg 暂存"
        ok("超大 JPEG：原地覆盖重压，长边缩进上限，无暂存残留")

        # 带透明的 PNG：转 JPEG 时垫白底，文件名换成 .jpg，原 PNG 删掉
        noise = PILImage.effect_noise((1600, 1200), 64).convert("RGBA")
        alpha = PILImage.new("L", noise.size, 255)
        ImageDraw.Draw(alpha).rectangle(
            [0, 0, noise.size[0], noise.size[1] // 2], fill=0
        )
        noise.putalpha(alpha)
        p = d / "shot.png"
        noise.save(p, "PNG")
        assert recompress_image(p, long_side=5000, quality=85)
        assert not p.exists(), "原 PNG 应当删掉"
        out = d / "shot.jpg"
        assert out.exists()
        with PILImage.open(out) as im:
            assert im.format == "JPEG" and im.mode == "RGB"
            top = im.getpixel((800, 10))  # 全透明区 -> 垫白
            assert all(abs(v - 255) < 10 for v in top[:3]), top
        ok("透明 PNG：换成 .jpg 落盘，透明区垫白底，原文件清理")

        # 压完反而更大（纯色小 PNG）：保留原图返回 False，两边都不留垃圾
        tiny = d / "tiny.png"
        PILImage.new("RGB", (100, 100), (30, 144, 255)).save(tiny, "PNG")
        before = tiny.stat().st_size
        assert not recompress_image(tiny, long_side=5000, quality=85)
        assert tiny.exists() and tiny.stat().st_size == before
        assert not list(d.glob("*.cp.jpg"))
        ok("压完更大时保留原图，原文件与目录状态不变")

        # 动图：GIF 转成 JPEG 会丢帧，直接拒绝
        g = d / "anim.gif"
        frame = PILImage.new("RGB", (3000, 2000), (10, 200, 10))
        frame.save(
            g,
            save_all=True,
            append_images=[PILImage.new("RGB", (3000, 2000), (200, 10, 10))],
        )
        assert not recompress_image(g, long_side=5000, quality=85)
        assert g.exists()
        ok("动图（GIF）拒绝重压，原样保留")

    print("\nimg_compress 离线单测全部通过")


if __name__ == "__main__":
    main()
