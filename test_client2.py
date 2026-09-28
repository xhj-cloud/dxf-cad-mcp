#!/usr/bin/env python3
"""端到端测试（第二批）：标注/填充/样条/图块 + PDF 导出，走 MCP stdio 协议。

产物输出到本目录 test_output/（已 gitignore）。
"""
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
OUT = str(ROOT / "test_output" / "mcp_test2.dxf")
PNG = str(ROOT / "test_output" / "mcp_test2.png")
PDF = str(ROOT / "test_output" / "mcp_test2.pdf")
PDF_A4 = str(ROOT / "test_output" / "mcp_test2_a4.pdf")


async def main():
    out_dir = ROOT / "test_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in (OUT, PNG, PDF, PDF_A4):
        if Path(f).exists():
            Path(f).unlink()
    params = StdioServerParameters(
        command=str(ROOT / ".venv" / "bin" / "python"), args=["server.py"],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOLS:", names)
            new = ["dxf_dimensions", "dxf_hatch", "dxf_splines", "dxf_blocks", "dxf_export_pdf"]
            missing = [n for n in new if n not in names]
            assert not missing, f"缺少新工具: {missing}"

            # 先画一个基础图形当标注对象
            await s.call_tool("dxf_draw", {
                "path": OUT,
                "entities": [
                    {"type": "rectangle", "corner": [0, 0], "width": 120, "height": 80,
                     "layer": "OUTLINE", "color": 7},
                    {"type": "circle", "center": [85, 55], "radius": 18, "layer": "OUTLINE"},
                ],
            })

            for name, args in [
                ("dxf_dimensions", {
                    "path": OUT,
                    "dims": [
                        {"type": "linear", "p1": [0, 0], "p2": [120, 0],
                         "location": [60, -12], "layer": "DIM"},
                        {"type": "linear", "p1": [0, 0], "p2": [0, 80],
                         "location": [-12, 40], "angle": 90, "layer": "DIM"},
                        {"type": "aligned", "p1": [120, 0], "p2": [120, 80],
                         "location": [138, 40], "layer": "DIM"},
                        {"type": "radius", "center": [85, 55], "radius": 18,
                         "angle": 30, "layer": "DIM"},
                        {"type": "diameter", "center": [85, 55], "radius": 18,
                         "angle": 210, "layer": "DIM"},
                        {"type": "angular", "center": [0, 80], "p1": [22, 80],
                         "p2": [0, 102], "location": [26, 106], "layer": "DIM"},
                    ],
                }),
                ("dxf_hatch", {
                    "path": OUT,
                    "hatches": [
                        {"points": [[0, 0], [120, 0], [120, 20], [0, 20]],
                         "pattern": "ANSI31", "scale": 2},
                        {"points": [[5, 25], [25, 25], [25, 45], [5, 45]],
                         "pattern": "solid", "color": "green"},
                    ],
                }),
                ("dxf_splines", {
                    "path": OUT,
                    "splines": [
                        {"points": [[0, 70], [30, 95], [60, 70], [90, 95], [120, 70]],
                         "degree": 3, "color": "blue"},
                    ],
                }),
                ("dxf_blocks", {
                    "path": OUT,
                    "blocks": [
                        {"name": "BOLT", "base_point": [0, 0], "entities": [
                            {"type": "circle", "center": [0, 0], "radius": 4},
                            {"type": "line", "start": [-5, 0], "end": [5, 0]},
                            {"type": "line", "start": [0, -5], "end": [0, 5]},
                        ]},
                    ],
                    "refs": [
                        {"block": "BOLT", "insert": [15, 60]},
                        {"block": "BOLT", "insert": [45, 60], "rotation": 45},
                        {"block": "BOLT", "insert": [75, 60], "x_scale": 2, "y_scale": 2},
                    ],
                }),
                ("dxf_read", {"path": OUT}),
                ("dxf_render", {"path": OUT, "png_path": PNG, "dpi": 150,
                                "background": "white"}),
                ("dxf_export_pdf", {"path": OUT, "pdf_path": PDF}),
                ("dxf_export_pdf", {"path": OUT, "pdf_path": PDF_A4, "page_size": "A4"}),
            ]:
                res = await s.call_tool(name, args)
                data = json.loads(res.content[0].text)
                print(f"--- {name} ---")
                print(json.dumps(data, ensure_ascii=False, indent=2))

            # 错误路径：重复图块名
            res = await s.call_tool("dxf_blocks", {
                "path": OUT,
                "blocks": [{"name": "BOLT", "entities": [
                    {"type": "line", "start": [0, 0], "end": [1, 1]}]}],
            })
            data = json.loads(res.content[0].text)
            print("--- dup block (expect ok=false) ---")
            print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
