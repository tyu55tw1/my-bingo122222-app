# -*- coding: utf-8 -*-
"""🚉 智慧交通規劃 — Streamlit 版。  執行:streamlit run app.py"""
import html as _html
import json
import re
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

VERSION = "v8.5.2-新增方塊"
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
    lat, lng = _fp(flat, "departure.place.location.lat"), _fp(flat, "departure.place.location.lng", "departure.place.location.lon")
    return {"kind": kind, "emoji": emoji, "label": label, "line": str(line or ""), "start": st, "end": en, "minutes": mins,
            "meters": int(meters) if isinstance(meters, (int, float)) else None,
            "from_ll": (float(lat), float(lng)) if isinstance(lat, (int, float)) and isinstance(lng, (int, float)) else None,
            "long": str(_fp(flat, "transport.longName", "transport.long_name") or ""),   # 例如「紅4臺南轉運站→奇美博物館」,用來分辨方向
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


def plan_best(token, o, d, depart, **kw):
    """回傳 (最佳路線, 其他方案)。只查 1 次。"""
    first = sorted((parse_route(r) for r in plan(token, o, d, depart=depart, top=5, **kw)),
                   key=lambda x: (x["end"] or datetime.max.replace(tzinfo=TW), x["start"] or datetime.max.replace(tzinfo=TW)))
    first = [r for r in first if r["start"] and r["end"]]
    return (first[0], first[1:3]) if first else (None, [])


def plan_next(token, o, d, after, n_next=3, **kw):
    """after(第一趟的出發時間)之後的 n 趟。以『前一趟出發 +1 分鐘』重新查詢,每趟查 1 次。"""
    seq, last = [], after
    for _ in range(n_next):
        cands = [r for r in (parse_route(x) for x in plan(token, o, d, depart=last + timedelta(minutes=1), top=3, **kw))
                 if r["start"] and r["end"] and r["start"] > last]
        if not cands:
            break
        seq.append(min(cands, key=lambda x: (x["start"], x["end"])))
        last = seq[-1]["start"]
    return seq


def plan_with_next(token, o, d, depart, n_next=3, **kw):
    """回傳 (最佳路線, 其他方案, 後面 n 趟)。"""
    best, others = plan_best(token, o, d, depart, **kw)
    return (best, others, plan_next(token, o, d, best["start"], n_next, **kw)) if best else (None, [], [])


# ── 時刻表(台鐵 / 高鐵 / 公車)──────────────────────────
BASIC_URL = "https://tdx.transportdata.tw/api/basic"
# TDX 縣市代碼。「公路客運」(國道、跨縣市客運)另外放在 InterCity
BUS_CITIES = {"臺北市": "Taipei", "新北市": "NewTaipei", "桃園市": "Taoyuan", "臺中市": "Taichung", "臺南市": "Tainan",
              "高雄市": "Kaohsiung", "基隆市": "Keelung", "新竹市": "Hsinchu", "新竹縣": "HsinchuCounty", "苗栗縣": "MiaoliCounty",
              "彰化縣": "ChanghuaCounty", "南投縣": "NantouCounty", "雲林縣": "YunlinCounty", "嘉義市": "Chiayi", "嘉義縣": "ChiayiCounty",
              "屏東縣": "PingtungCounty", "宜蘭縣": "YilanCounty", "花蓮縣": "HualienCounty", "臺東縣": "TaitungCounty",
              "金門縣": "KinmenCounty", "澎湖縣": "PenghuCounty", "連江縣": "LienchiangCounty", "公路客運(國道/跨縣市)": "InterCity"}
_WEEKDAY = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def tdx_get(token, path, wait=20, **params):
    """查 TDX 基本資料。免費金鑰每分鐘只能查 5 次:被限流時,要等的秒數不超過 wait 就自動等一下再查,否則請使用者稍後再試。"""
    import time as _time
    for attempt in range(2):
        base = BASIC_URL.rsplit("/basic", 1)[0] if path.startswith("/advanced/") else BASIC_URL   # 附近站牌在 advanced 底下
        r = _http("GET", base + path, params={"$format": "JSON", **params},
                  headers={"authorization": f"Bearer {token}"})
        if r.status_code == 429:
            try:
                left = int(r.headers.get("RateLimit-Reset") or r.headers.get("Retry-After") or 60)
            except ValueError:
                left = 60
            if attempt == 0 and left <= wait:
                _time.sleep(left + 1)
                continue
            raise RuntimeError(f"TDX 免費金鑰每分鐘只能查 5 次,已經用完了,請 {left} 秒後再查")
        if r.status_code == 401:
            raise RuntimeError("TDX 授權失效,請重新確認金鑰")
        if r.status_code != 200:
            raise RuntimeError(f"TDX 查詢失敗(HTTP {r.status_code})")
        return r.json()
    raise RuntimeError("TDX 查詢失敗")


def _zh(x):
    return ((x or {}).get("Zh_tw") or "").replace("臺", "台") if isinstance(x, dict) else str(x or "")


def _hm_min(t):
    h, m = str(t)[:5].split(":")
    return int(h) * 60 + int(m)


def _ride_min(dep, arr):
    """出發、抵達(HH:MM)→ 行車分鐘;抵達比出發早代表跨過午夜。"""
    m = _hm_min(arr) - _hm_min(dep)
    return m + 1440 if m < 0 else m


def thsr_stations(token, wait=20):
    return [(s["StationID"], _zh(s["StationName"])) for s in tdx_get(token, "/v2/Rail/THSR/Station", wait=wait, **{"$select": "StationID,StationName"})]


def thsr_timetable(token, o, d, day, wait=20):
    """高鐵起訖站當天所有車次:[{車次, 出發, 抵達, 行車, 往}],依出發時間排序。"""
    rows = []
    for x in tdx_get(token, f"/v2/Rail/THSR/DailyTimetable/OD/{o}/to/{d}/{day}", wait=wait):
        info, a, b = x.get("DailyTrainInfo", {}), x.get("OriginStopTime", {}), x.get("DestinationStopTime", {})
        dep, arr = a.get("DepartureTime"), b.get("ArrivalTime")
        if dep and arr:
            rows.append({"車次": info.get("TrainNo", ""), "出發": dep, "抵達": arr, "行車": _ride_min(dep, arr),
                         "往": _zh(info.get("EndingStationName"))})
    return sorted(rows, key=lambda r: r["出發"])


def tra_stations(token, wait=20):
    j = tdx_get(token, "/v3/Rail/TRA/Station", wait=wait, **{"$select": "StationID,StationName"})
    return [(s["StationID"], _zh(s["StationName"])) for s in (j.get("Stations") or [])]


def tra_timetable(token, o, d, day, wait=20):
    """台鐵起訖站當天所有車次:[{車次, 車種, 出發, 抵達, 行車, 往, 備註}],依出發時間排序。"""
    rows = []
    j = tdx_get(token, f"/v3/Rail/TRA/DailyTrainTimetable/OD/{o}/to/{d}/{day}", wait=wait)
    for x in j.get("TrainTimetables") or []:
        info, stops = x.get("TrainInfo", {}), x.get("StopTimes") or []
        if len(stops) < 2:
            continue
        a, b = stops[0], stops[-1]
        dep, arr = a.get("DepartureTime"), b.get("ArrivalTime")
        if not (dep and arr):
            continue
        note = []
        if info.get("SuspendedFlag") or a.get("SuspendedFlag") or b.get("SuspendedFlag"):
            note.append("⛔ 停駛")
        if info.get("ExtraTrainFlag"):
            note.append("加班車")
        icons = "".join(i for f, i in (("BikeFlag", "🚲"), ("WheelChairFlag", "♿"), ("BreastFeedFlag", "🍼"),
                                       ("DiningFlag", "🍱")) if info.get(f))
        rows.append({"車次": info.get("TrainNo", ""), "車種": _zh(info.get("TrainTypeName")).split("(")[0],
                     "出發": dep, "抵達": arr, "行車": _ride_min(dep, arr), "往": _zh(info.get("EndingStationName")),
                     "服務": icons, "備註": "、".join(note)})
    return sorted(rows, key=lambda r: r["出發"])


def _bus_path(kind, city):
    return f"/v2/Bus/{kind}/InterCity" if city == "InterCity" else f"/v2/Bus/{kind}/City/{city}"


def _route_filter(route):
    return "RouteName/Zh_tw eq '" + str(route).replace("'", "''") + "'"


def bus_routes(token, city):
    """這個縣市所有公車路線名稱(給使用者挑)。"""
    names = {_zh(x.get("RouteName")) for x in tdx_get(token, _bus_path("Route", city), **{"$select": "RouteName"})}
    return sorted((n for n in names if n), key=lambda n: (not n[:1].isdigit(), len(n), n))


def _route_zh(route):
    return str(route).replace("台", "臺")   # TDX 的路線名稱用「臺」,使用者常打「台」


def bus_schedule(token, city, route, day):
    """公車時刻表。回傳 [{名稱, 方向, 發車:[HH:MM...] 或 None, 班距:[(起, 迄, 最短, 最長)...]}]。
    有固定發車時刻的路線給「發車」(起點站),只有班距的路線(例如台北 307)給「班距」;只留那天有開的。"""
    wd = _WEEKDAY[day.weekday()]
    out = []
    j = tdx_get(token, _bus_path("Schedule", city), **{"$filter": _route_filter(_route_zh(route))})
    for x in j:
        times, freqs = [], []
        for t in x.get("Timetables") or []:
            if (t.get("ServiceDay") or {}).get(wd, 1) and t.get("StopTimes"):
                s0 = t["StopTimes"][0]
                v = s0.get("DepartureTime") or s0.get("ArrivalTime")
                if v:
                    times.append(v[:5])
        for f in x.get("Frequencys") or []:
            if (f.get("ServiceDay") or {}).get(wd, 1):
                freqs.append((f.get("StartTime", ""), f.get("EndTime", ""), f.get("MinHeadwayMins"), f.get("MaxHeadwayMins")))
        name = _zh(x.get("SubRouteName")) or _zh(x.get("RouteName"))
        out.append({"名稱": name, "方向": x.get("Direction", 0), "發車": sorted(set(times)) or None, "班距": sorted(set(freqs))})
    return sorted(out, key=lambda r: (r["方向"], r["名稱"]))


_ETA_STATUS = {1: "尚未發車", 2: "交管不停靠", 3: "末班車已過", 4: "今日未營運"}


def bus_eta(token, city, route):
    """即時到站:{(方向, 名稱): [(站名, 狀態文字)]},依站序排列。要查 2 次(站序 + 預估時間)。"""
    f = _route_filter(_route_zh(route))
    order = tdx_get(token, _bus_path("StopOfRoute", city), **{"$filter": f})
    eta = tdx_get(token, _bus_path("EstimatedTimeOfArrival", city), **{"$filter": f})
    est = {}
    for e in eta:
        sec, stat = e.get("EstimateTime"), e.get("StopStatus", 0)
        if sec is not None and stat in (0, None):
            txt = "進站中" if sec < 60 else f"{sec // 60} 分"
        elif e.get("NextBusTime"):
            txt = f"{_ETA_STATUS.get(stat, '')} 下一班 {str(e['NextBusTime'])[11:16]}".strip()
        else:
            txt = _ETA_STATUS.get(stat, "—")
        est[(e.get("Direction", 0), e.get("StopUID"))] = txt
    out = {}
    for r in order:
        key = (r.get("Direction", 0), _zh(r.get("SubRouteName")) or _zh(r.get("RouteName")))
        out[key] = [(_zh(s.get("StopName")), est.get((key[0], s.get("StopUID")), "—")) for s in r.get("Stops") or []]
    return out, bool(eta)


# ── 「我要搭的這班車」:上車站今天的時刻 ──────────────────
# 縣市用 OpenStreetMap 反查(不佔 TDX 額度);ISO 3166-2 代碼 → TDX 縣市代碼
_ISO_CITY = {"TPE": "Taipei", "NWT": "NewTaipei", "TAO": "Taoyuan", "TXG": "Taichung", "TNN": "Tainan", "KHH": "Kaohsiung",
             "KEE": "Keelung", "HSZ": "Hsinchu", "HSQ": "HsinchuCounty", "MIA": "MiaoliCounty", "CHA": "ChanghuaCounty",
             "NAN": "NantouCounty", "YUN": "YunlinCounty", "CYI": "Chiayi", "CYQ": "ChiayiCounty", "PIF": "PingtungCounty",
             "ILA": "YilanCounty", "HUA": "HualienCounty", "TTT": "TaitungCounty", "KIN": "KinmenCounty", "PEN": "PenghuCounty",
             "LIE": "LienchiangCounty"}


def city_of(lat, lon):
    """座標 → TDX 縣市代碼(例如 Tainan);查不到回傳 None。"""
    try:
        r = _http("GET", "https://nominatim.openstreetmap.org/reverse",
                  params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "accept-language": "zh-TW"})
        a = r.json().get("address", {}) if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        return None
    code = _ISO_CITY.get(str(a.get("ISO3166-2-lvl4", "")).replace("TW-", ""))
    if code:
        return code
    name = str(a.get("city") or a.get("county") or a.get("state") or "").replace("台", "臺")
    return BUS_CITIES.get(name)


def _norm(s):
    return re.sub(r"[\s()()]", "", str(s or "")).replace("台", "臺")


def bus_raw(token, city, route, wait=20):
    """公車路線的(時刻表, 站序)原始資料。一天內不會變,呼叫端可以快取很久。查 2 次。"""
    f = _route_filter(_route_zh(route))
    return (tdx_get(token, _bus_path("Schedule", city), wait=wait, **{"$filter": f}),
            tdx_get(token, _bus_path("StopOfRoute", city), wait=wait, **{"$filter": f}))


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + ((a[1] - b[1]) * 0.92) ** 2


