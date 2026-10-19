"""Show the review queue. Usage:  python check_queue.py            (summary)
                                 python check_queue.py duplicate  (details for one kind)"""
import json, sqlite3, sys
c = sqlite3.connect("proposals.db"); c.row_factory = sqlite3.Row
kind = sys.argv[1] if len(sys.argv) > 1 else None
if not kind:
    for r in c.execute("select kind,state,count(*) n from proposals group by 1,2"): print(r["kind"], r["state"], r["n"])
else:
    for r in c.execute("select * from proposals where kind=? order by state,id", (kind,)):
        ev = json.loads(r["evidence"] or "{}")
        print(f"\n[{r['state']}] account {r['account_id']}  - {r['why']}")
        for part, d in ev.items():
            if isinstance(d, dict) and "id" in d:
                print(f"   {part}: {d.get('name')} | {d.get('address')} | parent={d.get('parent')} | status={d.get('status')} | rev={d.get('lifetime_revenue')} ar={d.get('outstanding_ar')} | id={d['id']}")