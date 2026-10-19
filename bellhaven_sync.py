#!/usr/bin/env python3
"""
Bellhaven website -> CRM sync with MANUAL APPROVAL ONLY.

  python bellhaven_sync.py scan      # daily (cron). Scrapes + diffs. NEVER writes to the CRM.
  python bellhaven_sync.py review    # local review UI (127.0.0.1:5055). Only place CRM writes happen.
  python bellhaven_sync.py selftest  # offline checks of parsing/matching
  python bellhaven_sync.py probe     # show the CRM API's real response shape

Env: CRM_BASE, CRM_API_KEY, SITE_BASE (defaults below), REVIEW_DB (default proposals.db)
Cron: 0 6 * * *  cd /path && python bellhaven_sync.py scan
"""
import hashlib, json, os, re, sqlite3, sys, time
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

SITE = os.getenv("SITE_BASE", "https://analyst-assessment-production.up.railway.app")
CRM = os.getenv("CRM_BASE", SITE + "/api/v1")
KEY = os.getenv("CRM_API_KEY", "bh_CFeEsytp1KcTTllPCYSo_w")
DB = os.getenv("REVIEW_DB", "proposals.db")
PARENT_NAME = "Bellhaven Senior Living"

# CRM field names. Confirm against /api/docs; startup check warns if any are missing.
F = dict(name="name", street="billing_street", city="billing_city", state="billing_state",
         zip="billing_zip", care="care_type", parent="parent_id", status="status",
         note="note", dup="duplicate_of_account", chow="chow_current_account",
         rev="lifetime_revenue", ar="outstanding_ar")
CARE_MAP = {"assisted living": "Assisted Living", "memory support": "Memory Care",
            "short-term rehabilitation & nursing": "Skilled Nursing",
            "independent living": "Independent Living"}
MIN_RATIO = 0.6  # abort scan if site returns <60% of last known count (broken scrape != closures)

# ------------------------------------------------------------------ scraper
def get(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, timeout=20, **kw); r.raise_for_status(); return r
        except requests.RequestException:
            if i == 2: raise
            time.sleep(2 * (i + 1))

def parse_detail(html, slug):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True).split("\n")
    def after(label):
        return text[text.index(label) + 1:] if label in text else []
    name = soup.find("h1").get_text(strip=True)
    addr = after("Address")[:2]
    m = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5})", addr[1]) if len(addr) == 2 else None
    block = " ".join(after("Care Offerings")[:3])
    stops = ("Administrator", "Phone")
    for s in stops: block = block.split(s)[0]
    offers = [v for k, v in CARE_MAP.items() if k in block.lower()] or []
    unknown = not offers and block.strip()
    return dict(slug=slug, name=name, street=addr[0] if addr else "",
                city=m.group(1) if m else "", state=m.group(2) if m else "", zip=m.group(3) if m else "",
                care=sorted(set(offers)), care_raw=block.strip(), parse_ok=bool(m and addr and not unknown))

def scrape():
    slugs, url = [], SITE + "/communities"
    while url:
        soup = BeautifulSoup(get(url).text, "html.parser")
        slugs += [a["href"].rstrip("/").split("/")[-1] for a in soup.select('a[href*="/communities/"]')]
        nxt = next((a for a in soup.find_all("a") if "Next" in a.get_text()), None)
        url = requests.compat.urljoin(url, nxt["href"]) if nxt else None
    out = []
    for s in dict.fromkeys(slugs):
        out.append(parse_detail(get(f"{SITE}/communities/{s}").text, s))
    return out

# ------------------------------------------------------------------ CRM client
ID_KEYS = ("id", "account_id", "Id", "ID", "accountId", "uuid", "sfid")
def with_id(r):
    """Make sure a record has 'id', whatever the API calls it."""
    if not isinstance(r, dict): raise RuntimeError(f"Unexpected record (not an object): {str(r)[:200]}. Run: python bellhaven_sync.py probe")
    k = next((k for k in ID_KEYS if r.get(k)), None)
    if not k: raise RuntimeError(f"No id field in record. Keys: {sorted(r)}. Add the right one to ID_KEYS or run: python bellhaven_sync.py probe")
    return {**r, "id": r[k]}

