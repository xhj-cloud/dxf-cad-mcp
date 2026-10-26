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
from ezdxf import bbox
from ezdxf.math import Matrix44

from mcp.server.fastmcp import FastMCP


def _register_cjk_fonts() -> None:
    """matplotlib 默认不注册 macOS 的 .ttc 字体集合，不注册时 DXF 里的中文
    在 PNG 预览中渲染为空白（AutoCAD 端不受影响）。静默注册系统 CJK 字体。"""
    import glob as _glob

    import matplotlib

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


_MPL: dict | None = None


def _mpl() -> dict:
    """懒加载 matplotlib 与 ezdxf 绘图后端（仅 dxf_render / dxf_export_pdf 需要）。

    为什么必须懒加载：matplotlib 首次导入要扫描系统字体并（缓存缺失时）重建
    字体缓存，冷启动可达 8~9 秒。MCP 客户端在 initialize 之后要等 tools/list
    返回才拿到本服务器的工具表——如果这 9 秒卡在模块导入期，客户端建立会话时
    的工具清单就可能只捕获到「本服务器尚未注册」的状态，导致这些工具在整轮
    会话里对模型不可见。延迟到真正渲染时再导入，可让握手在 1 秒内完成。
    """
    global _MPL
    if _MPL is None:
        import matplotlib

        matplotlib.use("Agg")  # 无显示环境渲染
        _register_cjk_fonts()
        import matplotlib.pyplot as plt
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

        _MPL = {
            "plt": plt,
            "Frontend": Frontend,
            "RenderContext": RenderContext,
            "BackgroundPolicy": BackgroundPolicy,
            "Configuration": Configuration,
            "MatplotlibBackend": MatplotlibBackend,
        }
    return _MPL

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
    if spec.get("lineweight") is not None:
        attrs["lineweight"] = _lineweight(int(spec["lineweight"]))
    return attrs


def _lineweight(v: int) -> int:
    """线宽归一（单位 1/100 mm）。支持标准值 0/5/9/13/15/18/20/25/30/35/40/50/
    53/60/70/80/90/100/106/120/140/158/200/211；非标准值就近取标准档。"""
    std = (0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80,
           90, 100, 106, 120, 140, 158, 200, 211)
    if v in std:
        return v
    return min(std, key=lambda s: abs(s - v))


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
        if spec.get("style"):
            ta["style"] = str(spec["style"])
        msp.add_text(spec["text"], dxfattribs=ta)
    elif t == "mtext":
        ta = dict(attrs)
        ta["insert"] = spec["point"]
        ta["char_height"] = float(spec.get("char_height", 2.5))
        if spec.get("width"):
            ta["width"] = float(spec["width"])
        if spec.get("style"):
            ta["style"] = str(spec["style"])
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
    color（ACI 数字 / 颜色名如 "red" / "#RRGGBB"）、
    lineweight（线宽，单位 1/100mm，如 50=0.5mm 粗实线 / 25=0.25mm /
    13=0.13mm 细线；不传则跟随图层）。

    实体类型与必填字段：
      line:      start, end
      polyline:  points[[x,y],...], closed(bool, 默认 false)
      rectangle: corner[x,y], width, height
      circle:    center[x,y], radius
      arc:       center[x,y], radius, start_angle, end_angle(度, 逆时针)
      ellipse:   center[x,y], major_radius, ratio(0~1), rotation(度, 可选)
      point:     point[x,y]
      text:      point[x,y], text, height, rotation(度, 可选), style(文字样式, 可选)
      mtext:     point[x,y], text, char_height, width(可选), style(文字样式, 可选)
      style 需先用 dxf_styles 创建（中文文字必须挂中文样式，否则 CAD 乱码）
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
       如 "Continuous"/"Dashed"/"Center"/"Hidden"),
       lineweight(可选 int, 单位 1/100mm，如 50=0.5mm 粗实线 /
       25=0.25mm / 13=0.13mm 细线；AutoCAD 需开"线宽显示"才可见),
       on(可选 bool), frozen(可选 bool), locked(可选 bool)}     → 创建/更新
      {name(必填), rename_to: "新名"}                           → 重命名
      {name(必填), delete: true}                                → 删除
       （非空图层会被保护性拒绝，返回该图层上的实体数提示）
    状态说明: on=false 关闭图层(不显示不打印) / frozen=true 冻结
    (不显示且不参与重生成) / locked=true 锁定(显示但不能编辑)。
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
            if spec.get("lineweight") is not None:
                # 注意: 必须写 layer.dxf.lineweight（group 370）。
                # layer.lineweight 是 ezdxf 只读便捷属性，直接赋值会变成
                # 实例属性影子、dxf 值不变（静默失败）。
                layer.dxf.lineweight = _lineweight(int(spec["lineweight"]))
            # 图层状态（ezdxf 的编码：off = 颜色取负号；frozen/locked = group 70 位）。
            # 先设颜色再开关，off 会把颜色变负——所以 on/off 必须放在 color 之后。
            if spec.get("on") is not None:
                layer.on() if spec["on"] else layer.off()
            if spec.get("frozen") is not None:
                layer.freeze() if spec["frozen"] else layer.thaw()
            if spec.get("locked") is not None:
                layer.lock() if spec["locked"] else layer.unlock()
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


