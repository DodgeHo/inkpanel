#!/usr/bin/env python3
"""Small local HTTP server for a 16-level grayscale H9 dashboard."""

from __future__ import annotations

import argparse
import json
import socket
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.example.json"


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    width = int(data.get("width", 825))
    height = int(data.get("height", 1200))
    if not 100 <= width <= 4000 or not 100 <= height <= 4000:
        raise ValueError("width/height must be between 100 and 4000")
    data["width"] = width
    data["height"] = height
    return data


def pick_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def quantize_16_gray(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    # Ordered dithering keeps broad gray regions readable on a 16-level E-Ink panel.
    return gray.quantize(colors=16, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.ORDERED).convert("L")


def render_dashboard(config: dict[str, Any]) -> Image.Image:
    width, height = config["width"], config["height"]
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    title_font = pick_font(max(28, width // 24))
    body_font = pick_font(max(22, width // 34))
    small_font = pick_font(max(17, width // 46))
    margin = max(28, width // 18)
    now = datetime.now().astimezone()

    draw.rectangle((0, 0, width - 1, height - 1), outline=0, width=4)
    draw.text((margin, margin), str(config.get("title", "TOPSIR H9")), fill=0, font=title_font)
    draw.text((margin, margin + title_font.size + 12), str(config.get("subtitle", "E-Ink dashboard")), fill=80, font=small_font)
    draw.text((width - margin - 260, margin + 8), now.strftime("%Y-%m-%d"), fill=0, font=body_font)
    draw.text((width - margin - 190, margin + 8 + body_font.size + 8), now.strftime("%H:%M"), fill=0, font=title_font)

    top = margin * 3 + title_font.size + body_font.size
    box_gap = max(18, margin // 2)
    box_height = max(150, (height - top - margin * 2 - box_gap * 2) // 3)
    boxes = [
        ("WEATHER", "Configure PC-side weather collector"),
        ("CALENDAR", "No calendar source configured"),
        ("TODO", "Ready for local tasks or a JSON feed"),
    ]
    for index, (heading, message) in enumerate(boxes):
        y = top + index * (box_height + box_gap)
        draw.rectangle((margin, y, width - margin, y + box_height), outline=0, width=3)
        draw.text((margin + 22, y + 22), heading, fill=0, font=body_font)
        draw.line((margin + 22, y + 22 + body_font.size + 12, width - margin - 22, y + 22 + body_font.size + 12), fill=0, width=2)
        draw.text((margin + 22, y + 70 + body_font.size), message, fill=50, font=small_font)

    footer = f"Generated {now.strftime('%H:%M:%S')}  |  {width}x{height}  |  16 gray levels"
    draw.text((margin, height - margin - small_font.size), footer, fill=0, font=small_font)
    return quantize_16_gray(image)


class DashboardHandler(BaseHTTPRequestHandler):
    config: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/health":
            body = json.dumps({"ok": True, "dashboard": "/dash.png", "size": [self.config["width"], self.config["height"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split("?", 1)[0] == "/dash.png":
            from io import BytesIO

            buffer = BytesIO()
            render_dashboard(self.config).save(buffer, format="PNG", optimize=True)
            body = buffer.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    config = load_config(args.config)
    DashboardHandler.config = config
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard listening on http://{socket.gethostbyname(socket.gethostname())}:{args.port}/dash.png")
    print(f"Health endpoint: http://127.0.0.1:{args.port}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
