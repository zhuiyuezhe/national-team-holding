# -*- coding: utf-8 -*-
"""
一键重建看板数据包（GitHub Pages 自动部署入口）。

职责：
1. 读取 www/dashboard_data.json 作为基线（断网 / 接口失败时保底）；
2. 拉取东财国家队持仓季度快照，重算近两年历史环比聚合（新进/加仓/不变/减仓/清仓）；
3. 拉取新浪上证 / 沪深300 近两年日线，重算归一化序列与分季度收益；
4. 回测净值 / 因子面板暂不自动重算：btAvailable=False，前端隐藏对应面板；
5. 将最新 JSON 重新内联进 HTML，产物落在 www/ 供 Pages 发布。

仅使用 Python 标准库，无需 pip 安装。
"""
import json
import re
import urllib.request
import urllib.parse
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WWW = ROOT / "www"
JSON_PATH = WWW / "dashboard_data.json"
HTML_PATH = WWW / "index.html"

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SINA = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")

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


def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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
    # 回测净值 / 因子尚未接入自动重算，先隐藏对应面板
    pk["btAvailable"] = False
    refresh_history(pk)
    refresh_index(pk)
    JSON_PATH.write_text(json.dumps(pk, ensure_ascii=False), encoding="utf-8")
    inline_json(pk)
    print("[done] 看板数据包重建完成，产物在 www/")


if __name__ == "__main__":
    main()
