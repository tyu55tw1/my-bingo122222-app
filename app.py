# -*- coding: utf-8 -*-
"""🚉 智慧交通規劃 — Streamlit 版。  執行:streamlit run app.py"""
import html as _html
from datetime import datetime, time as dtime

import pandas as pd
import streamlit as st

import transit as T

try:
    from streamlit_geolocation import streamlit_geolocation
except Exception:  # noqa: BLE001  未安裝時改用 IP 概略定位
    streamlit_geolocation = None

st.set_page_config(page_title="智慧交通規劃", page_icon="🚉", layout="centered")
esc = lambda x: _html.escape(str(x), quote=True)
st.markdown("""<style>
:root{--soft:rgba(128,128,128,.11);--line:rgba(128,128,128,.28);--ac:#d9441a}
.block-container{padding:1.2rem 1rem 3rem;max-width:880px}header[data-testid=stHeader]{background:transparent}footer{visibility:hidden}
.hero{display:flex;align-items:center;gap:14px;background:linear-gradient(120deg,#1f6feb,#35c2d6 60%,#7ee0a6);padding:18px 22px;border-radius:22px;margin:4px 0 16px;color:#fff;box-shadow:0 10px 28px rgba(31,111,235,.28)}
.hero h1{margin:0;font-size:1.6rem;color:#fff;padding:0}.hero p{margin:2px 0 0;opacity:.95;color:#fff}.hero .ico{font-size:2.5rem}
.rt{background:var(--soft);border:1px solid var(--line);border-radius:18px;padding:14px 16px;margin:0 0 12px}.rt.best{border:2px solid var(--ac)}
.tm{font-size:1.7rem;font-weight:800;line-height:1.1}.sub{opacity:.72;font-size:.88rem}.sum{font-weight:700;margin:6px 0 2px}
.tag{display:inline-block;padding:1px 9px;border-radius:99px;background:var(--ac);color:#fff;font-size:.74rem;font-weight:700;margin-right:6px}
.leg{display:flex;gap:10px;padding:7px 0;border-top:1px dashed var(--line)}.leg .lt{min-width:92px;font-weight:700;font-size:.9rem}.leg .lb{flex:1;font-size:.93rem}
.nx{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:6px 0 14px}
.nx>div{background:var(--soft);border:1px solid var(--line);border-radius:16px;padding:12px}.nx .tm{font-size:1.3rem}
@media(max-width:640px){.hero{padding:14px 16px}.hero h1{font-size:1.25rem}.tm{font-size:1.45rem}.leg .lt{min-width:78px;font-size:.82rem}}
</style>""", unsafe_allow_html=True)
st.markdown("<div class='hero'><span class='ico'>🚉</span><div><h1>智慧交通規劃</h1><p>輸入起點與終點,自動規劃公車、台鐵、高鐵、捷運轉乘,並附上後面 3 趟</p></div></div>", unsafe_allow_html=True)


@st.cache_data(ttl=3000, show_spinner=False)
def token_for(cid, secret):
    return T.get_token(cid, secret)


@st.cache_data(ttl=3600, show_spinner=False)
def geo(q):
    return T.geocode(q)


@st.cache_data(ttl=90, show_spinner=False)
def planned(cid, secret, o, d, depart_iso, modes, gc, transfer, first, last):
    kw = dict(modes=modes, gc=gc, transfer=transfer, first=first, last=last)
    return T.plan_with_next(token_for(cid, secret), o, d, datetime.fromisoformat(depart_iso), **kw)


def leg_html(l):
    t = f"{l['start']:%H:%M}–{l['end']:%H:%M}" if l["start"] and l["end"] else (f"約 {l['minutes']} 分" if l["minutes"] else "")
    place = f"<div class='sub'>{esc(l['from'])} ➜ {esc(l['to'])}</div>" if (l["from"] or l["to"]) else ""
    dur = f"　<span class='sub'>{T.fmt_min(l['minutes'])}</span>" if l["minutes"] and l["start"] else ""
    return f"<div class='leg'><div class='lt'>{t}</div><div class='lb'><b>{l['emoji']} {esc(l['line'] or l['label'])}</b>{dur}{place}</div></div>"