STYLE_PRESETS = {
    "宋体": ("simsun.ttc", ""),
    "黑体": ("simhei.ttf", ""),
    "楷体": ("simkai.ttf", ""),
    "仿宋": ("simfang.ttf", ""),
    "gbcbig": ("gbenor.shx", "gbcbig.shx"),
    "simplex": ("simplex.shx", ""),
    "arial": ("arial.ttf", ""),
}


@mcp.tool()
def dxf_styles(path: str, styles: list[dict]) -> dict:
    """创建/更新/删除文字样式——中文显示正确的关键。
    默认样式 Standard 挂的是 txt.shx（无中文字形），CAD 里中文必乱码；
    把文字挂到挂中文 TTF/SHX 字体的样式上即可。

    Args:
        path: DXF 文件（不存在会先创建）。
        styles: 样式列表，每项:
          name     (必填) 样式名，如 "ZHONGWEN"
          preset   中文字体预设: 宋体/黑体/楷体/仿宋/gbcbig(SHX 双字体)，
                   或直接给 font
          font     字体文件名: TTF 如 "simsun.ttc"/"simhei.ttf"，SHX 如 "gbenor.shx"
          bigfont  大字体（SHX 专用，如 "gbcbig.shx"；gbcbig 预设已含）
          width    宽度因子（默认 1.0）
          delete   true 时删除该样式
    使用: dxf_draw 的 text/mtext 传 style 字段，或 dxf_modify 的 style 参数
    给已有文字改样式。字体缺失时 AutoCAD 会弹替换对话框，任选一个中文字体
    即可正常显示（文字按 Unicode 存储，换字体不丢字）。
    """
    try:
        doc, _ = _load_or_create(path)
        results = []
        for spec in styles:
            name = str(spec.get("name", "")).strip()
            if not name:
                return _err("每个样式必须给 name")
            if spec.get("delete"):
                if name in doc.styles:
                    del doc.styles[name]
                    results.append(f"deleted {name}")
                else:
                    results.append(f"{name} 不存在（跳过）")
                continue
            font = str(spec.get("font", ""))
            bigfont = str(spec.get("bigfont", ""))
            preset = str(spec.get("preset", "")).strip()
            if preset:
                if preset not in STYLE_PRESETS:
                    return _err(f"未知预设 {preset!r}，可选: {sorted(STYLE_PRESETS)}")
                font, bigfont = STYLE_PRESETS[preset]
            if not font:
                return _err(f"样式 {name} 缺 font/preset")
            existed = name in doc.styles
            st = (doc.styles.get(name) if existed
                  else doc.styles.add(name, font=font))
            st.dxf.font = font
            if bigfont:
                st.dxf.bigfont = bigfont
            else:
                try:
                    del st.dxf.bigfont
                except Exception:  # noqa: BLE001
                    pass
            st.dxf.width = float(spec.get("width", 1.0))
            results.append(
                f"{'updated' if existed else 'created'} {name}: font={font}"
                + (f" bigfont={bigfont}" if bigfont else ""))
        doc.saveas(str(_resolve(path)))
        return _ok(path=path, results=results)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


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
    style: str | None = None,
    lineweight: int | None = None,
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
        style: 文字样式名（仅 TEXT/MTEXT 有效，样式需先用 dxf_styles 创建；
            中文乱码就靠它：样式挂中文 TTF/SHX 字体）
        lineweight: 线宽（1/100mm，如 50=0.5mm 粗实线 / 13=0.13mm 细线；
            非标准值就近取标准档）。实体级线宽优先级高于图层线宽。
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
        if style is not None:
            sname = str(style)
            if sname not in doc.styles:
                return _err(f"样式 {sname} 不存在（先用 dxf_styles 创建）")
            bad = {e.dxftype() for e in targets
                   if e.dxftype() not in ("TEXT", "MTEXT")}
            if bad:
                return _err(f"style 仅对 TEXT/MTEXT 有效，目标含 {sorted(bad)}")
            for e in targets:
                e.dxf.style = sname
            applied.append(f"style={sname}")
        if lineweight is not None:
            lw = _lineweight(int(lineweight))
            for e in targets:
                e.dxf.lineweight = lw
            applied.append(f"lineweight={lw}")
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
        layers=[
            {"name": l.dxf.name,
             "on": l.is_on(),
             "frozen": l.is_frozen(),
             "locked": l.is_locked(),
             "color": l.get_color()}
            for l in doc.layers
        ],
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
    m = _mpl()
    plt = m["plt"]
    fixed = str(page_size).upper() in ("A4", "A3", "A2")
    if fixed:
        # 横版纸张尺寸（英寸）；adjust_figure=False 防止 finalize 按
        # 数据宽高比重设图大小、把纸张尺寸冲掉
        sizes = {"A4": (11.69, 8.27), "A3": (16.54, 11.69), "A2": (23.39, 16.54)}
        fig = plt.figure(figsize=sizes[str(page_size).upper()])
        ax = fig.add_axes([0.04, 0.04, 0.92, 0.92])
        backend = m["MatplotlibBackend"](ax, adjust_figure=False)
    else:
        fig = plt.figure()
        ax = fig.add_axes([0.02, 0.02, 0.96, 0.96])
        backend = m["MatplotlibBackend"](ax)
    config = m["Configuration"](
        background_policy=m["BackgroundPolicy"].CUSTOM,
        custom_bg_color=bg,
    )
    ctx = m["RenderContext"](doc)
    frontend = m["Frontend"](ctx, backend, config=config)
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
        _mpl()["plt"].close(fig)
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
        _mpl()["plt"].close(fig)
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


