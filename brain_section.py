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


def _roof_height(edges, tan):
    """h(P) 标量（建筑外部返回 None）与向量化 h_arr(X,Y)（外部 NaN）。"""
    import numpy as np

    def h(P):
        v = [ (P[0] - A[0]) * n[0] + (P[1] - A[1]) * n[1] for A, n in edges ]
        pos = [d for d in v if d > 1e-9]
        if not pos:
            return None
        return min(pos) * tan

    def h_arr(X, Y):
        dmin = None
        for A, n in edges:
            d = (X - A[0]) * n[0] + (Y - A[1]) * n[1]
            d = np.where(d > 1e-9, d, np.inf)
            dmin = d if dmin is None else np.minimum(dmin, d)
        return np.where(np.isfinite(dmin), dmin, np.nan) * tan

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
            # 约束区间（全部线性）
            lo, hi = -10 * size, 10 * size
            # 1) 建筑内部
            for (ta, tb) in _line_intervals(p0, (ux, uy), poly, lo, hi):
                lo2, hi2 = max(lo, ta), min(hi, tb)
                if hi2 - lo2 < 1e-6:
                    continue
                # 2) 本面为正  3) 不被其他面压低
                l, h = lo2, hi2
                if b[i] > 0:
                    l = max(l, (1e-9 - a[i]) / b[i])
                elif b[i] < 0:
                    h = min(h, (1e-9 - a[i]) / b[i])
                for k in range(ne):
                    if k in (i, j):
                        continue
                    A2, B2 = a[k] - a[i], b[k] - b[i]
                    if B2 > 1e-12:
                        l = max(l, (-1e-9 - A2) / B2)
                    elif B2 < -1e-12:
                        h = min(h, (-1e-9 - A2) / B2)
                if h - l > 1e-6:
                    x0 = p0[0] + ux * l
                    y0 = p0[1] + uy * l
                    x1 = p0[0] + ux * h
                    y1 = p0[1] + uy * h
                    z0 = (a[i] + b[i] * l) * tan
                    z1 = (a[i] + b[i] * h) * tan
                    lines.append({
                        "p0": [round(x0, 4), round(y0, 4)],
                        "z0": round(z0, 4),
                        "p1": [round(x1, 4), round(y1, 4)],
                        "z1": round(z1, 4),
                        "eaves": [i, j],
                    })
    # 去重（同一交线可能被相邻边对重复求出）
    uniq = []
    for ln in lines:
        for u in uniq:
            if u["eaves"] == ln["eaves"]:
                continue
            # 共线且区间重叠
            (x0, y0), (x1, y1) = ln["p0"], ln["p1"]
            (u0, v0), (u1, v1) = u["p0"], u["p1"]
            d1 = math.hypot(x1 - x0, y1 - y0)
            d2 = math.hypot(u1 - u0, v1 - v0)
            if d1 < 1e-9 or d2 < 1e-9:
                continue
            tmin = 0.0
            tmax = 1.0
            # 投影 ln 到 u 的区间
            t0 = ((x0 - u0) * (u1 - u0) + (y0 - v0) * (v1 - v0)) / d2
            t1 = ((x1 - u0) * (u1 - u0) + (y1 - v0) * (v1 - v0)) / d2
            ta, tb = min(t0, t1), max(t0, t1)
            # 垂直距离
            cross = abs((u1 - u0) * (y0 - v0) - (v1 - v0) * (x0 - u0)) / d2
            if cross < 1e-6 and max(tmin, ta) < min(tmax, tb) - 1e-6:
                break
        else:
            uniq.append(ln)
    return {"edges": [list(A) + list(n) for A, n in edges],
            "tan": round(tan, 6),
            "lines": uniq}


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
        t0, t1 = ts[k], ts[q]
        runs.append(((x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                     (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1),
                     bool(vis[k])))
        k = q
    return runs