def route_html(r, label="", best=False):
    fare = f"　💰 約 {esc(r['fare'])} 元" if r.get("fare") else ""
    legs = "".join(leg_html(l) for l in r["legs"]) if r["legs"] else "<div class='sub'>(此路線沒有提供分段資料,請展開下方「原始資料」查看)</div>"
    return (f"<div class='rt{' best' if best else ''}'>{f'<span class=tag>{label}</span>' if label else ''}"
            f"<div class='tm'>{r['start']:%H:%M} ➜ {r['end']:%H:%M}</div>"
            f"<div class='sub'>{T.fmt_min(r['minutes'])}・轉乘 {r['transfers']} 次{fare}</div><div class='sum'>{esc(r['summary'])}</div>{legs}</div>")


# ── 金鑰 ──
def _secret(k):
    try:  # 沒有 secrets 檔時,部分 Streamlit 版本會丟例外
        return str(st.secrets.get(k, "") or "")
    except Exception:  # noqa: BLE001
        return ""


cid, secret = _secret("TDX_CLIENT_ID"), _secret("TDX_CLIENT_SECRET")
if cid and secret:
    st.caption("🔑 已載入 TDX 金鑰")
else:
    with st.expander("🔑 設定 TDX 金鑰(免費,只需一次)", expanded=True):
        st.caption("路線規劃使用交通部 TDX 平台。到 tdx.transportdata.tw 免費註冊 → 會員中心 → 資料服務 → API金鑰,貼上 Client Id / Secret。"
                   "部署時建議放在 Streamlit 的 Secrets(TDX_CLIENT_ID、TDX_CLIENT_SECRET),就不用每次輸入。")
        cid = st.text_input("Client Id", value=st.session_state.get("cid", ""))
        secret = st.text_input("Client Secret", value=st.session_state.get("secret", ""), type="password")
        st.session_state["cid"], st.session_state["secret"] = cid, secret

# ── 起終點 ──
use_gps = st.toggle("📍 使用目前位置當起點(GPS)")
origin = None
if use_gps:
    if streamlit_geolocation:
        loc = streamlit_geolocation() or {}
        if loc.get("latitude") is not None:
            origin = {"name": "我的位置(GPS)", "lat": float(loc["latitude"]), "lon": float(loc["longitude"])}
        else:
            st.info("請按上方定位按鈕並允許瀏覽器取得位置。")
    else:
        ip = ((st.context.headers.get("X-Forwarded-For", "") if hasattr(st, "context") else "") or "").split(",")[0].strip()
        origin = T.ip_location(ip) if ip else None
        st.caption("未安裝 streamlit-geolocation,改用 IP 概略位置(誤差可能達數公里)。" if origin else "無法定位,請改為手動輸入起點。")
else:
    q = st.text_input("起點", placeholder="例如:中華醫事科技大學、台南火車站、23.0,120.2")
    cands = geo(q) if q.strip() else []
    if cands:
        origin = cands[st.selectbox("起點候選", range(len(cands)), format_func=lambda i: cands[i]["name"]) if len(cands) > 1 else 0]
    elif q.strip():
        st.warning("找不到這個起點,請換個說法(例如加上縣市)或直接輸入「緯度,經度」。")
if origin and use_gps:
    st.success(f"起點:{origin['name']}({origin['lat']:.5f}, {origin['lon']:.5f})")

q2 = st.text_input("終點", placeholder="例如:台北車站、高雄左營高鐵站、台南市安定區")
dc = geo(q2) if q2.strip() else []
dest = None
if dc:
    dest = dc[st.selectbox("終點候選", range(len(dc)), format_func=lambda i: dc[i]["name"]) if len(dc) > 1 else 0]
elif q2.strip():
    st.warning("找不到這個終點,請換個說法或直接輸入「緯度,經度」。")

