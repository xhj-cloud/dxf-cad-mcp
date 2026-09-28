#!/usr/bin/env python3
"""独立端到端测试（第一批）：通过 MCP stdio 协议调用 dxf-cad-mcp 的基础工具。

产物输出到本目录 test_output/（已 gitignore）。
"""
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
OUT = str(ROOT / "test_output" / "mcp_test.dxf")
PNG = str(ROOT / "test_output" / "mcp_test.png")


async def main():
    out_dir = ROOT / "test_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in (OUT, PNG):
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
            print("TOOLS:", [t.name for t in tools.tools])

            for name, args in [
                ("dxf_layers", {
                    "path": OUT,
                    "layers": [
                        {"name": "WALLS", "color": "red"},
                        {"name": "DETAIL", "color": "blue", "linetype": "Dashed"},
                        {"name": "TEXT", "color": "green"},
                    ],
                }),
                ("dxf_draw", {
                    "path": OUT,
                    "entities": [
                        {"type": "rectangle", "corner": [0, 0], "width": 100, "height": 100,
                         "layer": "WALLS"},
                        {"type": "circle", "center": [50, 50], "radius": 30,
                         "layer": "DETAIL", "color": "blue"},
                        {"type": "arc", "center": [50, 50], "radius": 40,
                         "start_angle": 0, "end_angle": 90, "layer": "DETAIL"},
                        {"type": "polyline", "points": [[10, 10], [90, 10], [90, 90]],
                         "layer": "WALLS", "color": 1},
                        {"type": "line", "start": [0, 0], "end": [100, 100],
                         "layer": "DETAIL"},
                        {"type": "text", "point": [30, 112], "text": "dxf-cad-mcp test",
                         "height": 6, "layer": "TEXT"},
                        {"type": "mtext", "point": [10, -15], "text": "drawn by dsh agent",
                         "char_height": 4},
                    ],
                }),
                ("dxf_read", {"path": OUT}),
                ("dxf_render", {"path": OUT, "png_path": PNG, "dpi": 150,
                                "background": "white"}),
            ]:
                res = await s.call_tool(name, args)
                text = res.content[0].text
                data = json.loads(text)
                print(f"--- {name} ---")
                print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