class Crm:
    def __init__(self):
        self.s = requests.Session(); self.s.headers.update({"Authorization": f"Bearer {KEY}", "X-API-Key": KEY})
    def _req(self, m, path, **kw):
        r = self.s.request(m, CRM + path, timeout=30, **kw)
        if r.status_code >= 400: raise RuntimeError(f"{m} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}
    def accounts(self):
        rows, page, size, total = [], 1, 100, None
        while True:
            try: d = self._req("GET", "/accounts", params={"page": page, "page_size": size})
            except RuntimeError:
                if size is None: raise
                size = None; continue   # server rejected page_size; use its default
            if isinstance(d, dict) and total is None: total = d.get("total")
            batch = d if isinstance(d, list) else next((d[k] for k in ("data", "items", "results", "accounts") if k in d), [])
            batch = [with_id(b) for b in batch]
            ids = {r["id"] for r in rows}
            new = [b for b in batch if b["id"] not in ids]
            if not new: break
            rows += new; page += 1
        if total is not None and len(rows) != total:
            raise RuntimeError(f"Fetched {len(rows)} accounts but CRM reports {total}. Aborting so no partial data is used.")
        return rows
    def one(self, i): return with_id(self._req("GET", f"/accounts/{i}"))
    def create(self, p):
        d = self._req("POST", "/accounts", json=p)
        return d["data"] if isinstance(d, dict) and isinstance(d.get("data"), dict) else d
    def update(self, i, p):
        try: return self._req("PATCH", f"/accounts/{i}", json=p)
        except RuntimeError as e:
            if "405" in str(e): return self._req("PUT", f"/accounts/{i}", json=p)
            raise

# ------------------------------------------------------------------ matching
ABBR = {"street": "st", "west": "w", "east": "e", "north": "n", "south": "s", "road": "rd",
        "avenue": "ave", "drive": "dr", "lane": "ln", "boulevard": "blvd", "court": "ct"}
def nstreet(s): return " ".join(ABBR.get(t, t) for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split())
def nname(s):
    s = (s or "").lower().replace("&", " and ").replace("health care", "healthcare").replace("centre", "center")
    s = s.replace("rehab ", "rehabilitation ")
    return " ".join(t for t in re.sub(r"[^a-z ]", " ", s).split() if t not in {"the", "of", "at"})
def akey(a): return (nstreet(a.get(F["street"])), str(a.get(F["zip"]) or "")[:5])
def ckey(a): return ((a.get(F["city"]) or "").lower(), (a.get(F["state"]) or "").upper())
def money(v):
    try: return float(str(v or 0).replace("$", "").replace(",", ""))
    except ValueError: return 0.0

def richness(a):  # duplicate survivor score: real business data beats cosmetics
    return (money(a.get(F["rev"])) + money(a.get(F["ar"])) > 0, len(a.get("contacts") or []),
            any(c.get("email") for c in a.get("contacts") or []), bool(a.get(F["care"])), a.get("updated_at") or a.get("last_updated") or "")

def diffs(keep, want):
    d = {}
    for k, v in want.items():
        cur = keep.get(k)
        if k == F["care"] and not v: continue
        if k == F["name"]: ne = (cur or "").strip().lower() != v.lower()
        elif k == F["street"]: ne = nstreet(cur) != nstreet(v)
        else: ne = str(cur or "").strip() != v
        if ne: d[k] = v
    return d

def site_ev(s):
    return {"url": f"{SITE}/communities/{s.get('slug', '')}", "name": s["name"],
            "address": f"{s['street']}, {s['city']}, {s['state']} {s['zip']}", "care": "; ".join(s["care"]) or s.get("care_raw", "")}

def plan(site, accts, enrich=None, stats=None):
    stats = stats if stats is not None else {}
    bump = lambda k: stats.__setitem__(k, stats.get(k, 0) + 1)
    parent = next(a for a in accts if a[F["name"]].startswith(PARENT_NAME) and "Parent" in a[F["name"]])
    pid, names = parent["id"], {a["id"]: a.get(F["name"]) for a in accts}
    parents = {a["id"] for a in accts if "(Parent Account)" in a[F["name"]]}
    # superseded accounts (duplicates, or already handed off via CHOW) never take part in matching
    fac = [a for a in accts if a["id"] not in parents and not a.get(F["dup"]) and not a.get(F["chow"])]
    def snap(a):
        return {"id": a["id"], "name": a.get(F["name"]), "parent": names.get(a.get(F["parent"]), a.get(F["parent"]) or "none"),
                "address": f"{a.get(F['street']) or ''}, {a.get(F['city']) or ''}, {a.get(F['state']) or ''} {a.get(F['zip']) or ''}",
                "care": a.get(F["care"]), "status": a.get(F["status"]),
                "lifetime_revenue": money(a.get(F["rev"])), "outstanding_ar": money(a.get(F["ar"]))}
    props, used = [], set()
    def P(kind, acct, payload, why, before=None, ev=None):
        props.append(dict(kind=kind, account_id=acct, payload=payload, why=why, before=before or {}, evidence=ev or {}))
    for s in site:
        want = {F["name"]: s["name"], F["street"]: s["street"], F["city"]: s["city"], F["state"]: s["state"],
                F["zip"]: s["zip"], F["care"]: "; ".join(s["care"])}
        if not s["parse_ok"]:  # never act on data we could not parse; also don't call its accounts "absent"
            used |= {a["id"] for a in fac if ckey(a) == (s["city"].lower(), s["state"]) or nname(a[F["name"]]) == nname(s["name"])}
            continue
        hits = [a for a in fac if akey(a)[0] and akey(a) == akey({F["street"]: s["street"], F["zip"]: s["zip"]})]
        how = "address"
        if not hits: hits, how = [a for a in fac if nname(a[F["name"]]) == nname(s["name"]) and ckey(a) == (s["city"].lower(), s["state"])], "name + city"
        if not hits:
            c = [a for a in fac if ckey(a) == (s["city"].lower(), s["state"]) and a["id"] not in used]
            if len(c) == 1 and c[0].get(F["parent"]) in (pid, None): hits, how = c, "city only (low confidence)"
            elif c:
                for a in c:
                    P("review", a["id"], {F["status"]: "Needs Review", F["note"]: f"Website lists '{s['name']}' in {s['city']}, {s['state']}; this account may be a rename, change of ownership or unrelated. Verify."},
                      f"Ambiguous: {len(c)} CRM account(s) in {s['city']}, {s['state']} but no address/name match for website '{s['name']}'",
                      {F["status"]: a.get(F["status"])}, {"website": site_ev(s), "crm": snap(a)})
                bump("ambiguous"); continue
        if not hits:
            P("create", None, {**want, F["parent"]: pid, F["status"]: "Active"}, f"On website, no CRM account ({s['name']})",
              ev={"website": site_ev(s)}); bump("new"); continue
        if len(hits) > 1 and enrich: hits = [enrich(a) for a in hits]
        hits.sort(key=lambda a: (a.get(F["parent"]) == pid,) + richness(a) + (a["id"],), reverse=True); keep, losers = hits[0], hits[1:]
        used |= {a["id"] for a in hits}
        ev = {"website": site_ev(s), "crm": snap(keep), "matched_by": how}
        chow = False
        if keep.get(F["parent"]) != pid:   # ownership SOP: check billing history before touching the parent
            full = enrich(keep) if enrich else keep
            ev["crm"] = snap(full)
            chow = money(full.get(F["rev"])) > 0 and money(full.get(F["ar"])) > 0
        if chow:  # old account stays exactly as is; new account created under Bellhaven; old gets chow_current_account
            P("chow", keep["id"], {**want, F["parent"]: pid, F["status"]: "Active"},
              f"CHANGE OF OWNERSHIP: has revenue history AND open AR, so old account is preserved. Creates new account under Bellhaven and sets {F['chow']} on the old one.",
              {F["parent"]: keep.get(F["parent"])}, ev); bump("chow")
        else:
            d = diffs(keep, want)
            if keep.get(F["parent"]) != pid: d[F["parent"]] = pid
            if keep.get(F["status"]) == "Inactive": d[F["status"]] = "Needs Review"
            if d: P("update", keep["id"], d, f"Website differs from CRM (matched by {how})", {k: keep.get(k) for k in d}, ev); bump("fix")
            else: bump("confident")
        for l in losers:
            P("duplicate", l["id"], {F["dup"]: keep["id"], F["status"]: "Inactive",
              F["note"]: f"Duplicate of {keep['id']} ({keep[F['name']]}); same location. Survivor chosen for richer data."},
              f"Duplicate of {keep[F['name']]} ({keep['id']})", {F["status"]: l.get(F["status"])},
              {"website": site_ev(s), "losing_copy": snap(l), "surviving_copy": snap(keep)}); bump("duplicate")
    for a in fac:  # under Bellhaven in CRM but not on website => review, never auto-inactivate
        if a.get(F["parent"]) == pid and a["id"] not in used and a.get(F["status"]) != "Needs Review":
            P("review", a["id"], {F["status"]: "Needs Review", F["note"]: f"Not listed on Bellhaven website (checked {datetime.now(timezone.utc):%Y-%m-%d}). Confirm closure, sale or re-parenting before inactivating."},
              "Under Bellhaven in CRM but absent from website", {F["status"]: a.get(F["status"])}, {"crm": snap(a)}); bump("absent")
    return props

# ------------------------------------------------------------------ queue (SQLite)
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.executescript("""CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY, fp TEXT UNIQUE, kind TEXT,
      account_id TEXT, payload TEXT, before TEXT, why TEXT, state TEXT DEFAULT 'pending', created TEXT,
      decided_by TEXT, decided_at TEXT, result TEXT, evidence TEXT DEFAULT '{}', new_id TEXT);
      CREATE TABLE IF NOT EXISTS runs(ts TEXT, site_count INT);""")
    for col in ("evidence TEXT DEFAULT '{}'", "new_id TEXT"):  # migrate older databases
        try: c.execute(f"ALTER TABLE proposals ADD COLUMN {col}")
        except sqlite3.OperationalError: pass
    return c

def scan():
    site = scrape(); c = db()
    last = c.execute("SELECT site_count FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    if not site or (last and len(site) < MIN_RATIO * last[0]):
        sys.exit(f"ABORT: scraped {len(site)} vs {last[0] if last else '?'} last time. No proposals created.")
    crm, stats = Crm(), {}
    props = plan(site, crm.accounts(), enrich=lambda a: {**a, **crm.one(a["id"])}, stats=stats); seen = set(); new = 0
    for p in props:
        fp = hashlib.sha256(json.dumps([p["kind"], p["account_id"], {k: v for k, v in p["payload"].items() if k != F["note"]}], sort_keys=True).encode()).hexdigest()
        seen.add(fp)
        ex = c.execute("SELECT state FROM proposals WHERE fp=?", (fp,)).fetchone()
        if ex and ex[0] == "stale": c.execute("UPDATE proposals SET state='pending' WHERE fp=?", (fp,)); new += 1
        if not ex:  # already decided / already queued proposals are never re-proposed
            c.execute("INSERT INTO proposals(fp,kind,account_id,payload,before,why,created,evidence) VALUES(?,?,?,?,?,?,?,?)",
                      (fp, p["kind"], p["account_id"], json.dumps(p["payload"]), json.dumps(p["before"]), p["why"],
                       datetime.now(timezone.utc).isoformat(), json.dumps(p["evidence"]))); new += 1
    for r in c.execute("SELECT id,fp FROM proposals WHERE state='pending'").fetchall():
        if r["fp"] not in seen: c.execute("UPDATE proposals SET state='stale' WHERE id=?", (r["id"],))  # no longer true
    c.execute("INSERT INTO runs VALUES(?,?)", (datetime.now(timezone.utc).isoformat(), len(site)))
    c.commit()
    print(f"scraped {len(site)} | classification: {stats} | {new} new proposals awaiting approval; CRM untouched.")

# ------------------------------------------------------------------ the ONLY write path
def drifted(cur, before): return any(str(cur.get(k) or "") != str(v or "") for k, v in before.items())

def apply_approved(pid, reviewer):
    c = db(); r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if not r or r["state"] not in ("pending", "failed") or not reviewer.strip(): return "not pending / no reviewer"
    payload, before, crm, new_id = json.loads(r["payload"]), json.loads(r["before"]), Crm(), r["new_id"]
    def fin(state, res):
        c.execute("UPDATE proposals SET state=?,decided_by=?,decided_at=?,result=? WHERE id=?",
                  (state, reviewer, datetime.now(timezone.utc).isoformat(), str(res)[:500], pid)); c.commit(); return state
    try:
        if r["kind"] in ("create", "chow"):
            if r["kind"] == "chow" and not new_id and drifted(crm.one(r["account_id"]), before):
                return fin("conflict", "CRM changed since proposal; rescan")
            if not new_id:  # step 1: new account. Saved immediately so a retry never creates a second one.
                new_id = with_id(crm.create(payload))["id"]
                c.execute("UPDATE proposals SET new_id=? WHERE id=?", (new_id, pid)); c.commit()
            if r["kind"] == "chow":  # step 2: old account untouched except the pointer to the new one
                res = crm.update(r["account_id"], {F["chow"]: new_id})
            else: res = {"created": new_id}
        else:
            cur = crm.one(r["account_id"])
            if drifted(cur, before): return fin("conflict", "CRM changed since proposal; rescan")
            if F["note"] in payload and cur.get(F["note"]):  # append, never overwrite notes
                payload[F["note"]] = f"{cur[F['note']]}\n---\n{payload[F['note']]}"
            res = crm.update(r["account_id"], payload)
        return fin("applied", res)
    except Exception as e: return fin("failed", e)

# ------------------------------------------------------------------ review UI (standard library only)
def review():
    import html as H
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs
    tok = os.urandom(8).hex()
    def val(k, v):
        if k == "url": return f'<a href="{H.escape(v)}" target=_blank>{H.escape(v)}</a>'
        return H.escape(str(v))
    def ev_html(ev):
        out = []
        for sec, d in ev.items():
            if not isinstance(d, dict): out.append(f"<div><b>{H.escape(sec)}</b>: {H.escape(str(d))}</div>"); continue
            rows = "".join(f"<tr><td>{H.escape(k)}</td><td>{val(k, v)}</td></tr>" for k, v in d.items())
            link = f' <a href="{SITE}/crm/{KEY}/accounts/{H.escape(str(d["id"]))}" target=_blank>open in CRM</a>' if d.get("id") else ""
            out.append(f"<div class=e><b>{H.escape(sec.replace('_', ' '))}</b>{link}<table>{rows}</table></div>")
        return "".join(out)
    def page():
        rows = db().execute("SELECT * FROM proposals WHERE state IN ('pending','failed') ORDER BY kind,id").fetchall()
        out = [f"""<!doctype html><meta charset=utf-8><title>CRM review</title><style>body{{font:14px system-ui;max-width:1000px;margin:2em auto}}
        .c{{border:1px solid #ccc;border-radius:8px;padding:10px;margin:10px 0}}.k{{font-weight:600;text-transform:uppercase;font-size:11px;background:#eef;padding:2px 6px;border-radius:4px}}
        code{{background:#f3f3f3;padding:1px 4px}}table{{font-size:12px;border-collapse:collapse}}td{{border-bottom:1px solid #eee;padding:1px 8px 1px 0}}
        .e{{display:inline-block;vertical-align:top;margin:6px 16px 6px 0}}.err{{color:#b00}}</style><h2>Pending changes ({len(rows)})</h2>
        <form method=post action=/decide><input type=hidden name=t value={tok}>Reviewer: <input name=who required>"""]
        for r in rows:
            before = json.loads(r["before"]); payload = json.loads(r["payload"])
            lines = "".join(f"<code>{H.escape(k)}</code>: {H.escape(str(before.get(k, '—')))} &rarr; <b>{H.escape(str(v))}</b><br>" for k, v in payload.items())
            err = f"<div class=err>Last attempt failed: {H.escape(r['result'] or '')}</div>" if r["state"] == "failed" else ""
            out.append(f"""<div class=c><span class=k>{H.escape(r['kind'])}</span> {H.escape(r['account_id'] or 'new account')} &mdash; {H.escape(r['why'])}<br>{lines}{err}
            {ev_html(json.loads(r['evidence'] or '{}'))}<br>
            <button name=act value="approve:{r['id']}">Approve &amp; write</button> <button name=act value="reject:{r['id']}">Reject</button></div>""")
        return "".join(out) + "</form>"
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, loc=None):
            self.send_response(code)
            if loc: self.send_header("Location", loc)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body.encode())
        def do_GET(self): self._send(200, page())
        def do_POST(self):
            f = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()).items()}
            if self.path != "/decide" or f.get("t") != tok: return self._send(403, "bad token")
            act, i = f["act"].split(":"); who = f.get("who", "").strip()
            if not who: return self._send(400, "Reviewer name required")
            if act == "approve": apply_approved(int(i), who)
            else:
                c = db(); c.execute("UPDATE proposals SET state='rejected',decided_by=?,decided_at=? WHERE id=? AND state IN ('pending','failed')",
                                    (who, datetime.now(timezone.utc).isoformat(), int(i))); c.commit()
            self._send(303, "", "/")
        def log_message(self, *a): pass
    print("Review page running at http://127.0.0.1:5055  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", 5055), Handler).serve_forever()

