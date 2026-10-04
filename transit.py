# -*- coding: utf-8 -*-
"""🚉 大眾運輸規劃:地點查詢 + TDX 旅運規劃(公車/台鐵/高鐵/捷運轉乘) + 往後 3 趟。"""
import re
from datetime import datetime, timedelta, timezone

import requests

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
    return ("other", "🚏", str(v) if v else "移動")


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
    line = _fp(flat, "transport.name", "transport.shortName", "transport.short_name", "transport.longName", "route_name", "line_name",
               "routeName", "short_name", "train_no", "train_number", "transport.headsign", "headsign", "name") if kind not in ("walk", "wait") else None
    if kind == 3 and not line:
        line = "高鐵"
    st = parse_time(_fp(flat, "departure.time", "departure_time", "start_time", "dep_time", "origin.time", "from.time"))
    en = parse_time(_fp(flat, "arrival.time", "arrival_time", "end_time", "arr_time", "destination.time", "to.time"))
    secs = _fp(flat, "travelSummary.duration", "travel_summary.duration", "summary.duration", "duration", "travel_time")
    mins = round(secs / 60) if isinstance(secs, (int, float)) else (round((en - st).total_seconds() / 60) if st and en else None)
    meters = _fp(flat, "travelSummary.length", "travel_summary.length", "length", "distance")
    place = lambda *p: str(_fp(flat, *p) or "")
    return {"kind": kind, "emoji": emoji, "label": label, "line": str(line or ""), "start": st, "end": en, "minutes": mins,
            "meters": int(meters) if isinstance(meters, (int, float)) else None,
            "headsign": str(_fp(flat, "transport.headsign", "headsign") or "") if kind not in ("walk", "wait") else "",
            "from": place("departure.place.name", "departure.name", "from.name", "from", "origin.name", "origin", "start_stop.name", "start_stop",
                          "from_name", "departure_stop", "start_name", "start_station"),
            "to": place("arrival.place.name", "arrival.name", "to.name", "to", "destination.name", "destination", "end_stop.name", "end_stop",
                        "to_name", "arrival_stop", "end_name", "end_station")}


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