def _hm(m):
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def board_times(sched, stops, long_name, headsign, stop_name, stop_ll, board_hm, day):
    """算出上車站當天每一班車的時間。
    有些縣市的時刻表有每一站的時間(直接用);台南等只有起點站發車時間,就用「這次規劃的上車時間 − 對應那班的發車時間」
    當成起點到這站的時間,套用到全天的班次(推估,畫面上標「約」)。
    回傳 {名稱, 方向, 站名, 站UID, 第幾站, 時刻[HH:MM], 推估, 班距, 說明};對不上路線回傳 None。"""
    # 1. 找方向:子路線名稱跟規劃結果的 longName 一樣最好,其次看終點(headsign)
    want, head, sn = _norm(long_name), _norm(headsign), _norm(stop_name)

    def score(r):
        name = _norm(_zh(r.get("SubRouteName")) or _zh(r.get("RouteName")))
        names = [_norm(_zh(s.get("StopName"))) for s in r.get("Stops") or []]
        return ((want and name == want) * 4 + (bool(head) and name.endswith(head)) * 2 + (sn in names) * 3
                - (bool(want) and "小巴" in name and "小巴" not in want))
    if not stops:
        return None
    sub = max(stops, key=score)
    seq = sub.get("Stops") or []
    # 2. 找上車站:站名一樣,不然找離上車點最近的站牌
    k = next((i for i, s in enumerate(seq) if _norm(_zh(s.get("StopName"))) == sn), None)
    if k is None and stop_ll and seq:
        def ll(s):
            p = s.get("StopPosition") or {}
            return p.get("PositionLat", 0), p.get("PositionLon", 0)
        k = min(range(len(seq)), key=lambda i: _dist2(ll(seq[i]), stop_ll))
        if _dist2(ll(seq[k]), stop_ll) > 0.003 ** 2:   # 超過約 300 公尺就算對不上
            k = None
    stop = seq[k] if k is not None else {}
    uid = stop.get("StopUID")
    # 3. 對應的時刻表(同一條子路線)
    sid, direction = sub.get("SubRouteUID"), sub.get("Direction", 0)
    rows = [x for x in sched if x.get("SubRouteUID") == sid] or [x for x in sched if x.get("Direction", 0) == direction]
    wd = _WEEKDAY[day.weekday()]
    exact, firsts, freqs = [], [], []
    for x in rows:
        for t in x.get("Timetables") or []:
            if not (t.get("ServiceDay") or {}).get(wd, 1):
                continue
            sts = t.get("StopTimes") or []
            hit = next((s for s in sts if uid and s.get("StopUID") == uid), None)
            if hit and (hit.get("ArrivalTime") or hit.get("DepartureTime")):
                exact.append((hit.get("ArrivalTime") or hit.get("DepartureTime"))[:5])
            elif sts and (sts[0].get("DepartureTime") or sts[0].get("ArrivalTime")):
                firsts.append(_hm_min((sts[0].get("DepartureTime") or sts[0].get("ArrivalTime"))[:5]))
        for f in x.get("Frequencys") or []:
            if (f.get("ServiceDay") or {}).get(wd, 1):
                freqs.append((f.get("StartTime", ""), f.get("EndTime", ""), f.get("MinHeadwayMins"), f.get("MaxHeadwayMins")))
    out = {"名稱": _zh(sub.get("SubRouteName")) or _zh(sub.get("RouteName")), "方向": direction,
           "站名": _zh(stop.get("StopName")) or stop_name, "站UID": uid, "第幾站": (k + 1) if k is not None else None,
           "時刻": [], "推估": False, "班距": sorted(set(freqs)), "說明": ""}
    if exact and len(exact) >= len(firsts):
        out["時刻"] = sorted(set(exact))
    elif firsts:
        firsts = sorted(set(firsts))
        board = _hm_min(board_hm) if board_hm else None
        if k == 0:
            off = 0
        else:
            expect = (k or 0) * 2.0   # 平均每站約 2 分鐘(台南紅4:第 16 站、起點發車後 32 分)
            cands = [t for t in firsts if board is not None and 0 <= board - t <= 150]
            off = (board - min(cands, key=lambda t: abs(board - t - expect))) if cands else round(expect)
            out["推估"] = True
            out["說明"] = f"這條路線的時刻表只有起點站發車時間,上車站的時間是推估的(起點站發車後約 {off} 分到站),以即時到站為準。"
        out["時刻"] = [_hm(t + off) for t in firsts]
    if k is None:
        out["說明"] = "在 TDX 的站牌資料裡找不到你的上車站,下面是起點站的發車時間。"
    return out


def stop_eta(token, city, route, stop_uid, stop_name, direction, wait=20):
    """上車站的即時到站:[(子路線名稱, 狀態文字)]。查 1 次。"""
    f = _route_filter(_route_zh(route))
    f += f" and StopUID eq '{stop_uid}'" if stop_uid else " and StopName/Zh_tw eq '" + str(stop_name).replace("'", "''") + "'"
    out = []
    for e in tdx_get(token, _bus_path("EstimatedTimeOfArrival", city), wait=wait, **{"$filter": f}):
        if e.get("Direction", direction) != direction:
            continue
        sec, stat = e.get("EstimateTime"), e.get("StopStatus", 0)
        if sec is not None and stat in (0, None):
            txt = "進站中" if sec < 60 else f"還有 {sec // 60} 分"
        elif e.get("NextBusTime"):
            txt = f"{_ETA_STATUS.get(stat, '')} 下一班約 {str(e['NextBusTime'])[11:16]}".strip()
        else:
            txt = _ETA_STATUS.get(stat, "沒有資料")
        out.append((_zh(e.get("SubRouteName")) or _zh(e.get("RouteName")), txt))
    return out


# ── 「我附近的公車」:附近站牌 → 經過的路線 → 即時到站 ──────────
WALK_M_PER_MIN = 70   # 走路速度(公尺/分),用來算走到站牌要幾分


def meters(a, b):
    """兩個 (緯度, 經度) 之間的大約距離(公尺)。"""
    import math
    dy = (a[0] - b[0]) * 111000
    dx = (a[1] - b[1]) * 111000 * math.cos(math.radians((a[0] + b[0]) / 2))
    return (dx * dx + dy * dy) ** 0.5


def nearby_stops(token, lat, lon, radius=300, wait=20):
    """附近 radius 公尺內的公車站牌:[{uid, 站名, 位置, 縣市, 距離}],由近到遠。查 1 次。"""
    j = tdx_get(token, "/advanced/v2/Bus/Stop/NearBy", wait=wait,
                **{"$spatialFilter": f"nearby({lat}, {lon}, {int(radius)})", "$select": "StopUID,StopName,StopPosition,City"})
    out = []
    for s in j:
        p = s.get("StopPosition") or {}
        ll = (p.get("PositionLat"), p.get("PositionLon"))
        if None in ll or not s.get("City"):
            continue
        out.append({"uid": s["StopUID"], "站名": _zh(s.get("StopName")), "位置": ll, "縣市": s["City"],
                    "距離": round(meters((lat, lon), ll))})
    return sorted(out, key=lambda x: x["距離"])


def stop_routes(token, city, uids, wait=20):
    """經過這些站牌的子路線(站序原始資料,含每條子路線完整的站序)。查 1 次。"""
    f = " or ".join(f"Stops/any(d: d/StopUID eq '{u}')" for u in uids)
    return tdx_get(token, _bus_path("StopOfRoute", city), wait=wait, **{"$filter": f})


def stops_eta(token, city, uids, wait=20):
    """這些站牌所有路線的即時到站原始資料。查 1 次。"""
    f = " or ".join(f"StopUID eq '{u}'" for u in uids)
    return tdx_get(token, _bus_path("EstimatedTimeOfArrival", city), wait=wait, **{"$filter": f})


def eta_text(e):
    """一筆即時到站 → (到站分鐘或 None, 文字)。"""
    sec, stat = e.get("EstimateTime"), e.get("StopStatus", 0)
    if sec is not None and stat in (0, None):
        return sec // 60, ("進站中" if sec < 60 else f"還有 {sec // 60} 分")
    if e.get("NextBusTime"):
        t = str(e["NextBusTime"])[11:16]
        return None, f"{_ETA_STATUS.get(stat, '')} 下一班約 {t}".strip()
    return None, _ETA_STATUS.get(stat, "沒有即時資料")