# ── 選項 ──
with st.expander("⚙️ 運具與偏好", expanded=True):
    modes = st.multiselect("要搭乘的運具", list(T.MODES), default=[3, 4, 5, 6], format_func=lambda k: f"{T.MODES[k][0]} {T.MODES[k][1]}")
    c1, c2 = st.columns(2)
    pref = c1.radio("偏好", list(T.PREFS), index=1, horizontal=True)
    fm = c2.selectbox("第一哩路(到車站)", list(T.FIRST_MILE))
    c1, c2 = st.columns(2)
    walk = c1.slider("最長步行/接駁(分鐘)", 5, 30, 15)
    xfer = c2.slider("轉乘等待(分鐘)", 0, 60, (5, 40))
    d1, d2 = st.columns(2)
    day = d1.date_input("出發日期", T.tw_now().date())
    now = T.tw_now()
    tt = d2.time_input("出發時間", dtime(now.hour, now.minute))

go = st.button("🚀 規劃路線", type="primary")
if go:
    if not (cid and secret):
        st.error("請先設定 TDX 金鑰。")
    elif not origin or not dest:
        st.error("請先輸入起點與終點。")
    elif not modes:
        st.error("請至少選擇一種運具。")
    else:
        dep = datetime.combine(day, tt).replace(tzinfo=T.TW)
        dep = max(dep, T.tw_now() + pd.Timedelta(minutes=1))  # TDX 不接受過去時間
        o, d = (origin["lat"], origin["lon"]), (dest["lat"], dest["lon"])
        try:
            with st.spinner("規劃路線中(含後面 3 趟,約需 5~15 秒)…"):
                best, others, nxt = planned(cid, secret, o, d, dep.replace(tzinfo=None).isoformat(), tuple(modes), T.PREFS[pref], xfer,
                                            (T.FIRST_MILE[fm], walk), (0, walk))
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            best = None
            st.link_button("改用 Google 地圖查看 ↗", T.maps_link(origin["name"], dest["name"], o, d))
        else:
            if best is None:
                st.warning("這個時段找不到符合條件的大眾運輸。可試著放寬運具、步行上限或換個出發時間。")
                st.link_button("用 Google 地圖查看 ↗", T.maps_link(origin["name"], dest["name"], o, d))
            else:
                st.markdown(route_html(best, "最佳路線", True), unsafe_allow_html=True)
                if nxt:
                    st.markdown("##### ⏭️ 後面 3 趟(以防錯過)")
                    st.markdown("<div class='nx'>" + "".join(
                        f"<div><span class='tag'>下一班 {i}</span><div class='tm'>{r['start']:%H:%M} ➜ {r['end']:%H:%M}</div>"
                        f"<div class='sub'>{T.fmt_min(r['minutes'])}・轉乘 {r['transfers']} 次</div><div class='sum'>{esc(r['summary'])}</div></div>"
                        for i, r in enumerate(nxt, 1)) + "</div>", unsafe_allow_html=True)
                    with st.expander("查看後面班次的詳細轉乘"):
                        for i, r in enumerate(nxt, 1):
                            st.markdown(route_html(r, f"下一班 {i}"), unsafe_allow_html=True)
                if others:
                    with st.expander(f"🔀 其他方案({len(others)})"):
                        for r in others:
                            st.markdown(route_html(r), unsafe_allow_html=True)
                st.map(pd.DataFrame({"lat": [o[0], d[0]], "lon": [o[1], d[1]]}), size=60)
                with st.expander("🧾 原始資料(排查用)"):
                    st.json(best["raw"])
                st.link_button("在 Google 地圖開啟 ↗", T.maps_link(origin["name"], dest["name"], o, d))
else:
    st.info("輸入起點與終點(或開啟 GPS),選好運具後按「規劃路線」。")
st.caption("路線資料來源:交通部 TDX 運輸資料流通服務平台。班次為規劃參考,實際以各運輸業者公告與現場為準。")
