#!/usr/bin/env python3
"""Разовая проверка: какие поля про источник рекламы есть в свежих сделках amo.
Печатает только названия полей и значения полей-источников (utm, реклама), без контактов."""
import json, os, re, sys, urllib.request, urllib.parse
cfg = json.load(open(sys.argv[1]))
a = cfg["crm"]["amo"]
base = f"https://{a['domain']}/api/v4/"
hdr = {"Authorization": f"Bearer {os.environ[a['token_env']]}"}
get = lambda p: json.load(urllib.request.urlopen(urllib.request.Request(base + p, headers=hdr), timeout=40))
SRC = re.compile(r"utm|source|рекл|кампан|объявл|ad|fbclid|ctwa|источн|креатив|referr|канал|campaign|content|term|medium", re.I)
PHONE = re.compile(r"\+?\d[\d\s()-]{8,}\d")
leads = get("leads?" + urllib.parse.urlencode({"filter[pipeline_id]": a["pipeline_id"], "order[created_at]": "desc", "limit": 8, "with": "source_id"}))["_embedded"]["leads"]
names = set()
for l in leads:
    print("\nсделка", l["id"], "создана", l["created_at"], "source_id", l.get("source_id"))
    for f in l.get("custom_fields_values") or []:
        names.add(f["field_name"])
        if SRC.search(f["field_name"]) or f.get("field_code", "") and SRC.search(f.get("field_code") or ""):
            v = "; ".join(str(x.get("value")) for x in f["values"])
            print("  поле", f["field_name"], f.get("field_code"), "=", PHONE.sub("[скрыто]", v)[:200])
    tags = [t["name"] for t in (l.get("_embedded", {}).get("tags") or [])]
    print("  теги", tags)
    try:
        notes = get(f"leads/{l['id']}/notes?limit=20").get("_embedded", {}).get("notes", [])
        for n in notes:
            t = json.dumps(n.get("params", {}), ensure_ascii=False)
            hit = SRC.search(t) or "fb.me" in t or "facebook" in t.lower() or "instagram" in t.lower()
            print("  примечание", n["note_type"], (PHONE.sub("[скрыто]", t)[:300] if hit else ""))
    except Exception as e:
        print("  примечания недоступны", e)
print("\nвсе поля сделок:", sorted(names))
