# -*- coding: utf-8 -*-
"""
机构（名称级）持仓变动穿透 · 采集与聚合

口径：
- 股票池取看板基线 dashboard_data.json 的在持(rows)+退出(exits) 全部代码；
- 逐股抓东财 F10 前十大流通股东名册（近 6 期），本地缓存 holders_raw.json；
- 基准期 2026-09-30 对比上期 2026-06-30，diff 出每家机构的
  新进 / 加仓 / 减仓 / 不变 / 退出；
- 个人大股东剔除；香港中央结算(代理人)/香港中央结算 单列“陆股通/结算”；
- 输出 holder_flow.json，供看板“机构名称级穿透”模块消费。

仅用 Python 标准库；接口失败保留缓存基线，不阻塞出包。
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
WWW = os.path.join(ROOT, "..", "www")
BASE_JSON = os.path.join(WWW, "dashboard_data.json")
RAW_CACHE = os.path.join(ROOT, "holders_raw.json")
OUT_JSON = os.path.join(ROOT, "holder_flow.json")
PACK_JSON = os.path.join(ROOT, "holder_flow_pack.json")

CURRENT = "2026-09-30"
PRIOR = "2026-06-30"
LABEL_CUR = "2026Q3（环比 2026Q2）"

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://emweb.securities.eastmoney.com/",
}

PERSON_TYPES = {"个人"}
NOMINAL_KEYWORDS = ["香港中央结算"]
NOMINAL_PREFIX = "HKSCC"
NOMINAL_NAME = "香港中央结算（陆股通/代理人）"
# 穿透主表保留的机构大类（其它里的结算通道单列，个人剔除）
KEEP_KINDS = {"机构", "陆股通/结算"}


def sec_code(code):
    return code + (".SH" if code.startswith(("6", "9")) else ".SZ")


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def is_nominal(holder_name):
    name = holder_name or ""
    return any(k in name for k in NOMINAL_KEYWORDS) or name.upper().startswith(NOMINAL_PREFIX)


def classify(holder_type, holder_name):
    t = holder_type or ""
    if t in PERSON_TYPES:
        return "个人"
    if is_nominal(holder_name):
        return "陆股通/结算"
    return "机构"


def holder_key(holder_name, kind):
    """结算通道多种写法归一到同一席位；机构保留原名。"""
    return NOMINAL_NAME if kind == "陆股通/结算" else (holder_name or "")


def fetch_one(code, name):
    """抓单只股票近 6 期名册；返回 {period: [holder_row, ...]}，失败抛异常。"""
    q = urllib.parse.urlencode({
        "reportName": "RPT_F10_EH_FREEHOLDERS",
        "columns": "ALL",
        "pageNumber": 1,
        "pageSize": 60,
        "sortColumns": "END_DATE,HOLDER_RANK",
        "sortTypes": "-1,1",
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECUCODE="{sec_code(code)}")',
    })
    d = json.loads(http_get(EM_URL + "?" + q).decode("utf-8", "ignore"))
    if not d.get("success"):
        raise RuntimeError(d.get("message") or "api success=false")
    data = (d.get("result") or {}).get("data") or []
    periods = {}
    for x in data:
        date = (x.get("END_DATE") or "")[:10]
        if not date:
            continue
        periods.setdefault(date, []).append({
            "holder": x.get("HOLDER_NAME") or "",
            "type": x.get("HOLDER_TYPE") or "",
            "kind": classify(x.get("HOLDER_TYPE"), x.get("HOLDER_NAME")),
            "shares": x.get("HOLD_NUM"),
            "ratio": x.get("FREE_HOLDNUM_RATIO"),
            "state": x.get("HOLDER_STATE_NEW") or "不变",
            "chg": x.get("HOLD_CHANGE"),
            "chgRatio": x.get("CHANGE_RATIO"),
        })
    return {"name": name, "periods": periods}


def load_cache():
    if os.path.exists(RAW_CACHE):
        try:
            return json.load(open(RAW_CACHE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    json.dump(cache, open(RAW_CACHE, "w", encoding="utf-8"), ensure_ascii=False)


def collect(codes, refresh=False):
    cache = {} if refresh else load_cache()
    todo = [c for c in codes if c not in cache]
    fail = []
    print(f"[collect] 池 {len(codes)}，缓存 {len(cache)}，待抓 {len(todo)}")

    def work(code):
        name = names.get(code, "")
        for attempt in range(2):
            try:
                return code, fetch_one(code, name), None
            except Exception as e:
                if attempt == 1:
                    return code, None, str(e)
                time.sleep(0.6)

    if todo:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(work, c): c for c in todo}
            done = 0
            for f in as_completed(futs):
                code, payload, err = f.result()
                done += 1
                if err:
                    fail.append({"code": code, "err": err})
                else:
                    cache[code] = payload
                if done % 50 == 0:
                    print(f"  ...{done}/{len(todo)}")
                    save_cache(cache)
        save_cache(cache)
    return cache, fail


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _slim_stocks(stocks):
    out = []
    for s in stocks:
        r = s.get("ratio")
        out.append({
            "c": s["code"], "n": s["name"],
            "s": s.get("shares"),
            "r": round(r, 3) if r is not None else None,
        })
    return out


def slim_pack(flow):
    """生成可内联进看板的精简包：剔除“不变”桶与冗余字段。"""
    holders = []
    for h in flow["byHolder"]:
        holders.append({
            "h": h["holder"], "t": h["type"], "k": h["kind"],
            "新进": _slim_stocks(h["新进"]),
            "加仓": _slim_stocks(h["加仓"]),
            "减仓": _slim_stocks(h["减仓"]),
            "退出": _slim_stocks(h["退出"]),
        })
    return {
        "report_date": flow["report_date"],
        "prior_date": flow["prior_date"],
        "label": flow["label"],
        "holders": holders,
        "meta": flow.get("meta", {}),
    }


def build_flow(cache):
    by_holder = {}
    by_stock = {}
    holder_type = {}

    def _index(rows):
        """按归一 key 归集；同一 key 多条取持股最大一条。"""
        out = {}
        for h in rows:
            if h["kind"] not in KEEP_KINDS:
                continue
            k = holder_key(h["holder"], h["kind"])
            prev = out.get(k)
            if prev is None or (num(h["shares"]) or 0) > (num(prev["shares"]) or 0):
                out[k] = h
        return out

    for code, payload in cache.items():
        periods = payload.get("periods", {})
        cur = _index(periods.get(CURRENT, []))
        prior = _index(periods.get(PRIOR, []))
        name = payload.get("name", "")
        stock_holders = []

        # 本期在册
        for k, h in cur.items():
            entry = {
                "holder": h["holder"], "type": h["type"], "kind": h["kind"],
                "state": h["state"], "shares": h["shares"], "ratio": h["ratio"],
                "chg": h["chg"], "chgRatio": h["chgRatio"],
            }
            stock_holders.append(entry)
            bucket = h["state"]  # 新进/加仓/减仓/不变
            agg = by_holder.setdefault(k, {
                "type": h["type"], "kind": h["kind"],
                "新进": [], "加仓": [], "减仓": [], "不变": [], "退出": [],
            })
            agg["type"] = h["type"]; agg["kind"] = h["kind"]
            agg[bucket].append({
                "code": code, "name": name,
                "shares": h["shares"], "ratio": h["ratio"],
                "chg": h["chg"], "chgRatio": h["chgRatio"],
            })

        # 退出：上期在册、本期不在前十大（按归一 key 匹配）
        for k, h in prior.items():
            if k in cur:
                continue
            stock_holders.append({
                "holder": h["holder"], "type": h["type"], "kind": h["kind"],
                "state": "退出", "shares": h["shares"], "ratio": h["ratio"],
                "chg": "退出", "chgRatio": None,
            })
            agg = by_holder.setdefault(k, {
                "type": h["type"], "kind": h["kind"],
                "新进": [], "加仓": [], "减仓": [], "不变": [], "退出": [],
            })
            agg["退出"].append({
                "code": code, "name": name,
                "shares": h["shares"], "ratio": h["ratio"],
                "chg": "退出", "chgRatio": None,
            })

        stock_holders.sort(key=lambda x: -(num(x["shares"]) or 0))
        by_stock[code] = {"name": name, "holders": stock_holders}

    # 机构清单按本期+退出涉及股数排序
    holder_list = []
    for holder, agg in by_holder.items():
        total = sum(len(v) for k, v in agg.items() if k != "不变")
        holder_list.append({"holder": holder, "type": agg["type"], "kind": agg["kind"],
                            "total": total, **{k: v for k, v in agg.items()}})
    holder_list.sort(key=lambda x: -x["total"])

    return {
        "report_date": CURRENT,
        "prior_date": PRIOR,
        "label": LABEL_CUR,
        "byHolder": holder_list,
        "byStock": by_stock,
    }


if __name__ == "__main__":
    import sys
    refresh = "--refresh" in sys.argv
    base = json.load(open(BASE_JSON, encoding="utf-8"))
    names = {x["code"]: x["name"] for x in base["rows"]}
    names.update({x["code"]: x["name"] for x in base.get("exits", [])})
    codes = list(names.keys())

    cache, fail = collect(codes, refresh=refresh)
    flow = build_flow(cache)
    flow["meta"] = {
        "pool": len(codes),
        "cached": len(cache),
        "fail": fail,
        "holders": len(flow["byHolder"]),
        "stocks": len(flow["byStock"]),
    }
    json.dump(flow, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    pack = slim_pack(flow)
    json.dump(pack, open(PACK_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[done] 机构 {flow['meta']['holders']}，个股 {flow['meta']['stocks']}，失败 {len(fail)}")

    if "--merge" in sys.argv:
        base = json.load(open(BASE_JSON, encoding="utf-8"))
        base["holderFlow"] = pack
        json.dump(base, open(BASE_JSON, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[merge] holderFlow 已并入 {os.path.relpath(BASE_JSON, ROOT)}")