# ─────────────────────────────────────────────────────────────────────────
# “算图脑”：制图几何计算工具（纯计算，不读写 DXF 文件）
#
# 面向《工程制图》同坡屋面类题目：
#   已知屋檐线 H 面投影（封闭多边形）+ 坡面倾角 α →
#   1) dxf_roof_solve        求屋面交线（3D：平面端点 + 高度 z）
#   2) dxf_roof_three_view   求三面投影（第一角法 H/V/W，含可见性、
#                            轮廓线、45° 辅助线/传递线，直接可画）
#
# 模型：每条檐线定义一个倾角 α 的斜面，屋面高度（建筑内部）
#       h(P) = min{ d_e(P) | d_e(P) > 0 } · tanα
#       d_e = P 到檐线 e 的有符号垂距（朝该檐线的建筑内侧为正）。
#       屋面交线 = 两个檐面同时取最小值的“屋脊”轨迹。
#       凹形多边形（内凹檐线）自动适用——内侧判定用“边中点+ε·法向”
#       奇偶测试，不依赖质心。
# ─────────────────────────────────────────────────────────────────────────

def _pip(pt, poly) -> bool:
    """点是否在多边形内（奇偶规则）。边界上返回 False。"""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def _poly_size(poly) -> float:
    return max(math.hypot(b[0] - a[0], b[1] - a[1])
               for a, b in zip(poly, poly[1:] + poly[:1]))


def _roof_edges(poly):
    """每条檐线 → (端点 A, 指向建筑内侧的单位法向 n)。
    d_e(P) = dot(P - A, n)，内侧为正。"""
    eps = 1e-3 * _poly_size(poly)
    edges = []
    n = len(poly)
    for i in range(n):
        A, B = poly[i], poly[(i + 1) % n]
        vx, vy = B[0] - A[0], B[1] - A[1]
        L = math.hypot(vx, vy)
        if L < 1e-12:
            raise ValueError(f"檐线 {i} 长度为零（点 {A} 与 {B} 重合）")
        nx, ny = vy / L, -vx / L
        Mx, My = (A[0] + B[0]) / 2, (A[1] + B[1]) / 2
        if not _pip((Mx + nx * eps, My + ny * eps), poly):
            nx, ny = -nx, -ny
        edges.append((A, (nx, ny)))
    return edges


