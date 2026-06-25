"""
Territory Rotation Planner — Streamlit app.
Describe your techs in plain English (or edit a table), upload a territory ZIP plan,
and generate a year-long event rotation + Excel workbook. Chat to refine.
Run:  streamlit run app.py
"""
import os, copy
import pandas as pd, streamlit as st
import engine, excel_export, chat

st.set_page_config(page_title="Territory Rotation Planner", layout="wide")

def _secret(key, default=None):
    """Read from Streamlit secrets, falling back to an env var (UPPER_CASE), then default."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key.upper(), default)

def require_login():
    """Gate the app behind a password from secrets. If no password is set, the app is open (local dev)."""
    pw = _secret("app_password")
    if not pw or st.session_state.get("authed"):
        return True
    st.title("🔒 Territory Rotation Planner")
    entered = st.text_input("Enter password to continue", type="password")
    if entered and entered == str(pw):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Incorrect password.")
    return False

if not require_login():
    st.stop()

# Anthropic key comes from secrets/env on the server — never entered by the user.
API_KEY = _secret("anthropic_api_key", "")

DEFAULT_TECH_COLS = ["name","home_zip","days_per_week","working_days","hard_cap","aversion","overnight_ok","frontier_ok","notes"]

@st.cache_data(show_spinner=False)
def geocode_zips(zips):
    import pgeocode
    nomi = pgeocode.Nominatim("us")
    res = nomi.query_postal_code([str(z) for z in zips])
    return {str(z): (float(la), float(lo)) for z, la, lo in zip(res["postal_code"], res["latitude"], res["longitude"])
            if pd.notna(la) and pd.notna(lo)}

def need_geocoding(df, techs):
    return not all(f"{t['name'].lower()}_drive_minutes" in df.columns for t in techs)

def techs_from_df(tdf):
    techs = []
    for _, r in tdf.iterrows():
        if not str(r.get("name","")).strip(): continue
        wd = [d.strip() for d in str(r.get("working_days","") or "").split(",") if d.strip()]
        t = dict(name=str(r["name"]).strip(),
                 home_zip=str(r.get("home_zip","") or "").strip(),
                 days_per_week=int(r.get("days_per_week",4) or 4),
                 hard_cap=int(r.get("hard_cap",90) or 90),
                 aversion=float(r.get("aversion",1.0) or 1.0),
                 overnight_ok=bool(r.get("overnight_ok",False)))
        if wd: t["working_days"] = wd
        if bool(r.get("frontier_ok", False)): t["frontier_ok"] = True
        techs.append(t)
    return techs

ss = st.session_state
ss.setdefault("tech_df", pd.DataFrame(columns=DEFAULT_TECH_COLS))
ss.setdefault("config", None); ss.setdefault("results", None)
ss.setdefault("chatlog", []); ss.setdefault("parse_note", "")

st.title("🗺️ Territory Rotation Planner")
st.caption("Describe your techs in plain English, upload a ZIP plan, and get a year-long rotation. Refine by chatting.")

with st.sidebar:
    st.header("Territory file")
    up = st.file_uploader("CSV with `zip` and `recommended_events_per_year`", type=["csv"])
    st.caption("Optional: `city`, `Event Naming`, `scheduling_status`, or precomputed `<tech>_drive_minutes`.")
    if _secret("app_password"):
        if st.button("Log out"):
            st.session_state["authed"] = False; st.rerun()

if up is None:
    st.info("⬆️ Upload a territory CSV in the sidebar to begin."); st.stop()
df = pd.read_csv(up); df["zip"] = df["zip"].astype(str)
if "recommended_events_per_year" not in df.columns:
    st.error("CSV needs a `recommended_events_per_year` column."); st.stop()
st.success(f"Loaded {len(df)} ZIPs.")

# ----------------- techs: describe in plain English, then edit -----------------
st.header("3 · Techs")
st.markdown("Describe each rep however you'd phrase it to a scheduler — days, day off, travel, distance limits. "
            "Works for **one tech**, a **main + part-timer**, or a full team.")
instr = st.text_area("Plain-English instructions (optional)", height=150, placeholder=(
    "Kim: lives in 60441, 4 days/week, off every Friday, strict no travel, no overnights.\n"
    "April: 60407, 4 days/week off Fridays, no overnight stays but long day-trips OK.\n"
    "Rose: 60468, works 4 days a week, no set day off, will travel with advance notice including overnights."))
c1, c2 = st.columns([1,3])
with c1:
    if st.button("🪄 Parse instructions"):
        if not API_KEY: st.warning("The app's Anthropic key isn't configured. Add `anthropic_api_key` to Streamlit secrets.")
        elif not instr.strip(): st.warning("Type some instructions first.")
        else:
            with st.spinner("Reading instructions…"):
                try:
                    parsed, note = chat.parse_techs(API_KEY, instr)
                    rows = []
                    for t in parsed:
                        rows.append({"name":t.get("name",""),"home_zip":t.get("home_zip",""),
                                     "days_per_week":t.get("days_per_week",4),
                                     "working_days":", ".join(t.get("working_days",[])),
                                     "hard_cap":t.get("hard_cap",90),"aversion":t.get("aversion",1.0),
                                     "overnight_ok":t.get("overnight_ok",False),
                                     "frontier_ok":t.get("frontier_ok",False),"notes":t.get("notes","")})
                    ss.tech_df = pd.DataFrame(rows, columns=DEFAULT_TECH_COLS); ss.parse_note = note
                except Exception as e:
                    st.error(f"Parse failed: {e}")
if ss.parse_note: st.info("🪄 " + ss.parse_note)

st.caption("Review / edit the parsed reps below (or fill this in manually). `working_days` = comma list like `Mon, Tue, Wed, Thu`; "
           "leave blank to auto-derive from days/week. `aversion` 0–3 (higher = dislikes driving). `frontier_ok` = willing to do far overnight seeds.")
ss.tech_df = st.data_editor(ss.tech_df, num_rows="dynamic", use_container_width=True, key="tech_editor",
    column_config={
        "days_per_week": st.column_config.NumberColumn(min_value=1, max_value=6),
        "hard_cap": st.column_config.NumberColumn("max drive (min)", min_value=20, max_value=200),
        "aversion": st.column_config.NumberColumn(min_value=0.0, max_value=3.0, step=0.1),
        "overnight_ok": st.column_config.CheckboxColumn(),
        "frontier_ok": st.column_config.CheckboxColumn(),
    })

# ----------------- plan settings -----------------
st.header("4 · Plan settings")
g1, g2, g3 = st.columns(3)
with g1:
    rot = st.selectbox("Rotation length (weeks)", [6,8,10], 1)
    start = st.date_input("Start week (Monday)", pd.to_datetime("2026-07-06")).strftime("%Y-%m-%d")
with g2:
    W = st.slider("Prioritize high-recommendation towns", 5, 100, 30, 5)
    title = st.text_input("Plan title", "Territory Annual Plan")
with g3:
    use_urban = st.checkbox("Cap a dense-urban area")
    urban = None
    if use_urban:
        urban = {"city_contains": st.text_input("City starts with", "Chicago city"),
                 "max_events_per_year": int(st.number_input("Max events/yr there", 0, 12, 2))}
    min_sep = st.number_input("Keep same-day events ≥ N miles apart", 0, 50, 10,
                              help="Two techs won't be sent to towns within this many miles on the same weekday.")
with st.expander("Advanced (lock-closest, far seeds, recommendation bumps)"):
    a1, a2 = st.columns(2)
    techs_now = techs_from_df(ss.tech_df); names_now = [t["name"] for t in techs_now]
    with a1:
        lock_on = st.checkbox("Lock closest towns to a drive-averse tech")
        closest_lock = None
        if lock_on and names_now:
            closest_lock = {"tech": st.selectbox("Tech", names_now, key="lk"),
                            "within_min": int(st.slider("Within minutes", 10, 45, 25, 5))}
        rec_bumps = {"map":{"4":5,"5":6,"6":7},"add":{"10-12":1,"13-999":2}} if st.checkbox(
            "Bump suburban recs (4→5, 5→6, 6→7, 10+ +1–2)") else None
    with a2:
        fr_on = st.checkbox("Allow a tech far new-region seeds (overnights)")
        frontier = None
        if fr_on and names_now:
            ftd = [t["name"] for t in techs_now if t.get("frontier_ok")] or names_now
            frontier = {"tech": st.selectbox("Tech", ftd, key="fr"),
                        "budget": int(st.number_input("Far visits/yr", 0, 60, 12)),
                        "max_min": int(st.slider("Max far drive", 90, 200, 135, 5)), "min_rec": 4}

def assemble_config():
    return dict(title=title, rotation_weeks=int(rot), value_weight=int(W), start_date=start,
                urban=urban, rec_bumps=rec_bumps, closest_lock=closest_lock,
                frontier=frontier, frontier_bonus=500, min_sep_miles=int(min_sep), techs=techs_from_df(ss.tech_df))

def run_plan(config):
    if not config["techs"]: raise ValueError("Add at least one tech.")
    centroids = None
    if need_geocoding(df, config["techs"]):
        miss = [t["name"] for t in config["techs"] if not t.get("home_zip")]
        if miss: raise ValueError(f"Need a home ZIP for: {', '.join(miss)} (or precomputed drive columns).")
        with st.spinner("Geocoding ZIPs…"):
            centroids = geocode_zips(list(df["zip"]) + [t["home_zip"] for t in config["techs"]])
    drive = engine.build_drive_matrix(df, config["techs"], centroids)
    asg = engine.assign(df, config["techs"], config, drive)
    grids, wdays, conflicts = engine.schedule_year(asg, config["techs"], drive, df, weeks=52,
                                                   coords=centroids, min_sep_miles=config.get("min_sep_miles", 10))
    mets = engine.metrics(asg, config["techs"])
    # build the workbook once here so clicking Download (which reruns Streamlit) doesn't rebuild the plan
    NAME = dict(zip(df["zip"].astype(str), df.get("Event Naming", df["zip"].astype(str))))
    buf = "/tmp/territory_plan.xlsx"
    excel_export.build_workbook(buf, asg, config["techs"], grids, wdays, config, mets, df, NAME)
    with open(buf, "rb") as f: xbytes = f.read()
    return dict(asg=asg, grids=grids, wdays=wdays, mets=mets, drive=drive, conflicts=conflicts, xlsx=xbytes)

if st.button("🚀 Build plan", type="primary"):
    try:
        ss.config = assemble_config(); ss.results = run_plan(ss.config); ss.chatlog = []
    except Exception as e:
        st.error(f"Could not build plan: {e}")

# ----------------- results -----------------
if ss.results:
    cfg, res = ss.config, ss.results
    st.header("Results")
    mdf = pd.DataFrame([{"Tech":n,"Events/yr":m["events"],"Median drive":f"{m['median']} min",
                         "Mean drive":f"{m['mean']} min","% long days":f"{m['pct_long']:.0f}%","Far towns":m["far_towns"]}
                        for n,m in res["mets"].items()])
    st.dataframe(mdf, hide_index=True, use_container_width=True)
    mc1, mc2 = st.columns(2)
    total = int(res["asg"]["visits_yr"].sum())
    mc1.metric("Events / year", f"{total} (~{total/52:.1f}/week)")
    mc2.metric("Same-day events too close", res.get("conflicts", 0),
               help=f"Pairs of techs within {cfg.get('min_sep_miles',10)} mi on the same weekday (lower is better).")

    NAME = dict(zip(df["zip"], df.get("Event Naming", df["zip"])))
    tabs = st.tabs([t["name"] for t in cfg["techs"]] + ["Territory","Deferred"])
    for ti, t in enumerate(cfg["techs"]):
        with tabs[ti]:
            n = t["name"]; days = res["wdays"][n]; rows = []
            for w in range(52):
                row = {"Week": w+1}
                for day in days:
                    z = res["grids"][n][w].get(day)
                    row[day] = f"{NAME.get(z,z)} ({int(res['drive'][n][z])}m)" if z else "—"
                rows.append(row)
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, height=420)
    with tabs[-2]:
        st.dataframe(res["asg"][["zip","city","tech","visits_yr","drive","rec"]], hide_index=True, use_container_width=True)
    with tabs[-1]:
        dfd = df[~df["zip"].isin(set(res["asg"]["zip"]))]
        st.dataframe(dfd[["zip"]+[c for c in ["Event Naming","recommended_events_per_year","scheduling_status"] if c in df.columns]],
                     hide_index=True, use_container_width=True)

    st.download_button("⬇️ Download Excel workbook", res["xlsx"], file_name="territory_annual_plan.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    st.header("💬 Refine")
    st.caption("e.g. “make Kim more comfortable, she hates driving” · “cut Chicago to 1/yr and bump suburbs” · “share load between April and Rose”.")
    for role, text in ss.chatlog:
        with st.chat_message(role): st.write(text)
    prompt = st.chat_input("Ask for a change…")
    if prompt:
        if not API_KEY: st.warning("The app's Anthropic key isn't configured. Add `anthropic_api_key` to Streamlit secrets.")
        else:
            ss.chatlog.append(("user", prompt))
            with st.spinner("Thinking…"):
                try:
                    newcfg, expl = chat.refine(API_KEY, prompt, cfg, res["mets"])
                    ss.config = newcfg; ss.results = run_plan(newcfg); ss.chatlog.append(("assistant", expl))
                except Exception as e:
                    ss.chatlog.append(("assistant", f"Error: {e}"))
            st.rerun()