def nearby_board(stops, routes, etas):
    """整理成「站牌 → 經過的公車」:[{站名, 距離, 走路, 路線:[{路線, 往, 子路線, uid, 方向, 分鐘, 即時, raw}]}]。
    同名、距離差不多的站牌(對向、不同業者)併成一組,依距離排序。"""
    by_uid = {s["uid"]: s for s in stops}
    eta = {(e.get("SubRouteUID") or e.get("RouteUID"), e.get("StopUID")): e for e in etas}
    eta_r = {(_zh(e.get("RouteName")), e.get("Direction", 0), e.get("StopUID")): e for e in etas}
    groups = {}
    for r in routes:
        name = _zh(r.get("SubRouteName")) or _zh(r.get("RouteName"))
        for s in r.get("Stops") or []:
            st = by_uid.get(s.get("StopUID"))
            if not st:
                continue
            e = eta.get((r.get("SubRouteUID"), st["uid"])) or eta_r.get((_zh(r.get("RouteName")), r.get("Direction", 0), st["uid"]))
            mins, txt = eta_text(e) if e else (None, "沒有即時資料")
            # 「往哪裡」:子路線名稱「…→後壁厝(繞中華醫事科大)」取箭頭後面;沒有箭頭才用最後一站
            last = re.sub(r"[((].*$", "", name.split("→")[-1]).strip() if "→" in name else                 _zh((r.get("Stops") or [{}])[-1].get("StopName"))
            g = groups.setdefault((st["站名"], st["距離"] // 60), {"站名": st["站名"], "距離": st["距離"], "路線": []})
            g["距離"] = min(g["距離"], st["距離"])
            g["路線"].append({"路線": _zh(r.get("RouteName")), "往": last, "子路線": name, "uid": st["uid"], "縣市": st["縣市"],
                            "方向": r.get("Direction", 0), "分鐘": mins, "即時": txt, "raw": r})
    out = sorted(groups.values(), key=lambda g: g["距離"])
    for g in out:
        g["走路"] = max(1, round(g["距離"] / WALK_M_PER_MIN))
        g["路線"].sort(key=lambda x: (x["分鐘"] is None, x["分鐘"] if x["分鐘"] is not None else 0, x["路線"], x["方向"]))
    return out


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
_GPS = {"shown": False, "loc": None}   # 每次畫面重跑都會重設:GPS 元件一頁只放一次


def my_location(what):
    """目前位置(GPS)。路線規劃和附近公車都要用:GPS 元件只放在第一個用到的地方,第二個地方共用同一個結果。
    沒裝 streamlit-geolocation 時改用 IP 概略位置。"""
    if not streamlit_geolocation:
        ip = ((st.context.headers.get("X-Forwarded-For", "") if hasattr(st, "context") else "") or "").split(",")[0].strip()
        loc = T.ip_location(ip) if ip else None
        st.caption("未安裝 streamlit-geolocation,改用 IP 概略位置(誤差可能達數公里)。" if loc else f"無法定位,請改為手動輸入{what}。")
        return loc
    if not _GPS["shown"]:
        _GPS["shown"] = True
        loc = streamlit_geolocation() or {}
        if loc.get("latitude") is not None:
            _GPS["loc"] = {"name": "我的位置(GPS)", "lat": float(loc["latitude"]), "lon": float(loc["longitude"])}
            st.session_state["last_gps"] = _GPS["loc"]
        else:
            st.info("請按上方定位按鈕並允許瀏覽器取得位置。")
    else:
        st.caption("使用「路線規劃」分頁取得的 GPS 位置。")
    return _GPS["loc"] or st.session_state.get("last_gps")
esc = lambda x: _html.escape(str(x), quote=True)
st.markdown("""<style>
:root{--soft:rgba(128,128,128,.11);--line:rgba(128,128,128,.28);--ac:#d9441a}
.block-container{padding:3.4rem 1rem 3rem;max-width:880px}header[data-testid=stHeader]{background:transparent}footer{visibility:hidden}
.hero{display:flex;align-items:center;gap:14px;background:linear-gradient(120deg,#1f6feb,#35c2d6 60%,#7ee0a6);padding:18px 22px;border-radius:22px;margin:4px 0 16px;color:#fff;box-shadow:0 10px 28px rgba(31,111,235,.28)}
.hero h1{margin:0;font-size:1.6rem;color:#fff;padding:0}.hero p{margin:2px 0 0;opacity:.95;color:#fff}.hero .ico{font-size:2.5rem}
.rt{background:var(--soft);border:1px solid var(--line);border-radius:18px;padding:14px 16px;margin:0 0 12px}.rt.best{border:2px solid var(--ac)}
.tm{font-size:1.7rem;font-weight:800;line-height:1.1}.sub{opacity:.72;font-size:.88rem}.sum{font-weight:700;margin:6px 0 2px}
.tag{display:inline-block;padding:1px 9px;border-radius:99px;background:var(--ac);color:#fff;font-size:.74rem;font-weight:700;margin-right:6px}
.leg{display:flex;gap:10px;padding:7px 0;border-top:1px dashed var(--line)}.leg .lt{min-width:92px;font-weight:700;font-size:.9rem}.leg .lb{flex:1;font-size:.93rem}.leg.mute{opacity:.62;font-size:.85rem;padding:4px 0}.leg.mute .lb{font-size:.85rem}
.nx{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:6px 0 14px}
.nx>div{background:var(--soft);border:1px solid var(--line);border-radius:16px;padding:12px}.nx .tm{font-size:1.3rem}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 8px}.chip{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:4px 9px;font-weight:700;line-height:1.15;text-align:center}.chip small{display:block;font-weight:400;font-size:.72rem;opacity:.8}.chip.past{opacity:.38;font-weight:400}.chip.plan{border:2px solid var(--ac);background:rgba(217,68,26,.12)}
.stoplist{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:14px;margin:6px 0 10px}.sr{display:flex;align-items:center;gap:10px;padding:7px 12px;border-bottom:1px solid var(--line)}.sr:last-child{border-bottom:0}.sr .n{width:22px;opacity:.55;font-size:.8rem;text-align:right}.sr .nm{flex:1}.sr.hl{background:rgba(217,68,26,.12)}.sr.past{opacity:.45}.eta{font-weight:800;min-width:66px;text-align:right;white-space:nowrap}.eta.now{color:#d9441a}.eta.soon{color:#e8590c}.eta.mute{opacity:.55;font-weight:400;font-size:.82rem}.sr .bus{display:inline-block;margin-left:8px;font-size:.72rem;background:#d9441a;color:#fff;border-radius:8px;padding:1px 7px;font-weight:700}.dot{width:12px;height:12px;border-radius:50%;display:inline-block}.bk{background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:10px 12px;margin:0 0 8px}.bkn{display:flex;gap:16px;font-size:.9rem;margin:4px 0}.bkn b{font-size:1.15rem}.bkbar{display:flex;height:7px;border-radius:4px;overflow:hidden;background:rgba(128,128,128,.15)}.bkbar i{display:block;height:100%}.fgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:6px 0 10px}.ftile{display:flex;flex-direction:column;gap:3px;border:1px solid var(--line);border-radius:20px;padding:14px 16px;background:var(--soft);text-decoration:none!important;color:inherit!important;min-height:120px}.ftile:hover{border-color:var(--ac)}.ftile .tl{font-weight:800;line-height:1.3}.ftile .tsub{opacity:.65;font-weight:400;font-size:.85rem}.ftile .tv{font-size:1.9rem;font-weight:800;line-height:1.15}.ftile .tv small{font-size:.85rem;font-weight:600;opacity:.7}.ftile .tv.now{color:#d9441a}.ftile .tv.soon{color:#e8590c}.ftile .tv.mute{font-size:1.05rem;opacity:.6}.ftile .trow{display:flex;justify-content:space-between;gap:8px;font-size:.9rem}.ftile .ts{opacity:.7;font-size:.83rem}.ftile b.now{color:#d9441a}.ftile b.ok{color:#2e7d32}.ftile .tmore{margin-top:auto;padding-top:4px;color:var(--ac);font-weight:800;font-size:.85rem}.ftile.add{border-style:dashed;background:transparent;min-height:0;padding:12px 14px}.ftile.add .tv{font-size:1.4rem}.ftile.add .ts{font-size:.78rem}@media(max-width:380px){.fgrid:not(.addg){grid-template-columns:1fr}.fgrid.addg{gap:8px}}.fav{display:flex;justify-content:space-between;align-items:center;background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:10px 14px;margin:0 0 4px}.fav .eta{font-size:1.35rem}
@media(max-width:640px){.hero{padding:10px 14px;margin:0 0 8px;border-radius:16px}.hero p{display:none}.hero .ico{font-size:1.6rem}.hero h1{font-size:1.15rem}.tm{font-size:1.45rem}.leg .lt{min-width:78px;font-size:.82rem}}
</style>""", unsafe_allow_html=True)
st.markdown("<div class='hero'><span class='ico'>🚉</span><div><h1>智慧交通規劃</h1><p>常用站一打開就看到下一班・公車動態地圖・台鐵誤點・高鐵停靠・捷運・YouBike</p></div></div>", unsafe_allow_html=True)


@st.cache_data(ttl=3000, show_spinner=False)
def token_for(cid, secret):
    return T.get_token(cid, secret)


@st.cache_data(ttl=3600, show_spinner=False)
def geo(q):
    return T.geocode(q)


@st.cache_data(ttl=90, show_spinner=False)
def planned(cid, secret, o, d, depart_iso, modes, gc, transfer, first, last):
    """(最佳路線, 其他方案):查 1 次。後面 3 趟改成按鈕才查(c_next),省 TDX 額度。"""
    kw = dict(modes=modes, gc=gc, transfer=transfer, first=first, last=last)
    return T.plan_best(token_for(cid, secret), o, d, datetime.fromisoformat(depart_iso), **kw)


@st.cache_data(ttl=90, show_spinner=False)
def c_next(cid, secret, o, d, after_iso, modes, gc, transfer, first, last):
    kw = dict(modes=modes, gc=gc, transfer=transfer, first=first, last=last)
    return T.plan_next(token_for(cid, secret), o, d, datetime.fromisoformat(after_iso), 3, **kw)


# TDX 免費金鑰每分鐘只能查 5 次:車站、路線一天查一次,時刻表 30 分鐘內重複查詢直接用上次的結果
@st.cache_data(ttl=86400, show_spinner=False)
def c_stations(kind, cid, secret, wait=20):
    return (T.tra_stations if kind == "tra" else T.thsr_stations)(token_for(cid, secret), wait=wait)


@st.cache_data(ttl=1800, show_spinner=False)
def c_rail(kind, cid, secret, o, d, day_iso, wait=20):
    return (T.tra_timetable if kind == "tra" else T.thsr_timetable)(token_for(cid, secret), o, d, day_iso, wait=wait)


@st.cache_data(ttl=86400, show_spinner=False)
def c_city(lat, lon):
    return T.city_of(lat, lon)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def c_bus_raw(cid, secret, city, route):
    return T.bus_raw(token_for(cid, secret), city, route, wait=70)


@st.cache_data(ttl=86400, show_spinner=False)
def c_near_stops(cid, secret, lat, lon, radius):
    return T.nearby_stops(token_for(cid, secret), lat, lon, radius, wait=70)


@st.cache_data(ttl=86400, show_spinner=False)
def c_stop_routes(cid, secret, city, uids):
    return T.stop_routes(token_for(cid, secret), city, list(uids), wait=70)


@st.cache_data(ttl=30, show_spinner=False)
def c_stops_eta(cid, secret, city, uids):
    return T.stops_eta(token_for(cid, secret), city, list(uids), wait=70)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def c_bus_sched(cid, secret, city, route):
    """只查時刻表(1 次);站序已經在附近公車查過了。"""
    return T.tdx_get(token_for(cid, secret), T._bus_path("Schedule", city), wait=70,
                     **{"$filter": T._route_filter(T._route_zh(route))})


@st.cache_data(ttl=30, show_spinner=False)
def c_stop_eta(cid, secret, city, route, stop_uid, stop_name, direction):
    return T.stop_eta(token_for(cid, secret), city, route, stop_uid, stop_name, direction, wait=70)


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


# ── 「我要搭的車」卡片:每一段要搭的公車/火車,上車站今天的時刻 ──
BUFFER_MIN = 2   # 算「幾點前出門」時多留的緩衝分鐘


def _nearest(times, plan_hm, tol=3):
    """規劃結果的時間跟時刻表常差 1、2 分鐘(例如台鐵仁德站 08:12 發車,規劃寫 08:13):找最接近、差不到 tol 分的那班。"""
    if not times or not plan_hm:
        return None
    best = min(times, key=lambda t: abs(T._hm_min(t) - T._hm_min(plan_hm)))
    return best if abs(T._hm_min(best) - T._hm_min(plan_hm)) <= tol else None


def _chips(times, plan_hm, now_hm, lead=None, approx=False):
    """一排時刻小方塊:已經過的變淡、規劃的那班加框;lead(走到站的分鐘)有給就加上「幾點前出門」。"""
    out = []
    plan_hm = _nearest(times, plan_hm)
    for t in times:
        past = t < now_hm
        leave = T._hm(T._hm_min(t) - lead - BUFFER_MIN) if lead is not None else ""
        cls = "chip plan" if t == plan_hm else ("chip past" if past else "chip")
        sub = f"<small>{leave} 前出門</small>" if leave and not past else ""
        out.append(f"<div class='{cls}'>{'約 ' if approx else ''}{t}{sub}</div>")
    return "<div class='chips'>" + "".join(out) + "</div>"


def bus_card(leg, lead):
    if not leg["from_ll"]:
        st.caption("規劃結果沒有上車站的位置,查不到這段的時刻表。")
        return
    city = c_city(round(leg["from_ll"][0], 4), round(leg["from_ll"][1], 4))
    if not city:
        st.caption("查不出上車站在哪個縣市,查不到這段的時刻表。")
        return
    day, plan_hm = leg["start"].date(), f"{leg['start']:%H:%M}"
    today = day == T.tw_now().date()
    with st.spinner("查詢上車站的時刻表…(TDX 每分鐘只能查 5 次,額度不夠時會自動等一下,最多約 1 分鐘)"):
        sched, stops = c_bus_raw(cid, secret, city, leg["line"])
    info = T.board_times(sched, stops, leg["long"], leg["headsign"], leg["from"], leg["from_ll"], plan_hm, day)
    if not info:
        st.caption("TDX 沒有這條路線的資料。")
        return
    if today:
        try:
            with st.spinner("查詢即時到站…"):
                eta = c_stop_eta(cid, secret, city, leg["line"], info["站UID"], info["站名"], info["方向"])
        except Exception as e:  # noqa: BLE001
            eta = None
            st.caption(f"即時到站查不到:{e}")
        if eta:
            st.markdown("🚏 **即時到站:**" + "　".join(f"{esc(t)}" if len(eta) == 1 else f"{esc(n)}:{esc(t)}" for n, t in eta))
        elif eta is not None:
            st.caption("🚏 目前沒有即時到站資料。")
    stop_txt = f"{info['站名']}(第 {info['第幾站']} 站)" if info["第幾站"] else info["站名"]
    st.markdown(f"<div class='sub'>{esc(info['名稱'])}・{esc(stop_txt)}・今天的到站時間</div>", unsafe_allow_html=True)
    if info["時刻"]:
        st.markdown(_chips(info["時刻"], plan_hm, f"{T.tw_now():%H:%M}" if today else "00:00", lead, info["推估"]),
                    unsafe_allow_html=True)
    if info["班距"]:
        st.dataframe(pd.DataFrame([{"時段": f"{a}–{b}", "班距": f"每 {lo}–{hi} 分" if lo != hi else f"每 {lo} 分"}
                                   for a, b, lo, hi in info["班距"]]), hide_index=True, use_container_width=True)
    if not info["時刻"] and not info["班距"]:
        st.caption("這天沒有這個方向的時刻表資料。")
    if info["說明"]:
        st.caption(info["說明"])


def rail_card(leg, lead):
    kind = "thsr" if leg["kind"] == 3 else "tra"
    with st.spinner("查詢這段的火車時刻…"):
        stations = c_stations(kind, cid, secret, wait=70)
    by = {T._norm(n): i for i, n in stations}
    find = lambda n: by.get(T._norm(n)) or by.get(T._norm(re.sub(r"^高鐵|站$", "", str(n))))
    o, d = find(leg["from"]), find(leg["to"])
    if not (o and d):
        st.caption(f"在{'高鐵' if kind == 'thsr' else '台鐵'}車站清單裡找不到「{leg['from']}」或「{leg['to']}」,查不到時刻表。")
        return
    day, plan_hm = leg["start"].date(), f"{leg['start']:%H:%M}"
    with st.spinner("查詢這段的火車時刻…"):
        rows = c_rail(kind, cid, secret, o, d, day.isoformat(), wait=70)
    if not rows:
        st.caption("這天這兩站之間沒有直達車次。")
        return
    now_hm = f"{T.tw_now():%H:%M}" if day == T.tw_now().date() else "00:00"
    st.markdown(f"<div class='sub'>{esc(leg['from'])} ➜ {esc(leg['to'])}・今天的發車時間(點開下方看車次)</div>", unsafe_allow_html=True)
    st.markdown(_chips([r["出發"] for r in rows], plan_hm, now_hm, lead), unsafe_allow_html=True)
    with st.expander("車次明細"):
        df = pd.DataFrame(rows)
        hit = _nearest([r["出發"] for r in rows], plan_hm)
        df.insert(0, " ", ["👉 規劃的這班" if r["出發"] == hit else "" for r in rows])
        df["行車"] = df["行車"].map(T.fmt_min)
        st.dataframe(df, hide_index=True, use_container_width=True, height=min(38 * (len(df) + 1), 420))


def ride_cards(best):
    """最佳路線裡每一段要搭的公車/台鐵/高鐵各一張卡。第一段自動查,後面的按了才查(省額度)。"""
    legs = best["legs"]
    rides = [(i, l) for i, l in enumerate(legs) if l["kind"] in (3, 4, 5)]
    if not rides:
        return
    st.markdown("##### 🎫 我要搭的車(上車站今天的時刻)")
    first_i = rides[0][0]
    walk_before = sum(l["minutes"] or 0 for l in legs[:first_i] if l["kind"] in ("walk", "bike"))
    for n, (i, leg) in enumerate(rides):
        head = f"(往 {leg['headsign']})" if leg.get("headsign") and leg["headsign"] != leg["line"] else ""
        title = f"{leg['emoji']} {leg['line'] or leg['label']}{head}｜{leg['from']} 上車 {leg['start']:%H:%M}"
        key = f"card_{leg['start']:%m%d%H%M}_{leg['line']}"
        with st.container(border=True):
            st.markdown(f"**{esc(title)}**")
            if n > 0 and not st.session_state.get(key):
                st.button("顯示這段的時刻表", key=key + "_btn", on_click=lambda k=key: st.session_state.__setitem__(k, True))
                continue
            lead = walk_before if n == 0 else None   # 只有第一段要算「幾點前出門」
            try:
                (bus_card if leg["kind"] == 5 else rail_card)(leg, lead)
            except Exception as e:  # noqa: BLE001
                st.warning(f"這段的時刻表查不到:{e}")
    st.caption(f"「幾點前出門」= 車到站時間 − 走到站的時間({walk_before} 分)− 緩衝 {BUFFER_MIN} 分。標「約」的是推估時間,以即時到站為準。")


def show_plan(res):
    best, others, origin, dest, o, d = res["best"], res["others"], res["origin"], res["dest"], res["o"], res["d"]
    if best is None:
        st.warning("這個時段找不到符合條件的大眾運輸。可試著放寬運具、步行上限或換個出發時間。")
        st.link_button("用 Google 地圖查看 ↗", T.maps_link(origin["name"], dest["name"], o, d))
        return
    st.markdown(route_html(best, "最佳路線", True), unsafe_allow_html=True)
    ride_cards(best)
    if res["nxt"] is None:
        if st.button("⏭️ 查後面 3 趟(以防錯過,會用掉 3 次查詢額度)"):
            try:
                with st.spinner("查詢後面 3 趟…"):
                    res["nxt"] = c_next(cid, secret, o, d, best["start"].isoformat(), *res["kw"])
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    nxt = res["nxt"]
    if nxt:
        st.markdown("##### ⏭️ 後面 3 趟(以防錯過)")
        st.markdown("<div class='nx'>" + "".join(
            f"<div><span class='tag'>下一班 {i}</span><div class='tm'>{r['start']:%H:%M} ➜ {r['end']:%H:%M}</div>"
            f"<div class='sub'>{T.fmt_min(r['minutes'])}・轉乘 {r['transfers']} 次</div><div class='sum'>{esc(r['summary'])}</div></div>"
            for i, r in enumerate(nxt, 1)) + "</div>", unsafe_allow_html=True)
        with st.expander("查看後面班次的詳細轉乘"):
            for i, r in enumerate(nxt, 1):
                st.markdown(route_html(r, f"下一班 {i}"), unsafe_allow_html=True)
    elif nxt == []:
        st.caption("後面沒有其他班次了。")
    if others:
        with st.expander(f"🔀 其他方案({len(others)})"):
            for r in others:
                st.markdown(route_html(r), unsafe_allow_html=True)
    st.map(pd.DataFrame({"lat": [o[0], d[0]], "lon": [o[1], d[1]]}), size=60)
    with st.expander("🧾 原始資料(排查用)"):
        st.json(best["raw"])
    st.link_button("在 Google 地圖開啟 ↗", T.maps_link(origin["name"], dest["name"], o, d))


# ── 金鑰 ──
# ═══════════════ v8：地圖、公車動態、YouBike、捷運、台鐵誤點、高鐵停靠、★ 常用 ═══════════════
# TDX 免費金鑰每分鐘只能查 5 次：一天不會變的資料（路線線型、站序、車站位置）快取一整天，
# 即時資料快取 20～60 秒；自動更新只重跑「即時」那一小塊，每 60 秒一次。
try:
    from streamlit_js_eval import streamlit_js_eval      # 把常用清單存在手機瀏覽器裡（localStorage）
except ImportError:
    streamlit_js_eval = None

FAV_KEY = "tw_transit_fav_v1"
LIVE_EVERY = 60
METRO = {"高雄捷運": "KRTC", "台北捷運": "TRTC", "桃園機場捷運": "TYMC", "高雄輕軌": "KLRT"}
LINE_COLORS = {("KRTC", "O"): "#F39800", ("KRTC", "R"): "#E2211C", ("KLRT", "C"): "#7CBD52",
               ("TRTC", "BL"): "#0070BD", ("TRTC", "R"): "#E3002C", ("TRTC", "G"): "#008659",
               ("TRTC", "O"): "#F8B61C", ("TRTC", "BR"): "#C48C31", ("TRTC", "Y"): "#FFDB00",
               ("TYMC", "A"): "#8246AF"}
MAP_COLORS = {"stop": [30, 110, 230], "hl": [217, 68, 26], "bus": [200, 20, 50], "bike": [245, 170, 0],
              "bike_empty": [150, 150, 150], "me": [20, 160, 90], "station": [120, 60, 200], "train": [200, 20, 50]}


def _route_f(route):
    return T._route_filter(T._route_zh(route))


def _pts(wkt):
    """WKT 的 LINESTRING／MULTILINESTRING → [[經度, 緯度], ...]"""
    return [[float(a), float(b)] for a, b in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", wkt or "")]


def show_map(paths=(), points=(), height=330):
    """地圖：paths = [{path: [[經度, 緯度]...], color}]，points = [{lat, lon, name, kind, label?}]。
    kind：stop 站牌、hl 重點站、bus 公車、bike／bike_empty YouBike、me 我的位置、station 車站、train 列車。"""
    import math
    import pydeck as pdk
    lats = [p["lat"] for p in points] + [c[1] for x in paths for c in x["path"]]
    lons = [p["lon"] for p in points] + [c[0] for x in paths for c in x["path"]]
    if not lats:
        return
    span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.92, 0.003)
    zoom = max(8.0, min(16.5, math.log2(360 / span) - 0.6))
    lit = pdk.types.String                  # pydeck 會把一般字串當成算式（"pixels" 變成 @@=pixels），文字參數要這樣包起來
    layers = []
    if paths:
        layers.append(pdk.Layer("PathLayer", data=[dict(x, color=x.get("color", [130, 70, 210])) for x in paths],
                                get_path="path", get_color="color", get_width=4, width_min_pixels=4, width_units=lit("pixels"),
                                cap_rounded=True, joint_rounded=True))
    for kind, size in (("stop", 5), ("station", 6), ("bike_empty", 6), ("bike", 7), ("hl", 9), ("me", 8), ("bus", 9),
                       ("train", 10)):
        pts = [dict(p, color=MAP_COLORS[kind]) for p in points if p.get("kind") == kind]
        if pts:
            layers.append(pdk.Layer("ScatterplotLayer", data=pts, get_position=["lon", "lat"], get_fill_color="color",
                                    get_line_color=[255, 255, 255], stroked=True, line_width_min_pixels=2,
                                    get_radius=size, radius_units=lit("pixels"), pickable=True))
    for below in (False, True):             # 公車、列車的標籤放在點的下方，站名放上方，才不會疊在一起
        labels = [p for p in points if p.get("label") and (p.get("kind") in ("bus", "train")) == below]
        if labels:
            layers.append(pdk.Layer("TextLayer", data=labels, get_position=["lon", "lat"], get_text="label", get_size=13,
                                    get_color=[180, 10, 40] if below else [25, 25, 25],
                                    get_pixel_offset=[0, 20] if below else [0, -20], character_set=lit("auto"),
                                    font_family=lit('"Microsoft JhengHei","Noto Sans TC","PingFang TC",sans-serif'),
                                    font_weight=700, background=True, get_background_color=[255, 255, 255, 225],
                                    background_padding=[5, 2, 5, 2]))
    st.pydeck_chart(pdk.Deck(layers=layers, map_provider="carto", map_style="light", tooltip={"text": "{name}"},
                             initial_view_state=pdk.ViewState(latitude=(max(lats) + min(lats)) / 2,
                                                              longitude=(max(lons) + min(lons)) / 2, zoom=zoom)),
                    height=height)


def eta_badge(e):
    """一筆公車到站資料 → (樣式, 文字)：進站中、幾分、尚未發車…"""
    if not e:
        return "mute", "—"
    mins, txt = T.eta_text(e)
    if mins is not None:
        return ("now", "進站中") if mins <= 1 else ("soon" if mins <= 5 else "", f"{mins} 分")
    return "mute", txt.replace("下一班約", "下班")


# ─── 公車：路線線型、即時位置、到站 ───
@st.cache_data(ttl=86400, show_spinner=False)
def c_bus_shape(cid, secret, city, route):
    j = T.tdx_get(token_for(cid, secret), T._bus_path("Shape", city), wait=70, **{"$filter": _route_f(route)})
    return [{"sub": x.get("SubRouteUID"), "dir": x.get("Direction", 0), "path": _pts(x.get("Geometry"))} for x in j]


@st.cache_data(ttl=20, show_spinner=False)
def c_bus_live(cid, secret, city, route):
    """整條路線的即時資料（查 3 次）：到站時間、公車位置、公車在哪一站（進站／離站）。"""
    tok, f = token_for(cid, secret), _route_f(route)

    def get(kind):
        return T.tdx_get(tok, T._bus_path(kind, city), wait=70, **{"$filter": f})
    return get("EstimatedTimeOfArrival"), get("RealTimeByFrequency"), get("RealTimeNearStop")


def _same_sub(x, sub):
    """這筆即時資料是不是這個方向（有子路線編號就比子路線，沒有就比方向）。"""
    if x.get("SubRouteUID") and sub.get("SubRouteUID"):
        return x["SubRouteUID"] == sub["SubRouteUID"]
    return x.get("Direction", 0) == sub.get("Direction", 0)


def bus_live_body(city, route, subs, si):
    """路線動態：地圖（路線、站牌、公車）＋每一站還有幾分、哪一站有車進站。"""
    sub = subs[si]
    seq = sub["Stops"]
    try:
        etas, pos, near = c_bus_live(cid, secret, city, route)
    except Exception as e:  # noqa: BLE001
        st.warning(str(e))
        etas, pos, near = [], [], []
    eta = {}
    for e in etas:
        if e.get("Direction", 0) == sub.get("Direction", 0):
            eta.setdefault(e.get("StopUID"), e)
    for e in etas:                                 # 子路線完全一樣的優先
        if _same_sub(e, sub) and e.get("SubRouteUID"):
            eta[e.get("StopUID")] = e
    at = {n.get("StopUID"): (n.get("PlateNumb", ""), n.get("A2EventType")) for n in near if _same_sub(n, sub)}
    buses = [b for b in pos if _same_sub(b, sub) and (b.get("BusPosition") or {}).get("PositionLat")]
    hl = st.session_state.get(f"bus_stop_{city}_{route}_{si}")
    try:
        shapes = c_bus_shape(cid, secret, city, route)
    except Exception:  # noqa: BLE001
        shapes = []
    shape = next((s for s in shapes if s["sub"] == sub.get("SubRouteUID")), None) or \
        next((s for s in shapes if s["dir"] == sub.get("Direction", 0)), None)
    points = []
    for i, s in enumerate(seq):
        p = s.get("StopPosition") or {}
        if p.get("PositionLat"):
            _, txt = eta_badge(eta.get(s.get("StopUID")))
            name = f"{i + 1}. {T._zh(s.get('StopName'))}　{txt}"
            points.append({"lat": p["PositionLat"], "lon": p["PositionLon"], "name": name,
                           "kind": "hl" if i == hl else "stop", "label": T._zh(s.get("StopName")) if i == hl else ""})
    for b in buses:
        p = b["BusPosition"]
        points.append({"lat": p["PositionLat"], "lon": p["PositionLon"], "kind": "bus", "name": f"🚌 {b.get('PlateNumb', '')}",
                       "label": b.get("PlateNumb", "")})
    show_map([{"path": shape["path"]}] if shape and shape["path"] else [], points, height=360)
    rows = []
    for i, s in enumerate(seq):
        cls, txt = eta_badge(eta.get(s.get("StopUID")))
        bus = at.get(s.get("StopUID"))
        tag = (f"<span class='bus'>🚌 {esc(bus[0])} {'進站中' if bus[1] == 1 else '剛離站'}</span>" if bus else "")
        rows.append(f"<div class='sr{' hl' if i == hl else ''}'><span class='n'>{i + 1}</span>"
                    f"<span class='nm'>{esc(T._zh(s.get('StopName')))}{tag}</span><span class='eta {cls}'>{esc(txt)}</span></div>")
    st.markdown("<div class='stoplist'>" + "".join(rows) + "</div>", unsafe_allow_html=True)
    st.caption(f"{T.tw_now():%H:%M:%S} 更新・路上有 {len(buses)} 台車"
               + ("" if etas else "・目前沒有即時資料（可能還沒發車、末班已過或今天未營運）"))


def bus_live(city, route, subs, si):
    auto = st.toggle(f"自動更新（每 {LIVE_EVERY} 秒）", key=f"live_auto_{city}_{route}",
                     help="只更新這一塊，不會整頁重跑。TDX 免費金鑰每分鐘只能查 5 次，所以不能更新得更頻繁")
    st.fragment(run_every=LIVE_EVERY if auto else None)(bus_live_body)(city, route, subs, si)


# ─── YouBike ───
def _bike_name(s):
    return re.sub(r"^YouBike\d(?:\.\d)?_", "", T._zh(s.get("StationName")))


@st.cache_data(ttl=86400, show_spinner=False)
def c_bike_stations(cid, secret, lat, lon, radius):
    j = T.tdx_get(token_for(cid, secret), "/advanced/v2/Bike/Station/NearBy", wait=70,
                  **{"$spatialFilter": f"nearby({lat}, {lon}, {int(radius)})"})
    out = []
    for s in j:
        p = s.get("StationPosition") or {}
        if p.get("PositionLat"):
            out.append({"uid": s["StationUID"], "name": _bike_name(s), "lat": p["PositionLat"], "lon": p["PositionLon"],
                        "auth": s.get("AuthorityID", ""), "addr": T._zh(s.get("StationAddress"))})
    return out


@st.cache_data(ttl=60, show_spinner=False)
def c_bike_avail_near(cid, secret, lat, lon, radius):
    j = T.tdx_get(token_for(cid, secret), "/advanced/v2/Bike/Availability/NearBy", wait=70,
                  **{"$spatialFilter": f"nearby({lat}, {lon}, {int(radius)})"})
    return {a["StationUID"]: a for a in j}


@st.cache_data(ttl=60, show_spinner=False)
def c_bike_avail_city(cid, secret, city, uids):
    f = " or ".join(f"StationUID eq '{u}'" for u in uids)
    j = T.tdx_get(token_for(cid, secret), f"/v2/Bike/Availability/City/{city}", wait=70, **{"$filter": f})
    return {a["StationUID"]: a for a in j}


def _bike_merge(s, a):
    d = (a or {}).get("AvailableRentBikesDetail") or {}
    rent = (a or {}).get("AvailableRentBikes")
    return dict(s, gen=d.get("GeneralBikes", rent), ebike=d.get("ElectricBikes", 0), ret=(a or {}).get("AvailableReturnBikes"),
                ok=(a or {}).get("ServiceStatus", 1) == 1, known=bool(a))


def bikes_near(lat, lon, radius):
    """附近 YouBike：[{uid, name, lat, lon, gen 一般車, ebike 電輔車, ret 可還, dist}]，由近到遠。查 2 次。"""
    av = c_bike_avail_near(cid, secret, lat, lon, radius)
    out = []
    for s in c_bike_stations(cid, secret, lat, lon, radius):
        b = _bike_merge(s, av.get(s["uid"]))
        b["dist"] = round(T.meters((lat, lon), (s["lat"], s["lon"])))
        out.append(b)
    return sorted(out, key=lambda x: x["dist"])


def bike_html(b, extra=""):
    if not b.get("known"):
        body = "<div class='sub'>沒有即時車位資料</div>"
    elif not b.get("ok"):
        body = "<div class='sub'>暫停營運</div>"
    else:
        g, e, r = b.get("gen") or 0, b.get("ebike") or 0, b.get("ret") or 0
        tot = max(g + e + r, 1)
        body = (f"<div class='bkn'><span>可借一般 <b>{g}</b></span><span>可借電輔 <b>{e}</b></span><span>可還 <b>{r}</b></span></div>"
                f"<div class='bkbar'><i style='width:{g / tot * 100:.0f}%;background:#c08a2b'></i>"
                f"<i style='width:{e / tot * 100:.0f}%;background:#ee6c2b'></i>"
                f"<i style='width:{r / tot * 100:.0f}%;background:rgba(128,128,128,.25)'></i></div>")
    dist = f"<span class='sub'>　{b['dist']} 公尺</span>" if b.get("dist") is not None else ""
    return f"<div class='bk'><div><b>🚲 {esc(b['name'])}</b>{dist}{extra}</div>{body}</div>"


def bike_points(bikes):
    return [{"lat": b["lat"], "lon": b["lon"], "kind": "bike" if (b.get("gen") or 0) + (b.get("ebike") or 0) else "bike_empty",
             "name": f"🚲 {b['name']}　一般 {b.get('gen') or 0}・電輔 {b.get('ebike') or 0}・可還 {b.get('ret') or 0}",
             "label": str((b.get("gen") or 0) + (b.get("ebike") or 0)) if b.get("known") else ""}
            for b in bikes]


# ─── 捷運 ───
@st.cache_data(ttl=86400, show_spinner=False)
def c_metro_stations(cid, secret, op):
    """捷運車站：[(站名, [車站代碼...], 緯度, 經度)]；轉乘站（同名不同線）合成一個。"""
    by = {}
    for s in T.tdx_get(token_for(cid, secret), f"/v2/Rail/Metro/Station/{op}", wait=70):
        n = T._zh(s.get("StationName"))
        p = s.get("StationPosition") or {}
        e = by.setdefault(n, [n, [], p.get("PositionLat"), p.get("PositionLon")])
        e[1].append(s["StationID"])
    return sorted(by.values(), key=lambda x: x[1][0])


@st.cache_data(ttl=20, show_spinner=False)
def c_metro_board(cid, secret, op):
    return T.tdx_get(token_for(cid, secret), f"/v2/Rail/Metro/LiveBoard/{op}", wait=70)


def metro_rows(op, board, ids):
    rows = []
    for x in board:
        if x.get("StationID") not in ids:
            continue
        dest = T._zh(x.get("DestinationStationName")) or T._zh(x.get("TripHeadSign"))
        dest = dest if dest.startswith(("往", "逆", "順")) else f"往{dest}"
        t, ss = x.get("EstimateTime"), x.get("ServiceStatus", 0)
        txt = ("進站中" if t == 0 else f"{t} 分") if ss == 0 and t is not None else "—"
        line = x.get("LineID") or x.get("LineNO") or ""
        rows.append((line, T._zh(x.get("LineName")), dest, t if ss == 0 and t is not None else 999, txt))
    return sorted(rows, key=lambda r: (r[0], r[2]))


def metro_html(op, rows):
    if not rows:
        return "<div class='sub'>目前沒有即時資料（可能還沒營運或末班已過）</div>"
    out = []
    for line, lname, dest, t, txt in rows:
        c = LINE_COLORS.get((op, line), "#888")
        out.append(f"<div class='sr'><span class='dot' style='background:{c}'></span><span class='nm'>{esc(dest)}"
                   f"<span class='sub'>　{esc(lname)}</span></span><span class='eta {'now' if t == 0 else ''}'>{esc(txt)}</span></div>")
    return "<div class='stoplist' style='max-height:none'>" + "".join(out) + "</div>"


# ─── 台鐵：車站看板（誤點）、單一車次 ───
@st.cache_data(ttl=30, show_spinner=False)
def c_tra_board(cid, secret, station_id):
    j = T.tdx_get(token_for(cid, secret), f"/v3/Rail/TRA/StationLiveBoard/Station/{station_id}", wait=70)
    return {x.get("TrainNo"): x.get("DelayTime", 0) for x in j.get("StationLiveBoards") or []}


@st.cache_data(ttl=3600, show_spinner=False)
def c_tra_train(cid, secret, no):
    j = T.tdx_get(token_for(cid, secret), f"/v3/Rail/TRA/DailyTrainTimetable/Today/TrainNo/{no}", wait=70)
    t = (j.get("TrainTimetables") or [{}])[0]
    return [(s.get("StationID"), T._zh(s.get("StationName")), (s.get("ArrivalTime") or "")[:5],
             (s.get("DepartureTime") or "")[:5]) for s in t.get("StopTimes") or []]


@st.cache_data(ttl=30, show_spinner=False)
def c_tra_live(cid, secret, no):
    j = T.tdx_get(token_for(cid, secret), "/v3/Rail/TRA/TrainLiveBoard", wait=70, **{"$filter": f"TrainNo eq '{no}'"})
    x = (j.get("TrainLiveBoards") or [None])[0]
    return None if not x else {"站": x.get("StationID"), "狀態": x.get("TrainStationStatus"), "誤點": x.get("DelayTime", 0)}


@st.cache_data(ttl=86400, show_spinner=False)
def c_rail_pos(cid, secret, kind):
    """車站位置 {代碼: (緯度, 經度)}，畫地圖用。"""
    if kind == "tra":
        j = T.tdx_get(token_for(cid, secret), "/v3/Rail/TRA/Station", wait=70, **{"$select": "StationID,StationPosition"})
        j = j.get("Stations") or []
    else:
        j = T.tdx_get(token_for(cid, secret), "/v2/Rail/THSR/Station", wait=70, **{"$select": "StationID,StationPosition"})
    return {s["StationID"]: ((s.get("StationPosition") or {}).get("PositionLat"), (s.get("StationPosition") or {}).get("PositionLon"))
            for s in j}


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def c_thsr_day(cid, secret, day_iso):
    """高鐵當天每一班車的停靠站：{車次: [(代碼, 站名, 到, 開)...]}。查 1 次。"""
    out = {}
    for x in T.tdx_get(token_for(cid, secret), f"/v2/Rail/THSR/DailyTimetable/TrainDate/{day_iso}", wait=70):
        no = (x.get("DailyTrainInfo") or {}).get("TrainNo")
        out[no] = [(s.get("StationID"), T._zh(s.get("StationName")), (s.get("ArrivalTime") or "")[:5],
                    (s.get("DepartureTime") or "")[:5]) for s in x.get("StopTimes") or []]
    return out


def delay_text(d):
    return "" if d is None else ("準點" if not d else f"晚 {d} 分")


def train_view(kind, no, day_iso):
    """一班車的沿途停靠站、現在到哪一站（台鐵用即時位置；高鐵依時刻推算）＋地圖。"""
    today = day_iso == T.tw_now().date().isoformat()
    try:
        stops = c_tra_train(cid, secret, no) if kind == "tra" else c_thsr_day(cid, secret, day_iso).get(no, [])
        pos = c_rail_pos(cid, secret, kind)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    if not stops:
        st.caption("查不到這班車的停靠站。")
        return
    cur, note = None, ""
    if kind == "tra" and today:
        try:
            live = c_tra_live(cid, secret, no)
        except Exception:  # noqa: BLE001
            live = None
        if live:
            cur = next((i for i, s in enumerate(stops) if s[0] == live["站"]), None)
            status = {0: "進站中", 1: "在站上", 2: "已離站"}.get(live["狀態"], "")
            note = f"🚆 現在：{stops[cur][1] if cur is not None else live['站']} {status}・{delay_text(live['誤點'])}"
        else:
            note = "目前沒有這班車的即時位置（可能還沒發車或已經到站）"
    elif today:                       # 高鐵沒有即時位置：依時刻推算
        now = f"{T.tw_now():%H:%M}"
        passed = [i for i, s in enumerate(stops) if (s[3] or s[2]) and (s[3] or s[2]) <= now]
        if passed and passed[-1] < len(stops) - 1:
            cur = passed[-1]
            note = f"🚄 依時刻推算：已過 {stops[cur][1]}，下一站 {stops[cur + 1][1]}"
        elif passed:
            note = "這班車已經到終點了"
        else:
            note = f"還沒發車（{stops[0][3] or stops[0][2]} 從{stops[0][1]}出發）"
    if note:
        st.markdown(f"**{esc(note)}**")
    rows, path, points = [], [], []
    for i, (sid, name, arr, dep) in enumerate(stops):
        cls = " hl" if i == cur else (" past" if cur is not None and i < cur else "")
        t = f"到 {arr}　開 {dep}" if arr and dep and arr != dep else (dep or arr)
        rows.append(f"<div class='sr{cls}'><span class='n'>{i + 1}</span><span class='nm'>{esc(name)}</span>"
                    f"<span class='eta' style='font-weight:600'>{esc(t)}</span></div>")
        ll = pos.get(sid)
        if ll and ll[0]:
            path.append([ll[1], ll[0]])
            points.append({"lat": ll[0], "lon": ll[1], "name": f"{name} {t}", "kind": "train" if i == cur else "station",
                           "label": name if i in (0, len(stops) - 1, cur) else ""})
    show_map([{"path": path}] if len(path) > 1 else [], points, height=300)
    st.markdown("<div class='stoplist'>" + "".join(rows) + "</div>", unsafe_allow_html=True)


# ─── ★ 常用：存在手機瀏覽器（localStorage），換手機或清除瀏覽資料才會不見 ───
def fav_sync():
    """每次執行一開始呼叫：第一次從瀏覽器讀常用清單；有改過就寫回去。"""
    if streamlit_js_eval is None:
        st.session_state.setdefault("fav", [])
        return
    pend = st.session_state.pop("fav_pending", None)
    if pend is not None:
        n = st.session_state["fav_n"] = st.session_state.get("fav_n", 0) + 1
        streamlit_js_eval(js_expressions=f"localStorage.setItem('{FAV_KEY}', {json.dumps(json.dumps(pend, ensure_ascii=False))})",
                          key=f"fav_set_{n}")
    if "fav" not in st.session_state:
        v = streamlit_js_eval(js_expressions=f"localStorage.getItem('{FAV_KEY}') || '[]'", key="fav_load")
        if v is not None:
            try:
                st.session_state["fav"] = [x for x in json.loads(v) if isinstance(x, dict)]
            except ValueError:
                st.session_state["fav"] = []


def fav_list():
    return st.session_state.get("fav")


def fav_id(x):
    return "|".join(str(x.get(k, "")) for k in ("t", "city", "route", "sub", "stop", "uid", "op", "name", "kind", "o", "d"))


def fav_add(item):
    favs = list(fav_list() or [])
    if fav_id(item) not in {fav_id(x) for x in favs}:
        favs.append(item)
        st.session_state["fav"] = favs
        st.session_state["fav_pending"] = favs
        st.toast(f"已加入常用：{item.get('label', '')}", icon="⭐")


def fav_remove(fid):
    favs = [x for x in fav_list() or [] if fav_id(x) != fid]
    st.session_state["fav"] = favs
    st.session_state["fav_pending"] = favs


def fav_button(item, key):
    have = fav_id(item) in {fav_id(x) for x in fav_list() or []}
    st.button("★ 已在常用" if have else "☆ 加入常用", key=key, disabled=have, on_click=fav_add, args=(item,))


def _ftile(title, value="", sub="", rows=(), link=None, more="", cls="", extra=""):
    """常用頁的方塊卡片（跟生活助手首頁同一種樣子）；有 link 就整張可以點。"""
    r = "".join(f"<div class='trow'><span>{x}</span><span>{y}</span></div>" for x, y in rows)
    v = f"<div class='tv {cls}'>{value}</div>" if value else ""
    m = f"<div class='tmore'>{more} →</div>" if link and more else ""
    inner = f"<div class='tl'>{title}</div>{v}{r}{extra}<div class='ts'>{sub}</div>{m}"
    return f"<a class='ftile' href='{link}' target='_self'>{inner}</a>" if link else f"<div class='ftile'>{inner}</div>"


def _qs(**kw):
    from urllib.parse import urlencode
    return "?" + urlencode({k: v for k, v in kw.items() if v not in (None, "")})


def fav_tiles(favs):
    """每個常用項目 → 一張卡片的 HTML。公車同一個縣市一次查完、YouBike 同一個縣市一次查完（省 TDX 額度）。"""
    now = T.tw_now()
    now_hm = f"{now:%H:%M}"
    by_city, by_auth = {}, {}
    for x in favs:
        if x.get("t") == "bus":
            by_city.setdefault(x["city"], set()).add(x["stop"])
        elif x.get("t") == "bike":
            by_auth.setdefault(T._ISO_CITY.get(x.get("auth", "")), []).append(x["uid"])
    etas, bike_av, errs = {}, {}, []
    for city, uids in by_city.items():
        try:
            for e in c_stops_eta(cid, secret, city, tuple(sorted(uids))):
                etas.setdefault((e.get("StopUID"), T._zh(e.get("RouteName"))), []).append(e)
        except Exception as ex:  # noqa: BLE001
            errs.append(str(ex))
    for city, uids in by_auth.items():
        if city:
            try:
                bike_av.update(c_bike_avail_city(cid, secret, city, tuple(sorted(uids))))
            except Exception as ex:  # noqa: BLE001
                errs.append(str(ex))
    tiles = []
    for x in favs:
        t = x.get("t")
        if t == "bus":
            cands = etas.get((x["stop"], x["route"]), [])
            e = next((c for c in cands if c.get("SubRouteUID") == x.get("sub")), None) or \
                next((c for c in cands if c.get("Direction", 0) == x.get("dir", 0)), None)
            cls, txt = eta_badge(e)
            tiles.append(_ftile(f"🚌 {esc(x['route'])} <span class='tsub'>往 {esc(x.get('to', ''))}</span>", esc(txt),
                                f"🚏 {esc(x['name'])}",
                                link=_qs(p="bus", city=x["city"], route=x["route"], sub=x.get("sub"), stop=x["stop"]),
                                more="路線動態", cls=cls))
        elif t == "bike":
            b = _bike_merge(x, bike_av.get(x["uid"]))
            if b.get("known") and b.get("ok"):
                g, e_, r_ = b.get("gen") or 0, b.get("ebike") or 0, b.get("ret") or 0
                tot = max(g + e_ + r_, 1)
                bar = (f"<div class='bkbar'><i style='width:{g / tot * 100:.0f}%;background:#c08a2b'></i>"
                       f"<i style='width:{e_ / tot * 100:.0f}%;background:#ee6c2b'></i>"
                       f"<i style='width:{r_ / tot * 100:.0f}%;background:rgba(128,128,128,.25)'></i></div>")
                tiles.append(_ftile(f"🚲 {esc(b['name'])}", f"{g + e_} <small>台可借</small>", "",
                                    [("一般車", g), ("電輔車", e_), ("可還", r_)], extra=bar,
                                    cls="" if g + e_ else "mute"))
            else:
                tiles.append(_ftile(f"🚲 {esc(b['name'])}", "—", "暫停營運" if b.get("known") else "沒有即時資料", cls="mute"))
        elif t == "metro":
            try:
                rows = metro_rows(x["op"], c_metro_board(cid, secret, x["op"]), x["ids"])
            except Exception as ex:  # noqa: BLE001
                rows = []
                errs.append(str(ex))
            rr = [(f"<span class='dot' style='background:{LINE_COLORS.get((x['op'], ln), '#888')}'></span> {esc(dest)}",
                   f"<b class='{'now' if tt == 0 else ''}'>{esc(txt)}</b>") for ln, _, dest, tt, txt in rows[:5]]
            tiles.append(_ftile(f"🚇 {esc(x['name'])} <span class='tsub'>{esc(x.get('opname', ''))}</span>", "",
                                "" if rr else "目前沒有即時資料", rr,
                                link=_qs(p="metro", op=x["op"], st=x["name"]), more="捷運"))
        elif t == "rail":
            kind = x["kind"]
            try:
                rows = [r for r in c_rail(kind, cid, secret, x["o"], x["d"], now.date().isoformat()) if r["出發"] >= now_hm]
            except Exception as ex:  # noqa: BLE001
                rows = []
                errs.append(str(ex))
            icon = "🚆" if kind == "tra" else "🚄"
            title = f"{icon} {esc(x['on'])} → {esc(x['dn'])}"
            link = _qs(p="tra" if kind == "tra" else "hsr", o=x["on"], d=x["dn"])
            if not rows:
                tiles.append(_ftile(title, "今天沒有了", "明天再查", link=link, more="時刻表", cls="mute"))
                continue
            n = rows[0]
            dl = ""
            if kind == "tra":
                try:
                    d = c_tra_board(cid, secret, x["o"]).get(n["車次"])
                    dl = "" if d is None else (" <b class='ok'>準點</b>" if not d else f" <b class='now'>晚 {d} 分</b>")
                except Exception:  # noqa: BLE001
                    dl = ""
            ttag = f"<span class='tag' style='{_train_css(n.get('車種', ''))}'>{esc(n['車種'])}</span>" if n.get("車種") else ""
            more_rows = [(f"{r['出發']} → {r['抵達']}", f"{esc(r.get('車種', ''))} {esc(r['車次'])}") for r in rows[1:3]]
            tiles.append(_ftile(title, n["出發"], f"{ttag}{esc(n['車次'])} 次・{n['抵達']} 到{dl}", more_rows,
                                link=link, more="時刻表"))
    return tiles, errs


def fav_body():
    favs = fav_list() or []
    tiles, errs = fav_tiles(favs)
    st.markdown("<div class='fgrid'>" + "".join(tiles) + "</div>", unsafe_allow_html=True)
    for e in dict.fromkeys(errs):
        st.caption(f"⚠️ {e}")
    st.caption(f"{T.tw_now():%H:%M:%S} 更新・點卡片可以看詳細")


def fav_page():
    favs = fav_list()
    if favs is None:
        st.caption("讀取常用清單…")
        return
    if not favs:
        st.markdown("**還沒有常用的站，點下面的方塊去加：**")
        add = [("🚌", "加公車", "查路線 → 選上車站<br>→ ☆ 加入常用", "bus"),
               ("🚆", "加台鐵", "查時刻表<br>→ ☆ 加入常用", "tra"),
               ("🚄", "加高鐵", "查時刻表<br>→ ☆ 加入常用", "hsr"),
               ("🚇", "加捷運", "選車站<br>→ ☆ 加入常用", "metro"),
               ("🚲", "加 YouBike", "找附近站<br>→ ☆ 加入常用", "bike")]
        st.markdown("<div class='fgrid addg'>" + "".join(
            f"<a class='ftile add' href='?p={pg}' target='_self'><div class='tv'>{ic} ＋</div>"
            f"<div class='tl'>{t}</div><div class='ts'>{d}</div></a>" for ic, t, d, pg in add) + "</div>",
            unsafe_allow_html=True)
        st.caption("加好之後，打開 app 就會先看到這一頁，直接顯示下一班幾分到。"
                   + ("" if streamlit_js_eval else "　⚠ 沒有安裝 streamlit_js_eval，常用清單關掉網頁就會不見。"))
        return
    c1, c2 = st.columns(2)
    auto = c1.toggle(f"自動更新（每 {LIVE_EVERY} 秒）", value=True, key="fav_auto")
    edit = c2.toggle("✏️ 編輯常用", key="fav_edit")
    if edit:
        for x in favs:
            a, b_ = st.columns([8, 1], vertical_alignment="center")
            a.markdown(esc(x.get("label") or x.get("name") or ""))
            if b_.button("✕", key=f"fav_rm_{fav_id(x)}", help="從常用移除"):
                fav_remove(fav_id(x))
                st.rerun()
        st.divider()
    st.fragment(run_every=LIVE_EVERY if auto else None)(fav_body)()
    st.caption("常用清單存在這支手機的瀏覽器裡（換手機或清除瀏覽資料才會不見）。")


# ─── 地點輸入（YouBike 頁用；附近公車有自己的） ───
def place_input(key):
    if st.toggle("📍 用目前位置(GPS)", key=f"{key}_gps"):
        return my_location("所在位置")
    q = st.text_input("我現在在哪", placeholder="例如:台南火車站、成功大學、23.0,120.2", key=f"{key}_q")
    cands = geo(q) if q.strip() else []
    if cands:
        return cands[st.selectbox("地點候選", range(len(cands)), format_func=lambda i: cands[i]["name"], key=f"{key}_pick")
                     if len(cands) > 1 else 0]
    if q.strip():
        st.warning("找不到這個地點,請換個說法(例如加上縣市)或直接輸入「緯度,經度」。")
    return None


def bike_page():
    st.markdown("#### 🚲 附近 YouBike")
    here = place_input("bike")
    c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
    radius = c1.select_slider("找多遠的站", [300, 500, 800, 1200], value=500, key="bike_r",
                              format_func=lambda m: f"{m} 公尺")
    if c2.button("🔎 查 YouBike", type="primary", key="bike_go", disabled=not here):
        st.session_state["bike_query"] = (here["name"], round(here["lat"], 5), round(here["lon"], 5), radius)
    q = st.session_state.get("bike_query")
    if not q:
        st.caption("輸入位置（或開 GPS）後按「查 YouBike」：會列出附近站點可借幾台一般車、電輔車，還有幾個空位可以還。")
        return
    name, lat, lon, rad = q
    try:
        with st.spinner("查詢附近 YouBike…"):
            bikes = bikes_near(lat, lon, rad)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    st.markdown(f"**{esc(name)}** 附近 {rad} 公尺・{len(bikes)} 個站・{T.tw_now():%H:%M} 查詢")
    if not bikes:
        st.warning("附近沒有 YouBike 站，可以把範圍調大一點。")
        return
    show_map([], [{"lat": lat, "lon": lon, "kind": "me", "name": f"📍 {name}"}] + bike_points(bikes), height=320)
    for k, b in enumerate(bikes[:15]):
        c1, c2 = st.columns([5, 2], vertical_alignment="center")
        c1.markdown(bike_html(b), unsafe_allow_html=True)
        with c2:
            fav_button({"t": "bike", "uid": b["uid"], "name": b["name"], "lat": b["lat"], "lon": b["lon"],
                        "auth": b["auth"], "label": f"🚲 {b['name']}"}, key=f"bike_fav_{b['uid']}")
    st.caption("車位每分鐘更新。資料來源：交通部 TDX。")


def metro_page():
    st.markdown("#### 🚇 捷運即時到站")
    names = list(METRO)
    opname = st.segmented_control("系統", names, default=st.session_state.get("metro_op_last", names[0]), key="metro_op") or names[0]
    st.session_state["metro_op_last"] = opname
    op = METRO[opname]
    try:
        with st.spinner("載入車站…"):
            stations = c_metro_stations(cid, secret, op)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    labels = [f"{s[0]}（{'/'.join(s[1])}）" for s in stations]
    pre = st.session_state.pop("_pre_metro", None)
    if pre and pre[0] == op:
        i = next((i for i, s_ in enumerate(stations) if s_[0] == pre[1]), None)
        if i is not None:
            st.session_state[f"metro_st_{op}"] = i
    k = st.selectbox("車站（可以直接打字搜尋）", range(len(stations)), index=None, format_func=lambda i: labels[i],
                     placeholder="例如:美麗島、台北車站", key=f"metro_st_{op}")
    if k is None:
        st.caption("選車站後會顯示各方向下一班還有幾分到。台中捷運目前 TDX 沒有即時資料。")
        return
    sname, ids, lat, lon = stations[k]
    fav_button({"t": "metro", "op": op, "opname": opname, "ids": ids, "name": sname, "label": f"🚇 {sname}"},
               key=f"metro_fav_{op}_{sname}")
    auto = st.toggle("自動更新（每 30 秒）", value=True, key="metro_auto")

    def body():
        try:
            rows = metro_rows(op, c_metro_board(cid, secret, op), ids)
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            return
        st.markdown(metro_html(op, rows), unsafe_allow_html=True)
        st.caption(f"{T.tw_now():%H:%M:%S} 更新")
    st.fragment(run_every=30 if auto else None)(body)()
    if lat:
        show_map([], [{"lat": lat, "lon": lon, "kind": "station", "name": sname, "label": sname}], height=240)


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

# 分頁：只跑目前看的那一頁（st.tabs 會每一頁都跑，即時資料會白白用掉 TDX 額度）。網址記住目前頁面，加到主畫面就直接開那一頁
fav_sync()
PAGES = {"★ 常用": "fav", "🚌 公車": "bus", "🧭 路線規劃": "plan", "🚆 台鐵": "tra", "🚄 高鐵": "hsr",
         "🚇 捷運": "metro", "🚲 YouBike": "bike"}
_codes = {v: k for k, v in PAGES.items()}
_p0 = _codes.get(st.query_params.get("p", ""), "★ 常用")
page = st.segmented_control("功能", list(PAGES), default=_p0, key="page", label_visibility="collapsed") or _p0
if st.query_params.get("p") != PAGES[page]:
    st.query_params["p"] = PAGES[page]


def apply_link_params():
    """從常用卡片點進來時，網址會帶路線／車站：先放進對應元件的狀態，畫面就直接選好。"""
    qp, ss = st.query_params, st.session_state
    if qp.get("city") and qp.get("route"):
        code = qp["city"]
        ss["bus_city_name"] = {v: k for k, v in T.BUS_CITIES.items()}.get(code, ss.get("bus_city_name", "臺南市"))
        ss["_pre_route"] = (code, qp["route"])
        ss["_pre_sub"], ss["_pre_stop"] = qp.get("sub"), qp.get("stop")
    if qp.get("op") and qp.get("st"):
        ss["metro_op_last"] = {v: k for k, v in METRO.items()}.get(qp["op"], ss.get("metro_op_last"))
        ss.pop("metro_op", None)
        ss["_pre_metro"] = (qp["op"], qp["st"])
    if qp.get("o") and qp.get("d"):
        kind = "thsr" if qp.get("p") == "hsr" else "tra"
        for k, v in ((f"{kind}_o", qp["o"]), (f"{kind}_d", qp["d"])):
            ss[k] = ss[f"{k}_txt"] = v
            ss[f"{k}_v"] = ss.get(f"{k}_v", 0) + 1     # 輸入框換新的 key，才會顯示帶進來的站名
        ss[f"_pre_{kind}_go"] = True
    for k in ("city", "route", "sub", "stop", "op", "st", "o", "d"):
        if k in qp:
            del qp[k]


apply_link_params()

if page == "🧭 路線規劃":
    # ── 起終點 ──
    use_gps = st.toggle("📍 使用目前位置當起點(GPS)")
    origin = None
    if use_gps:
        origin = my_location("起點")
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
            kw = (tuple(modes), T.PREFS[pref], xfer, (T.FIRST_MILE[fm], walk), (0, walk))
            try:
                with st.spinner("規劃路線中…"):
                    best, others = planned(cid, secret, o, d, dep.replace(tzinfo=None).isoformat(), *kw)
            except Exception as e:  # noqa: BLE001
                st.session_state.pop("plan", None)
                st.error(str(e))
                st.link_button("改用 Google 地圖查看 ↗", T.maps_link(origin["name"], dest["name"], o, d))
            else:   # 存起來:之後按卡片上的按鈕(畫面重跑)結果才不會消失
                st.session_state["plan"] = {"best": best, "others": others, "nxt": None, "origin": origin, "dest": dest,
                                            "o": o, "d": d, "kw": kw}
    if st.session_state.get("plan"):
        show_plan(st.session_state["plan"])
    elif not go:
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
# ── 時刻表分頁(台鐵 / 高鐵 / 公車)──
@st.cache_data(ttl=86400, show_spinner=False)
def c_bus_routes(cid, secret, city):
    return T.bus_routes(token_for(cid, secret), city)


@st.cache_data(ttl=1800, show_spinner=False)
def c_bus_schedule(cid, secret, city, route, day_iso):
    return T.bus_schedule(token_for(cid, secret), city, route, datetime.fromisoformat(day_iso).date())


@st.cache_data(ttl=30, show_spinner=False)
def c_bus_eta(cid, secret, city, route):
    return T.bus_eta(token_for(cid, secret), city, route)


def _swap(a, b):
    st.session_state[a], st.session_state[b] = st.session_state[b], st.session_state[a]


def _after_filter(day, key):
    """「幾點以後」:今天預設現在時間,其他日期預設 00:00。"""
    now = T.tw_now()
    t = st.time_input("幾點以後的班次", dtime(now.hour, now.minute) if day == now.date() else dtime(0, 0), key=f"{key}_{day}")
    return f"{t:%H:%M}"


_RAIL_ALIAS = {"北車": "台北", "南車": "台南", "高車": "高雄", "中車": "台中"}
# 車種顏色（底色, 字色）：跟台鐵 app 一樣，一眼分得出快車慢車
TRAIN_COLORS = {"普悠瑪": ("#FDE2E2", "#B91C1C"), "太魯閣": ("#FDE2E2", "#B91C1C"), "自強": ("#FFE4CC", "#B45309"),
                "莒光": ("#FFF4BF", "#8A5A00"), "區間快": ("#D7F0FF", "#0369A1"), "區間": ("#E3E8FF", "#3730A3"),
                "復興": ("#E8F5E9", "#2E7D32"), "普快": ("#EEEEEE", "#424242")}


def _train_css(name):
    c = next((v for k, v in TRAIN_COLORS.items() if str(name).startswith(k)), None)
    return f"background:{c[0]};color:{c[1]};font-weight:700" if c else ""


def _norm_station(t):
    """打的站名 → 標準寫法：去空白、臺→台、去掉結尾的「站」（「台北車站」→ 台北）、常見簡稱。"""
    t = re.sub(r"\s+", "", str(t or "")).replace("臺", "台")
    t = re.sub(r"(車站|站)$", "", t) if len(t) > 2 else t
    return _RAIL_ALIAS.get(t, t)


try:
    from st_keyup import st_keyup          # 每打一個字就更新（不用按完成），候選站邊打邊出現
except ImportError:
    st_keyup = None


def _bump(key):
    """輸入框換一個新的 key 重新建立，裡面的字才會換成程式設定的內容（選了候選站、按了 ⇄）。"""
    st.session_state[f"{key}_v"] = st.session_state.get(f"{key}_v", 0) + 1


def _swap_station(kind):
    """⇄：起訖站對調（選好的站和輸入框的字一起換）。"""
    for a, b in ((f"{kind}_o", f"{kind}_d"), (f"{kind}_o_txt", f"{kind}_d_txt")):
        st.session_state[a], st.session_state[b] = st.session_state.get(b), st.session_state.get(a)
    for k in (f"{kind}_o", f"{kind}_d"):
        st.session_state.pop(f"{k}_pick", None)
        _bump(k)


def _pick_station(key):
    pick = st.session_state.get(f"{key}_pick")
    if pick:
        st.session_state[key] = pick
        st.session_state[f"{key}_txt"] = pick
        _bump(key)


def station_input(col, label, names, key):
    """直接打站名（不用在下拉選單裡找）：完全一樣就選定；只打一部分就列出候選，點一下選。回傳站名或 None。"""
    txt_key = f"{key}_txt"
    if txt_key not in st.session_state:
        st.session_state[txt_key] = st.session_state.get(key, "")
    if st_keyup:
        with col:
            typed = st_keyup(label, value=st.session_state[txt_key], key=f"{key}_ku{st.session_state.get(f'{key}_v', 0)}",
                             debounce=300, placeholder="打站名，例如：台南、左營")
        st.session_state[txt_key] = typed or ""
    else:
        typed = col.text_input(label, key=txt_key, placeholder="打站名，例如：台南、左營",
                               help="打完按鍵盤的「完成」或 Enter（或點旁邊空白處），才會出現候選站")
    t = _norm_station(typed)
    if not t:
        col.caption("打站名，邊打會邊出現候選站" if st_keyup else "打站名後按「完成」")
        return None
    if t in names:
        st.session_state[key] = t
        return t
    cands = [n for n in names if t in n] or [n for n in names if all(ch in n for ch in t)]
    if cands:
        col.pills("是哪一站？", cands[:10], key=f"{key}_pick", on_change=_pick_station, args=(key,),
                  label_visibility="collapsed")
        col.caption("點一下選站" if len(cands) <= 10 else f"有 {len(cands)} 站符合，多打幾個字")
    else:
        col.caption(f"找不到「{typed}」，換個寫法試試")
    return None


def rail_tab(kind, defaults):
    if not (cid and secret):
        st.info("請先在上方設定 TDX 金鑰。")
        return
    try:
        with st.spinner("載入車站清單…"):
            stations = c_stations(kind, cid, secret)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    names = [n for _, n in stations]
    ids = {n: i for i, n in stations}
    ko, kd = f"{kind}_o", f"{kind}_d"
    for k, dv in ((ko, defaults[0]), (kd, defaults[1])):
        if st.session_state.get(k) not in names:
            st.session_state[k] = dv if dv in names else names[0]
    c1, c2, c3 = st.columns([5, 1, 5], vertical_alignment="top")
    on = station_input(c1, "起站", names, ko)
    c2.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    c2.button("⇄", key=f"{kind}_sw", on_click=_swap_station, args=(kind,), help="起訖站對調")
    dn = station_input(c3, "迄站", names, kd)
    d1, d2 = st.columns(2)
    with d1:
        day = st.date_input("日期", T.tw_now().date(), key=f"{kind}_day")
    with d2:
        after = _after_filter(day, f"{kind}_after")
    o, d = (ids[on] if on else None), (ids[dn] if dn else None)
    if st.button("🔍 查詢時刻表", type="primary", key=f"{kind}_go", disabled=not (o and d)) or \
            (st.session_state.pop(f"_pre_{kind}_go", False) and o and d):
        st.session_state[f"{kind}_q"] = (o, d, day.isoformat())
    q = st.session_state.get(f"{kind}_q")
    if not q:
        st.caption("選好起訖站和日期後按「查詢時刻表」。")
        return
    if q[0] == q[1]:
        st.warning("起站和迄站是同一站。")
        return
    try:
        with st.spinner("查詢時刻表…"):
            rows = c_rail(kind, cid, secret, *q)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    names_by_id = {i: n for i, n in stations}
    st.markdown(f"**{names_by_id.get(q[0], q[0])} ➜ {names_by_id.get(q[1], q[1])}**　{q[2]}　共 {len(rows)} 班")
    on_, dn_ = names_by_id.get(q[0], q[0]), names_by_id.get(q[1], q[1])
    fav_button({"t": "rail", "kind": kind, "o": q[0], "d": q[1], "on": on_, "dn": dn_, "name": f"{on_}→{dn_}",
                "label": f"{'🚆' if kind == 'tra' else '🚄'} {on_} → {dn_}"}, key=f"{kind}_fav_{q[0]}_{q[1]}")
    if not rows:
        st.warning("這天這兩站之間沒有直達車次(台鐵可能需要轉車,可改用「路線規劃」分頁)。")
        return
    same = q == (o, d, day.isoformat())
    later = [r for r in rows if r["出發"] >= after] if same else rows
    today = q[2] == T.tw_now().date().isoformat()
    delays, stops_of = {}, {}
    if kind == "tra" and today:          # 台鐵起站的即時看板：快到站的車有沒有誤點
        try:
            delays = c_tra_board(cid, secret, q[0])
        except Exception:  # noqa: BLE001
            delays = {}
    if kind == "thsr":                   # 高鐵當天每班車的停靠站
        try:
            stops_of = c_thsr_day(cid, secret, q[2])
        except Exception:  # noqa: BLE001
            stops_of = {}
    if not same:   # 改了起訖站或日期但還沒重新查:不要拿舊結果當「下一班」
        st.info("起訖站或日期改了,按「查詢時刻表」更新結果。下面是上次查詢的結果。")
    elif later:
        n = later[0]
        kind_txt = f"{n.get('車種', '')} " if n.get("車種") else ""
        dl = delay_text(delays[n["車次"]]) if n["車次"] in delays else ""
        dl_html = f"<span class='tag' style='background:{'#2e7d32' if dl == '準點' else '#c62828'}'>{dl}</span>" if dl else ""
        ttag = f"<span class='tag' style='{_train_css(n.get('車種', ''))}'>{esc(n['車種'])}</span>" if n.get("車種") else ""
        st.markdown(f"<div class='rt best'><span class='tag'>下一班</span>{ttag}{dl_html}"
                    f"<div class='tm'>{n['出發']} ➜ {n['抵達']}</div>"
                    f"<div class='sub'>{esc(n['車次'])} 次・{T.fmt_min(n['行車'])}・往 {esc(n['往'])}"
                    f"{'　' + n['服務'] if n.get('服務') else ''}</div></div>", unsafe_allow_html=True)
    else:
        st.info(f"{after} 以後沒有班次了。")
    df = pd.DataFrame(later or rows)
    df["行車"] = df["行車"].map(T.fmt_min)
    if delays:
        df.insert(1, "誤點", [delay_text(delays[x]) if x in delays else "" for x in df["車次"]])
    if stops_of:
        def mid(no):
            ids = [x[0] for x in stops_of.get(no, [])]
            if q[0] in ids and q[1] in ids:
                i, j = ids.index(q[0]), ids.index(q[1])
                names_ = [x[1] for x in stops_of[no][min(i, j) + 1:max(i, j)]]
                return "、".join(names_) if names_ else "直達"
            return ""
        df["停靠"] = [mid(x) for x in df["車次"]]
    sty = df.style
    if "車種" in df:
        sty = sty.map(lambda v: _train_css(v), subset=["車種"])
    if "誤點" in df:
        sty = sty.map(lambda v: "color:#2e7d32;font-weight:700" if v == "準點" else
                      ("color:#c62828;font-weight:700" if str(v).startswith("晚") else ""), subset=["誤點"])
    st.dataframe(sty, hide_index=True, use_container_width=True, height=min(38 * (len(df) + 1), 520))
    if kind == "tra":
        st.caption("🚲 可帶自行車　♿ 無障礙座位　🍼 哺乳室　🍱 餐車／便當")
    if delays:
        st.caption("「誤點」是起站的即時看板，只有快到站的車才有資料。")
    trains = list(df["車次"])
    pick = st.selectbox("🔎 看某一班車的停靠站和現在到哪", trains, index=None, placeholder="選車次", key=f"{kind}_train")
    if pick:
        if kind == "tra" and not today:
            st.caption("台鐵的車次停靠站只能查今天的。")
        else:
            train_view(kind, pick, q[2])
    st.caption("資料來源:交通部 TDX。實際以台鐵/高鐵公告為準(誤點、停駛請看各公司即時資訊)。")


if page == "🚆 台鐵":
    rail_tab("tra", ("台南", "台北"))

if page == "🚄 高鐵":
    rail_tab("thsr", ("台南", "台北"))

def _leave_text(mins, walk):
    """公車還有 mins 分到站、走到站牌要 walk 分 → 「幾點前出門」。"""
    if mins is None:
        return ""
    if mins < walk:
        return "⚠️ 走過去來不及這班"
    leave = T.tw_now() + pd.Timedelta(minutes=mins - walk - BUFFER_MIN)
    if leave <= T.tw_now():
        return "🏃 現在就出門"
    return f"{leave:%H:%M} 前出門(還有 {mins - walk - BUFFER_MIN} 分)"


def near_section():
    """📍 我附近的公車:定位(GPS 或打地名)→ 附近站牌 → 經過的公車、即時到站、幾點前出門 → 點路線看這站今天的時刻。"""
    st.markdown("#### 📍 我附近的公車")
    here = None
    if st.toggle("📍 用目前位置(GPS)", key="near_gps"):
        here = my_location("所在位置")
    else:
        q = st.text_input("我現在在哪", placeholder="例如:中華醫事科技大學、台南火車站、23.0,120.2", key="near_q")
        cands = geo(q) if q.strip() else []
        if cands:
            here = cands[st.selectbox("地點候選", range(len(cands)), format_func=lambda i: cands[i]["name"], key="near_pick")
                         if len(cands) > 1 else 0]
        elif q.strip():
            st.warning("找不到這個地點,請換個說法(例如加上縣市)或直接輸入「緯度,經度」。")
    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    radius = c1.select_slider("找多遠的站牌", [200, 300, 500, 800], value=300, key="near_r", format_func=lambda m: f"{m} 公尺")
    st.toggle("地圖上也顯示 YouBike", value=True, key="near_bike")
    if c2.button("🔎 查附近公車", type="primary", key="near_go", disabled=not here):
        st.session_state["near"] = (here["name"], round(here["lat"], 5), round(here["lon"], 5), radius)
    c3.button("🔄 更新即時到站", key="near_refresh", disabled="near" not in st.session_state,
              help="即時到站 30 秒內重複按會用同一份資料")
    near = st.session_state.get("near")
    if not near:
        st.caption("輸入你現在的位置(或開 GPS)後按「查附近公車」:會列出附近站牌有哪些公車、還有幾分到、幾點前要出門。")
        return
    name, lat, lon, rad = near
    try:
        with st.spinner("查詢附近站牌和即時到站…(TDX 每分鐘只能查 5 次,第一次查會用掉 3 次,額度不夠時會自動等一下)"):
            stops = c_near_stops(cid, secret, lat, lon, rad)
            routes, etas = [], []
            for city in sorted({s["縣市"] for s in stops}):
                uids = tuple(sorted(s["uid"] for s in stops if s["縣市"] == city))
                routes += c_stop_routes(cid, secret, city, uids)
                etas += c_stops_eta(cid, secret, city, uids)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    groups = T.nearby_board(stops, routes, etas)
    st.markdown(f"**{esc(name)}** 附近 {rad} 公尺・{T.tw_now():%H:%M} 查詢")
    bikes = []
    if st.session_state.get("near_bike", True):
        try:
            with st.spinner("查詢附近 YouBike…"):
                bikes = bikes_near(lat, lon, max(rad, 300))[:8]
        except Exception as e:  # noqa: BLE001
            st.caption(f"YouBike 查不到:{e}")
    seen, pts = set(), [{"lat": lat, "lon": lon, "kind": "me", "name": f"📍 {name}"}]
    for s_ in stops:
        if s_["位置"] not in seen:
            seen.add(s_["位置"])
            pts.append({"lat": s_["位置"][0], "lon": s_["位置"][1], "kind": "stop", "name": f"🚏 {s_['站名']}（{s_['距離']} 公尺）"})
    show_map([], pts + bike_points(bikes), height=300)
    if not groups:
        st.warning(f"{rad} 公尺內沒有公車站牌,可以把範圍調大一點。")
    if not etas:
        st.caption("目前這些站牌都沒有即時到站資料(可能還沒發車、末班已過或今天未營運)。點「今天時刻」可以看全天的時刻表。")
    for gi, g in enumerate(groups):
        with st.container(border=True):
            st.markdown(f"**🚏 {esc(g['站名'])}**　<span class='sub'>{g['距離']} 公尺・走路約 {g['走路']} 分</span>",
                        unsafe_allow_html=True)
            for ri, r in enumerate(g["路線"]):
                a, b, c, d = st.columns([3, 2, 3, 2], vertical_alignment="center")
                a.markdown(f"**🚌 {esc(r['路線'])}** <span class='sub'>往 {esc(r['往'])}</span>"
                           f"<div class='sub' style='font-size:.75rem'>{esc(r['子路線'])}</div>", unsafe_allow_html=True)
                b.markdown(f"**{esc(r['即時'])}**" if r["分鐘"] is not None else f"<span class='sub'>{esc(r['即時'])}</span>",
                           unsafe_allow_html=True)
                c.markdown(esc(_leave_text(r["分鐘"], g["走路"])))
                key = f"near_tt_{gi}_{ri}_{r['uid']}_{r['方向']}"
                if d.button("今天時刻", key=key + "_btn"):
                    st.session_state[key] = not st.session_state.get(key)
                if st.session_state.get(key):
                    try:
                        with st.spinner("查詢這條路線的時刻表…"):
                            sched = c_bus_sched(cid, secret, r["縣市"], r["路線"])
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))
                        continue
                    board = (f"{T.tw_now() + pd.Timedelta(minutes=r['分鐘']):%H:%M}" if r["分鐘"] is not None else None)
                    info = T.board_times(sched, [r["raw"]], r["子路線"], r["往"], g["站名"], None, board, T.tw_now().date())
                    if info and info["時刻"]:
                        st.markdown(f"<div class='sub'>{esc(r['子路線'])}・{esc(g['站名'])} 今天的到站時間</div>", unsafe_allow_html=True)
                        upcoming = [t for t in info["時刻"] if t >= f"{T.tw_now():%H:%M}"]
                        st.markdown(_chips(info["時刻"], upcoming[0] if upcoming else None, f"{T.tw_now():%H:%M}", g["走路"],
                                           info["推估"]), unsafe_allow_html=True)
                        if info["推估"]:
                            st.caption(("用現在的即時到站推算全天時刻。" if board else
                                        "現在沒有即時到站可以對時,全天時刻是用站序粗估的,可能差 10 分鐘以上。") + "以即時到站為準。")
                    elif info and info["班距"]:
                        st.dataframe(pd.DataFrame([{"時段": f"{x}–{y}", "班距": f"每 {lo}–{hi} 分" if lo != hi else f"每 {lo} 分"}
                                                   for x, y, lo, hi in info["班距"]]), hide_index=True, use_container_width=True)
                    else:
                        st.caption("今天沒有這個方向的時刻表資料。")
    if bikes:
        st.markdown("##### 🚲 附近 YouBike")
        st.markdown("".join(bike_html(b) for b in bikes[:4]), unsafe_allow_html=True)
        st.caption("更多 YouBike 站、加入常用:到「🚲 YouBike」頁。")
    st.caption(f"「幾點前出門」= 公車到站時間 − 走到站牌的時間 − 緩衝 {BUFFER_MIN} 分。走路以每分鐘 {T.WALK_M_PER_MIN} 公尺估算。")


