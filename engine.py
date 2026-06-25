"""
Territory rotation engine — generalized from the Chicago build.
Pure functions: drive-time computation, min-cost-flow assignment, 52-week scheduler, metrics.
No Streamlit / no API here so it can be unit-tested headless.
"""
from __future__ import annotations
import math, datetime as dt
from collections import Counter
import numpy as np, pandas as pd, networkx as nx

# ----------------------------- drive times -----------------------------
def _haversine_miles(a, b):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 2*R*math.asin(math.sqrt(h))

def estimate_minutes(miles, circuity=1.25, mph=50.0):
    """Straight-line miles -> estimated drive minutes (matches the BigQuery est_drive_minutes model)."""
    return miles * circuity / mph * 60.0

def build_drive_matrix(df, techs, zip_centroids=None, tech_coords=None):
    """
    Returns {tech_name: {zip: minutes}}.
    Priority 1: precomputed column '<tech>_drive_minutes' if present (lets us reuse the Chicago file).
    Priority 2: estimate from the tech's origin coords to each ZIP centroid.
                Origin = tech_coords[name] (from a street address, preferred) else the home_zip centroid.
    zip_centroids: {zip:(lat,lon)} for destination ZIPs (and home ZIPs as fallback origins).
    tech_coords:   {name:(lat,lon)} resolved from full addresses, if available.
    """
    tech_coords = tech_coords or {}
    out = {}
    for t in techs:
        name = t["name"]; col = f"{name.lower()}_drive_minutes"
        if col in df.columns:
            out[name] = dict(zip(df["zip"].astype(str), df[col].astype(float)))
            continue
        home = tech_coords.get(name) or (zip_centroids or {}).get(t.get("home_zip"))
        if home is None:
            raise ValueError(f"No location for {name}: provide a home address or home ZIP (or a precomputed drive column).")
        d = {}
        for z in df["zip"].astype(str):
            c = (zip_centroids or {}).get(z)
            d[z] = round(estimate_minutes(_haversine_miles(home, c)), 0) if c else 999
        out[name] = d
    return out

# ----------------------------- helpers -----------------------------
def apply_rec_bumps(rec, bumps):
    """bumps: {'map':{4:5,...}, 'add':{'10-12':1,'13-999':2}}; keys may be int or str (JSON-safe)."""
    if not bumps: return rec
    mp = {int(k): v for k, v in bumps.get("map", {}).items()}
    if rec in mp: return mp[rec]
    for k, inc in bumps.get("add", {}).items():
        if isinstance(k, (tuple, list)): lo, hi = k
        else: lo, hi = (int(x) for x in str(k).split("-"))
        if lo <= rec <= hi: return rec + inc
    return rec

def effective_rec(row, config):
    rec = int(row["recommended_events_per_year"])
    urb = config.get("urban")
    if urb and str(row.get("city", "")).startswith(urb["city_contains"]):
        return min(rec, urb["max_events_per_year"])
    return apply_rec_bumps(rec, config.get("rec_bumps"))

def aversion_penalty(drive, aversion):
    """Convex per-day drive penalty; higher aversion = steeper past comfort thresholds."""
    a = float(aversion)
    return drive + (0.3+0.3*a)*max(0, drive-50) + (0.5+0.9*a)*max(0, drive-(70-5*a))