def _roof_height(poly, edges, tan):
    """h(P) 标量（建筑外部返回 None）与向量化 h_arr(X,Y)（外部 NaN）。

    关键：必须先用奇偶测试判定“是否在建筑内部”，再取到各檐线内侧
    垂距的最小值——凹口（内凹檐线围出的外部区域）里的点若不做此判定，
    会被“内侧垂距取最小”误算出一个虚假的正高度。"""
    import numpy as np

    def h(P):
        if not _pip(P, poly):
            return None
        v = [ (P[0] - A[0]) * n[0] + (P[1] - A[1]) * n[1] for A, n in edges ]
        act = [d for d in v if d >= -1e-9]  # 含 0：檐口线上高度恰为 0
        if not act:
            return None
        return min(act) * tan

    def h_arr(X, Y):
        dmin = None
        for A, n in edges:
            d = (X - A[0]) * n[0] + (Y - A[1]) * n[1]
            d = np.where(d >= -1e-9, d, np.inf)
            dmin = d if dmin is None else np.minimum(dmin, d)
        # 奇偶测试（向量化）：建筑内部才有效
        cnt = np.zeros(X.shape, dtype=np.int32)
        m_ = len(poly)
        for i in range(m_):
            xi, yi = poly[i]
            xj, yj = poly[(i + 1) % m_]
            if yi == yj:
                continue
            m = (yi > Y) != (yj > Y)
            xint = (xj - xi) * (Y - yi) / (yj - yi) + xi
            cnt += np.where(m & (X < xint), 1, 0)
        inside = (cnt % 2) == 1
        return np.where(inside & np.isfinite(dmin), dmin, np.nan) * tan

    h._arr = h_arr  # type: ignore[attr-defined]
    return h


def _line_intervals(p0, u, poly, tmin, tmax):
    """直线 p0 + t·u 位于多边形内部的参数区间 [(t0,t1), ...]。"""
    ts = []
    n = len(poly)
    ux, uy = u
    for i in range(n):
        A, B = poly[i], poly[(i + 1) % n]
        ex, ey = B[0] - A[0], B[1] - A[1]
        den = ux * ey - uy * ex
        if abs(den) < 1e-12:
            continue
        t = ((A[0] - p0[0]) * ey - (A[1] - p0[1]) * ex) / den
        s = ((A[0] - p0[0]) * uy - (A[1] - p0[1]) * ux) / den
        if -1e-9 <= s <= 1.0 + 1e-9:
            ts.append(t)
    ts.sort()
    ded = []
    for t in ts:
        if not ded or t - ded[-1] > 1e-6:
            ded.append(t)
    out = []
    cand = [tmin] + ded + [tmax]
    for a, b in zip(cand, cand[1:]):
        if b - a < 1e-6:
            continue
        tm = 0.5 * (a + b)
        if _pip((p0[0] + ux * tm, p0[1] + uy * tm), poly):
            out.append((a, b))
    return out


