# -*- coding: utf-8 -*-
"""🚉 智慧交通規劃 — Streamlit 版。  執行:streamlit run app.py"""
import html as _html
from datetime import datetime, time as dtime

import pandas as pd
import streamlit as st

import types as _types

# ═══ 內建的路線規劃模組(單檔部署:不再需要另外的 transit.py,避免兩個檔案版本不一致) ═══
_TRANSIT_SRC = r'''# -*- coding: utf-8 -*-
"""🚉 大眾運輸規劃:地點查詢 + TDX 旅運規劃(公車/台鐵/高鐵/捷運轉乘) + 往後 3 趟。"""
import re
from datetime import datetime, timedelta, timezone

import requests

VERSION = "v3-中文路段"
TW = timezone(timedelta(hours=8))
UA = "streamlit-transit-planner/1.0 (personal project)"
TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
ROUTING_URL = "https://tdx.transportdata.tw/api/maas/routing"
MODES = {3: ("🚄", "高鐵"), 4: ("🚆", "台鐵"), 5: ("🚌", "公車"), 6: ("🚇", "捷運"), 7: ("🚊", "輕軌"),
         8: ("⛴️", "渡輪"), 9: ("🚡", "纜車"), 20: ("✈️", "航空")}
PREFS = {"最快": 1.0, "平衡": 0.5, "最省錢": 0.0}
FIRST_MILE = {"走路": 0, "腳踏車": 1, "開車": 2, "共享單車": 3}


def tw_now():
    return datetime.now(TW)


def _http(method, url, **kw):
    kw.setdefault("timeout", (6, 25))
    h = kw.pop("headers", {})
    h.setdefault("User-Agent", UA)
    return requests.request(method, url, headers=h, **kw)


def get_token(cid, secret):
    r = _http("POST", TOKEN_URL, data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret})
    if r.status_code != 200:
        raise RuntimeError("TDX 金鑰驗證失敗,請確認 Client Id / Client Secret 是否正確")
    return r.json()["access_token"]


# ── 地點 ────────────────────────────────────────────
_LL = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,，]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _short(name):
    parts = [p.strip() for p in str(name).split(",")]
    return ",".join(parts[:3]) if len(parts) > 3 else str(name)


def geocode(q, limit=5):
    """地名 → [{name, lat, lon}]。可直接輸入「緯度,經度」。Nominatim 失敗時改用 Photon。"""
    q = (q or "").strip()
    m = _LL.match(q)
    if m:
        lat, lon = float(m[1]), float(m[2])
        return [{"name": f"座標 {lat:.5f}, {lon:.5f}", "lat": lat, "lon": lon}]
    if not q:
        return []
    out = []
    try:
        r = _http("GET", "https://nominatim.openstreetmap.org/search", params={
            "q": q, "format": "jsonv2", "limit": limit, "countrycodes": "tw", "accept-language": "zh-TW"})
        if r.status_code == 200:
            out = [{"name": _short(x["display_name"]), "lat": float(x["lat"]), "lon": float(x["lon"])} for x in r.json()]
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        try:
            r = _http("GET", "https://photon.komoot.io/api/", params={"q": q, "limit": limit * 2, "lat": 23.7, "lon": 121.0})
            for f in (r.json().get("features") or []) if r.status_code == 200 else []:
                p, (lon, lat) = f.get("properties", {}), f["geometry"]["coordinates"]
                if p.get("countrycode", "TW") == "TW":
                    out.append({"name": ",".join(x for x in (p.get("name"), p.get("district"), p.get("city"), p.get("state")) if x),
                                "lat": lat, "lon": lon})
        except Exception:  # noqa: BLE001
            out = []
    return out[:limit]


def ip_location(ip):
    """依 IP 概略定位(僅城市等級,誤差大)。"""
    try:
        r = _http("GET", f"http://ip-api.com/json/{ip}", params={"lang": "zh-CN", "fields": "status,lat,lon,city,regionName"}, timeout=(4, 6))
        j = r.json()
        if j.get("status") == "success":
            return {"name": f"{j.get('regionName', '')}{j.get('city', '')}(IP 概略位置)", "lat": j["lat"], "lon": j["lon"]}
    except Exception:  # noqa: BLE001
        pass
    return None


# ── 規劃 ────────────────────────────────────────────
def parse_time(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", str(s)))
        return d.astimezone(TW) if d.tzinfo else d.replace(tzinfo=TW)
    except ValueError:
        return None


_FMT = "%Y-%m-%dT%H:%M:%S"
_STATE = {"i": 0, "name": "完整參數"}  # 記住上次成功的參數寫法


def _variants(o, d, depart, arrival, modes, gc, top, transfer, first, last):
    """規格書對部分參數的寫法不夠明確,準備多種寫法,被回報『參數有誤』時依序嘗試。"""
    transit = ",".join(map(str, modes))
    # 與官方範例同格式:座標固定 6 位小數(GPS/地名查詢常回傳 7~15 位,伺服器可能因此判定格式錯誤)、gc 一位小數
    base = {"origin": f"{float(o[0]):.6f},{float(o[1]):.6f}", "destination": f"{float(d[0]):.6f},{float(d[1]):.6f}",
            "gc": f"{float(gc):.1f}", "top": int(top), "transit": transit}
    when = {"arrival": arrival.strftime(_FMT)} if arrival else {"depart": (depart or tw_now()).strftime(_FMT)}
    full = {**base, "transfer_time": f"{transfer[0]},{transfer[1]}", "first_mile_mode": first[0], "first_mile_time": first[1],
            "last_mile_mode": last[0], "last_mile_time": last[1]}
    return [("完整參數", {**full, **when}, False), ("完整參數(原樣符號)", {**full, **when}, True),
            ("精簡參數", {**base, **when}, False), ("精簡參數(原樣符號)", {**base, **when}, True),
            ("運具加中括號", {**base, **when, "transit": f"[{transit}]"}, True), ("不指定時間", base, True)]


def _call(token, p, raw):
    h = {"authorization": f"Bearer {token}"}
    if raw:  # 逗號、冒號不做百分比編碼,與官方文件範例寫法一致
        return _http("GET", ROUTING_URL + "?" + "&".join(f"{k}={v}" for k, v in p.items()), headers=h)
    return _http("GET", ROUTING_URL, params=p, headers=h)


def _is_spec_error(r, j):
    return r.status_code == 404 or (isinstance(j, dict) and (j.get("error") or {}).get("code") == 40001)


def plan(token, o, d, depart=None, arrival=None, modes=(3, 4, 5, 6), gc=0.5, top=5,
         transfer=(5, 60), first=(0, 15), last=(0, 15)):
    """呼叫 TDX 旅運規劃。o/d 為 (lat, lon)。回傳原始 routes 列表。"""
    variants = _variants(o, d, depart, arrival, modes, gc, top, transfer, first, last)
    order = [_STATE["i"]] + [i for i in range(len(variants)) if i != _STATE["i"]]
    last_err = None
    for i in order:
        name, p, raw = variants[i]
        r = _call(token, p, raw)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"規劃服務回應異常(HTTP {r.status_code})")
        if r.status_code == 401:
            raise RuntimeError("TDX 授權失效,請重新確認金鑰")
        if _is_spec_error(r, j):
            last_err = j
            continue
        if j.get("result") != "success":
            raise RuntimeError(f"規劃失敗:{(j.get('error') or {}).get('msg', '未知原因')}")
        _STATE.update(i=i, name=name)
        return (j.get("data") or {}).get("routes") or []
    msg = ((last_err or {}).get("error") or {}).get("msg", "")
    raise RuntimeError(f"TDX 回報參數格式不符(已嘗試 {len(variants)} 種寫法):{msg}。請使用頁面下方「TDX 連線診斷」並把結果回報。")


def diagnose(token, o, d, depart=None):
    """逐一測試每種參數寫法,回傳 [(寫法, HTTP 狀態, 回應摘要)],供排查。"""
    out = []
    for name, p, raw in _variants(o, d, depart, None, (3, 4, 5, 6), 0.5, 2, (5, 60), (0, 15), (0, 15)):
        try:
            r = _call(token, p, raw)
            out.append((name, r.status_code, (r.text or "")[:220].replace("\n", " ")))
        except Exception as e:  # noqa: BLE001
            out.append((name, "—", f"{type(e).__name__}: {e}"))
    return out


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _name(x):
    if isinstance(x, dict):
        return _pick(x, "name", "Zh_tw", "zh_tw", "stop_name", "station_name", "title") or ""
    return str(x) if x is not None else ""


def norm_mode(v):
    """各種模式表示法 → (kind, emoji, 文字)。kind: 'walk' | 'wait' | 'bike' | 運具代碼 | 'other'"""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        k = int(v)
        if k in MODES:
            return (k,) + MODES[k]
        if k == 0:
            return ("walk", "🚶", "步行")
    s = str(v or "").strip().lower()
    if s in ("pedestrian", "walk", "walking", "foot", "步行") or any(k in s for k in ("步行", "pedestrian")):
        return ("walk", "🚶", "步行")
    if s in ("waiting", "wait", "等車"):
        return ("wait", "⏳", "等車")
    if s == "tra":
        return (4,) + MODES[4]
    # 順序很重要:輕軌/捷運含有 "rail" 字樣,必須先於台鐵判斷;高鐵先於一般火車
    for keys, kind in ((("highspeed", "thsr", "hsr", "高鐵"), 3), (("lightrail", "lrt", "tram", "輕軌"), 7),
                       (("subway", "metro", "mrt", "monorail", "捷運"), 6),
                       (("train", "rail", "台鐵", "臺鐵", "火車"), 4), (("bus", "公車", "客運"), 5),
                       (("ferry", "渡輪"), 8), (("cable", "aerial", "gondola", "inclined", "纜車"), 9), (("flight", "plane", "航空"), 20)):
        if any(k in s for k in keys):
            return (kind,) + MODES[kind]
    if any(k in s for k in ("bike", "bicycle", "youbike", "腳踏車", "單車")):
        return ("bike", "🚲", "單車")
    if s == "transit":
        return ("other", "🚏", "搭乘")
    return ("other", "🚏", str(v) if (v and re.search(r"[^\x00-\x7f]", str(v))) else "其他運具")  # 純英文代碼不顯示


def _flat(d, prefix="", depth=0, out=None):
    """把巢狀 dict 攤平成 {'departure.place.name': ...}(不展開清單,避免把沿途站點混進來)。"""
    out = {} if out is None else out
    if depth > 4:
        return out
    for k, v in d.items():
        if isinstance(v, dict):
            _flat(v, f"{prefix}{k}.", depth + 1, out)
        elif not isinstance(v, list):
            out[f"{prefix}{k}"] = v
    return out


def _fp(flat, *paths):
    """依優先順序取第一個有值的欄位(路徑不分大小寫)。"""
    low = {k.lower(): v for k, v in flat.items()}
    for p in paths:
        v = low.get(p.lower())
        if v not in (None, ""):
            return v
    return None


_SIDE = {"dep": ("departure", "from", "origin", "start", "startstop", "boarding", "board"),
         "arr": ("arrival", "to", "destination", "end", "endstop", "alighting", "alight")}
_NOT_LINE = {"place", "station", "stop", "agency", "operator", "location", "departure", "arrival", "from", "to", "origin", "destination", "start", "end"}
_LINE_LEAVES = ("shortname", "short_name", "routename", "route_name", "linename", "line_name", "name", "longname", "number", "routeno", "route_no", "trainno", "train_no", "label")
_NUMERIC_OK = {"number", "routeno", "route_no", "trainno", "train_no"}


def _split(k):
    """'a.b_c.name' → (['a','b','c'], 'name');路徑 token 以 . 與 _ 切開,末段保留原樣。"""
    parts = k.lower().split(".")
    return [t for p in parts[:-1] for t in p.split("_") if t], parts[-1]


def _scan_mode(flat):
    for k, v in flat.items():
        path, leaf = _split(k)
        if k.lower() != "type" and "place" not in path and isinstance(v, (str, int)) and \
                leaf in ("mode", "vehicle", "vehicletype", "transporttype", "transit_type", "traffictype", "routetype", "route_type", "category", "type", "kind"):
            if norm_mode(v)[0] != "other":
                return v
    return None


def _scan_line(flat):
    for want in _LINE_LEAVES:
        for k, v in flat.items():
            path, leaf = _split(k)
            if leaf != want or v in (None, "") or _NOT_LINE & set(path):
                continue
            if str(v).isdigit() and want not in _NUMERIC_OK:
                continue
            return str(v)
    return None


def _scan_place(flat, side):
    for k, v in flat.items():
        path, leaf = _split(k)
        if v in (None, ""):
            continue
        if leaf in ("name", "stopname", "stationname") and set(_SIDE[side]) & set(path):
            return str(v)
        if leaf.endswith("name") and any(leaf.startswith(p) for p in _SIDE[side]):  # from_name / to_name / startname
            return str(v)
    return ""


def _scan_time(flat, side):
    for k, v in flat.items():
        path, leaf = _split(k)
        if isinstance(v, str) and v and ("time" in leaf or leaf == "datetime") and (set(_SIDE[side]) & set(path) or any(leaf.startswith(p) for p in _SIDE[side])):
            t = parse_time(v)
            if t:
                return t
    return None


def parse_leg(sec):
    """解析單一路段。支援 HERE 風格(type=pedestrian/transit/waiting + transport/departure/arrival)與扁平欄位兩種寫法。"""
    flat = _flat(sec)
    for k, v in list(flat.items()):  # 相容 {"transit": {...}} 這類多包一層的寫法
        for pre in ("transit.", "detail.", "info.", "route."):
            if k.startswith(pre):
                flat.setdefault(k[len(pre):], v)
    typ = str(sec.get("type") if not isinstance(sec.get("type"), dict) else "").strip().lower()
    if typ in ("pedestrian", "walk", "walking"):
        kind, emoji, label = norm_mode("pedestrian")
    elif typ in ("waiting", "wait"):
        kind, emoji, label = norm_mode("waiting")
    else:
        raw = _fp(flat, "transport.mode", "mode", "transit_type", "transport_mode", "vehicle.type", "vehicle", "travel_mode", "route_type", "transport.category")
        kind, emoji, label = norm_mode(raw if raw is not None else typ)
        if kind == "other":  # 欄位名稱不在預期中:掃描所有欄位找運具
            alt = _scan_mode(flat)
            if alt is not None:
                kind, emoji, label = norm_mode(alt)
    line = _fp(flat, "transport.name", "transport.shortName", "transport.short_name", "transport.longName", "route_name", "line_name",
               "routeName", "short_name", "train_no", "train_number", "transport.headsign", "headsign", "name") if kind not in ("walk", "wait") else None
    if not line and kind not in ("walk", "wait"):
        line = _scan_line(flat)
    if kind == 3 and not line:
        line = "高鐵"
    st = parse_time(_fp(flat, "departure.time", "departure_time", "start_time", "dep_time", "origin.time", "from.time"))
    en = parse_time(_fp(flat, "arrival.time", "arrival_time", "end_time", "arr_time", "destination.time", "to.time"))
    st = st or _scan_time(flat, "dep")
    en = en or _scan_time(flat, "arr")
    secs = _fp(flat, "travelSummary.duration", "travel_summary.duration", "summary.duration", "duration", "travel_time")
    mins = round(secs / 60) if isinstance(secs, (int, float)) else (round((en - st).total_seconds() / 60) if st and en else None)
    meters = _fp(flat, "travelSummary.length", "travel_summary.length", "length", "distance")
    place = lambda *p: str(_fp(flat, *p) or "")
    frm_scan, to_scan = _scan_place(flat, "dep"), _scan_place(flat, "arr")
    return {"kind": kind, "emoji": emoji, "label": label, "line": str(line or ""), "start": st, "end": en, "minutes": mins,
            "meters": int(meters) if isinstance(meters, (int, float)) else None,
            "headsign": str(_fp(flat, "transport.headsign", "headsign") or "") if kind not in ("walk", "wait") else "",
            "from": place("departure.place.name", "departure.name", "from.name", "from", "origin.name", "origin", "start_stop.name", "start_stop",
                          "from_name", "departure_stop", "start_name", "start_station") or frm_scan,
            "to": place("arrival.place.name", "arrival.name", "to.name", "to", "destination.name", "destination", "end_stop.name", "end_stop",
                        "to_name", "arrival_stop", "end_name", "end_station") or to_scan}


def _sections(sec):
    if isinstance(sec, list):
        return [x for x in sec if isinstance(x, dict)]
    if isinstance(sec, dict):
        out = []
        for v in sec.values():
            out += [x for x in v if isinstance(x, dict)] if isinstance(v, list) else ([v] if isinstance(v, dict) else [])
        return out
    return []


def parse_route(r):
    st, en = parse_time(r.get("start_time")), parse_time(r.get("end_time"))
    secs = r.get("travel_time") or ((en - st).total_seconds() if st and en else 0)
    legs = [parse_leg(s) for s in _sections(r.get("sections"))]
    ride = [x for x in legs if x["kind"] not in ("walk", "bike", "wait")]
    return {"start": st, "end": en, "minutes": round(secs / 60), "legs": legs,
            "transfers": r.get("transfers") if isinstance(r.get("transfers"), int) else max(len(ride) - 1, 0),
            "summary": " ➜ ".join(f"{x['emoji']} {x['line'] or x['label']}" for x in ride) or "🚶 步行",
            "fare": _pick(r, "price", "fare", "total_price", "cost"), "raw": r}


def plan_with_next(token, o, d, depart, n_next=3, **kw):
    """回傳 (最佳路線, 其他方案, 後面 n 趟)。後續班次以『前一趟出發 +1 分鐘』重新查詢。"""
    first = sorted((parse_route(r) for r in plan(token, o, d, depart=depart, top=5, **kw)),
                   key=lambda x: (x["end"] or datetime.max.replace(tzinfo=TW), x["start"] or datetime.max.replace(tzinfo=TW)))
    first = [r for r in first if r["start"] and r["end"]]
    if not first:
        return None, [], []
    best, others, seq = first[0], first[1:3], [first[0]]
    for _ in range(n_next):
        cursor = seq[-1]["start"] + timedelta(minutes=1)
        cands = [r for r in (parse_route(x) for x in plan(token, o, d, depart=cursor, top=3, **kw)) if r["start"] and r["end"] and r["start"] > seq[-1]["start"]]
        if not cands:
            break
        seq.append(min(cands, key=lambda x: (x["start"], x["end"])))
    return best, others, seq[1:]


def fmt_min(m):
    m = int(m or 0)
    return f"{m // 60} 小時 {m % 60} 分" if m >= 60 else f"{m} 分鐘"


def maps_link(o_name, d_name, o=None, d=None):
    from urllib.parse import quote
    a = f"{o[0]},{o[1]}" if o else quote(o_name)
    b = f"{d[0]},{d[1]}" if d else quote(d_name)
    return f"https://www.google.com/maps/dir/?api=1&origin={a}&destination={b}&travelmode=transit"
'''
T = _types.ModuleType("transit_inline")
exec(compile(_TRANSIT_SRC, "<transit 內建模組>", "exec"), T.__dict__)

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
.leg{display:flex;gap:10px;padding:7px 0;border-top:1px dashed var(--line)}.leg .lt{min-width:92px;font-weight:700;font-size:.9rem}.leg .lb{flex:1;font-size:.93rem}.leg.mute{opacity:.62;font-size:.85rem;padding:4px 0}.leg.mute .lb{font-size:.85rem}
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
    tm = f"{l['start']:%H:%M}–{l['end']:%H:%M}" if l["start"] and l["end"] else ""
    if l["kind"] in ("walk", "wait", "bike"):  # 步行/等車:淡色小字
        if not l["minutes"] and not tm:
            return ""
        extra = f"(約 {l['meters']:,} 公尺)" if l["kind"] == "walk" and l["meters"] else ""
        dest = f" → {esc(l['to'])}" if l["kind"] == "walk" and l["to"] else ""
        return (f"<div class='leg mute'><div class='lt'>{tm}</div><div class='lb'>{l['emoji']} {l['label']} "
                f"{l['minutes'] or 0} 分{extra}{dest}</div></div>")
    place = f"<div class='sub'>{esc(l['from'])} ➜ {esc(l['to'])}</div>" if (l["from"] or l["to"]) else ""
    head = f"<div class='sub'>往 {esc(l['headsign'])}</div>" if l.get("headsign") and l["headsign"] != l["line"] else ""
    dur = f"　<span class='sub'>{T.fmt_min(l['minutes'])}</span>" if l["minutes"] else ""
    when = tm or (f"約 {l['minutes']} 分" if l["minutes"] else "")
    return (f"<div class='leg'><div class='lt'>{when}</div>"
            f"<div class='lb'><b>{l['emoji']} {esc(l['line'] or l['label'])}</b>{dur}{place}{head}</div></div>")


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
with st.expander("🔧 TDX 連線診斷(規劃失敗時使用)"):
    st.caption("會用你的金鑰,以台南站 ➜ 台北車站測試每一種參數寫法,並顯示伺服器的實際回應。")
    if st.button("開始診斷"):
        if not (cid and secret):
            st.error("請先設定 TDX 金鑰。")
        else:
            try:
                tk = token_for(cid, secret)
                st.success("✅ 金鑰驗證成功,已取得存取權杖")
                res = T.diagnose(tk, (22.9971, 120.2127), (25.0478, 121.5170), T.tw_now() + pd.Timedelta(minutes=5))
                st.dataframe(pd.DataFrame(res, columns=["參數寫法", "HTTP", "回應(前 220 字)"]), hide_index=True)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
if getattr(T, "_STATE", {"i": 0})["i"] >= 2:
    st.caption(f"ℹ️ TDX 目前只接受「{getattr(T, '_STATE', {'name': ''})['name']}」的寫法,轉乘等待與步行上限等進階設定可能未套用。")
st.caption(f"程式版本:{getattr(T, 'VERSION', '未知')}")
st.caption("路線資料來源:交通部 TDX 運輸資料流通服務平台。班次為規劃參考,實際以各運輸業者公告與現場為準。")