def stop_section(city, route):
    """🚏 選方向 + 上車站 → 這站的即時到站、下一班幾點、今天全部到站時間。
    回傳 (sched, subs, 方向索引)，下面的「路線動態」接著用；即時到站跟路線動態共用同一份資料（不多查）。"""
    try:
        with st.spinner("載入站牌清單…"):
            sched, subs = c_bus_raw(cid, secret, city, route)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return None, None, None
    subs = [r for r in subs if r.get("Stops")]
    if not subs:
        st.caption("TDX 沒有這條路線的站牌資料。")
        return None, None, None
    names = [_zh_name(r) for r in subs]
    pre_sub, pre_stop = st.session_state.pop("_pre_sub", None), st.session_state.pop("_pre_stop", None)
    if pre_sub:      # 從常用卡片點進來：方向和上車站直接選好
        i = next((i for i, r in enumerate(subs) if r.get("SubRouteUID") == pre_sub), None)
        if i is not None:
            st.session_state[f"bus_dir_{city}_{route}"] = i
            k = next((k for k, x in enumerate(subs[i]["Stops"]) if x.get("StopUID") == pre_stop), None)
            if k is not None:
                st.session_state[f"bus_stop_{city}_{route}_{i}"] = k
    c1, c2 = st.columns(2)
    si = c1.selectbox("方向", range(len(subs)), format_func=lambda i: names[i], key=f"bus_dir_{city}_{route}")
    sub = subs[si]
    seq = sub["Stops"]
    ki = c2.selectbox("上車站(可以直接打字搜尋)", range(len(seq)), index=None, placeholder="選你要上車的站",
                      format_func=lambda i: f"{i + 1}. {T._zh(seq[i].get('StopName'))}", key=f"bus_stop_{city}_{route}_{si}")
    if ki is None:
        st.caption("選好上車站,就會顯示這站下一班車幾點到,也可以加入常用。")
        return sched, subs, si
    stop = seq[ki]
    stop_name, uid, direction = T._zh(stop.get("StopName")), stop.get("StopUID"), sub.get("Direction", 0)
    now = T.tw_now()
    now_hm = f"{now:%H:%M}"
    mins, live = None, None
    try:
        with st.spinner("查詢即時到站…"):
            etas = c_bus_live(cid, secret, city, route)[0]
        e = next((x for x in etas if x.get("StopUID") == uid and _same_sub(x, sub) and x.get("SubRouteUID")), None) or \
            next((x for x in etas if x.get("StopUID") == uid and x.get("Direction", 0) == direction), None)
        if e:
            mins, live = T.eta_text(e)
    except Exception as ex:  # noqa: BLE001
        st.caption(f"即時到站查不到:{ex}")
    board = f"{now + pd.Timedelta(minutes=mins):%H:%M}" if mins is not None else None
    info = T.board_times(sched, [sub], names[si], "", stop_name, None, board, now.date())
    times = (info or {}).get("時刻") or []
    upcoming = [t for t in times if t >= now_hm]
    approx = bool(info and info["推估"])
    with st.container(border=True):
        st.markdown(f"**🚏 {esc(stop_name)}**　<span class='sub'>{esc(names[si])}・第 {ki + 1} 站</span>", unsafe_allow_html=True)
        if mins is not None:
            st.markdown(f"<div class='tm'>{esc(live)}</div><div class='sub'>即時到站・約 {board} 到</div>", unsafe_allow_html=True)
        elif upcoming:
            left = T._hm_min(upcoming[0]) - T._hm_min(now_hm)
            st.markdown(f"<div class='tm'>下一班{'約 ' if approx else ' '}{upcoming[0]}</div>"
                        f"<div class='sub'>還有 {left} 分{'・' + esc(live) if live else ''}</div>", unsafe_allow_html=True)
        elif times:
            st.markdown("**今天這站的班次都過了。**" + (f" <span class='sub'>{esc(live)}</span>" if live else ""),
                        unsafe_allow_html=True)
        elif live:
            st.markdown(f"**{esc(live)}**")
        if times:
            st.markdown(f"<div class='sub'>今天這站的到站時間{'(推估)' if approx else ''}</div>", unsafe_allow_html=True)
            st.markdown(_chips(times, upcoming[0] if upcoming else None, now_hm, None, approx), unsafe_allow_html=True)
        elif info and info["班距"]:
            st.dataframe(pd.DataFrame([{"時段": f"{a}–{b}", "班距": f"每 {lo}–{hi} 分" if lo != hi else f"每 {lo} 分"}
                                       for a, b, lo, hi in info["班距"]]), hide_index=True, use_container_width=True)
        elif mins is None and not live:
            st.caption("這站今天沒有時刻表資料。")
        if approx:
            st.caption(("用現在的即時到站推算全天時刻。" if board else
                        "這條路線只有起點站發車時間,這站的時間是用站序粗估的,可能差 10 分鐘以上。") + "以即時到站為準。")
        to = re.sub(r"[((].*$", "", names[si].split("→")[-1]).strip() if "→" in names[si] else \
            T._zh((seq[-1] or {}).get("StopName"))
        fav_button({"t": "bus", "city": city, "route": route, "sub": sub.get("SubRouteUID"), "dir": direction,
                    "stop": uid, "name": stop_name, "to": to, "label": f"🚌 {route} {stop_name}"},
                   key=f"bus_fav_{city}_{route}_{si}_{ki}")
    return sched, subs, si