def probe():
    """Print what the CRM API really returns, so field names can be confirmed."""
    s = requests.Session(); s.headers.update({"Authorization": f"Bearer {KEY}", "X-API-Key": KEY})
    for path in ("/accounts?limit=2", "/accounts"):
        r = s.get(CRM + path, timeout=30); print(f"GET {path} -> {r.status_code}")
        try: d = r.json()
        except ValueError: print(r.text[:500]); continue
        print("top-level:", type(d).__name__, list(d)[:10] if isinstance(d, dict) else f"{len(d)} items")
        rows = d if isinstance(d, list) else next((d[k] for k in ("data", "items", "results", "accounts") if k in d), [])
        if rows: print(json.dumps(rows[0], indent=2)[:1500]); return
    print("No records found; paste the output above.")

# ------------------------------------------------------------------ offline self-test
def selftest():
    html = """<h1>Bellhaven Shores of Erie</h1><dl><dt>Address</dt><dd>100 Lake St<br>Erie, PA 16501</dd>
    <dt>Care Offerings</dt><dd><span>Assisted Living</span><span>Memory Support</span></dd><dt>Administrator</dt><dd>X</dd></dl>"""
    d = parse_detail(html, "x")
    assert d["care"] == ["Assisted Living", "Memory Care"] and d["zip"] == "16501" and d["parse_ok"], d
    assert nstreet("1120 West Main Street") == nstreet("1120 W Main St")
    assert nname("Bellhaven Rehab and Nursing of Grove City") == nname("Bellhaven Rehabilitation & Nursing of Grove City")
    print("selftest ok")

if __name__ == "__main__":
    {"scan": scan, "review": review, "selftest": selftest, "probe": probe}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()