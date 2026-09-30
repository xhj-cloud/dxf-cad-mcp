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
  dxf_layers          创建/更新/重命名/删除图层（名称、颜色、线型）
  dxf_read            检查图纸（DXF 直读 / DWG 经 LibreDWG 转读）：实体统计、
                      类型分布、包围盒、图层列表
  dxf_query           查找实体（按类型/图层/handle/文字），返回 handle + 摘要
  dxf_delete          删除实体（handle/类型/图层；删标注时清理其 *D 图形块）
  dxf_modify          修改实体属性（图层/颜色/线型/文字）与变换（移动/旋转/缩放）
  dxf_restore         从自动滚动备份（.bak1~.bak5）恢复文件
                      所有修改类工具在落盘前自动滚动备份
  dxf_render          渲染 PNG 预览（ezdxf + matplotlib，无需打开 CAD）
  dxf_export_pdf      导出矢量 PDF（matplotlib 矢量后端，可 A4 版面）
  dxf_convert_dwg     转换 DXF → DWG（LibreDWG dxf2dwg，纯本地）
  dxf_open_in_autocad 用 macOS open 把文件打开到本机 AutoCAD

依赖：mcp, ezdxf, matplotlib（venv 安装）。
"""

from __future__ import annotations

import glob
import math
import os
import shutil
import subprocess
import tempfile
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
from ezdxf.math import Matrix44
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
        p = _resolve(path)
        if not created:
            _backup_file(p)
        doc.saveas(str(p))
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
        p = _resolve(path)
        if not created:
            _backup_file(p)
        doc.saveas(str(p))
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
        p = _resolve(path)
        if not created:
            _backup_file(p)
        doc.saveas(str(p))
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
        p = _resolve(path)
        if not created:
            _backup_file(p)
        doc.saveas(str(p))
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
        p = _resolve(path)
        if not created:
            _backup_file(p)
        doc.saveas(str(p))
        return _ok(path=str(_resolve(path)), created_new=created,
                   blocks_made=made, refs_inserted=inserted)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_layers(path: str, layers: list[dict]) -> dict:
    """创建/更新/重命名/删除图层（修改前自动滚动备份，dxf_restore 可回滚）。

    每个图层:
      {name(必填), color(可选, 同 color 规则), linetype(可选,
       如 "Continuous"/"Dashed"/"Center"/"Hidden")}            → 创建/更新
      {name(必填), rename_to: "新名"}                          → 重命名
      {name(必填), delete: true}                               → 删除
       （非空图层会被保护性拒绝，返回该图层上的实体数提示）
    文件不存在时先创建。
    """
    try:
        doc, created = _load_or_create(path)
        result = []
        for spec in layers:
            name = str(spec["name"])
            if spec.get("delete"):
                if name not in doc.layers:
                    return _err(f"图层 {name} 不存在，无法删除")
                # ezdxf 的 remove() 不检查实体引用，自己数（含图块内实体，
                # *Model_Space 与 modelspace 同一空间需排除以免重复计数），
                # 非空则拒绝，避免生成引用不存在图层的坏文件
                count = sum(
                    1
                    for blk in ([doc.modelspace()]
                                + [b for b in doc.blocks
                                   if b.name != "*Model_Space"])
                    for e in blk if e.dxf.layer == name
                )
                if count:
                    return _err(f"图层 {name} 不能删除：上面还有 {count} 个实体"
                                f"（先用 dxf_modify 移到别的图层，或 dxf_delete 删除）")
                doc.layers.remove(name)
                result.append(f"deleted:{name}")
                continue
            if spec.get("rename_to"):
                if name not in doc.layers:
                    return _err(f"图层 {name} 不存在，无法重命名")
                new_name = str(spec["rename_to"])
                if new_name in doc.layers:
                    return _err(f"目标图层 {new_name} 已存在")
                # 关键：ezdxf 没有图层改名 API，只改 group 2 会留下
                # "实体仍引用旧图层名"的坏文件。必须先把所有引用旧名的
                # 实体（模型空间 + 图块内）改指新名，再改图层条目名。
                for blk in ([doc.modelspace()]
                            + [b for b in doc.blocks
                               if b.name != "*Model_Space"]):
                    for e in blk:
                        if e.dxf.layer == name:
                            e.dxf.layer = new_name
                doc.layers.get(name).dxf.name = new_name
                result.append(f"renamed:{name}->{new_name}")
                continue
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
        p = _resolve(path)
        if not created:
            _backup_file(p)
        doc.saveas(str(p))
        return _ok(path=str(p), created_new=created, layers=result,
                   backup=".bak1（dxf_restore 可回滚）")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


# ── 编辑类工具（查询 / 删除 / 修改 / 备份恢复）────────────────────────────
#
# 解决 append-only 痛点：agent 画图是试错过程，需要"定位→改/删→回滚"闭环。
# 所有修改类工具落盘前自动滚动备份（.bak1 最新 ~ .bak5 最旧）。

BACKUP_MAX = 5


def _backup_file(p: Path) -> None:
    """滚动备份：.bakN → .bakN+1 后移，当前文件 → .bak1。
    调用方必须在内存文档已加载、且随后会用 saveas 重新落盘时调用。"""
    for i in range(BACKUP_MAX - 1, 0, -1):
        src = p.with_name(p.name + f".bak{i}")
        if src.exists():
            src.replace(p.with_name(p.name + f".bak{i + 1}"))
    p.replace(p.with_name(p.name + ".bak1"))


def _match_entities(msp, types=None, layer=None, handles=None) -> list:
    """按条件匹配模型空间实体。handles 给出时只做精确匹配（忽略 types/layer）；
    否则按 types（大写归一）/layer 过滤，条件可缺省。"""
    types_up = {str(t).upper() for t in types} if types else None
    handles_set = {str(h) for h in handles} if handles else None
    out = []
    for e in msp:
        if handles_set is not None:
            if e.dxf.handle not in handles_set:
                continue
        else:
            if types_up and e.dxftype() not in types_up:
                continue
            if layer is not None and e.dxf.layer != layer:
                continue
        out.append(e)
    return out


def _entity_summary(e) -> dict:
    """实体一句话摘要（供 dxf_query 返回，帮 agent 判断改哪个）。"""
    t = e.dxftype()
    try:
        if t == "LINE":
            return {"start": list(e.dxf.start), "end": list(e.dxf.end)}
        if t == "CIRCLE":
            return {"center": list(e.dxf.center), "radius": e.dxf.radius}
        if t == "ARC":
            return {"center": list(e.dxf.center), "radius": e.dxf.radius,
                    "start_angle": e.dxf.start_angle, "end_angle": e.dxf.end_angle}
        if t in ("TEXT", "MTEXT"):
            return {"text": str(e.dxf.text)[:50]}
        if t == "LWPOLYLINE":
            return {"points": len(list(e.get_points())), "closed": e.closed}
        if t == "DIMENSION":
            return {"dimtype": e.dxf.dimtype}
        if t == "HATCH":
            return {"pattern": e.dxf.pattern_name if e.dxf.pattern else "solid"}
        if t == "INSERT":
            return {"block": e.dxf.name, "insert": list(e.dxf.insert)}
    except Exception:  # noqa: BLE001
        pass
    return {}


@mcp.tool()
def dxf_query(
    path: str,
    types: list[str] | None = None,
    layer: str | None = None,
    handles: list[str] | None = None,
    text_contains: str | None = None,
    limit: int = 100,
) -> dict:
    """查找图纸中的实体，返回 handle + 类型 + 图层 + 简要信息。
    所有编辑工具（dxf_delete / dxf_modify）的前置步骤：先用它定位目标。

    Args:
        path: DXF 文件。
        types: 类型过滤，如 ["LINE","CIRCLE","TEXT"]（大小写不敏感）
        layer: 图层名过滤
        handles: 精确 handle 列表（给出时忽略 types/layer）
        text_contains: 按文字子串匹配 TEXT/MTEXT
        limit: 最多返回条数（默认 100）
    """
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        doc = ezdxf.readfile(str(p))
        msp = doc.modelspace()
        ents = _match_entities(msp, types, layer, handles)
        if text_contains is not None:
            tc = str(text_contains)
            ents = [e for e in ents
                    if e.dxftype() in ("TEXT", "MTEXT") and tc in str(e.dxf.text)]
        total = len(ents)
        out = []
        for e in ents[:max(0, int(limit))]:
            out.append({"handle": e.dxf.handle, "type": e.dxftype(),
                        "layer": e.dxf.layer, "summary": _entity_summary(e)})
        return _ok(path=str(p), matched=total, returned=len(out),
                   truncated=total > len(out), entities=out)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_delete(
    path: str,
    handles: list[str] | None = None,
    types: list[str] | None = None,
    layer: str | None = None,
) -> dict:
    """删除实体（删除前自动滚动备份，dxf_restore 可回滚）。

    Args:
        path: DXF 文件。
        handles: 精确 handle 列表（最高优先；给出时忽略 types/layer）
        types: 按类型删除，如 ["TEXT","DIMENSION"]
        layer: 删除某图层上的全部实体
    三者至少给一个（不允许整图删除）。
    注意：删除 DIMENSION 时同步清空其缓存图形块（*D），不留孤儿图形；
    Defpoints 层中的定义点不动（不可见、无害）。
    """
    try:
        if not (handles or types or layer):
            return _err("handles / types / layer 至少给一个（不允许整图删除）")
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        doc = ezdxf.readfile(str(p))
        msp = doc.modelspace()
        targets = _match_entities(msp, types, layer, handles)
        if not targets:
            return _err("没有匹配的实体（检查 handle/类型/图层拼写）")
        by_type: dict[str, int] = {}
        for e in targets:
            if e.dxftype() == "DIMENSION":
                geo = getattr(e.dxf, "geometry", None)
                if geo:
                    try:
                        doc.blocks.get(geo).delete_all_entities()
                    except Exception:  # noqa: BLE001
                        pass
            msp.delete_entity(e)
            by_type[e.dxftype()] = by_type.get(e.dxftype(), 0) + 1
        _backup_file(p)
        doc.saveas(str(p))
        return _ok(path=str(p), deleted=len(targets), by_type=by_type,
                   backup=".bak1（dxf_restore 可回滚）")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_modify(
    path: str,
    handles: list[str],
    layer: str | None = None,
    color=None,
    linetype: str | None = None,
    text: str | None = None,
    move: list[float] | None = None,
    rotate_deg: float | None = None,
    rotate_about: list[float] | None = None,
    scale: float | None = None,
    scale_about: list[float] | None = None,
) -> dict:
    """修改实体（修改前自动滚动备份，dxf_restore 可回滚）。

    Args:
        path: DXF 文件。
        handles: 目标 handle 列表（必填，用 dxf_query 获取）。
        layer: 移到图层（不存在会自动创建）
        color: 新颜色（ACI 数字 / 颜色名 / #RRGGBB）
        linetype: 新线型（Continuous/Dashed/Center/Hidden/Phantom/Dashdot）
        text: 替换文字内容（仅 TEXT/MTEXT 有效）
        move: [dx, dy] 平移
        rotate_deg: 旋转角度（度，逆时针为正）
        rotate_about: 旋转中心 [x, y]，默认 [0, 0]
        scale: 均匀缩放倍数
        scale_about: 缩放中心 [x, y]，默认 [0, 0]
    变换按 move → rotate → scale 顺序应用。
    """
    try:
        if not handles:
            return _err("handles 必填（用 dxf_query 获取）")
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        doc = ezdxf.readfile(str(p))
        msp = doc.modelspace()
        targets = _match_entities(msp, handles=handles)
        if not targets:
            return _err(f"没有匹配的实体（handles={list(handles)[:5]}...）")
        applied: list[str] = []
        if layer is not None:
            lname = str(layer)
            if lname not in doc.layers:
                doc.layers.add(lname)
            for e in targets:
                e.dxf.layer = lname
            applied.append(f"layer={lname}")
        if color is not None:
            ca = _color_attr(color)
            if "color" in ca:
                for e in targets:
                    e.dxf.color = ca["color"]
                applied.append(f"color={color}")
        if linetype:
            lt = str(linetype)
            if lt not in doc.linetypes:
                pattern = STANDARD_LINETYPE_PATTERNS.get(lt.upper())
                if pattern is None:
                    return _err(f"线型 {lt} 不存在且无内置 pattern 定义")
                doc.linetypes.add(lt, pattern)
            for e in targets:
                e.dxf.linetype = lt
            applied.append(f"linetype={lt}")
        if text is not None:
            bad = {e.dxftype() for e in targets
                   if e.dxftype() not in ("TEXT", "MTEXT")}
            if bad:
                return _err(f"text 仅对 TEXT/MTEXT 有效，目标含 {sorted(bad)}")
            for e in targets:
                e.dxf.text = str(text)
            applied.append(f"text={str(text)[:30]!r}")
        # 变换：A*B = 先 A 后 B；绕点 = T(-c) * M * T(c)
        m: Matrix44 | None = None
        if move:
            dx, dy = float(move[0]), float(move[1])
            m = Matrix44.translate(dx, dy, 0)
            applied.append(f"move=({dx:g},{dy:g})")
        if rotate_deg is not None:
            c = rotate_about or [0, 0]
            mr = (Matrix44.translate(-float(c[0]), -float(c[1]), 0)
                  * Matrix44.z_rotate(math.radians(float(rotate_deg)))
                  * Matrix44.translate(float(c[0]), float(c[1]), 0))
            m = mr if m is None else m * mr
            applied.append(f"rotate={rotate_deg:g}deg@({c[0]:g},{c[1]:g})")
        if scale is not None:
            c = scale_about or [0, 0]
            s = float(scale)
            ms = (Matrix44.translate(-float(c[0]), -float(c[1]), 0)
                  * Matrix44.scale(s, s, 1)
                  * Matrix44.translate(float(c[0]), float(c[1]), 0))
            m = ms if m is None else m * ms
            applied.append(f"scale={s:g}@({c[0]:g},{c[1]:g})")
        if m is not None:
            for e in targets:
                e.transform(m)
        if not applied:
            return _err("没有给出任何修改参数"
                        "（layer/color/linetype/text/move/rotate_deg/scale 至少一项）")
        _backup_file(p)
        doc.saveas(str(p))
        return _ok(path=str(p), modified=len(targets), applied=applied,
                   backup=".bak1（dxf_restore 可回滚）")
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_restore(path: str, slot: int = 1) -> dict:
    """把文件恢复到某份自动备份（修改类工具落盘前自动滚动备份）。

    Args:
        path: DXF 文件。
        slot: 备份槽位 1~5，1 = 最近一次修改前的状态（默认），5 = 最旧。
    """
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        slot = int(slot)
        if not 1 <= slot <= BACKUP_MAX:
            return _err(f"slot 需在 1~{BACKUP_MAX} 之间")
        bak = p.with_name(p.name + f".bak{slot}")
        if not bak.exists():
            avail = [i for i in range(1, BACKUP_MAX + 1)
                     if p.with_name(p.name + f".bak{i}").exists()]
            return _err(f"备份 .bak{slot} 不存在；可用槽位: {avail or '无（还没有备份）'}")
        bak.replace(p)
        return _ok(path=str(p), restored_from=bak.name, slot=slot,
                   size_bytes=p.stat().st_size)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


_DWG_MAGIC_VERSIONS = {
    "AC1006": "R12",
    "AC1009": "R13",
    "AC1012": "R14",
    "AC1015": "R2000",
    "AC1018": "R2004",
    "AC1021": "R2007",
    "AC1024": "R2010",
    "AC1027": "R2013",
    "AC1032": "R2018",
}


def _find_libredwg_tool(name: str) -> str | None:
    """定位 LibreDWG 命令行工具（dxf2dwg / dwg2dxf / ...）。"""
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        candidate = os.path.join(prefix, name)
        if os.path.exists(candidate):
            return candidate
    return None


def _dwg_version(p: Path) -> str | None:
    """从 DWG 文件头 magic（前 6 字节，如 AC1015）识别版本。"""
    try:
        with open(p, "rb") as f:
            magic = f.read(6).decode("ascii", "replace")
    except OSError:
        return None
    return _DWG_MAGIC_VERSIONS.get(magic)


def _dwg_to_dxf(p: Path, tmpdir: Path) -> tuple[Path, str]:
    """DWG → 临时 DXF（LibreDWG dwg2dxf）。返回 (临时 DXF 路径, 版本字符串)。

    dwg2dxf 把输出写到当前工作目录（文件名 = 输入文件名改 .dxf），
    所以把子进程 cwd 指到临时目录即可。
    """
    exe = _find_libredwg_tool("dwg2dxf")
    if exe is None:
        raise RuntimeError("未找到 dwg2dxf：读取 DWG 依赖 LibreDWG，"
                           "macOS 请先执行 `brew install libredwg`")
    r = subprocess.run([exe, str(p)], cwd=str(tmpdir), capture_output=True,
                       text=True, timeout=300)
    out = tmpdir / (p.stem + ".dxf")
    if r.returncode != 0 or not out.exists():
        detail = ((r.stdout or "") + (r.stderr or "")).strip()
        raise RuntimeError(f"dwg2dxf 转换失败 (exit {r.returncode}): {detail[-500:]}")
    return out, _dwg_version(p) or "unknown"


def _dxf_stats(doc, p: Path) -> dict:
    """DXF 文档的公共统计（dxf_read 的 DXF 直读与 DWG 转读共用）。"""
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


@mcp.tool()
def dxf_read(path: str) -> dict:
    """检查一张图纸，DXF / DWG 均支持：

    - DXF：ezdxf 直接读取
    - DWG：经 LibreDWG dwg2dxf 自动转临时 DXF 后读取（需
      `brew install libredwg`，与 dxf_convert_dwg 同一依赖）

    返回：格式、版本、实体总数与类型分布、包围盒、图层列表。
    注意：DWG 走 LibreDWG 开源实现，MATERIAL 等专有对象会被跳过，
    统计与 AutoCAD 显示可能有细微差异，终显以 AutoCAD 为准。
    """
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        if p.suffix.lower() == ".dwg":
            with tempfile.TemporaryDirectory(prefix="dwg2dxf-read-") as td:
                tmp_dxf, version = _dwg_to_dxf(p, Path(td))
                try:
                    doc = ezdxf.readfile(str(tmp_dxf))
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(
                        "DWG→DXF 转出后 ezdxf 解析失败（LibreDWG 对该 DWG "
                        f"版本的转出不完整，如 0.14 的 r2004 往返 bug）: {e}") from e
                stats = _dxf_stats(doc, p)
            stats["source_format"] = "DWG"
            stats["dwg_version"] = version
            stats["note"] = ("LibreDWG dwg2dxf 转读；专有对象（MATERIAL 等）已跳过，"
                             "终显以 AutoCAD 为准。")
            return stats
        doc = ezdxf.readfile(str(p))
        stats = _dxf_stats(doc, p)
        stats["source_format"] = "DXF"
        return stats
    except subprocess.TimeoutExpired:
        return _err("DWG 读取超时（300s）：图纸可能过大")
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


# ── DXF → DWG 转换（LibreDWG）─────────────────────────────────────────────
#
# macOS 上可脚本化的 DXF→DWG 只有 LibreDWG（ODA File Converter 无 Mac 版，
# ezdxf 只能读写 DXF，DWG 是 Autodesk 专有格式）。brew install libredwg。

_DXF2DWG_VERSIONS = {
    "r12": ("AC1006", "R12"),
    "r14": ("AC1009", "R14"),
    "r2000": ("AC1015", "R2000"),
    "r2004": ("AC1018", "R2004"),
}


def _find_dxf2dwg() -> str | None:
    """定位 dxf2dwg（LibreDWG）可执行文件。"""
    return _find_libredwg_tool("dxf2dwg")


def _normalize_mtext_for_libredwg(src: Path, dst: Path) -> int:
    """LibreDWG 0.14 读取器 bug：无法解析 MTEXT 的 group code 50（旋转角），
    直接报 'Invalid DXF code 50 for MTEXT' 使整个转换失败（实测复现）。
    转换前把全部 MTEXT（模型空间 + 所有图块，含标注的缓存图形表示）的
    旋转角归零另存临时副本，源文件不动。返回被修改的 MTEXT 数量。

    显示不受影响：MTEXT 只是 DIMENSION 的缓存渲染，AutoCAD 打开 DWG 后
    会按标注定义点与 dimstyle 重新渲染，竖直标注文字自动恢复 90° 方向。
    """
    doc = ezdxf.readfile(str(src))
    fixed = 0
    for blk in [doc.modelspace()] + list(doc.blocks):
        for e in blk:
            if e.dxftype() == "MTEXT" and e.dxf.rotation != 0:
                e.dxf.rotation = 0
                fixed += 1
    doc.saveas(str(dst))
    return fixed


@mcp.tool()
def dxf_convert_dwg(
    path: str,
    output_path: str | None = None,
    version: str = "r2000",
    overwrite: bool = True,
) -> dict:
    """把 DXF 转换为 DWG（LibreDWG dxf2dwg，纯本地，不需要 AutoCAD）。

    Args:
        path: 源 DXF 文件。
        output_path: 输出 DWG 路径，默认与源文件同名改 .dwg 后缀。
        version: 目标 DWG 版本 "r12"/"r14"/"r2000"/"r2004"（LibreDWG 支持上限
            r2004），默认 r2000（AC1015，最成熟；AutoCAD 2024 均可打开）。
            注意：LibreDWG 0.14 的 r2004 读回不完整（回读验证会失败），
            建议优先 r2000。
        overwrite: 输出文件已存在时覆盖（默认 True）。

    注意：
        - 依赖系统安装 LibreDWG（macOS: `brew install libredwg`），
          未安装时返回安装提示。
        - 转换后自动回读验证（dwg2dxf → ezdxf 解析），verified=false 时
          文件仍可能用 AutoCAD 打开，但建议换 r2000 重转。
        - LibreDWG 是开源实现，成熟度低于 Autodesk 官方：MATERIAL /
          MLEADERSTYLE 等专有对象自动跳过（不影响几何）。
        - 转换前会把 MTEXT 旋转角归零（绕开 LibreDWG 解析 bug，源 DXF 不变）；
          AutoCAD 打开 DWG 后标注文字按定义点重新渲染，显示不受影响。
    """
    try:
        p = _resolve(path)
        if not p.exists():
            return _err(f"文件不存在: {p}")
        ver = str(version).lower()
        if ver not in _DXF2DWG_VERSIONS:
            return _err(
                f"不支持的版本: {version}；可选: {', '.join(_DXF2DWG_VERSIONS)}")
        exe = _find_dxf2dwg()
        if exe is None:
            return _err("未找到 dxf2dwg：转换依赖 LibreDWG，"
                        "macOS 请先执行 `brew install libredwg`")
        out = _resolve(output_path) if output_path else p.with_suffix(".dwg")
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and not overwrite:
            return _err(f"输出文件已存在（overwrite=False）: {out}")

        fixed = 0
        with tempfile.TemporaryDirectory(prefix="dxf2dwg-") as td:
            tmp = Path(td) / p.name
            fixed = _normalize_mtext_for_libredwg(p, tmp)
            cmd = [exe, "--as", ver, "-o", str(out), str(tmp)]
            if overwrite:
                cmd.append("-y")
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            detail = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            return _err(f"转换失败 (exit {r.returncode}): "
                        f"{detail.strip()[-500:]}")
        magic = out.read_bytes()[:6].decode("ascii", "replace")
        expected_magic, expected_name = _DXF2DWG_VERSIONS[ver]

        # 回读验证：把刚写出的 DWG 再 dwg2dxf 转回并用 ezdxf 解析，
        # 确认文件真的可读（能提前暴露 LibreDWG 某些版本的往返 bug）。
        verified, verify_err = True, None
        with tempfile.TemporaryDirectory(prefix="dwg2dwg-verify-") as vtd:
            try:
                v_dxf, _ = _dwg_to_dxf(out, Path(vtd))
                ezdxf.readfile(str(v_dxf))
            except Exception as ve:  # noqa: BLE001
                verified = False
                verify_err = f"{type(ve).__name__}: {str(ve)[:200]}"

        note = ("LibreDWG dxf2dwg 转换；MATERIAL 等专有对象已自动跳过，"
                "建议用 AutoCAD 打开确认终显（dxf_open_in_autocad）。")
        if not verified:
            note = ("⚠ 回读验证失败（LibreDWG 往返不完整）："
                    f"{verify_err}。文件仍可能用 AutoCAD 打开，"
                    "但建议换 version='r2000' 重转。")
        return _ok(
            path=str(p),
            dwg=str(out),
            size_bytes=out.stat().st_size,
            dwg_version=expected_name if magic == expected_magic else magic,
            mtext_rotation_fixed=fixed,
            verified=verified,
            note=note,
        )
    except subprocess.TimeoutExpired:
        return _err("转换超时（300s）：图纸可能过大")
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
