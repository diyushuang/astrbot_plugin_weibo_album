"""超大图重压成 JPEG：内存保护的关键一步。

base64 上传载荷会把整张图在进程里变成好几份内存拷贝，再乘上并发与群数，
20MB 级的微博原图一批下来就能把小内存服务器顶进 swap。群相册场景长边 5000、
质量 85 的 JPEG 与原图观感无异，体积却差一个数量级。

独立成模块是为了离线单测能直接覆盖（main.py 依赖 astrbot，离线导不进来）；
Pillow 是可选依赖，没装时调用方按"压缩不可用"处理，绝不挡上传。
"""

import os
from pathlib import Path

try:
    from PIL import Image as PILImage
except ImportError:  # 没装 Pillow：调用方检测到 None 就跳过压缩按原图上传
    PILImage = None


def recompress_image(path: Path, long_side: int, quality: int) -> bool:
    """把一张大图重压成 <同名>.jpg（原本就是 .jpg 则原地覆盖），成功返回 True。

    调用方要放进线程池跑：解码加 lanczos 缩放对超大图是秒级 CPU 活，不能进事件循环。
    压完不比原图小（截图类 PNG 偶尔如此）、打不开或动图，都算失败保留原图。
    """
    if PILImage is None:
        return False
    tmp = path.with_suffix(".cp.jpg")
    try:
        with PILImage.open(path) as im:
            im.load()
            if getattr(im, "is_animated", False):
                return False
            if im.mode != "RGB":
                if im.mode in ("RGBA", "LA", "P") or "A" in im.getbands():
                    # JPEG 没有透明通道：带透明的（截图贴纸类 PNG）垫白底再转
                    rgba = im.convert("RGBA")
                    flat = PILImage.new("RGB", rgba.size, (255, 255, 255))
                    flat.paste(rgba, mask=rgba.getchannel("A"))
                    im = flat
                else:
                    im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > long_side:
                k = long_side / max(w, h)
                im = im.resize(
                    (max(1, round(w * k)), max(1, round(h * k))),
                    PILImage.LANCZOS,
                )
            im.save(tmp, "JPEG", quality=quality, optimize=True)
    except Exception:
        tmp.unlink(missing_ok=True)
        return False
    try:
        if tmp.stat().st_size == 0 or tmp.stat().st_size >= path.stat().st_size:
            tmp.unlink(missing_ok=True)
            return False
        target = path.with_suffix(".jpg")
        os.replace(tmp, target)
        if target != path:
            path.unlink(missing_ok=True)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