def _roof_solve_core(poly, pitch_deg):
    """同坡屋面核心求解。返回 dict：edges / tan / lines（3D 交线）。"""
    edges = _roof_edges(poly)
    tan = math.tan(math.radians(pitch_deg))
    size = _poly_size(poly)

    def d_k(P, k):
        A, n = edges[k]
        return (P[0] - A[0]) * n[0] + (P[1] - A[1]) * n[1]

    lines = []
    ne = len(edges)
    for i in range(ne):
        for j in range(i + 1, ne):
            Ai, ni = edges[i]
            Aj, nj = edges[j]
            wx, wy = ni[0] - nj[0], ni[1] - nj[1]
            wl = math.hypot(wx, wy)
            if wl < 1e-9:
                continue  # 平行檐线，斜面平行不相交
            c = (Ai[0] * ni[0] + Ai[1] * ni[1]) - (Aj[0] * nj[0] + Aj[1] * nj[1])
            # 平分线: dot(P, w) = c；方向 u ⊥ w
            ux, uy = -wy / wl, wx / wl
            p0 = (c * wx / (wl * wl), c * wy / (wl * wl))
            # d_k(t) = a_k + t·b_k（线性）
            a = [(p0[0] - A[0]) * n[0] + (p0[1] - A[1]) * n[1] for A, n in edges]
            b = [ux * n[0] + uy * n[1] for _, n in edges]
            def is_ridge(tm):
                """t 处 P 是否位于 i、j 两檐面的共同最低（屋脊）上。
                规则：h = min{d_e : d_e ≥ -eps}，故要求 d_i ≥ -eps，
                且每个“激活”面（d_k ≥ -eps）都不低于 d_i；
                非激活面（d_k < -eps，如凹口外侧延伸）不构成约束。"""
                di = a[i] + b[i] * tm
                if di < -1e-9:
                    return False
                for k in range(ne):
                    if k in (i, j):
                        continue
                    dk = a[k] + b[k] * tm
                    if dk > 1e-9 and dk < di - 1e-9:
                        return False
                return True

            lo, hi = -10 * size, 10 * size
            for (ta, tb) in _line_intervals(p0, (ux, uy), poly, lo, hi):
                # 关键点：区间端点 + d_i=0 的根 + d_k=0 的根 + d_k=d_i 的根。
                # 每个子区间内“激活面集合”与 d_i 的符号都不变，
                # 中点判定即代表整段，端点由此精确（落在檐口/三面交汇点）。
                crit = [ta, tb]
                if abs(b[i]) > 1e-12:
                    crit.append(-a[i] / b[i])
                for k in range(ne):
                    if k in (i, j):
                        continue
                    if abs(b[k]) > 1e-12:
                        crit.append(-a[k] / b[k])
                    B2 = b[k] - b[i]
                    if abs(B2) > 1e-12:
                        crit.append((a[i] - a[k]) / B2)
                crit = sorted(t for t in crit if ta <= t <= tb)
                parts = []
                for s1, s2 in zip(crit, crit[1:]):
                    if s2 - s1 < 1e-9:
                        continue
                    if is_ridge(0.5 * (s1 + s2)):
                        if parts and abs(parts[-1][1] - s1) < 1e-9:
                            parts[-1][1] = s2
                        else:
                            parts.append([s1, s2])
                for (s1, s2) in parts:
                    if s2 - s1 < 1e-6:
                        continue
                    lines.append({
                        "p0": [round(p0[0] + ux * s1, 4),
                               round(p0[1] + uy * s1, 4)],
                        "z0": round((a[i] + b[i] * s1) * tan, 4),
                        "p1": [round(p0[0] + ux * s2, 4),
                               round(p0[1] + uy * s2, 4)],
                        "z1": round((a[i] + b[i] * s2) * tan, 4),
                        "eaves": [i, j],
                    })
    return {"edges": [list(A) + list(n) for A, n in edges],
            "tan": round(tan, 6),
            "lines": lines}


def _fit_silhouette(poly, h, axis, tan):
    """V/W 轮廓（上包络）：采样 → 分段拟合（斜率 ∈ {+tan, −tan, 0} 或竖直）。
    返回 [{t:'s',x0,z0,x1,z1} | {t:'v',x,z0,z1}]（局部坐标）。"""
    import numpy as np

    x0 = min(p[0] for p in poly)
    x1 = max(p[0] for p in poly)
    y0 = min(p[1] for p in poly)
    y1 = max(p[1] for p in poly)
    step = 0.02
    if axis == "V":
        ts = np.arange(x0, x1 + step / 2, step)
        other = np.arange(y0, y1 + step / 2, 0.15)
        X, O = np.meshgrid(ts, other, indexing="ij")
        Z = np.nanmax(h._arr(X, O), axis=1)
    else:
        ts = np.arange(y0, y1 + step / 2, step)
        other = np.arange(x0, x1 + step / 2, 0.15)
        X, O = np.meshgrid(other, ts, indexing="ij")
        Z = np.nanmax(h._arr(X, O), axis=0)
    zs = np.nan_to_num(Z, nan=-1e9)
    # 去掉端点处因“恰在边界”产生的 NaN 尖刺
    n = len(ts)
    for end in (0, n - 1):
        if zs[end] < -1e8:
            for d in (1, -1):
                q = end + d
                if 0 <= q < n and zs[q] > -1e8:
                    zs[end] = zs[q]
                    break
    segs = []
    i = 0
    while i < n - 1:
        if abs(zs[i + 1] - zs[i]) > 0.35:
            segs.append({"t": "v", "x": float(ts[i + 1]),
                         "z0": float(zs[i]), "z1": float(zs[i + 1])})
            i += 1
            continue
        best = None
        for slope in (tan, -tan, 0.0):
            j = i
            while j < n - 1:
                x0_, z0_ = ts[i], zs[i]
                x1_, z1_ = ts[j + 1], zs[j + 1]
                if abs(z1_ - (z0_ + slope * (x1_ - x0_))) > 0.06:
                    break
                j += 1
            if best is None or (j - i) > (best[1] - i):
                best = (slope, j)
        slope, j = best
        x0_, z0_ = float(ts[i]), float(zs[i])
        x1_, z1_ = float(ts[j]), float(zs[i] + slope * (ts[j] - ts[i]))
        if slope == 0.0:
            z1_ = z0_
        segs.append({"t": "s", "slope": slope, "x0": x0_, "z0": z0_,
                     "x1": x1_, "z1": z1_})
        i = j
    # 合并同向相邻段 + 去掉过短段
    m = []
    for s in segs:
        if m:
            pm = m[-1]
            if (s["t"] == "s" and pm["t"] == "s"
                    and s["slope"] == pm["slope"]
                    and abs(pm["z1"] - s["z0"]) < 0.06):
                pm["x1"] = s["x1"]
                pm["z1"] = s["z1"]
                continue
        m.append(s)
    m = [s for s in m
         if s["t"] == "v" or abs(s["x1"] - s["x0"]) > 0.15]
    # 端点竖直段若只到 z≈0（轮廓与基线相接）保留；
    # 修正微小 z 误差（端点吸附）
    for s in m:
        for k in ("z0", "z1"):
            s[k] = round(s[k], 3)
        if s["t"] == "s":
            s["x0"] = round(s["x0"], 3)
            s["x1"] = round(s["x1"], 3)
            if abs(s["z1"] - s["z0"]) < 0.05:
                s["z1"] = s["z0"]
        else:
            s["x"] = round(s["x"], 3)
    return m