# 縣市公車按鈕（跟台鐵 app 一樣用顏色分縣市）：常用的直接點，其他縣市收在「其他地區」
CITY_BTNS = [("臺北市", "#E53935"), ("新北市", "#D81B60"), ("桃園市", "#B71C1C"), ("臺中市", "#EF6C00"),
             ("臺南市", "#2E7D32"), ("高雄市", "#1E5BB8"), ("公路客運(國道/跨縣市)", "#7B1FA2")]


def _set_city(name):
    st.session_state["bus_city_name"] = name
    st.session_state["bus_city_more"] = False


def _other_city():
    """「其他縣市」選單一改就先記住，按鈕的外框才會在同一次畫面更新時移到「其他地區」。"""
    if st.session_state.get("bus_city_other"):
        st.session_state["bus_city_name"] = st.session_state["bus_city_other"]


def city_picker():
    """回傳選好的縣市（中文名）。按鈕用 CSS 依 key 上色（Streamlit 會在外層加 st-key-<key> 這個 class）。"""
    cur = st.session_state.get("bus_city_name", "臺南市")
    main = [n for n, _ in CITY_BTNS]
    css = []
    for i, (name, color) in enumerate(CITY_BTNS):
        ring = f"box-shadow:0 0 0 2px #fff,0 0 0 4px {color};" if name == cur else "opacity:.88;"
        css.append(f".st-key-city_{i} button{{background:{color}!important;color:#fff!important;border:0!important;"
                   f"border-radius:14px!important;font-weight:700!important;{ring}}}")
    ring = "box-shadow:0 0 0 2px #fff,0 0 0 4px #E0A800;" if cur not in main else "opacity:.9;"
    css.append(f".st-key-city_more button{{background:#F6C21C!important;color:#3B2F00!important;border:0!important;"
               f"border-radius:14px!important;font-weight:700!important;{ring}}}")
    st.markdown("<style>" + "".join(css) + "</style>", unsafe_allow_html=True)
    with st.container(horizontal=True):
        for i, (name, _) in enumerate(CITY_BTNS):
            label = "公路客運" if name.startswith("公路") else name.replace("臺", "台")
            st.button(label, key=f"city_{i}", icon=":material/directions_bus:", on_click=_set_city, args=(name,))
        st.button("其他地區", key="city_more", icon=":material/map:",
                  on_click=lambda: st.session_state.update(bus_city_more=True))
    if cur not in main or st.session_state.get("bus_city_more"):
        rest = [c for c in T.BUS_CITIES if c not in main]
        pick = st.selectbox("其他縣市", rest, index=rest.index(cur) if cur in rest else None, placeholder="選縣市",
                            key="bus_city_other", on_change=_other_city)
        if pick:
            cur = st.session_state["bus_city_name"] = pick
    return cur


