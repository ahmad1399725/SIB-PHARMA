"""Test data: python seed.py  (run once, DB env var = db path)"""
import time
import sehatnet_backend as B
with B.tx() as c:
    c.executescript(B.SCHEMA)
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]: raise SystemExit("already seeded")
    A = B.Ctx(True, None)
    c.execute("INSERT OR REPLACE INTO settings VALUES('min_withdraw','100')")
    ids = {}
    for key, name, phone, pin, role, sp in [("f","Test Franchise","9000000001","1111","franchise",None),("a","Test Agent","9000000002","2222","agent","f"),
        ("c1","Test Customer 1","9000000003","3333","customer","a"),("c2","Test Customer 2","9000000004","4444","customer","a")]:
        ids[key] = B.register({"name":name,"phone":phone,"kyc_id":"DEMO-"+phone,"pin":pin,"role":role,"sponsor_id":ids.get(sp),"address":"Lucknow, UP"}, c, A)["user_id"]
    pids = [B.products_add(p, c, A)["product_id"] for p in (
        {"name":"Paracetamol 500mg (10 tab)","category":"medicine","mrp":30,"discount_pct":50,"stock":500,"cost":10},
        {"name":"Multivitamin (60 cap)","category":"medicine","mrp":500,"discount_pct":60,"stock":200,"cost":150},
        {"name":"Antibiotic (Rx)","category":"medicine","mrp":250,"discount_pct":50,"stock":100,"rx":1,"cost":80},
        {"name":"Herbal Soap","category":"general","mrp":100,"discount_pct":20,"stock":300,"cost":40})]
    o = B.order_create({"user_id":ids["c2"],"items":[{"product_id":pids[1],"qty":25}]}, c, B.Ctx(True, None))
    B.order_pay({"utr":"DEMO"}, c, A, o["order_id"]); B.order_deliver({}, c, A, o["order_id"])
    B.offer_add({"title":"Naye member jodein","body":"Invite karein aur team badhayein","notify":False}, c, A)
    B.offer_add({"title":"Diwali Dhamaka","body":"Is hafte vitamins par extra savings!","notify":True}, c, A)
    B.admin_supply({"user_id":ids["a"],"product_id":pids[1],"qty":20,"unit_price":180}, c, A)
    B.order_create({"user_id":ids["c1"],"items":[{"product_id":pids[0],"qty":10}]}, c, B.Ctx(True, None))
print("seeded", ids)