def _view_runs(poly, edges, h, line3d, axis):
    """一条 3D 交线在 V 或 W 视图中的投影分段 + 可见性。
    返回 [(p0_2d, p1_2d, visible)]（2D 为局部坐标：V=(x,z)，W=(y,z)）。"""
    import numpy as np

    (x0, y0), (x1, y1) = line3d["p0"], line3d["p1"]
    z0, z1 = line3d["z0"], line3d["z1"]
    xmin, xmax = min(p[0] for p in poly), max(p[0] for p in poly)
    ymin, ymax = min(p[1] for p in poly), max(p[1] for p in poly)
    N = 33
    ts = np.linspace(0.0, 1.0, N)
    xs = x0 + (x1 - x0) * ts
    ys = y0 + (y1 - y0) * ts
    zs = z0 + (z1 - z0) * ts
    harr = h._arr
    vis = np.ones(N, dtype=bool)
    if axis == "V":
        # 视点在 −y 方向：被 y'<y 处更高的屋面遮挡
        scan = np.arange(ymin, ymax + 0.125, 0.25)
        for k in range(N):
            m = (scan <= ys[k]) & (scan > ymin - 1e-9)
            if not m.any():
                continue
            Xs = np.full(int(m.sum()), xs[k])
            Ys = scan[m]
            hv = harr(Xs, Ys)
            vis[k] = not (np.nanmax(hv) > zs[k] + 0.02)
    else:
        # 左视图：视点在 −x 方向：被 x'<x 处更高的屋面遮挡
        scan = np.arange(xmin, xmax + 0.125, 0.25)
        for k in range(N):
            m = (scan <= xs[k]) & (scan > xmin - 1e-9)
            if not m.any():
                continue
            Xs = scan[m]
            Ys = np.full(int(m.sum()), ys[k])
            hv = harr(Xs, Ys)
            vis[k] = not (np.nanmax(hv) > zs[k] + 0.02)
    runs = []
    k = 0
    while k < N - 1:
        q = k
        while q < N - 1 and vis[q + 1] == vis[k]:
            q += 1
        if q > k:
            t0, t1 = ts[k], ts[q]
            runs.append(((x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                         (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1),
                         bool(vis[k])))
        k = q + 1  # 单点 run 也要前进，否则死循环
    return runs




@mcp.tool()
def dxf_roof_solve(polygon: list[list[float]], pitch_deg: float = 30.0) -> dict:
    """【算图】同坡屋面交线求解（纯计算，不写文件）。

    已知：屋檐线的 H 面投影（封闭多边形，按序给出，顺/逆时针均可）
    与坡面倾角 α。每条檐线定义一个倾角 α 的斜面，屋面交线即相邻
    檐面的交线（屋脊线）。自动处理内凹檐线（凹口），交线端点精确
    到檐口/三面交汇点（无采样误差）。

    Args:
        polygon: [[x, y], ...] 檐线顶点（H 面投影，任意单位，建议 mm）。
        pitch_deg: 坡面倾角（度），默认 30。

    返回:
        tan:      tan(α)
        edges:    每条檐线的端点与内侧单位法向 [[ax,ay,nx,ny], ...]
        lines:    屋面交线（3D），每条:
                  p0/p1 平面端点 [x,y]，z0/z1 对应端点高度，
                  eaves [i, j] 该交线由第 i、j 条檐线决定
        max_z:    屋面最高高度
    """
    try:
        poly = [[float(p[0]), float(p[1])] for p in polygon]
        if len(poly) < 4:
            return _err("polygon 至少 4 个点")
        if not (0 < pitch_deg < 90):
            return _err("pitch_deg 必须在 0~90 度之间")
        r = _roof_solve_core(poly, float(pitch_deg))
        return _ok(
            tan=r["tan"],
            edges=r["edges"],
            lines=r["lines"],
            line_count=len(r["lines"]),
            max_z=round(max((v for ln in r["lines"]
                             for v in (ln["z0"], ln["z1"])), default=0.0), 4),
        )
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


@mcp.tool()
def dxf_roof_three_view(
    polygon: list[list[float]],
    pitch_deg: float = 30.0,
    h_origin: list[float] = None,
    gap_v: float = 18.0,
    gap_w: float = 0.0,
    with_transfer: bool = True,
) -> dict:
    """【算图】同坡屋面三面投影（第一角法，纯计算，不写文件）。

    在 dxf_roof_solve 的基础上，直接给出可画图的纸面坐标：
    H（俯视图）在 h_origin，V（主视图）在 H 正上方（共 x 轴），
    W（左视图）在 V 右侧、共基线；W 视图 y 轴翻转（物体前表面在右）。
    含：各视图轮廓线（含凹口墙竖直轮廓边）、交线投影（逐段可见性：
    可见=粗实线/隐藏=细虚线）、45° 辅助线与角点传递线。

    Args:
        polygon:     同 dxf_roof_solve。
        pitch_deg:   坡面倾角（度），默认 30。
        h_origin:    H 视图局部原点（多边形 xmin,ymin）的纸面坐标
                     [px, py]（y 向下为正，DXF 坐标）。
                     缺省 [0, 0]。
        gap_v:       H 视图顶边到 V 基线的间距（默认 18）。
        gap_w:       V 视图右边到 W 视图左边的间距（默认 0，紧贴）。
        with_transfer: 是否输出 45° 辅助线/传递线（默认 True）。

    返回（所有坐标均为纸面坐标，可直接喂给 dxf_draw）:
        layout:  H0/V_base/W_left/W_extent/D_extent/xmin... 布局参数
        H:       {polygon: [[x,y],...], lines: [[[x0,y0],[x1,y1]],...]}
        V:       {baseline: [[x0,y0],[x1,y1]],
                  silhouette: [[x0,y0],[x1,y1],...],
                  lines: [[[x0,y0],[x1,y1], visible, eaves], ...]}
        W:       同 V（局部坐标按 y 翻转）
        construction: {line45, trans_v, trans_w, trans_w_up}
        max_z / tan / 说明
    """
    try:
        poly = [[float(p[0]), float(p[1])] for p in polygon]
        if len(poly) < 4:
            return _err("polygon 至少 4 个点")
        if not (0 < pitch_deg < 90):
            return _err("pitch_deg 必须在 0~90 度之间")
        H0x, H0y = (h_origin or [0, 0])
        xmin = min(p[0] for p in poly)
        xmax = max(p[0] for p in poly)
        ymin = min(p[1] for p in poly)
        ymax = max(p[1] for p in poly)
        Wext = xmax - xmin
        Dext = ymax - ymin
        V_base = H0y - gap_v
        V_right = H0x + Wext
        W_left = V_right + gap_w

        def H(x, y):
            return [round(H0x + (x - xmin), 3), round(H0y + (y - ymin), 3)]

        def V(x, z):
            return [round(H0x + (x - xmin), 3), round(V_base - z, 3)]

        def Wv(y, z):
            return [round(W_left + (ymax - y), 3), round(V_base - z, 3)]

        edges = _roof_edges(poly)
        tan = math.tan(math.radians(pitch_deg))
        h = _roof_height(poly, edges, tan)
        sol = _roof_solve_core(poly, float(pitch_deg))

        # --- H 视图 ---
        Hview = {
            "polygon": [H(x, y) for x, y in poly],
            "lines": [[list(H(ln["p0"][0], ln["p0"][1])),
                        list(H(ln["p1"][0], ln["p1"][1]))]
                       for ln in sol["lines"]],
        }
        # --- V / W 视图 ---
        silV = _fit_silhouette(poly, h, "V", tan)
        silW = _fit_silhouette(poly, h, "W", tan)
        Vview = {
            "baseline": [[H0x, V_base], [H0x + Wext, V_base]],
            "silhouette": [
                [[H0x + s["x0"], V_base - s["z0"]],
                 [H0x + s["x1"], V_base - s["z1"]]]
                if s["t"] == "s" else
                [[H0x + s["x"], V_base - s["z0"]],
                 [H0x + s["x"], V_base - s["z1"]]]
                for s in silV],
            "lines": [],
        }
        Wview = {
            "baseline": [[W_left, V_base], [W_left + Dext, V_base]],
            "silhouette": [
                [[W_left + (ymax - s["x0"]), V_base - s["z0"]],
                 [W_left + (ymax - s["x1"]), V_base - s["z1"]]]
                if s["t"] == "s" else
                [[W_left + (ymax - s["x"]), V_base - s["z0"]],
                 [W_left + (ymax - s["x"]), V_base - s["z1"]]]
                for s in silW],
            "lines": [],
        }
        # 交线投影：z 由端点线性插值（run 端点参数已知）
        for ln in sol["lines"]:
            (x0, y0), (x1, y1) = ln["p0"], ln["p1"]
            z0, z1 = ln["z0"], ln["z1"]

            def _zt(xa, ya, xb, yb):
                # 按主导轴插值 z（避免 plan 竖直/水平线的除零）
                if abs(x1 - x0) > abs(y1 - y0):
                    return (z0 + (z1 - z0) * (xa - x0) / (x1 - x0),
                            z0 + (z1 - z0) * (xb - x0) / (x1 - x0))
                return (z0 + (z1 - z0) * (ya - y0) / (y1 - y0),
                        z0 + (z1 - z0) * (yb - y0) / (y1 - y0))

            for run in _view_runs(poly, edges, h, ln, "V"):
                (xa, ya), (xb, yb), vis = run
                za, zb = _zt(xa, ya, xb, yb)
                Vview["lines"].append(
                    [V(xa, za), V(xb, zb), vis, ln["eaves"]])
            for run in _view_runs(poly, edges, h, ln, "W"):
                (xa, ya), (xb, yb), vis = run
                za, zb = _zt(xa, ya, xb, yb)
                # W 视图横轴 = plan 的 y（前表面 y=0 在右）
                Wview["lines"].append(
                    [Wv(ya, za), Wv(yb, zb), vis, ln["eaves"]])
        # --- 作图辅助线 ---
        constr = {}
        if with_transfer:
            ext = Dext + 15
            constr["line45"] = [[V_right, H0y + Dext],
                                [V_right + ext, H0y - (ext - Dext)]]
            constr["trans_v"] = [
                [H(x, y), [H(x, y)[0], V_base - 0.8]] for x, y in poly]
            constr["trans_w"] = []
            constr["trans_w_up"] = []
            for x, y in poly:
                hx, hy = H(x, y)
                tx = round(V_right + (ymax - y), 3)
                constr["trans_w"].append([[hx, hy], [tx, hy]])
                constr["trans_w_up"].append([[tx, hy], [tx, V_base - 0.8]])
        return _ok(
            layout={
                "H0": [H0x, H0y], "V_base": round(V_base, 3),
                "W_left": round(W_left, 3), "W_extent": round(Wext, 3),
                "D_extent": round(Dext, 3),
                "xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax,
            },
            H=Hview, V=Vview, W=Wview,
            construction=constr,
            tan=round(tan, 6),
            max_z=sol.get("max_z") or round(max(
                (v for ln in sol["lines"] for v in (ln["z0"], ln["z1"])),
                default=0.0), 4),
            line_count=len(sol["lines"]),
            note="可见性: V 视点在 -y（前），W 视点在 -x（左）；"
                 "visible=True 画粗实线，False 画细虚线（隐藏线）",
        )
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")


def main() -> None:
    mcp.run()  # 默认 stdio 传输


if __name__ == "__main__":
    main()
