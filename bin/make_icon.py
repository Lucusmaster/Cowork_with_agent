"""生成协作台的应用图标（Windows .ico）。

一张 256×256 的透明画布上画：深色圆角托盘 + 三个节点连成三角形，最上面那个节点里
点一个白点（代表"大脑"，另外两个是执行和复核）。配色写死，不随机，跑几遍都一样。

16 / 24 两档**按小网格单独画**，不是把大图缩小——缩放在 16 上会让三条连线和三个节点
糊成一团；16 这一档只留三个点，连线全去掉，小尺寸下"三点"反而认得出来。

跑法：

    python bin/make_icon.py            # 产出 assets/agent-team.ico 和 assets/agent-team.png
    python bin/make_icon.py --out DIR  # 写到别的目录（测试用，别碰已提交的产物）
"""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)

TRAY = "#121821"        # 托盘底
NODE = "#3FD3A6"        # 节点（亮青绿）
LINK = "#2C6F5B"        # 连线（压暗一档，别抢节点的视觉重量）
CORE = "white"          # 顶上那个节点里的白点


def _geometry(size: int) -> tuple[int, int, int, list[tuple[int, int]]]:
    """按目标尺寸算托盘边长、左上角、圆角半径和三个节点圆心（都以 256 为设计基准）。"""
    tray = round(size * 232 / 256)
    left = (size - tray) // 2
    radius = max(2, round(size * 56 / 256))
    points = [(round(size * 128 / 256), round(size * 84 / 256)),   # 上
              (round(size * 84 / 256), round(size * 168 / 256)),   # 左下
              (round(size * 172 / 256), round(size * 168 / 256))]  # 右下
    return tray, left, radius, points


def draw_icon(size: int) -> Image.Image:
    """画一档尺寸；同样的输入永远画出同样的像素。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    tray, left, radius, points = _geometry(size)
    draw.rounded_rectangle((left, left, left + tray - 1, left + tray - 1),
                           radius=radius, fill=TRAY)

    node_radius = max(1, round(size * 22 / 256))
    if size <= 16:
        # 小尺寸只留三个点：连线在这个网格上会把它们粘成一根横杠
        for x, y in points:
            draw.ellipse((x - node_radius, y - node_radius, x + node_radius, y + node_radius),
                         fill=NODE)
        return image

    width = max(1, round(size * 10 / 256))
    for a, b in ((points[0], points[1]), (points[1], points[2]), (points[2], points[0])):
        draw.line((a, b), fill=LINK, width=width)
    for x, y in points:
        draw.ellipse((x - node_radius, y - node_radius, x + node_radius, y + node_radius),
                     fill=NODE)

    if size >= 32:          # 24 上这个白点只有不到 1 像素，画了也看不见
        core = max(1, round(size * 8 / 256))
        x, y = points[0]
        draw.ellipse((x - core, y - core, x + core, y + core), fill=CORE)
    return image


def _bmp_frame(image: Image.Image) -> bytes:
    """一帧 32 位 BMP（ICO 里 128 及以下的标准存法，行是自下而上、通道是 BGRA）。"""
    width, height = image.size
    flipped = image.convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM)
    red, green, blue, alpha = flipped.split()
    pixels = Image.merge("RGBA", (blue, green, red, alpha)).tobytes()
    mask_row = ((width + 31) // 32) * 4          # 1bpp 透明掩码，每行按 32 位对齐
    mask = b"\x00" * (mask_row * height)         # 透明交给 alpha 通道，掩码全 0
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
                         len(pixels) + len(mask), 0, 0, 0, 0)
    return header + pixels + mask


def _png_frame(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """自己拼一个 ICO：128 及以下用 BMP，256 用 PNG。

    别用 Pillow 的 ICO 写入器——它把所有帧都压成 PNG，Windows 在 16..128 上读不出来，
    桌面快捷方式就变成一个白板（"像无法识别的文件"）。256 用 PNG 是惯例，Explorer 认。
    """
    frames = [(image.size[0], _png_frame(image) if image.size[0] > 128 else _bmp_frame(image))
              for image in images]
    frames.sort(key=lambda frame: frame[0])
    offset = 6 + 16 * len(frames)
    directory, payload = b"", b""
    for size, data in frames:
        dim = 0 if size >= 256 else size          # 目录里 0 表示 256
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        payload += data
    path.write_bytes(struct.pack("<HHH", 0, 1, len(frames)) + directory + payload)


def main(argv: list[str] | None = None) -> None:
    """默认写到仓库的 assets/；测试用 `--out <临时目录>`，免得改到已提交的二进制。"""
    args = list(sys.argv[1:] if argv is None else argv)
    out = ASSETS
    if "--out" in args:
        index = args.index("--out")
        if index + 1 >= len(args):
            raise SystemExit("--out 后面要跟一个目录")
        out = Path(args[index + 1]).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(out / "agent-team.png", format="PNG")
    write_ico(out / "agent-team.ico", [draw_icon(size) for size in SIZES])


if __name__ == "__main__":
    main()