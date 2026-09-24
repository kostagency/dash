#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборщик данных для дашбордов клиентов.
Для каждого projects/*.json тянет рекламу из Meta (и CRM, если подключена),
пишет site/<slug>/data.json и кладёт рядом страницу из template/.

Локально:  python3 collect.py            (креды из ../ai-ads-agent/.mcp.json)
В GitHub:  креды из секретов META_ACCESS_TOKEN, META_APP_SECRET, AMO_TOKEN_*
"""
import json, hmac, hashlib, os, re, shutil, sys, time, glob
import urllib.request, urllib.parse, urllib.error
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
GRAPH = "https://graph.facebook.com/v20.0/"


def creds():
    t, s = os.environ.get("META_ACCESS_TOKEN"), os.environ.get("META_APP_SECRET")
    if t and s:
        return t, s
    cfg = json.load(open(os.path.join(HERE, "..", "ai-ads-agent", ".mcp.json")))
    env = cfg["mcpServers"]["meta-ads"]["env"]
    return env["META_ACCESS_TOKEN"], env["META_APP_SECRET"]


TOKEN, SECRET = creds()
PROOF = hmac.new(SECRET.encode(), TOKEN.encode(), hashlib.sha256).hexdigest()


def http_json(url, headers=None, tries=3):
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            try:
                j = json.load(e)
            except Exception:
                j = {"error": {"message": f"HTTP {e.code}"}}
            if e.code in (429, 500, 502, 503) or j.get("error", {}).get("code") in (4, 17, 613):
                time.sleep(10 * (t + 1))
                continue
            return j
        except Exception as e:
            if t == tries - 1:
                return {"error": {"message": str(e)}}
            time.sleep(3)
    return {"error": {"message": "retries exceeded"}}


def graph(path, params):
    params = dict(params, access_token=TOKEN, appsecret_proof=PROOF)
    out, url = [], GRAPH + path + "?" + urllib.parse.urlencode(params)
    while url:
        r = http_json(url)
        if "error" in r:
            raise RuntimeError(f"Meta API: {r['error'].get('message')}")
        out += r.get("data", [])
        url = r.get("paging", {}).get("next")
    return out


def lead_count(actions):
    """Лид: для WhatsApp это «Результаты» (started_7d), для сайта и форм pixel_lead/lead_grouped."""
    d = {a["action_type"]: int(float(a["value"])) for a in (actions or [])}
    msg = d.get("onsite_conversion.messaging_conversation_started_7d",
                d.get("onsite_conversion.total_messaging_connection", 0))
    return msg + d.get("offsite_conversion.fb_pixel_lead", 0) + d.get("onsite_conversion.lead_grouped", 0)


def usd_rate(target):
    """Курс USD к валюте отчёта. Для тенге берём Нацбанк РК."""
    if target == "USD":
        return 1.0
    if target == "KZT":
        try:
            with urllib.request.urlopen("https://nationalbank.kz/rss/rates_all.xml", timeout=20) as r:
                xml = r.read().decode("utf-8", "ignore")
            m = re.search(r"<title>USD</title>\s*<pubDate>[^<]*</pubDate>\s*<description>([\d.,]+)</description>", xml)
            if m:
                return float(m.group(1).replace(",", "."))
        except Exception as e:
            print("курс Нацбанка недоступен:", e)
        return float(os.environ.get("USD_KZT_FALLBACK", "510"))
    raise ValueError(f"валюта {target} не поддерживается")


# ---------- Meta ----------

def meta_rows(cfg, since, until):
    flt = [f.lower() for f in cfg["meta"].get("campaign_filter", [])]
    rows = []
    for acc in cfg["meta"]["accounts"]:
        data = graph(f"{acc}/insights", {
            "level": "campaign", "time_increment": 1, "limit": 500,
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,inline_link_clicks,actions",
        })
        for d in data:
            if flt and not any(f in d["campaign_name"].lower() for f in flt):
                continue
            rows.append({
                "date": d["date_start"],
                "campaign": d["campaign_name"],
                "spend_usd": float(d.get("spend", 0)),
                "impressions": int(d.get("impressions", 0)),
                "clicks": int(d.get("inline_link_clicks") or d.get("clicks") or 0),
                "leads": lead_count(d.get("actions")),
            })
    return rows


def meta_status(cfg):
    flt = [f.lower() for f in cfg["meta"].get("campaign_filter", [])]
    out = {}
    for acc in cfg["meta"]["accounts"]:
        for s in graph(f"{acc}/adsets", {"fields": "name,effective_status,daily_budget,campaign{name}", "limit": 500}):
            if flt and not any(f in s["campaign"]["name"].lower() for f in flt):
                continue
            out[s["id"]] = {"status": s["effective_status"],
                            "daily_budget_usd": int(s.get("daily_budget") or 0) / 100}
    return out


# ---------- CRM: amoCRM ----------

def amo_rows(cfg, since, until, tz):
    a = cfg["crm"]["amo"]
    token = os.environ.get(a["token_env"])
    if not token or not a.get("domain"):
        print("  amo: нет домена или токена, пропускаю")
        return None
    base = f"https://{a['domain']}/api/v4/"
    hdr = {"Authorization": f"Bearer {token}"}

    pipe = http_json(base + f"leads/pipelines/{a['pipeline_id']}", hdr)
    if "error" in pipe or "_embedded" not in pipe:
        raise RuntimeError(f"amo: не читается воронка {a['pipeline_id']}: {pipe}")
    order = {s["id"]: s["sort"] for s in pipe["_embedded"]["statuses"]}
    stages = a["stages"]
    if any(not st["status_ids"] for st in stages):
        # этапы ещё не сопоставлены: печатаем статусы воронки в лог, чтобы их разметить в конфиге
        print("  amo: статусы воронки (id, сортировка, название):")
        for s in sorted(pipe["_embedded"]["statuses"], key=lambda s: s["sort"]):
            print(f"    {s['id']}\t{s['sort']}\t{s['name']}")
    stage_sort = [min((order.get(i, 10**9) for i in st["status_ids"]), default=10**9) for st in stages]

    t0 = int(datetime.combine(since, datetime.min.time(), tz).timestamp())
    t1 = int(datetime.combine(until + timedelta(days=1), datetime.min.time(), tz).timestamp())

    def pages(path, params, key):
        out, page = [], 1
        while True:
            r = http_json(base + path + "?" + urllib.parse.urlencode(dict(params, limit=250 if key == "leads" else 100, page=page)), hdr)
            batch = r.get("_embedded", {}).get(key, [])
            out += batch
            if not r.get("_links", {}).get("next"):
                return out
            page += 1

    days = defaultdict(lambda: {"crm_leads": 0, "stages": [0] * len(stages), "revenue": 0})
    day = lambda ts: datetime.fromtimestamp(ts, tz).date().isoformat()

    # заявки: сделки воронки, созданные за период
    for l in pages("leads", {"filter[pipeline_id]": a["pipeline_id"],
                             "filter[created_at][from]": t0, "filter[created_at][to]": t1}, "leads"):
        days[day(l["created_at"])]["crm_leads"] += 1

    # этапы: по истории смены статусов. Сделка попадает в этап в тот день, когда впервые
    # перешла его порог (перескок через этапы засчитывает все пройденные). Уход в «не реализованные» не считается.
    events = pages("events", {"filter[type]": "lead_status_changed", "filter[entity]": "lead",
                              "filter[created_at][from]": t0, "filter[created_at][to]": t1}, "events")
    events.sort(key=lambda e: e["created_at"])
    seen, won = set(), {}
    for e in events:
        try:
            before = e["value_before"][0]["lead_status"]
            after = e["value_after"][0]["lead_status"]
        except (KeyError, IndexError, TypeError):
            continue
        if after.get("pipeline_id") != a["pipeline_id"] or after["id"] == 143:
            continue
        s_from = order.get(before["id"], -1) if before.get("pipeline_id") == a["pipeline_id"] else -1
        s_to = order.get(after["id"], -1)
        for i, thr in enumerate(stage_sort):
            if s_from < thr <= s_to and (e["entity_id"], i) not in seen:
                seen.add((e["entity_id"], i))
                days[day(e["created_at"])]["stages"][i] += 1
        if after["id"] == 142:
            won[e["entity_id"]] = day(e["created_at"])

    # выручка: бюджет выигранных сделок в день выигрыша
    ids = list(won)
    for i in range(0, len(ids), 50):
        q = [("filter[id][]", x) for x in ids[i:i + 50]] + [("limit", 250)]
        r = http_json(base + "leads?" + urllib.parse.urlencode(q), hdr)
        for l in r.get("_embedded", {}).get("leads", []):
            days[won[l["id"]]]["revenue"] += l.get("price") or 0
    print(f"  amo: заявок {sum(d['crm_leads'] for d in days.values())}, переходов {len(events)}, выиграно {len(won)}")
    return dict(days)


CRM = {"amo": amo_rows}


# ---------- сборка ----------

def build(path):
    cfg = json.load(open(path))
    tz = ZoneInfo(cfg.get("timezone", "Asia/Almaty"))
    today = datetime.now(tz).date()
    since = (today.replace(day=1) - timedelta(days=1)).replace(day=1)  # начало прошлого месяца
    if cfg.get("start_date"):
        # проект считается с даты старта: всё, что было раньше, в отчёт не попадает
        since = max(since, date.fromisoformat(cfg["start_date"]))
    print(f"{cfg['slug']}: {since} .. {today}")

    rate = usd_rate(cfg.get("currency", "USD"))
    ads = meta_rows(cfg, since, today)
    status = meta_status(cfg)
    for r in ads:
        r["spend"] = round(r.pop("spend_usd") * rate, 2)

    crm_type = cfg.get("crm", {}).get("type", "none")
    crm = CRM[crm_type](cfg, since, today, tz) if crm_type in CRM else None

    data = {
        "brand": cfg.get("brand", ""),
        "title": cfg["title"],
        "plan": cfg.get("plan", {}),
        "currency": cfg.get("currency", "USD"),
        "usd_rate": rate,
        "updated_at": datetime.now(tz).isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "start_date": since.isoformat() if cfg.get("start_date") else None,
        "norms": cfg.get("norms", {}),
        # этапы показываем всегда, даже пока CRM не подключена: страница рисует их с прочерками
        "crm": {"type": crm_type if crm is not None else "none",
                "stages": [{"label": s["label"], "short": s.get("short", s["label"])}
                           for s in cfg.get("crm", {}).get("amo", {}).get("stages", [])]},
        "ads": ads,
        "crm_days": crm or {},
        "budgets": {k: v["daily_budget_usd"] * rate for k, v in status.items() if v["status"] == "ACTIVE"},
    }
    out = os.path.join(SITE, cfg["slug"])
    os.makedirs(out, exist_ok=True)
    json.dump(data, open(os.path.join(out, "data.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    shutil.copy(os.path.join(HERE, "template", "index.html"), os.path.join(out, "index.html"))
    print(f"  реклама: {len(ads)} строк, CRM: {crm_type if crm is not None else 'нет'}")


def main():
    os.makedirs(SITE, exist_ok=True)
    # корень сайта пустой: список проектов наружу не показываем
    open(os.path.join(SITE, "index.html"), "w").write("<!doctype html><meta name=robots content=noindex><title>.</title>")
    open(os.path.join(SITE, "robots.txt"), "w").write("User-agent: *\nDisallow: /\n")
    open(os.path.join(SITE, ".nojekyll"), "w").write("")
    only = sys.argv[1:]
    failed = 0
    for p in sorted(glob.glob(os.path.join(HERE, "projects", "*.json"))):
        if only and not any(o in p for o in only):
            continue
        try:
            build(p)
        except Exception as e:
            failed += 1
            print(f"ОШИБКА {os.path.basename(p)}: {e}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
