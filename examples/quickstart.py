#!/usr/bin/env python3
"""快速上手示例：不经 MCP 协议，直接调用 11 个工具生成一张完整图纸。

运行：
    .venv/bin/python examples/quickstart.py

输出（均已 gitignore）：
    examples/quickstart.dxf   图纸本体
    examples/quickstart.png   PNG 预览
    examples/quickstart.pdf   矢量 PDF
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server as srv  # noqa: E402

OUT = Path(__file__).resolve().parent / "quickstart.dxf"
PNG = OUT.with_suffix(".png")
PDF = OUT.with_suffix(".pdf")


def main() -> None:
    for f in (OUT, PNG, PDF):
        if f.exists():
            f.unlink()

    # 1. 图层
    print(srv.dxf_layers(str(OUT), [
        {"name": "WALLS", "color": "red"},
        {"name": "DETAIL", "color": "blue", "linetype": "Dashed"},
        {"name": "DIM", "color": "green"},
    ]))

    # 2. 轮廓
    print(srv.dxf_draw(str(OUT), [
        {"type": "rectangle", "corner": [0, 0], "width": 120, "height": 80,
         "layer": "WALLS"},
        {"type": "circle", "center": [85, 55], "radius": 18, "layer": "DETAIL"},
    ]))

    # 3. 标注
    print(srv.dxf_dimensions(str(OUT), [
        {"type": "linear", "p1": [0, 0], "p2": [120, 0],
         "location": [60, -12], "layer": "DIM"},
        {"type": "linear", "p1": [0, 0], "p2": [0, 80],
         "location": [-12, 40], "angle": 90, "layer": "DIM"},
        {"type": "radius", "center": [85, 55], "radius": 18,
         "angle": 30, "layer": "DIM"},
    ]))

    # 4. 填充
    print(srv.dxf_hatch(str(OUT), [
        {"points": [[0, 0], [120, 0], [120, 20], [0, 20]],
         "pattern": "ANSI31", "scale": 2},
    ]))

    # 5. 样条
    print(srv.dxf_splines(str(OUT), [
        {"points": [[0, 90], [30, 110], [60, 90], [90, 110], [120, 90]],
         "color": "blue"},
    ]))

    # 6. 图块 + 引用
    print(srv.dxf_blocks(str(OUT),
        blocks=[{"name": "BOLT", "entities": [
            {"type": "circle", "center": [0, 0], "radius": 4},
            {"type": "line", "start": [-5, 0], "end": [5, 0]},
            {"type": "line", "start": [0, -5], "end": [0, 5]},
        ]}],
        refs=[{"block": "BOLT", "insert": [15, 60]},
              {"block": "BOLT", "insert": [45, 60], "rotation": 45}]))

    # 7. 检查 + 预览 + 导出
    print(srv.dxf_read(str(OUT)))
    print(srv.dxf_render(str(OUT), str(PNG)))
    print(srv.dxf_export_pdf(str(OUT), str(PDF)))
    print(f"\n完成：{OUT.name} / {PNG.name} / {PDF.name}")


if __name__ == "__main__":
    main()