def _zh_name(r):
    return T._zh(r.get("SubRouteName")) or T._zh(r.get("RouteName"))


if page == "🚌 公車":
    if not (cid and secret):
        st.info("請先在上方設定 TDX 金鑰。")
    else:
        near_section()
        st.divider()
        st.markdown("#### 🔍 用路線號碼查")
        city_name = city_picker()
        city = T.BUS_CITIES[city_name]
        routes = []
        try:
            with st.spinner("載入路線清單…"):
                routes = c_bus_routes(cid, secret, city)
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
        pre = st.session_state.pop("_pre_route", None)
        if pre and pre[0] == city and pre[1] in routes:   # 從常用卡片點進來：路線直接選好
            st.session_state[f"bus_route_{city}"] = pre[1]
        route = st.selectbox("路線(可以直接打字搜尋)", routes, index=None, placeholder="例如:21、綠幹線、1610", key=f"bus_route_{city}")
        if route:
            sched, subs, si = stop_section(city, route)
            if subs:
                st.markdown("##### 🗺️ 路線動態")
                bus_live(city, route, subs, si)
            day = T.tw_now().date()
            with st.expander("🕘 起點站發車時刻表"):
                try:
                    subs_t = c_bus_schedule(cid, secret, city, route, day.isoformat())
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
                    subs_t = []
                now_hm = f"{T.tw_now():%H:%M}"
                if not subs_t:
                    st.caption("這條路線沒有提供時刻表,請看上面的路線動態。")
                for s_ in subs_t:
                    st.markdown(f"**{esc(s_['名稱'])}**")
                    if s_["發車"]:
                        nxt = [t for t in s_["發車"] if t >= now_hm]
                        st.markdown(("今天接下來的發車:**" + "、".join(nxt[:6]) + "**") if nxt else "今天的班次都已經發車了。")
                        st.caption("全部:" + "、".join(s_["發車"]))
                    elif s_["班距"]:
                        st.dataframe(pd.DataFrame([{"時段": f"{a_}–{b_}", "班距": f"每 {lo}–{hi} 分" if lo != hi else f"每 {lo} 分"}
                                                   for a_, b_, lo, hi in s_["班距"]]), hide_index=True, use_container_width=True)
                    else:
                        st.caption("今天沒有這個方向的班次資料。")
        st.caption("資料來源:交通部 TDX。公車時刻以各客運公司公告為準。")

if page == "★ 常用":
    if cid and secret:
        fav_page()
    else:
        st.info("請先在上方設定 TDX 金鑰。")
if page in ("🚇 捷運", "🚲 YouBike"):
    if not (cid and secret):
        st.info("請先在上方設定 TDX 金鑰。")
    elif page == "🚇 捷運":
        metro_page()
    else:
        bike_page()

st.caption(f"程式版本:{getattr(T, 'VERSION', '未知')}")
st.caption("路線資料來源:交通部 TDX 運輸資料流通服務平台。班次為規劃參考,實際以各運輸業者公告與現場為準。")
