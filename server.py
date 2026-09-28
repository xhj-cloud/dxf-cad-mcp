#!/usr/bin/env python3
"""dxf-cad-mcp — 轻量级 DXF CAD MCP 服务器（macOS / AutoCAD 配套）。

设计目标：让 AI agent（如 dsh）无需在 AutoCAD 图形界面里点击，
直接以文件为中心完成「创建 DXF → 绘制实体 → 图层管理 → 检查 →
PNG 预览 → 一键打开到 AutoCAD」的完整流程。

工具列表：
  dxf_create          创建空 DXF 文件
  dxf_draw            向图纸追加 2D 实体（line/polyline/rectangle/circle/
                      arc/ellipse/point/text/mtext），文件不存在时自动创建
  dxf_dimensions      追加尺寸标注（linear/aligned/radius/diameter/angular）
  dxf_hatch           追加填充（solid 实底 / ANSI31 等标准图案，闭合多边形边界）
  dxf_splines         追加样条线（过给定点集的拟合样条，degree 2/3）
  dxf_blocks          定义图块（内含 dxf_draw 同款实体）并插入引用
  dxf_layers          创建/更新图层（名称、颜色、线型）
  dxf_read            检查图纸：实体统计、类型分布、包围盒、图层列表
  dxf_render          渲染 PNG 预览（ezdxf + matplotlib，无需打开 CAD）
  dxf_export_pdf      导出矢量 PDF（matplotlib 矢量后端，可 A4 版面）
  dxf_open_in_autocad 用 macOS open 把文件打开到本机 AutoCAD

依赖：mcp, ezdxf, matplotlib（venv 安装）。
"""

from __future__ import annotations

import glob
import math
import os
import subprocess
from pathlib import Path

import ezdxf
import matplotlib

matplotlib.use("Agg")  # 无显示环境渲染


def _register_cjk_fonts() -> None:
    """matplotlib 默认不注册 macOS 的 .ttc 字体集合，不注册时 DXF 里的中文
    在 PNG 预览中渲染为空白（AutoCAD 端不受影响）。启动时静默注册系统 CJK 字体。"""
    import glob as _glob

    for pattern in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti*.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/Library/Fonts/Songti.ttc",
    ):
        for p in _glob.glob(pattern):
            try:
                matplotlib.font_manager.fontManager.addfont(p)
            except Exception:  # noqa: BLE001
                pass


_register_cjk_fonts()
import matplotlib.pyplot as plt
from ezdxf import bbox
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("dxf-cad")

# ACI 颜色名 → 编号（常用子集；完整表见 ezdxf.colors 文档）
NAMED_COLORS = {
    "red": 1, "yellow": 2, "green": 3, "cyan": 4, "blue": 5,
    "magenta": 6, "white": 7, "black": 7, "gray": 8, "grey": 8,
    "orange": 30, "brown": 31, "olive": 44, "lime": 90,
    "purple": 141, "pink": 201,
}

VALID_VERSIONS = ["R12", "R13", "R14", "R2000", "R2004", "R2007", "R2010", "R2013", "R2018"]

