# -*- coding: utf-8 -*-
"""
一键重建看板数据包（GitHub Pages 自动部署入口）。

职责：
1. 读取 www/dashboard_data.json 作为基线（断网 / 接口失败时保底）；
2. 拉取东财国家队持仓季度快照，重算近两年历史环比聚合（新进/加仓/不变/减仓/清仓）；
3. 拉取新浪上证 / 沪深300 近两年日线，重算归一化序列与分季度收益；
4. 因子在线补全：腾讯批量行情刷新 收盘价 / PE / PB，股息率沿用基线（免费源不提供 TTM 股息率）；
5. 回测引擎：按 P1-P6 口径拉新浪日线重算组合净值 / 月度 / 季度收益与绩效；
   行情覆盖率不足或接口失败时回退基线序列，不阻塞发布；
6. 将最新 JSON 重新内联进 HTML，产物落在 www/ 供 Pages 发布。

仅使用 Python 标准库，无需 pip 安装。
"""
import json
import re
import math
import statistics
import urllib.request
import urllib.parse
from collections import defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WWW = ROOT / "www"
JSON_PATH = WWW / "dashboard_data.json"
HTML_PATH = WWW / "index.html"

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
TX_URL = "https://qt.gtimg.cn/q={syms}"

# 季报快照节点（含基准季 2024-06-30）；新增季度时在末尾补日期即可。
DATES = ["2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30",
         "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
LABEL = {
    "2024-09-30": "2024Q3（环比 2024Q2）",
    "2024-12-31": "2024Q4（环比 2024Q3）",
    "2025-03-31": "2025Q1（环比 2024Q4）",
    "2025-06-30": "2025Q2（环比 2025Q1）",
    "2025-09-30": "2025Q3（环比 2025Q2）",
    "2025-12-31": "2025Q4（环比 2025Q3）",
    "2026-03-31": "2026Q1（环比 2025Q4）",
    "2026-06-30": "2026Q2（环比 2026Q1）",
    "2026-09-30": "2026Q3（环比 2026Q2）",
}
QLABEL = ["2024Q3", "2024Q4", "2025Q1", "2025Q2", "2025Q3",
          "2025Q4", "2026Q1", "2026Q2", "2026Q3"]

HEADERS = {"User-Agent": "Mozilla/5.0"}
RISK_FREE = 0.02          # 无风险利率（年化），与原回测口径一致
ANN_DAYS = 252            # 年化交易日
BACKTEST_MIN_BARS = 20   # 个股最少 K 线根数，不足视为无行情
COVERAGE_FALLBACK = 0.60  # 市值覆盖率低于该比例则回退基线


def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ============================ 东财持仓快照 ============================
def fetch_snapshot(date):
    """拉取某季末全部国家队持仓快照；失败返回 None。"""
    rows, page = [], 1
    while True:
        q = urllib.parse.urlencode({
            "reportName": "RPT_NATIONAL_STATISTICS", "columns": "ALL",
            "pageNumber": page, "pageSize": 500,
            "filter": f"(REPORT_DATE='{date}')",
            "sortColumns": "SECURITY_CODE", "sortTypes": 1})
        try:
            d = json.loads(http_get(EM_URL + "?" + q).decode("utf-8", "ignore"))
            res = d["result"]
            rows += res["data"]
            if page >= res["pages"]:
                break
            page += 1
        except Exception as e:
            print(f"[warn] 快照 {date} 拉取失败：{e}")
            return None
    snap = {}
    for x in rows:
        code = x["SECURITY_CODE"]
        snap[code] = {
            "name": x.get("SECURITY_NAME_ABBR") or "",
            "shares": x.get("TOTAL_SHARES_SUM") or 0,
            "cap": (x.get("MARKET_CAP_SUM") or 0) / 1e8,
            "subj": main_subj(x),
        }
    return snap


def main_subj(x):
    arr = [
        ("中央汇金体系", x.get("MARKETCAP_SUM_1") or 0),
        ("中国证金", x.get("MARKETCAP_SUM_0") or 0),
        ("证金资管", x.get("MARKETCAP_SUM_2") or 0),
        ("社保及国资", x.get("MARKETCAP_SUM_4") or 0),
    ]
    arr = [a for a in arr if a[1] and a[1] > 0]
    return max(arr, key=lambda a: a[1])[0] if arr else "其他"


def aggregate(current, prior, meta):
    cat = {k: {"n": 0, "cap": 0.0} for k in ["新进", "加仓", "不变", "减仓", "清仓"]}
    bd = defaultdict(lambda: {"n": 0, "cap": 0.0})
    ind = defaultdict(lambda: {"n": 0, "cap": 0.0})
    subj = defaultdict(lambda: {"n": 0, "cap": 0.0})
    rows, exits = [], []
    for code, v in current.items():
        mt = meta.get(code, {"board": "其他", "ind": "其他"})
        if code not in prior:
            st = "新进"
        else:
            ps = prior[code]["shares"]
            st = "加仓" if v["shares"] > ps else ("减仓" if v["shares"] < ps else "不变")
        cat[st]["n"] += 1
        cat[st]["cap"] += v["cap"]
        bd[mt["board"]]["n"] += 1
        bd[mt["board"]]["cap"] += v["cap"]
        ind[mt["ind"]]["n"] += 1
        ind[mt["ind"]]["cap"] += v["cap"]
        subj[v["subj"]]["n"] += 1
        subj[v["subj"]]["cap"] += v["cap"]
        rows.append({"code": code, "name": v["name"], "ind": mt["ind"],
                     "board": mt["board"], "subj": v["subj"],
                     "cap": round(v["cap"], 2), "status": st})
    for code, v in prior.items():
        if code not in current:
            cat["清仓"]["n"] += 1
            cat["清仓"]["cap"] += v["cap"]
            exits.append({"code": code, "name": v["name"], "cap": round(v["cap"], 2)})
    ind_top = sorted(ind.items(), key=lambda kv: -kv[1]["cap"])[:14]
    rows.sort(key=lambda r: -r["cap"])
    return {
        "n": len(current),
        "cap": round(sum(v["cap"] for v in current.values()), 2),
        "cat": {k: {"n": v["n"], "cap": round(v["cap"], 2)} for k, v in cat.items()},
        "subj": {k: {"n": v["n"], "cap": round(v["cap"], 2)} for k, v in subj.items()},
        "bd": {k: {"n": v["n"], "cap": round(v["cap"], 2)} for k, v in bd.items()},
        "ind_top": [[k, v["n"], round(v["cap"], 2)] for k, v in ind_top],
        "rows": rows,
        "exits": exits,
    }


def refresh_history(pk):
    meta = {r["code"]: {"board": r["board"], "ind": r["ind"]} for r in pk.get("rows", [])}
    snaps = {}
    for dt in DATES:
        snaps[dt] = fetch_snapshot(dt)
    baseline = {h["date"]: h for h in pk.get("history", [])}
    history = []
    for i in range(1, len(DATES)):
        dt, pdt = DATES[i], DATES[i - 1]
        cur, prior = snaps[dt], snaps[pdt]
        if cur is None or prior is None:
            if dt in baseline:
                history.append(baseline[dt])
            continue
        entry = {"date": dt, "label": LABEL[dt], **aggregate(cur, prior, meta)}
        history.append(entry)
    if history:
        pk["history"] = history
        print(f"[ok] 历史季度重算 {len(history)} 个")


# ============================ 指数日线 ============================
def sina_kline(sym, n=700):
    raw = http_get(SINA.format(sym=sym, n=n), timeout=40).decode("utf-8", "ignore")
    return [(x["day"], float(x["close"])) for x in json.loads(raw)]


def quarter_idx(d):
    for qi, end in enumerate(DATES[1:]):
        if d <= end:
            return qi
    return len(QLABEL) - 1


def refresh_index(pk):
    try:
        series, dates = {}, None
        for sym, nm in [("sh000001", "上证指数"), ("sh000300", "沪深300")]:
            k = [(d, c) for d, c in sina_kline(sym) if d <= DATES[-1]]
            dates = [d for d, _ in k]
            base = k[0][1]
            series[nm] = [round(c / base, 4) for _, c in k]
        qend = {i: None for i in range(len(QLABEL))}
        for i, d in enumerate(dates):
            qend[quarter_idx(d)] = i
        index_q = {"quarters": QLABEL, "series": {}}
        for nm, vals in series.items():
            prev = vals[0]
            rets = []
            for qi in range(len(QLABEL)):
                ei = qend[qi]
                rets.append(round((vals[ei] / prev - 1) * 100, 2) if ei is not None else None)
                if ei is not None:
                    prev = vals[ei]
            full = round((vals[qend[len(QLABEL) - 1]] / vals[0] - 1) * 100, 2)
            index_q["series"][nm] = rets + [full]
        pk["index2y"] = {"dates": dates, **series}
        pk["indexQ"] = index_q
        print(f"[ok] 指数日线 {len(dates)} 根，{dates[0]} → {dates[-1]}")
    except Exception as e:
        print(f"[warn] 指数刷新失败，保留基线：{e}")


# ============================ 因子在线补全 ============================
def _tx_symbol(code):
    if code and code[0] in ("6", "9"):
        return "sh" + code
    if code and code[0] in ("0", "2", "3"):
        return "sz" + code
    return None  # 北交所等免费源不覆盖，跳过


def _fnum(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def fetch_quotes(codes):
    """腾讯批量行情：返回 {code: {close, pe, pb}}；股息率该源不提供。"""
    pairs = [(c, _tx_symbol(c)) for c in codes]
    pairs = [(c, s) for c, s in pairs if s]
    out = {}
    BATCH = 50
    for i in range(0, len(pairs), BATCH):
        chunk = pairs[i:i + BATCH]
        url = TX_URL.format(syms=",".join(s for _, s in chunk))
        try:
            raw = http_get(url, timeout=12).decode("gbk", "ignore")
        except Exception as e:
            print(f"[warn] 腾讯行情批次 {i // BATCH + 1} 失败：{e}")
            continue
        for line in raw.split(";"):
            m = re.search(r'v_(\w+)="([^"]*)"', line)
            if not m:
                continue
            f = m.group(2).split("~")
            if len(f) < 47:
                continue
            code = m.group(1)[2:]
            out[code] = {"close": _fnum(f[3]), "pe": _fnum(f[39]), "pb": _fnum(f[46])}
    return out


def refresh_factors(pk):
    rows = pk.get("rows", [])
    if not rows:
        return
    quotes = fetch_quotes([r["code"] for r in rows])
    n = 0
    for r in rows:
        q = quotes.get(r["code"])
        if not q:
            continue
        if q["close"]:
            r["close"] = round(q["close"], 2)
        if q["pe"] is not None:
            r["pe"] = round(q["pe"], 2)
        if q["pb"] is not None:
            r["pb"] = round(q["pb"], 2)
        n += 1

    def wavg(key, positive=False):
        g = [(r["cap"], r[key]) for r in rows
             if r.get(key) is not None and (not positive or r[key] > 0)]
        return round(sum(a * b for a, b in g) / sum(a for a, b in g), 2) if g else None

    summary = pk.setdefault("summary", {})
    summary["dy"] = wavg("dy")                 # 股息率沿用基线因子
    summary["pe"] = wavg("pe", positive=True)  # 亏损股 PE<=0 不纳入均值
    summary["pb"] = wavg("pb")
    summary["top"] = [[r["code"], r["name"], round(r["cap"], 1), r["close"], r["dy"]]
                      for r in sorted(rows, key=lambda z: -z["cap"])[:25]]
    print(f"[ok] 因子行情补全 {n}/{len(rows)} 只（股息率沿用基线）")


# ============================ 回测引擎 ============================
def _sina_symbol(code):
    if code and code[0] in ("6", "9"):
        return "sh" + code
    if code and code[0] in ("0", "2", "3"):
        return "sz" + code
    return None


def _one_close(code):
    sym = _sina_symbol(code)
    if not sym:
        return code, None
    try:
        raw = http_get(SINA.format(sym=sym, n=260), timeout=12)
        arr = json.loads(raw.decode("utf-8", "ignore"))
        kl = [(x["day"], float(x["close"])) for x in arr if x.get("close")]
        return code, kl if len(kl) >= BACKTEST_MIN_BARS else None
    except Exception:
        return code, None


def fetch_closes(codes):
    out = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for code, kl in ex.map(_one_close, codes):
            if kl:
                out[code] = kl
    return out


def _cap_weights(raw, cap=0.10):
    """单票上限：超限部分按比例回分给未超限股票，迭代至全部不超过 cap。"""
    s = sum(raw.values()) or 1.0
    w = {c: v / s for c, v in raw.items()}
    for _ in range(20):
        over = [k for k, v in w.items() if v > cap + 1e-9]
        if not over:
            break
        excess = sum(w[k] - cap for k in over)
        for k in over:
            w[k] = cap
        under = [k for k in w if w[k] < cap - 1e-9]
        us = sum(w[k] for k in under)
        if us <= 0:
            break
        for k in under:
            w[k] += excess * w[k] / us
    return w


def build_schemes(rows):
    """六个组合的初始权重（未归一化；按日在有行情股票内归一化）。"""
    bank = {r["code"] for r in rows if r["ind"] == "银行Ⅱ"}
    p3 = [r["code"] for r in rows
          if r["code"] not in bank
          and r.get("dy") is not None and r["dy"] >= 4
          and r.get("pb") is not None and r["pb"] < 1.5]
    cap = {r["code"]: max(r["cap"], 1e-6) for r in rows}
    allc = list(cap)
    schemes = {
        "P1": {c: cap[c] for c in allc},                                   # 全量市值加权
        "P2": {c: cap[c] for c in allc if c not in bank},                 # 剔除银行市值加权
        "P3": {c: cap[c] for c in p3},                                     # 精选市值加权
        "P4": {c: 1.0 for c in p3},                                        # 精选等权
        "P5": {c: 1.0 for c in allc},                                      # 全量等权
    }
    # P6：精选市值加权，单票上限 10%，超限部分回分给其余成分
    schemes["P6"] = _cap_weights({c: cap[c] for c in p3})
    return schemes


def nav_series(weights, closes, dates):
    """固定初始权重、买入持有；当日无行情的股票剔除，权重在现存股票内归一化。"""
    codes = [c for c in weights if closes.get(c)]
    price = {c: dict(closes[c]) for c in codes}
    nav = [1.0]
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        ret, ws = 0.0, 0.0
        for c in codes:
            p0, p1 = price[c].get(d0), price[c].get(d1)
            if not p0 or not p1:
                continue
            ret += weights[c] * (p1 / p0 - 1)
            ws += weights[c]
        if ws > 0:
            ret /= ws
        nav.append(nav[-1] * (1 + ret))
    return nav


def monthly_returns(nav, dates):
    ends = OrderedDict()
    for i, d in enumerate(dates):
        ends[d[:7]] = i
    labels, prev, rets = list(ends), nav[0], []
    for lb in labels:
        v = nav[ends[lb]]
        rets.append(round((v / prev - 1) * 100, 2))
        prev = v
    return labels, rets


def _qkey(d):
    y, m = d[:4], int(d[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def quarterly_returns(nav, dates):
    ends = OrderedDict()
    for i, d in enumerate(dates):
        ends[_qkey(d)] = i
    keys = list(ends)
    first_d, last_d = dates[0], dates[ends[keys[0]]]
    labels = []
    for k in keys:
        if k == keys[0] and first_d[5:7] != last_d[5:7]:
            labels.append(f"{k}({int(first_d[5:7])}-{int(last_d[5:7])}月)")
        else:
            labels.append(k)
    prev, rets = nav[0], []
    for k in keys:
        v = nav[ends[k]]
        rets.append(round((v / prev - 1) * 100, 2))
        prev = v
    rets.append(round((nav[-1] / nav[0] - 1) * 100, 2))
    return labels, rets


def _bucket_bounds(dates):
    """回测窗口内的季度桶边界：返回 (标签列表, [(起始下标, 结束下标), ...])。"""
    ends = OrderedDict()
    for i, d in enumerate(dates):
        ends[_qkey(d)] = i
    keys = list(ends)
    labels, bounds, prev = [], [], 0
    for k in keys:
        ei = ends[k]
        if k == keys[0] and dates[0][5:7] != dates[ei][5:7]:
            labels.append(f"{k}({int(dates[0][5:7])}-{int(dates[ei][5:7])}月)")
        else:
            labels.append(k)
        bounds.append((prev, ei))
        prev = ei + 1
    return labels, bounds


def build_contrib(rows, closes, dates, tot_cap):
    """按季度桶计算 P1 市值加权口径的个股 / 板块贡献（单位：百分点）。

    个股贡献 = 个股市值权重 × 个股区间涨跌幅；板块贡献 = 成分个股贡献之和。
    行情缺失的个股跳过；TOP30 个股附带桶内归一化迷你走势。
    """
    labels, bounds = _bucket_bounds(dates)
    if not labels or tot_cap <= 0:
        return None
    sectors_q, stocks_q = [], []
    for s, e in bounds:
        d0, d1 = dates[s], dates[e]
        stk = []
        sec = defaultdict(lambda: {"n": 0, "w": 0.0, "contrib": 0.0, "cap": 0.0})
        for r in rows:
            kl = closes.get(r["code"])
            if not kl:
                continue
            pm = dict(kl)
            p0, p1 = pm.get(d0), pm.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            w = r["cap"] / tot_cap
            ret = p1 / p0 - 1
            cb = w * ret * 100
            stk.append({"code": r["code"], "name": r["name"], "ind": r["ind"] or "其他",
                        "weight": round(w * 100, 3), "ret": round(ret * 100, 2),
                        "contrib": round(cb, 3)})
            g = sec[r["ind"] or "其他"]
            g["n"] += 1
            g["w"] += w * 100
            g["contrib"] += cb
            g["cap"] += r["cap"]
        top20 = sorted(stk, key=lambda x: -abs(x["contrib"]))[:20]
        spark_set = {x["code"] for x in top20}
        for x in stk:
            if x["code"] in spark_set:
                pm = dict(closes[x["code"]])
                seq = [pm[dates[j]] for j in range(s, e + 1) if pm.get(dates[j])]
                base = seq[0] if seq else None
                x["spark"] = [round(v / base, 3) for v in seq] if base else []
        secs = [{"ind": k, "n": v["n"], "weight": round(v["w"], 2),
                 "ret": round(v["contrib"] / v["w"] * 100, 2) if v["w"] > 0 else None,
                 "contrib": round(v["contrib"], 3), "cap": round(v["cap"], 1)}
                for k, v in sec.items()]
        pos = sorted([x for x in secs if x["contrib"] > 0], key=lambda a: -a["contrib"])[:15]
        neg = sorted([x for x in secs if x["contrib"] <= 0], key=lambda a: a["contrib"])[:15]
        sectors_q.append(sorted(pos + neg, key=lambda a: a["contrib"]))
        stocks_q.append(sorted(stk, key=lambda a: -a["contrib"]))
    return {"quarters": labels, "sectors": sectors_q, "stocks": stocks_q}


def perf_stats(nav, bench=None):
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
    cum = nav[-1] / nav[0] - 1
    ann = (nav[-1] / nav[0]) ** (ANN_DAYS / max(len(nav) - 1, 1)) - 1
    vol = statistics.stdev(rets) * math.sqrt(ANN_DAYS) if len(rets) > 1 else 0.0
    sharpe = (ann - RISK_FREE) / vol if vol > 0 else None
    peak, maxdd = nav[0], 0.0
    for v in nav:
        peak = max(peak, v)
        maxdd = min(maxdd, v / peak - 1)
    calmar = ann / abs(maxdd) if maxdd < 0 else None
    excess = None
    if bench and len(bench) == len(nav):
        excess = round((nav[-1] / nav[0] - bench[-1] / bench[0]) * 100, 2)
    return {
        "cum": round(cum * 100, 2),
        "ann": round(ann * 100, 2),
        "vol": round(vol * 100, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "maxdd": round(maxdd * 100, 2),
        "calmar": round(calmar, 2) if calmar is not None else None,
        "excess_vs_hs300": excess,
    }


def refresh_backtest(pk):
    rows = pk.get("rows", [])
    if len(rows) < 10:
        print("[warn] 持仓样本不足，回测保留基线")
        return
    try:
        bench_k = dict(sina_kline("sh000300", 260))
        sh_k = dict(sina_kline("sh000001", 260))
        dates = sorted(bench_k)
    except Exception as e:
        print(f"[warn] 基准行情失败，回测保留基线：{e}")
        return

    closes = fetch_closes([r["code"] for r in rows])
    tot_cap = sum(r["cap"] for r in rows) or 1.0
    cov_cap = sum(r["cap"] for r in rows if closes.get(r["code"]))
    if cov_cap / tot_cap < COVERAGE_FALLBACK:
        print(f"[warn] 行情覆盖率 {cov_cap / tot_cap:.0%} 低于 {COVERAGE_FALLBACK:.0%}，回测保留基线")
        return

    schemes = build_schemes(rows)
    navs = {name: nav_series(w, closes, dates) for name, w in schemes.items()}

    contrib = build_contrib(rows, closes, dates, tot_cap)
    if contrib:
        pk["contrib"] = contrib

    bv = [bench_k[d] for d in dates]
    bnav = [c / bv[0] for c in bv]
    sv = [sh_k.get(d) for d in dates]
    if sv[0]:
        shnav = [c / sv[0] if c else None for c in sv]
    else:
        shnav = [None] * len(dates)

    pk["curve"] = {
        "dates": dates,
        "上证指数": [round(x, 4) if x is not None else None for x in shnav],
        "沪深300": [round(x, 4) for x in bnav],
        "国家队全量(P1)": [round(x, 4) for x in navs["P1"]],
        "剔除银行(P2)": [round(x, 4) for x in navs["P2"]],
        "精选等权(P4)": [round(x, 4) for x in navs["P4"]],
    }

    m_labels, m_series = None, {}
    m_map = [
        ("P1", "P1 全部国家队(市值加权)"),
        ("P2", "P2 剔除银行(市值加权)"),
        ("P3", "P3 剔除银行+股息率≥4%+PB<1.5(市值加权)"),
        ("P4", "P4 同P3(等权)"),
        ("P5", "P5 全部国家队(等权)"),
        ("P6", "P6 同P3(单票上限10%)"),
    ]
    for sk, mk in m_map:
        m_labels, m_series[mk] = monthly_returns(navs[sk], dates)
    _, m_series["基准 沪深300"] = monthly_returns(bnav, dates)
    pk["monthly"] = {"dates": m_labels, "series": m_series}

    q_labels, q_series = None, {}
    q_map = [
        ("P1", "P1 全部国家队"),
        ("P2", "P2 剔除银行"),
        ("P3", "P3 精选市值加权"),
        ("P4", "P4 精选等权"),
        ("P5", "P5 全量等权"),
        ("P6", "P6 精选上限10%"),
    ]
    for sk, qk in q_map:
        q_labels, q_series[qk] = quarterly_returns(navs[sk], dates)
    _, q_series["沪深300"] = quarterly_returns(bnav, dates)
    _, q_series["上证指数"] = quarterly_returns([x if x else 1.0 for x in shnav], dates)
    pk["quarterly"] = {"quarters": q_labels, "series": q_series}

    pk["perf"] = {
        "benchmark": "沪深300",
        "window": {"start": dates[0], "end": dates[-1], "days": len(dates)},
        "coverage": round(cov_cap / tot_cap, 4),
        "schemes": {
            "P1 全部国家队(市值加权)": perf_stats(navs["P1"], bnav),
            "P2 剔除银行(市值加权)": perf_stats(navs["P2"], bnav),
            "P3 精选市值加权": perf_stats(navs["P3"], bnav),
            "P4 精选等权": perf_stats(navs["P4"], bnav),
            "P5 全量等权": perf_stats(navs["P5"], bnav),
            "P6 精选上限10%": perf_stats(navs["P6"], bnav),
            "沪深300": perf_stats(bnav),
        },
    }
    pk["btWindow"] = {"start": dates[0], "end": dates[-1]}
    print(f"[ok] 回测重算 {len(dates)} 个交易日，覆盖率 {cov_cap / tot_cap:.0%}，"
          f"{dates[0]} → {dates[-1]}")


# ============================ 打包内联 ============================
def inline_json(pk):
    html = HTML_PATH.read_text(encoding="utf-8")
    js = json.dumps(pk, ensure_ascii=False)
    html, n = re.subn(r"const RAW = \{.*?\};\n", "const RAW = " + js + ";\n",
                      html, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError("未找到内联 RAW 数据块，检查 HTML 模板")
    HTML_PATH.write_text(html, encoding="utf-8")


def main():
    pk = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    refresh_history(pk)
    refresh_index(pk)
    refresh_factors(pk)
    refresh_backtest(pk)
    # 基线已带回测序列时保持面板可见；序列缺失才隐藏
    pk["btAvailable"] = all(k in pk and pk[k].get("dates") if k == "curve"
                             else k in pk and pk[k].get("series")
                             for k in ("curve", "monthly", "quarterly"))
    JSON_PATH.write_text(json.dumps(pk, ensure_ascii=False), encoding="utf-8")
    inline_json(pk)
    print("[done] 看板数据包重建完成，产物在 www/")


if __name__ == "__main__":
    main()
