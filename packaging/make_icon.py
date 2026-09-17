# -*- coding: utf-8 -*-
"""
make_icon.py —— 生成 exe 图标（packaging/h9dash.ico）

仿墨水屏的纸感：纸白底 + 黑色「墨」字 + 一圈设备边框。
和渲染器同一个字体候选链（微软雅黑 → 黑体 → 宋体），
在任何中文 Windows 上都能出字；一个都找不到就用 PIL 默认字体，
顶多难看，不会失败。

用法：
    python packaging/make_icon.py     # 在仓库根目录跑
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 Bold
    r"C:\Windows\Fonts\msyh.ttc",     # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    r"C:\Windows\Fonts\simsun.ttc",   # 宋体
]

PAPER = "#f6f5f0"      # 纸白（不是纯白，留一点纸的暖）
INK = "#141414"        # 墨黑
GRAY = "#8a8a8a"


def font(size):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def main():
    s = 256
    img = Image.new("RGB", (s, s), PAPER)
    d = ImageDraw.Draw(img)

    # 设备边框：圆角、粗一点，像机器的塑框
    d.rounded_rectangle([10, 10, s - 10, s - 10], radius=30,
                        outline=INK, width=11)

    # 主体一个「墨」字
    d.text((s // 2, s // 2 - 8), "墨", font=font(150), fill=INK, anchor="mm")

    # 右下角小字，像机器上的丝印型号
    d.text((s - 34, s - 36), "H9", font=font(40), fill=GRAY, anchor="rb")

    img.save(HERE / "h9dash_icon.png")
    # 多尺寸一把梭：16 的任务栏、32 的 Alt-Tab、256 的资源管理器大图标
    img.save(HERE / "h9dash.ico",
             sizes=[(x, x) for x in (16, 24, 32, 48, 64, 128, 256)])
    print("OK ->", HERE / "h9dash.ico")


if __name__ == "__main__":
    main()
