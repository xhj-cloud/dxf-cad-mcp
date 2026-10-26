

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
            max_z=round(max((ln["z0"] for ln in r["lines"]),
                            (ln["z1"] for ln in r["lines"]), default=0.0), 4),
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
        h = _roof_height(edges, tan)
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
            "silhouette": (
                [[H0x + s["x0"], V_base - s["z0"]],
                 [H0x + s["x1"], V_base - s["z1"]]]
                if s["t"] == "s" else
                [[H0x + s["x"], V_base - s["z0"]],
                 [H0x + s["x"], V_base - s["z1"]]]
                for s in silV),
            "lines": [],
        }
        Wview = {
            "baseline": [[W_left, V_base], [W_left + Dext, V_base]],
            "silhouette": (
                [[W_left + (ymax - s["x0"]), V_base - s["z0"]],
                 [W_left + (ymax - s["x1"]), V_base - s["z1"]]]
                if s["t"] == "s" else
                [[W_left + (ymax - s["x"]), V_base - s["z0"]],
                 [W_left + (ymax - s["x"]), V_base - s["z1"]]]
                for s in silW),
            "lines": [],
        }
        # 交线投影：z 由端点线性插值（run 端点参数已知）
        for ln in sol["lines"]:
            (x0, y0), (x1, y1) = ln["p0"], ln["p1"]
            z0, z1 = ln["z0"], ln["z1"]
            for run in _view_runs(poly, edges, h, ln, "V"):
                (xa, ya), (xb, yb), vis = run
                za = z0 + (z1 - z0) * ((xa - x0) / (x1 - x0 or 1e-12))
                zb = z0 + (z1 - z0) * ((xb - x0) / (x1 - x0 or 1e-12))
                Vview["lines"].append(
                    [V(xa, za), V(xb, zb), vis, ln["eaves"]])
            for run in _view_runs(poly, edges, h, ln, "W"):
                (xa, ya), (xb, yb), vis = run
                za = z0 + (z1 - z0) * ((xa - x0) / (x1 - x0 or 1e-12))
                zb = z0 + (z1 - z0) * ((xb - x0) / (x1 - x0 or 1e-12))
                Wview["lines"].append(
                    [Wv(xa, za), Wv(xb, zb), vis, ln["eaves"]])
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
                (ln["z0"] for ln in sol["lines"]),
                (ln["z1"] for ln in sol["lines"]), default=0.0), 4),
            line_count=len(sol["lines"]),
            note="可见性: V 视点在 -y（前），W 视点在 -x（左）；"
                 "visible=True 画粗实线，False 画细虚线（隐藏线）",
        )
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")