# 常用标准线型的 pattern 定义（ezdxf 新建文档只带 Continuous/ByBlock/ByLayer，
# 其余需要带 pattern 创建；数值采用 AutoCAD 经典比例，单位无关）
STANDARD_LINETYPE_PATTERNS = {
    "DASHED": "A,1.27,-0.635",
    "HIDDEN": "A,0.635,-0.318",
    "CENTER": "A,3.175,1.27,-0.318,1.27,-0.318",
    "DASHDOT": "A,1.905,1.27,-0.318,1.27,-0.318",
    "PHANTOM": "A,7.62,2.54,-1.27,1.27,-1.27",
}


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _resolve(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    return p


def _color_attr(color) -> dict:
    """把颜色参数转成 dxfattribs 片段（ACI int / 颜色名 / #RRGGBB 真彩色）。"""
    if color is None:
        return {}
    if isinstance(color, (int, float)):
        return {"color": int(color)}
    s = str(color).strip()
    low = s.lower()
    if low in NAMED_COLORS:
        return {"color": NAMED_COLORS[low]}
    if s.startswith("#") and len(s) == 7:
        return {"true_color": int(s[1:], 16)}
    raise ValueError(f"无法识别的颜色: {color!r}（支持 ACI 数字、颜色名或 #RRGGBB）")


def _entity_attrs(spec: dict) -> dict:
    attrs: dict = {}
    if spec.get("layer"):
        attrs["layer"] = str(spec["layer"])
    attrs.update(_color_attr(spec.get("color")))
    return attrs


def _add_entity(msp, spec: dict) -> str:
    t = str(spec.get("type", "")).lower()
    attrs = _entity_attrs(spec)
    if t == "line":
        msp.add_line(spec["start"], spec["end"], dxfattribs=attrs)
    elif t == "polyline":
        msp.add_lwpolyline(
            [tuple(p) for p in spec["points"]],
            close=bool(spec.get("closed", False)),
            dxfattribs=attrs,
        )
    elif t == "rectangle":
        x, y = spec["corner"]
        w, h = float(spec["width"]), float(spec["height"])
        msp.add_lwpolyline(
            [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
            close=True,
            dxfattribs=attrs,
        )
    elif t == "circle":
        msp.add_circle(spec["center"], float(spec["radius"]), dxfattribs=attrs)
    elif t == "arc":
        msp.add_arc(
            spec["center"],
            float(spec["radius"]),
            float(spec.get("start_angle", 0)),
            float(spec.get("end_angle", 90)),
            dxfattribs=attrs,
        )
    elif t == "ellipse":
        import math

        r = float(spec["major_radius"])
        rot = math.radians(float(spec.get("rotation", 0)))
        major = (r * math.cos(rot), r * math.sin(rot))
        msp.add_ellipse(
            spec["center"],
            major_axis=major,
            ratio=float(spec.get("ratio", 1.0)),
            dxfattribs=attrs,
        )
    elif t == "point":
        msp.add_point(spec["point"], dxfattribs=attrs)
    elif t == "text":
        ta = dict(attrs)
        ta["insert"] = spec["point"]
        ta["height"] = float(spec.get("height", 2.5))
        ta["rotation"] = float(spec.get("rotation", 0))
        msp.add_text(spec["text"], dxfattribs=ta)
    elif t == "mtext":
        ta = dict(attrs)
        ta["insert"] = spec["point"]
        ta["char_height"] = float(spec.get("char_height", 2.5))
        if spec.get("width"):
            ta["width"] = float(spec["width"])
        msp.add_mtext(spec["text"], dxfattribs=ta)
    else:
        raise ValueError(f"未知实体类型: {t!r}")
    return t


def _load_or_create(path: str, dxfversion: str = "R2010"):
    p = _resolve(path)
    if p.exists():
        return ezdxf.readfile(str(p)), False
    p.parent.mkdir(parents=True, exist_ok=True)
    return ezdxf.new(dxfversion=dxfversion), True


@mcp.tool()
def dxf_create(path: str, dxfversion: str = "R2010") -> dict:
    """创建一个新的空 DXF 文件。

    Args:
        path: 输出文件完整路径（.dxf）。
        dxfversion: DXF 版本，可选 R12/R13/R14/R2000/R2004/R2007/R2010/R2013/R2018，
            默认 R2010（兼容性最好的常用版本；AutoCAD 2024 全部支持）。
            给老软件用就选更老的版本。
    """
    try:
        if dxfversion not in VALID_VERSIONS:
            return _err(f"不支持的版本 {dxfversion}，可选: {VALID_VERSIONS}")
        p = _resolve(path)
        if p.exists():
            return _err(f"文件已存在: {p}（如需重画请先删除，或用 dxf_draw 追加）")
        p.parent.mkdir(parents=True, exist_ok=True)
        ezdxf.new(dxfversion=dxfversion).saveas(str(p))
        return _ok(path=str(p), version=dxfversion, size_bytes=p.stat().st_size)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_draw(path: str, entities: list[dict], dxfversion: str = "R2010") -> dict:
    """向 DXF 图纸追加 2D 实体；文件不存在时自动创建。

    每个实体是一个 dict，公共可选字段：layer（图层名，默认 0）、
    color（ACI 数字 / 颜色名如 "red" / "#RRGGBB"）。

    实体类型与必填字段：
      line:      start, end
      polyline:  points[[x,y],...], closed(bool, 默认 false)
      rectangle: corner[x,y], width, height
      circle:    center[x,y], radius
      arc:       center[x,y], radius, start_angle, end_angle(度, 逆时针)
      ellipse:   center[x,y], major_radius, ratio(0~1), rotation(度, 可选)
      point:     point[x,y]
      text:      point[x,y], text, height, rotation(度, 可选)
      mtext:     point[x,y], text, char_height, width(可选)
    """
    try:
        doc, created = _load_or_create(path, dxfversion)
        msp = doc.modelspace()
        # 自动创建实体引用的图层，避免悬空图层名
        for spec in entities:
            if isinstance(spec, dict) and spec.get("layer") and str(spec["layer"]) not in doc.layers:
                doc.layers.add(str(spec["layer"]))
        added = []
        for i, spec in enumerate(entities):
            if not isinstance(spec, dict):
                raise ValueError(f"实体 #{i} 不是对象: {spec!r}")
            added.append(_add_entity(msp, spec))
        doc.saveas(str(_resolve(path)))
        return _ok(
            path=str(_resolve(path)),
            created_new=created,
            added=added,
            added_count=len(added),
        )
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# 常见标准填充图案名（AutoCAD 内置 pattern 表；matplotlib 预览端支持的子集）
KNOWN_HATCH_PATTERNS = [
    "ANSI31", "ANSI32", "ANSI33", "ANSI34", "ANSI37", "AR-CONC", "BRICK",
    "C30", "CROSS", "CROSS2", "DASH", "DENS", "DOTS", "DRABS", "EGG_DENS",
    "EGG2", "FAINT", "GRAVEL", "GRID", "NET", "NET2", "OUTLINE", "SAND",
    "SPARSE", "STEEL", "TRUSS", "WAVES", "WYTHE",
]


def _measure_dimension(spec: dict) -> float | None:
    """按标注类型计算实测值，给调用方即时反馈。"""
    t = spec.get("type")
    try:
        if t == "linear":
            (x1, y1), (x2, y2) = spec["p1"], spec["p2"]
            ang = math.radians(float(spec.get("angle", 0)))
            return abs((x2 - x1) * math.cos(ang) + (y2 - y1) * math.sin(ang))
        if t == "aligned":
            (x1, y1), (x2, y2) = spec["p1"], spec["p2"]
            return math.hypot(x2 - x1, y2 - y1)
        if t == "radius":
            return float(spec["radius"])
        if t == "diameter":
            return 2 * float(spec["radius"])
        if t == "angular":
            cx, cy = spec["center"]
            a1 = math.atan2(spec["p1"][1] - cy, spec["p1"][0] - cx)
            a2 = math.atan2(spec["p2"][1] - cy, spec["p2"][0] - cx)
            d = abs(a2 - a1)
            return min(d, 2 * math.pi - d) * 180 / math.pi
    except Exception:  # noqa: BLE001
        return None
    return None


def _add_dimension(msp, spec: dict):
    """按 spec 创建一个 DIMENSION 实体并渲染其图形表示。

    返回 (类型名, 实测值)。dimstyle 缺省 "Standard"（ezdxf 新建文档自带，
    AutoCAD 兼容；不传 ezdxf 默认的 EZDXF 以规避老版本 AutoCAD 找不到
    该 dimstyle 的告警）。
    """
    t = str(spec.get("type", "")).lower()
    attrs = _entity_attrs(spec)
    ds = str(spec.get("dimstyle", "Standard"))
    text = spec.get("text")
    kw = dict(dimstyle=ds, dxfattribs=attrs)
    if text is not None:
        kw["text"] = str(text)

    if t == "linear":
        dim = msp.add_linear_dim(
            base=tuple(spec.get("location") or [(spec["p1"][0] + spec["p2"][0]) / 2,
                                                (spec["p1"][1] + spec["p2"][1]) / 2]),
            p1=spec["p1"], p2=spec["p2"],
            angle=float(spec.get("angle", 0)),
            **kw,
        )
    elif t == "aligned":
        # add_aligned_dim 需要 p1p2 到标注线的垂直距离（带符号）；
        # 用 location 点反推，取负号使标注线落在 location 所在一侧
        (x1, y1), (x2, y2) = spec["p1"], spec["p2"]
        loc = tuple(spec.get("location") or [x1 + 5, y1 + 5])
        vx, vy = x2 - x1, y2 - y1
        length = math.hypot(vx, vy) or 1.0
        distance = -((loc[0] - x1) * vy - (loc[1] - y1) * vx) / length
        dim = msp.add_aligned_dim(p1=spec["p1"], p2=spec["p2"], distance=distance, **kw)
    elif t == "radius":
        dim = msp.add_radius_dim(
            center=spec["center"], radius=float(spec["radius"]),
            angle=spec.get("angle"), location=spec.get("location"), **kw,
        )
    elif t == "diameter":
        dim = msp.add_diameter_dim(
            center=spec["center"], radius=float(spec["radius"]),
            angle=spec.get("angle"), location=spec.get("location"), **kw,
        )
    elif t == "angular":
        # 注意：add_angular_dim_2l 的 base 是"标注弧上任意一点"（决定弧半径），
        # 而不是角度顶点；location 才是文字中点。顶点传错会得到 0 半径 → 除零。
        c = tuple(spec["center"])
        l1, l2 = tuple(spec["p1"]), tuple(spec["p2"])
        if spec.get("location"):
            arc_pt = tuple(spec["location"])
        else:
            v1 = (l1[0] - c[0], l1[1] - c[1])
            v2 = (l2[0] - c[0], l2[1] - c[1])
            n1, n2 = math.hypot(*v1) or 1.0, math.hypot(*v2) or 1.0
            u1 = (v1[0] / n1, v1[1] / n1)
            u2 = (v2[0] / n2, v2[1] / n2)
            bis = (u1[0] + u2[0], u1[1] + u2[1])
            nb = math.hypot(*bis)
            if nb < 1e-9:  # 180° 角，角平分线退化为零向量 → 取垂直方向
                bis = (-u1[1], u1[0])
            else:
                bis = (bis[0] / nb, bis[1] / nb)
            r = min(n1, n2) * 0.6
            arc_pt = (c[0] + bis[0] * r, c[1] + bis[1] * r)
        dim = msp.add_angular_dim_2l(base=arc_pt, line1=(c, l1), line2=(c, l2), **kw)
    else:
        raise ValueError(
            f"未知标注类型: {t!r}（可选 linear/aligned/radius/diameter/angular）"
        )
    dim.render()  # 把图形化表示（尺寸线/箭头/文字）写进 DXF，预览与 CAD 双端可见
    return t, _measure_dimension(spec)


@mcp.tool()
def dxf_dimensions(path: str, dims: list[dict], dxfversion: str = "R2010") -> dict:
    """向 DXF 图纸追加尺寸标注；文件不存在时自动创建。

    每个标注是一个 dict，公共可选字段：layer、color、dimstyle（默认 "Standard"）、
    text（自定义标注文字，不传则显示实测值）。

    标注类型与必填字段（坐标单位与绘图一致）：
      linear:   p1, p2, location[标注线位置点, 可选], angle(0=水平标注, 90=垂直)
      aligned:  p1, p2, location[标注线位置点, 可选]
      radius:   center, radius, angle(引线方向, 度, 可选), location(可选)
      diameter: center, radius, angle(可选), location(可选)
      angular:  center, p1, p2[两条射线的端点], location(标注弧位置点, 可选,
               不传则画在角平分线上)

    返回值含每个标注的实测测量值（measurement），可直接核对。
    注意：标注的预览渲染是 ezdxf 的简化实现，与 AutoCAD 的显示可能有差异
    （例如 angular 预览可能显示补角）；打开 AutoCAD 后 CAD 会按定义点
    重新渲染，以 CAD 显示为准。
    """
    try:
        doc, created = _load_or_create(path, dxfversion)
        msp = doc.modelspace()
        for spec in dims:
            if isinstance(spec, dict) and spec.get("layer") and str(spec["layer"]) not in doc.layers:
                doc.layers.add(str(spec["layer"]))
        results = []
        for i, spec in enumerate(dims):
            if not isinstance(spec, dict):
                raise ValueError(f"标注 #{i} 不是对象: {spec!r}")
            t, value = _add_dimension(msp, spec)
            results.append({"type": t, "measurement": round(value, 6) if value is not None else None})
        doc.saveas(str(_resolve(path)))
        return _ok(path=str(_resolve(path)), created_new=created, added=results,
                   added_count=len(results))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_hatch(path: str, hatches: list[dict], dxfversion: str = "R2010") -> dict:
    """向 DXF 图纸追加填充（HATCH）；文件不存在时自动创建。

    每个填充是一个 dict：
      points   (必填) 闭合多边形边界顶点 [[x,y],...]（按序首尾自动闭合）
      pattern  "solid"（实底，默认）或标准图案名，如 "ANSI31"（斜线）、
               "ANSI32"、"CROSS"（网格）、"DOTT"/"DOTS"、"NET"、"GRAVEL"、
               "BRICK"、"AR-CONC"（混凝土）、"TRUSS"（桁架）、"WAVES" 等
      scale    图案缩放（默认 1.0，越大线越疏）
      angle    图案旋转角度（默认 0）
      layer / color  公共可选字段

    注意：圆形/弧线边界暂不支持（仅多边形边界）；复杂边界建议拆成多个填充。
    """
    try:
        doc, created = _load_or_create(path, dxfversion)
        msp = doc.modelspace()
        for spec in hatches:
            if isinstance(spec, dict) and spec.get("layer") and str(spec["layer"]) not in doc.layers:
                doc.layers.add(str(spec["layer"]))
        added = []
        for i, spec in enumerate(hatches):
            if not isinstance(spec, dict):
                raise ValueError(f"填充 #{i} 不是对象: {spec!r}")
            pts = [tuple(p) for p in spec["points"]]
            if len(pts) < 3:
                raise ValueError(f"填充 #{i} 边界顶点至少 3 个")
            attrs = _entity_attrs(spec)
            color = attrs.get("color", 7)
            hatch = msp.add_hatch(color=color, dxfattribs=attrs)
            pattern = str(spec.get("pattern", "solid")).strip()
            note = None
            if pattern.lower() == "solid":
                hatch.set_solid_fill(color=color)
            else:
                name = pattern.upper()
                hatch.set_pattern_fill(
                    name, color=color,
                    angle=float(spec.get("angle", 0)),
                    scale=float(spec.get("scale", 1.0)),
                )
                if name not in KNOWN_HATCH_PATTERNS:
                    note = (f"非内置常见图案（常见: {', '.join(KNOWN_HATCH_PATTERNS[:10])} 等），"
                            f"AutoCAD 端可能按空图案显示")
            hatch.paths.add_polyline_path(pts, is_closed=True)
            entry = {"pattern": "solid" if pattern.lower() == "solid" else pattern.upper()}
            if note:
                entry["note"] = note
            added.append(entry)
        doc.saveas(str(_resolve(path)))
        return _ok(path=str(_resolve(path)), created_new=created, added=added,
                   added_count=len(added))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_splines(path: str, splines: list[dict], dxfversion: str = "R2010") -> dict:
    """向 DXF 图纸追加样条线（SPLINE）；文件不存在时自动创建。

    每个样条是一个 dict：
      points  (必填) 拟合点 [[x,y],...]（≥3 个，样条穿过这些点）
      degree  样条阶数，2 或 3（默认 3）
      layer / color  公共可选字段
    """
    try:
        doc, created = _load_or_create(path, dxfversion)
        msp = doc.modelspace()
        for spec in splines:
            if isinstance(spec, dict) and spec.get("layer") and str(spec["layer"]) not in doc.layers:
                doc.layers.add(str(spec["layer"]))
        added = []
        for i, spec in enumerate(splines):
            if not isinstance(spec, dict):
                raise ValueError(f"样条 #{i} 不是对象: {spec!r}")
            pts = [tuple(p) for p in spec["points"]]
            if len(pts) < 3:
                raise ValueError(f"样条 #{i} 拟合点至少 3 个")
            degree = int(spec.get("degree", 3))
            if degree not in (2, 3):
                raise ValueError(f"样条 #{i} degree 只能为 2 或 3")
            attrs = _entity_attrs(spec)
            msp.add_spline(fit_points=pts, degree=degree, dxfattribs=attrs)
            added.append({"fit_points": len(pts), "degree": degree})
        doc.saveas(str(_resolve(path)))
        return _ok(path=str(_resolve(path)), created_new=created, added=added,
                   added_count=len(added))
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_blocks(path: str, blocks: list[dict] | None = None, refs: list[dict] | None = None,
               dxfversion: str = "R2010") -> dict:
    """定义图块（BLOCK）并向图纸插入图块引用（INSERT）；文件不存在时自动创建。

    blocks: 图块定义列表，每个:
      name        (必填) 图块名（已存在同名图块会报错）
      base_point  插入基准点（可选，默认 [0,0]）
      entities    (必填) 图块内实体，格式与 dxf_draw 的 entities 完全相同
                  （line/polyline/rectangle/circle/arc/ellipse/point/text/mtext）

    refs: 图块引用列表（可省略），每个:
      block    (必填) 图块名
      insert   (必填) 插入点 [x,y]
      rotation 旋转角度（可选，度）
      x_scale / y_scale  缩放（可选，默认 1）
      layer / color      公共可选字段
    """
    try:
        blocks = blocks or []
        refs = refs or []
        if not blocks and not refs:
            return _err("blocks 和 refs 至少提供一个")
        doc, created = _load_or_create(path, dxfversion)
        msp = doc.modelspace()
        made, inserted = [], []
        for i, spec in enumerate(blocks):
            name = str(spec["name"])
            if name in doc.blocks:
                raise ValueError(f"图块 {name} 已存在（先删除文件或换个名字）")
            bp = tuple(spec.get("base_point") or (0, 0))
            blk = doc.blocks.new(name, base_point=bp)
            for j, espec in enumerate(spec.get("entities", [])):
                if not isinstance(espec, dict):
                    raise ValueError(f"图块 {name} 实体 #{j} 不是对象: {espec!r}")
                _add_entity(blk, espec)
            made.append(name)
        for i, spec in enumerate(refs):
            name = str(spec["block"])
            if name not in doc.blocks:
                raise ValueError(f"图块 {name} 不存在（先在同一次调用或之前定义）")
            attrs = _entity_attrs(spec)
            if spec.get("rotation") is not None:
                attrs["rotation"] = float(spec["rotation"])
            if spec.get("x_scale") is not None:
                attrs["xscale"] = float(spec["x_scale"])
            if spec.get("y_scale") is not None:
                attrs["yscale"] = float(spec["y_scale"])
            msp.add_blockref(name, insert=tuple(spec["insert"]), dxfattribs=attrs)
            inserted.append(name)
        doc.saveas(str(_resolve(path)))
        return _ok(path=str(_resolve(path)), created_new=created,
                   blocks_made=made, refs_inserted=inserted)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_layers(path: str, layers: list[dict]) -> dict:
    """创建或更新图层。

    每个图层: {name(必填), color(可选, 同 color 规则), linetype(可选,
    如 "Continuous"/"Dashed"/"Center"/"Hidden")}. 文件不存在时先创建。
    """
    try:
        doc, created = _load_or_create(path)
        result = []
        for spec in layers:
            name = str(spec["name"])
            if name not in doc.layers:
                doc.layers.add(name)
            layer = doc.layers.get(name)
            if spec.get("color") is not None:
                ca = _color_attr(spec["color"])
                if "color" in ca:
                    layer.color = ca["color"]
            if spec.get("linetype"):
                lt = str(spec["linetype"])
                if lt not in doc.linetypes:
                    pattern = STANDARD_LINETYPE_PATTERNS.get(lt.upper())
                    if pattern is None:
                        raise ValueError(f"线型 {lt} 不存在且无内置 pattern 定义")
                    doc.linetypes.add(lt, pattern)
                layer.linetype = lt
            result.append(name)
        doc.saveas(str(_resolve(path)))
        return _ok(path=str(_resolve(path)), created_new=created, layers=result)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_read(path: str) -> dict:
    """检查一张 DXF 图纸：DXF 版本、实体总数与类型分布、包围盒、图层列表。"""
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        doc = ezdxf.readfile(str(p))
        msp = doc.modelspace()
        by_type: dict[str, int] = {}
        total = 0
        for e in msp:
            total += 1
            by_type[e.dxftype()] = by_type.get(e.dxftype(), 0) + 1
        extents = None
        if total:
            try:
                b = bbox.extents(msp)
                extents = {
                    "min": [round(b.extmin.x, 4), round(b.extmin.y, 4)],
                    "max": [round(b.extmax.x, 4), round(b.extmax.y, 4)],
                }
            except Exception:  # noqa: BLE001
                extents = None
        return _ok(
            path=str(p),
            dxf_version=doc.dxfversion,
            entity_count=total,
            by_type=by_type,
            extents=extents,
            layers=[l.dxf.name for l in doc.layers],
            size_bytes=p.stat().st_size,
        )
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def _bg_hex(background: str) -> str:
    """把背景色参数归一化成 #RRGGBB。"""
    bg = str(background).lower()
    bg = {"dark": "black"}.get(bg, bg)
    if bg == "white":
        return "#ffffff"
    if bg == "black":
        return "#000000"
    if bg.startswith("#") and len(bg) == 7:
        return bg
    raise ValueError(f"无法识别的背景色: {background!r}（支持 white/black/dark/#RRGGBB）")


def _build_figure(doc, background: str, page_size: str):
    """构建已绘好 model space 的 matplotlib 图。返回 (fig, bg_hex)。

    关键：必须把实际背景色告诉 ezdxf 前端（BackgroundPolicy.CUSTOM）。
    否则前端默认按深色模型空间背景处理，ACI 7 号色（"白/黑"双色，
    未显式指定颜色的实体默认就是它）会解析成白色——白底图上标注、
    图块等默认色实体全部不可见。
    """
    bg = _bg_hex(background)
    fixed = str(page_size).upper() in ("A4", "A3", "A2")
    if fixed:
        # 横版纸张尺寸（英寸）；adjust_figure=False 防止 finalize 按
        # 数据宽高比重设图大小、把纸张尺寸冲掉
        sizes = {"A4": (11.69, 8.27), "A3": (16.54, 11.69), "A2": (23.39, 16.54)}
        fig = plt.figure(figsize=sizes[str(page_size).upper()])
        ax = fig.add_axes([0.04, 0.04, 0.92, 0.92])
        backend = MatplotlibBackend(ax, adjust_figure=False)
    else:
        fig = plt.figure()
        ax = fig.add_axes([0.02, 0.02, 0.96, 0.96])
        backend = MatplotlibBackend(ax)
    config = Configuration(
        background_policy=BackgroundPolicy.CUSTOM,
        custom_bg_color=bg,
    )
    ctx = RenderContext(doc)
    frontend = Frontend(ctx, backend, config=config)
    frontend.draw_layout(doc.modelspace(), finalize=True)
    ax.set_facecolor(bg)
    return fig, bg


@mcp.tool()
def dxf_render(
    path: str,
    png_path: str,
    dpi: int = 150,
    background: str = "white",
) -> dict:
    """把 DXF 渲染成 PNG 预览图（纯本地，不需要打开 AutoCAD）。

    Args:
        path: 源 DXF 文件。
        png_path: 输出 PNG 路径。
        dpi: 分辨率，默认 150（100~300）。
        background: 背景色，"white"/"black"/"dark" 或 #RRGGBB。
    """
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        out = _resolve(png_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = ezdxf.readfile(str(p))
        fig, bg = _build_figure(doc, background, page_size="auto")
        fig.savefig(str(out), dpi=int(dpi), facecolor=bg)
        plt.close(fig)
        return _ok(path=str(p), png=str(out), size_bytes=out.stat().st_size)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_export_pdf(
    path: str,
    pdf_path: str,
    background: str = "white",
    page_size: str = "auto",
) -> dict:
    """把 DXF 导出为矢量 PDF（线条/文字保持矢量，可无限放大，适合打印归档）。

    Args:
        path: 源 DXF 文件。
        pdf_path: 输出 PDF 路径。
        background: 背景色，"white"/"black"/"dark" 或 #RRGGBB。
        page_size: "auto"（默认，按图纸包围盒自适应）或 "A4"/"A3"/"A2"（横版）。
    """
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        out = _resolve(pdf_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        doc = ezdxf.readfile(str(p))
        fig, bg = _build_figure(doc, background, page_size)
        fig.savefig(str(out), facecolor=bg)
        plt.close(fig)
        return _ok(path=str(p), pdf=str(out), size_bytes=out.stat().st_size,
                   page_size=str(page_size).upper() if page_size != "auto" else "auto-fit")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def _find_autocad_apps() -> list[str]:
    apps = []
    for pattern in ("/Applications/Autodesk/*/AutoCAD*.app",
                    "/Applications/Autodesk/*/*/AutoCAD*.app"):
        apps.extend(glob.glob(pattern))
    apps = [a for a in set(apps) if "Remove" not in a and "Plot" not in a]
    # 完整版 AutoCAD 优先于 LT
    apps.sort(key=lambda a: (0 if "/LT" not in a and "LT " not in os.path.basename(a) else 1, a))
    return apps


@mcp.tool()
def dxf_open_in_autocad(path: str, prefer: str | None = None) -> dict:
    """把 DXF（或 DWG）文件打开到本机的 AutoCAD（macOS `open` 调用）。

    Args:
        path: 要打开的图纸文件。
        prefer: 可选，指定优先使用的 .app 路径（默认自动选择 /Applications/Autodesk
            下的 AutoCAD，完整版优先）。
    """
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        if prefer:
            apps = [prefer]
        else:
            apps = _find_autocad_apps()
        if apps:
            r = subprocess.run(
                ["open", "-a", apps[0], str(p)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return _ok(path=str(p), opened_with=apps[0])
            detail = (r.stderr or r.stdout or "").strip()
        # 兜底：系统默认应用
        r = subprocess.run(["open", str(p)], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return _ok(path=str(p), opened_with="system default",
                       note="未找到 AutoCAD，已用系统默认应用打开")
        return _err(f"打开失败: {(r.stderr or '').strip()}")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def main() -> None:
    mcp.run()  # 默认 stdio 传输


if __name__ == "__main__":
    main()