# ----------------------------- assignment -----------------------------
def assign(df, techs, config, drive):
    df = df.copy(); df["zip"] = df["zip"].astype(str)
    W = config.get("value_weight", 30)
    BIG = config.get("closest_bonus", 0)
    G = nx.DiGraph()
    names = [t["name"] for t in techs]
    tcfg = {t["name"]: t for t in techs}
    cap = {t["name"]: t["days_per_week"]*52 for t in techs}
    lock = config.get("closest_lock")            # {"tech":..,"within_min":..}
    fr   = config.get("frontier")                # {"tech":..,"budget":..,"max_min":..}

    for _, row in df.iterrows():
        z = "Z_"+row["zip"]; erec = effective_rec(row, config)
        if erec <= 0: continue
        G.add_edge("SRC", z, capacity=erec, weight=0)
        drives = {n: drive[n][row["zip"]] for n in names}
        # feasibility per tech (easy within hard_cap; frontier tech may exceed via budget)
        feas_drv = []
        for n in names:
            d = drives[n]
            if d <= tcfg[n].get("hard_cap", 90): feas_drv.append(d)
            elif fr and n == fr["tech"] and d <= fr["max_min"] and erec >= fr.get("min_rec",4): feas_drv.append(d)
        minf = min(feas_drv) if feas_drv else 0
        # closest-lock: if a tech is closest & within_min, only that tech may take it
        locked_to = None
        if lock:
            ln = lock["tech"]
            if drives[ln] <= lock["within_min"] and drives[ln] == min(drives.values()):
                locked_to = ln
        for n in names:
            if locked_to and n != locked_to: continue
            d = drives[n]; cfgn = tcfg[n]
            av = cfgn.get("aversion", 1.0)
            within_hard = d <= cfgn.get("hard_cap", 90)
            is_frontier = (fr and n == fr["tech"] and not within_hard and d <= fr["max_min"] and erec >= fr.get("min_rec",4))
            if not (within_hard or is_frontier): continue
            w = -W*erec + aversion_penalty(d, av) + BIG*(d-minf)
            if is_frontier: w -= config.get("frontier_bonus", 500)
            node = f"R_{n}" if not is_frontier else f"FR_{n}"
            G.add_edge(z, node, capacity=erec, weight=int(round(w)))
    for n in names:
        G.add_edge(f"R_{n}", "SNK", capacity=cap[n], weight=0)
    if fr:
        G.add_edge(f"FR_{fr['tech']}", f"R_{fr['tech']}", capacity=fr["budget"], weight=0)

    flow = nx.max_flow_min_cost(G, "SRC", "SNK")
    rows = []
    for _, row in df.iterrows():
        z = "Z_"+row["zip"]
        if z not in flow: continue
        for n in names:
            v = flow[z].get(f"R_{n}", 0) + (flow[z].get(f"FR_{n}", 0) if fr and n==fr["tech"] else 0)
            if v > 0:
                d = drive[n][row["zip"]]
                rows.append(dict(zip=row["zip"], city=row.get("city",""), tech=n, visits_yr=v,
                                 rec=int(row["recommended_events_per_year"]), drive=int(d),
                                 far=int(d > tcfg[n].get("hard_cap",90))))
    asg = pd.DataFrame(rows).sort_values("visits_yr", ascending=False).drop_duplicates("zip")
    return asg

# ----------------------------- 52-week scheduler -----------------------------
WEEKDAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

def working_days_for(t):
    """Resolve a tech's working weekdays from working_days / day_off / days_per_week."""
    dpw = int(t.get("days_per_week", 4))
    if t.get("working_days"):
        wd = list(t["working_days"])[:dpw]            # may be weekdays OR abstract slots ("Day 1"…)
    else:
        off = t.get("day_off")
        pool = [d for d in WEEKDAYS[:6] if d != off]      # Mon..Sat minus the day off
        wd = pool[:dpw]
    while len(wd) < dpw: wd.append(WEEKDAYS[len(wd)])      # pad if needed
    return wd

