#!/usr/bin/env python3
"""再生能源官方趨勢資料管線（只採官方原始來源，無需金鑰或 email）。

三類各自獨立抓取，任一類失敗不影響其他類，失敗時保留舊值並記入 failures：
  1. re_share   各國再生能源占總發電量(%)：IRENA PxWeb（動態查目錄找 RE-SHARE 表，
                不寫死 table id）；IRENA 無歐盟彙總，EU 改用 Eurostat nrg_ind_ren
                (nrg_bal=REN_ELC 電力占比)，該 series 以 "src" 欄位標明。
  2. tw_monthly 台灣逐月再生發電：能源署 OPEN API monthly/4/1（再生分項）
                與 monthly/3/2（全國總發電量），需帶 User-Agent。
  3. tw_reserve 台電每日備轉容量率：歷年 CSV（d006005）與當年 CSV（d006002）合併。

輸出 data/renewable_indicators.json。全部三類失敗 exit 1，部分失敗 exit 0 並印警告。
來源資料變少時以 年／月／日 為 key 合併，不刪舊點。

能源署 monthly/4/1 欄位對應（Column 編號，以表頭關鍵字校驗，不符即該類失敗）：
  Column2 合計(再生總量)   Column3 慣常水力 -> hydro   Column5 地熱 -> other
  Column7 太陽光電 -> solar   Column9 風力小計 -> wind
  Column15 生質能小計 -> other   Column21 廢棄物 -> other
  單位為千度(MWh)，換算成億度：1 億度 = 1e8 kWh = 1e5 MWh，故 MWh / 100000。
monthly/3/2 全國發電量：Column2 總計，單位百萬度(GWh)，億度 = GWh / 100。
再生占比 = 再生合計 / 全國總發電量 x 100。
注意：能源署 API 的月資料只含近約 21 個月（現為 2025-01 起），更早只有年表；
更早的月份靠每月執行累積。年表另輸出於 tw_monthly.annual（2007 起）。
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("INDICATORS_OUT") or os.path.join(
    ROOT, "data", "renewable_indicators.json")

BROWSER_UA = "Mozilla/5.0"

IRENA_BASE = os.environ.get("IND_IRENA_BASE") or (
    "https://pxweb.irena.org/api/v1/en/IRENASTAT/Power%20Capacity%20and%20Generation")
IRENA_SRC = "https://pxweb.irena.org/pxweb/en/IRENASTAT/IRENASTAT__Power%20Capacity%20and%20Generation"
TW_BASE = os.environ.get("IND_TW_BASE") or (
    "https://www.esist.org.tw/a0303/02/api/v1/zone")
TW_SRC = "https://www.esist.org.tw/a0303/02/api/pages/database/api"
TP_URL_HIST = os.environ.get("IND_TP_URL1") or (
    "https://service.taipower.com.tw/data/opendata/apply/file/d006005/001.csv")
TP_URL_CUR = os.environ.get("IND_TP_URL2") or (
    "https://service.taipower.com.tw/data/opendata/apply/file/d006002/001.csv")
TP_SRC = "https://service.taipower.com.tw/data/opendata/apply/file/d006005/001.csv"
EU_URL = os.environ.get("IND_EU_URL") or (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_ind_ren"
    "?format=JSON&lang=EN&geo=EU27_2020&nrg_bal=REN_ELC&unit=PC")
EU_SRC = "https://ec.europa.eu/eurostat/databrowser/view/nrg_ind_ren/default/table"

# IRENA 地區代碼 -> (key, 顯示名)
SERIES_DEF = [
    ("GLO", "WORLD", "全球"),
    ("TWN", "TW", "台灣"),
    ("CHN", "CN", "中國"),
    ("USA", "US", "美國"),
    ("JPN", "JP", "日本"),
    ("IND", "IN", "印度"),
]
TW_FIELDS = ["solar", "wind", "hydro", "other"]
TW_LABELS = {"solar": "太陽光電", "wind": "風力", "hydro": "水力", "other": "其他再生"}


def http_get(url, ua=BROWSER_UA, timeout=60, retries=3, data=None, headers=None):
    last = None
    for i in range(retries):
        try:
            h = {"User-Agent": ua}
            if headers:
                h.update(headers)
            req = Request(url, data=data, headers=h)
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # 網路暫時性錯誤
            last = e
        time.sleep(2 * (i + 1))
    raise last


def http_json(url, **kw):
    # strict=False：容忍來源 JSON 內的控制字元
    return json.loads(http_get(url, **kw).decode("utf-8-sig"), strict=False)


def r2(x):
    return None if x is None else round(float(x), 2)


def load_old():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------- re_share
def fetch_irena():
    cat = http_json(IRENA_BASE)
    tid = None
    for t in cat:
        if t.get("type") == "t" and t.get("id", "").startswith("RE-SHARE"):
            tid = t["id"]
            break
    if not tid:
        raise RuntimeError("IRENA 目錄找不到 RE-SHARE 表")
    url = IRENA_BASE + "/" + tid
    meta = http_json(url)
    var = {v["code"]: v for v in meta["variables"]}
    ind = var["Indicator"]
    gen_code = None
    for c, t in zip(ind["values"], ind["valueTexts"]):
        if "generation" in t.lower():
            gen_code = c
    if gen_code is None:
        raise RuntimeError("IRENA 表找不到發電占比 Indicator")
    year_codes = var["Year"]["values"]
    year_txt = {c: int(t) for c, t in zip(year_codes, var["Year"]["valueTexts"])}
    body = json.dumps({"query": [], "response": {"format": "json"}}).encode()
    raw = http_json(url, data=body, headers={"Content-Type": "application/json"})
    want = {code for code, _, _ in SERIES_DEF}
    got = {}
    for row in raw["data"]:
        reg, ic, yc = row["key"]
        if ic != gen_code or reg not in want:
            continue
        v = row["values"][0]
        try:
            got.setdefault(reg, {})[year_txt[yc]] = r2(v)
        except (TypeError, ValueError):
            got.setdefault(reg, {})[year_txt[yc]] = None
    if not got.get("GLO") or not got.get("TWN"):
        raise RuntimeError("IRENA 回應缺全球或台灣")
    return {key: got.get(code, {}) for code, key, _ in SERIES_DEF}, tid


def fetch_eu():
    d = http_json(EU_URL)
    ids, size = d["id"], d["size"]
    tpos = ids.index("time")
    other = 1
    for i, s in enumerate(size):
        if i != tpos:
            other *= s
    if other != 1:
        raise RuntimeError("Eurostat 維度非預期（除 time 外應各為 1）")
    idx = d["dimension"]["time"]["category"]["index"]
    out = {}
    for y, i in idx.items():
        v = d["value"].get(str(i))
        out[int(y)] = r2(v)
    return out


def build_re_share(old):
    irena, tid = fetch_irena()
    try:
        eu = fetch_eu()
        eu_err = None
    except Exception as e:
        eu, eu_err = {}, "%s: %s" % (type(e).__name__, e)
    old_rs = old.get("re_share") or {}
    old_series = {s["key"]: s for s in old_rs.get("series", [])}
    old_years = old_rs.get("years", [])

    def old_map(key):
        s = old_series.get(key)
        return dict(zip(old_years, s["values"])) if s else {}

    maps = {}
    for _, key, _ in SERIES_DEF:
        m = old_map(key)
        for y, v in irena[key].items():
            if v is not None or y not in m:
                m[y] = v
        maps[key] = m
    m = old_map("EU")
    for y, v in eu.items():
        if v is not None or y not in m:
            m[y] = v
    maps["EU"] = m
    # 年份軸：IRENA 全球有值的年份範圍（2025 發電占比尚為空值即自動不列入）
    glo = maps["WORLD"]
    last = max(y for y, v in glo.items() if v is not None)
    first = min(glo)
    years = list(range(first, last + 1))
    series = []
    for _, key, label in SERIES_DEF + [(None, "EU", "歐盟")]:
        s = {"key": key, "label": label, "values": [maps[key].get(y) for y in years]}
        if key == "EU":
            s["src"] = EU_SRC
        series.append(s)
    res = {
        "src": IRENA_SRC,
        "unit": "%",
        "metric": "再生能源占總發電量",
        "years": years,
        "series": series,
    }
    return res, eu_err


# -------------------------------------------------------------- tw_monthly
def _num(x):
    return float(x) if isinstance(x, (int, float)) else None


def _sheet(d, hint):
    for k, v in d.items():
        if hint in k:
            return v
    raise RuntimeError("能源署回應找不到工作表 %s" % hint)


def _check_header(rows, expect):
    """expect: {ColumnN: 表頭關鍵字}，在前 3 列內校驗，防欄位位置改版。"""
    for col, kw in expect.items():
        ok = any(kw in str(r.get(col, "")) for r in rows[:3])
        if not ok:
            raise RuntimeError("能源署表頭校驗失敗 %s 應含 %s" % (col, kw))


def parse_tw():
    d41 = http_json(TW_BASE + "/monthly/4/1")
    d32 = http_json(TW_BASE + "/monthly/3/2")
    r41 = _sheet(d41, "再生能源")
    # 4-01 有「再生能源」與「再生能源 (季)」兩張，取第一張（不含「季」）
    for k, v in d41.items():
        if "再生能源" in k and "季" not in k:
            r41 = v
            break
    r32 = _sheet(d32, "全國")
    _check_header(r41, {"Column2": "合計", "Column3": "水力", "Column5": "地熱",
                        "Column7": "太陽光電", "Column9": "風力",
                        "Column15": "生質能", "Column21": "廢棄物"})
    _check_header(r32, {"Column2": "總計", "Column15": "再生能源"})
    # 3-02 的 Column15 為再生合計，Column17.. 為分項，用 Column15 與 4-01 合計互驗
    per_re = re.compile(r"^(\d{4})/(\d{2})$")
    months, annual = {}, {}
    gen = {}  # 期間 -> 全國總發電量（億度）
    ref_re = {}  # 3-02 再生合計（GWh），用於交叉驗證
    for r in r32:
        p = r.get("Column29")
        t = _num(r.get("Column2"))
        if t is None:
            continue
        if isinstance(p, str) and per_re.match(p):
            gen["%s-%s" % tuple(per_re.match(p).groups())] = t / 100.0
            ref_re["%s-%s" % tuple(per_re.match(p).groups())] = _num(r.get("Column15"))
        elif isinstance(p, int):
            gen[p] = t / 100.0
    for r in r41:
        p = r.get("Column23")
        tot = _num(r.get("Column2"))
        if tot is None:
            continue
        rec = {
            "solar": _num(r.get("Column7")),
            "wind": _num(r.get("Column9")),
            "hydro": _num(r.get("Column3")),
            "other": sum(_num(r.get(c)) or 0 for c in ("Column5", "Column15", "Column21")),
            "re_total": tot,
        }
        if any(rec[k] is None for k in ("solar", "wind", "hydro")):
            continue
        rec = {k: rec[k] / 100000.0 for k in rec}  # MWh -> 億度
        if isinstance(p, str) and per_re.match(p):
            key = "%s-%s" % tuple(per_re.match(p).groups())
            months[key] = rec
        elif isinstance(p, int):
            annual[p] = rec
    if not months:
        raise RuntimeError("能源署 4-01 解析不到月資料")
    # 交叉驗證：4-01 合計（億度）與 3-02 再生合計（GWh/100）應一致
    for k, rec in months.items():
        ref = ref_re.get(k)
        if ref is not None and abs(ref / 100.0 - rec["re_total"]) > 0.01:
            raise RuntimeError("能源署 4-01 與 3-02 再生合計不一致 %s" % k)
    return months, annual, gen


def build_tw_monthly(old):
    months, annual, gen = parse_tw()
    old_tm = old.get("tw_monthly") or {}
    # 以月份為 key 合併舊資料
    store = {}
    om = old_tm.get("months", [])
    osr = {s["key"]: s["values"] for s in old_tm.get("series", [])}
    og = old_tm.get("total_gen", [])
    for i, m in enumerate(om):
        try:
            store[m] = {f: osr[f][i] for f in TW_FIELDS}
            store[m]["total_gen"] = og[i]
        except (KeyError, IndexError):
            pass
    for m, rec in months.items():
        row = {f: r2(rec[f]) for f in TW_FIELDS}
        row["total_gen"] = r2(gen.get(m))
        row["re_total"] = rec["re_total"]
        store[m] = row
    keys = sorted(store)
    series = [{"key": f, "label": TW_LABELS[f], "values": [store[m][f] for m in keys]}
              for f in TW_FIELDS]
    total = [store[m]["total_gen"] for m in keys]
    share = []
    for m in keys:
        row = store[m]
        tg = row["total_gen"]
        re_t = row.get("re_total")
        if re_t is None:  # 舊點沒有 re_total：用分項加總
            re_t = sum(row[f] or 0 for f in TW_FIELDS)
        share.append(r2(re_t / tg * 100) if tg else None)
    res = {
        "src": TW_SRC,
        "unit": "億度",
        "months": keys,
        "series": series,
        "total_gen": total,
        "re_share": share,
    }
    # 年表（2007 起，API 提供）：合併舊年表
    ann = {}
    oa = old_tm.get("annual") or {}
    for i, y in enumerate(oa.get("years", [])):
        ann[y] = {f: s["values"][i] for s in oa["series"] for f in [s["key"]]}
        ann[y]["total_gen"] = oa["total_gen"][i]
        ann[y]["re_share"] = oa["re_share"][i]
    for y, rec in annual.items():
        tg = gen.get(y)
        row = {f: r2(rec[f]) for f in TW_FIELDS}
        row["total_gen"] = r2(tg)
        row["re_share"] = r2(rec["re_total"] / tg * 100) if tg else None
        ann[y] = row
    ys = sorted(ann)
    res["annual"] = {
        "years": ys,
        "series": [{"key": f, "label": TW_LABELS[f], "values": [ann[y][f] for y in ys]}
                   for f in TW_FIELDS],
        "total_gen": [ann[y]["total_gen"] for y in ys],
        "re_share": [ann[y]["re_share"] for y in ys],
    }
    return res


# -------------------------------------------------------------- tw_reserve
def parse_taipower_csv(text):
    """回傳 {日期: (備轉容量率欄值, 自算容量率或 None)}。

    自算 = 備轉容量 / 尖峰負載 x 100（同表原始分量）。當年 CSV（d006002）
    沒有尖峰負載欄，自算為 None。依表頭欄名對應，不靠欄位位置。
    """
    rd = csv.reader(io.StringIO(text))
    head = [h.strip() for h in next(rd)]
    di = next(i for i, h in enumerate(head) if "日期" in h)
    ri = next(i for i, h in enumerate(head) if "備轉容量率" in h)
    ci = next((i for i, h in enumerate(head)
               if h.startswith("備轉容量") and "率" not in h), None)
    li = next((i for i, h in enumerate(head) if "尖峰負載" in h), None)
    out = {}
    for row in rd:
        if len(row) <= max(di, ri):
            continue
        ds = row[di].strip()
        if not re.match(r"^\d{8}$", ds):
            continue
        try:
            v = float(row[ri])
        except ValueError:
            continue
        comp = None
        if ci is not None and li is not None and len(row) > max(ci, li):
            try:
                load = float(row[li])
                if load > 0:
                    comp = r2(float(row[ci]) / load * 100)
            except ValueError:
                comp = None
        out["%s-%s-%s" % (ds[:4], ds[4:6], ds[6:])] = (v, comp)
    return out


RESERVE_TOL = 0.1  # 欄值與自算差距容許（百分點）


def build_tw_reserve(old):
    rep, comp, errs, bad, corrected = {}, {}, [], [], []
    got = False
    for url in (TP_URL_HIST, TP_URL_CUR):
        try:
            txt = http_get(url).decode("utf-8-sig")
            for day, (v, c) in parse_taipower_csv(txt).items():
                rep[day] = v
                if c is not None:
                    comp[day] = c
            got = True
        except Exception as e:
            errs.append("%s: %s" % (type(e).__name__, e))
    if not got or not rep:
        raise RuntimeError("台電 CSV 全部失敗 " + "; ".join(errs))
    days = {}
    for day, v in rep.items():
        c = comp.get(day)
        if c is not None and abs(v - c) > RESERVE_TOL:
            # 來源欄值與同表原始分量對不上（例如漏小數點）：採自算值
            corrected.append({"date": day, "reported": r2(v), "computed": c})
            v = c
        # 最後防線：0-60% 以外視為錯誤，該日不寫入
        if not 0 <= v <= 60:
            bad.append("%s=%s" % (day, v))
            continue
        days[day] = r2(v)
    old_tr = old.get("tw_reserve") or {}
    merged = dict(zip(old_tr.get("days", []), old_tr.get("values", [])))
    merged.update(days)  # 新值覆蓋同日舊值
    for item in bad:
        merged.pop(item.split("=")[0], None)
    if bad:
        errs.append("範圍外(0-60%%)略過 %d 日: %s" % (len(bad), ", ".join(sorted(bad))))
    seen = {c["date"] for c in corrected}
    corrected += [c for c in old_tr.get("corrected", [])
                  if c["date"] not in seen and c["date"] in merged]
    corrected.sort(key=lambda c: c["date"])
    keys = sorted(merged)
    res = {"src": TP_SRC, "unit": "%", "days": keys,
           "values": [merged[k] for k in keys], "corrected": corrected}
    return res, errs


# -------------------------------------------------------------------- main
def main():
    old = load_old()
    out = {
        "updated": old.get("updated"),
        "re_share": old.get("re_share") or {
            "src": IRENA_SRC, "unit": "%", "metric": "再生能源占總發電量",
            "years": [], "series": []},
        "tw_monthly": old.get("tw_monthly") or {
            "src": TW_SRC, "unit": "億度", "months": [], "series": [],
            "total_gen": [], "re_share": []},
        "tw_reserve": old.get("tw_reserve") or {
            "src": TP_SRC, "unit": "%", "days": [], "values": []},
        "failures": [],
    }
    fails, ok = [], 0
    try:
        out["re_share"], eu_err = build_re_share(old)
        ok += 1
        if eu_err:
            fails.append("re_share.EU: " + eu_err)
    except Exception as e:
        fails.append("re_share: %s: %s" % (type(e).__name__, e))
    try:
        out["tw_monthly"] = build_tw_monthly(old)
        ok += 1
    except Exception as e:
        fails.append("tw_monthly: %s: %s" % (type(e).__name__, e))
    try:
        out["tw_reserve"], errs = build_tw_reserve(old)
        ok += 1
        for x in errs:
            fails.append("tw_reserve(partial): " + x)
    except Exception as e:
        fails.append("tw_reserve: %s: %s" % (type(e).__name__, e))
    out["failures"] = fails
    for f in fails:
        print("WARN", f)
    if ok == 0:
        print("全部三類皆失敗，不寫檔")
        sys.exit(1)
    out["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("re_share years", len(out["re_share"]["years"]),
          "| tw_monthly months", len(out["tw_monthly"]["months"]),
          "| tw_reserve days", len(out["tw_reserve"]["days"]))


if __name__ == "__main__":
    main()