def schedule_year(asg, techs, drive, df, weeks=52, coords=None, min_sep_miles=10, fixed=None, area_window_weeks=3):
    """Place a year of events. Spacing rule: any two events within `min_sep_miles` of each other
    must be at least `area_window_weeks` weeks apart (across all techs), so a local audience isn't
    re-hit too soon. Booked/pinned events are fixed and never moved."""
    names = [t["name"] for t in techs]
    wdays = {t["name"]: working_days_for(t) for t in techs}
    fixed = fixed or {}
    W = max(1, int(area_window_weeks))
    pinned_zips = {fz for lst in fixed.values() for (_w,_d,fz,_l) in lst if isinstance(fz, str)}
    avail = set().union(*[set(drive[n].keys()) for n in names]) if names else set()
    fp_zips = set(asg["zip"]) | (pinned_zips & avail)
    fingerprint = {z: np.array([drive[n].get(z, 999) for n in names], float) for z in fp_zips}
    def gd(a, b):
        if a not in fingerprint or b not in fingerprint: return 999.0
        return float(np.linalg.norm(fingerprint[a]-fingerprint[b]))
    def too_close(a, b):
        if not isinstance(a, str) or not isinstance(b, str) or a == b: return False
        if coords and a in coords and b in coords:
            return _haversine_miles(coords[a], coords[b]) < min_sep_miles
        return gd(a, b) < 8

    # adjacency: zips within the buffer of each other
    zlist = sorted(fp_zips)
    neighbors = {z: [] for z in zlist}
    for i in range(len(zlist)):
        for j in range(i+1, len(zlist)):
            if too_close(zlist[i], zlist[j]):
                neighbors[zlist[i]].append(zlist[j]); neighbors[zlist[j]].append(zlist[i])
    neighbor_busy = {z: set() for z in zlist}          # weeks in which a NEIGHBOR of z is scheduled
    def mark(z, w):                                     # record that z occupies week w
        for zn in neighbors.get(z, ()): neighbor_busy[zn].add(w)
    def area_ok(z, w):                                  # is week w clear of nearby events within W weeks?
        busy = neighbor_busy.get(z)
        return not busy or all(abs(w-wb) >= W for wb in busy)

    # ---- 1) pre-load all pinned (booked) events first, globally ----
    pinned = {n: {} for n in names}; fixed_count = {n: {} for n in names}
    capw = {}
    for n in names:
        base_days = wdays[n]; slots = len(base_days)
        for (fw, fwd, fz, _lbl) in fixed.get(n, []):
            if 0 <= fw < weeks:
                pinned[n].setdefault(int(fw), {})[fwd] = fz
                fixed_count[n][fz] = fixed_count[n].get(fz, 0) + 1
                if isinstance(fz, str): mark(fz, int(fw))
        capw[n] = [slots - sum(1 for d in pinned[n].get(w, {}) if d in base_days) for w in range(weeks)]

    # ---- 2) distribute each town's remaining visits, honoring the area-window globally ----
    wk = {n: [[] for _ in range(weeks)] for n in names}
    for n in names:
        s = asg[asg.tech == n].sort_values("visits_yr", ascending=False)
        phase = 0.0
        for _, r in s.iterrows():
            z = r["zip"]; V = int(r["visits_yr"]) - fixed_count[n].get(r["zip"], 0)
            if V <= 0: continue
            step = weeks/V
            targets = [int((i+0.5)*step+phase) % weeks for i in range(V)]
            phase = (phase+step/2) % weeks
            for t in targets:
                order = sorted(range(weeks), key=lambda w: (abs(w-t), w))
                def usable(w): return capw[n][w] > 0 and z not in wk[n][w] and z not in pinned[n].get(w, {}).values()
                pick = next((w for w in order if usable(w) and area_ok(z, w)), None)   # prefer spaced weeks
                if pick is None:
                    pick = next((w for w in order if usable(w)), None)                 # fall back (residual)
                if pick is not None:
                    capw[n][pick] -= 1; wk[n][pick].append(z); mark(z, pick)

    # ---- 3) assemble weeks: route towns onto open days, overlay pins ----
    grids = {}
    for n in names:
        base_days = wdays[n]; grid = []
        for w in range(weeks):
            pins = pinned[n].get(w, {})
            open_days = [d for d in base_days if d not in pins]
            towns = sorted(wk[n][w], key=lambda z: drive[n][z])
            route = [towns.pop(0)] if towns else []
            while towns:
                nx_ = min(towns, key=lambda z: gd(route[-1], z)); towns.remove(nx_); route.append(nx_)
            day_map = {d: None for d in base_days}
            for i, d in enumerate(open_days):
                day_map[d] = route[i] if i < len(route) else None
            for fwd, fz in pins.items():
                day_map[fwd] = fz
            grid.append(day_map)
        grids[n] = grid

    # ---- residual: how many events still have a nearby event within W weeks (lower is better) ----
    by_zip = {}
    for n in names:
        for w in range(weeks):
            for z in grids[n][w].values():
                if isinstance(z, str): by_zip.setdefault(z, []).append(w)
    conflicts = 0
    for z, ws in by_zip.items():
        nb = neighbors.get(z, ())
        for w in ws:
            if any(any(abs(w-w2) < W for w2 in by_zip.get(zn, ())) for zn in nb):
                conflicts += 1
    return grids, wdays, conflicts

# ----------------------------- metrics -----------------------------
def metrics(asg, techs):
    out = {}
    for t in techs:
        n = t["name"]; s = asg[asg.tech == n]
        if len(s) == 0:
            out[n] = dict(events=0, mean=0, median=0, pct_long=0, far_towns=0); continue
        w = np.repeat(s["drive"].values, s["visits_yr"].astype(int).values)
        out[n] = dict(events=int(s["visits_yr"].sum()), mean=round(float(w.mean()),1),
                      median=int(np.median(w)), pct_long=round(float((w>75).mean()*100),0),
                      far_towns=int((s["far"]==1).sum()))
    return out
