"""SehatNet backend - run: ADMIN_TOKEN=secret python sehatnet_backend.py  (port 8000)
No deposit/joining fee. Customers buy goods at any amount. Commission only on paid + delivered goods orders.
Returns: goods credit (no cash refund) + commission reversal. All rates editable via /admin/settings."""
import os, re, json, time, sqlite3, hashlib, secrets, html, threading, queue, urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get("DB", "sehatnet.db")
ADMIN = os.environ.get("ADMIN_TOKEN", "change-me")
ONBOARD = {"franchise": {"agent", "customer"}, "agent": {"agent", "customer"}, "customer": {"customer"}}
DEFAULTS = {
    "first_direct_pct": 5,                       # one-time, sponsor only, on a user's first order
    "first_levels_pct": [10, 1, 1, 1, 1, .5, .5],  # 7-level plan on first order
    "repeat_levels_pct": [2, 1, .5, .3, .1, .1, .1],  # A/B matching on repeat orders
    "level_base": 2000, "level_mult": 20,  # level n done = mult**n IDs (each bought >= base) OR mult**n * base business at that depth
    "return_days": 30,
    "upi_id": "yourname@upi",
    "min_withdraw": 500, "ai_support": True, "support_wa": "",
    "biz_name": "SehatNet", "gstin": "", "gst_pct": 0,
    "rank_names": ["Member", "Star", "Silver", "Gold", "Platinum", "Diamond", "Crown", "Royal"],
    "onboard_rules": {"franchise": ["agent", "customer"], "agent": ["agent", "customer"], "customer": ["customer"]},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, phone TEXT UNIQUE, role TEXT, sponsor_id INTEGER,
  kyc_id TEXT UNIQUE, kyc_doc TEXT, wa_optin INTEGER DEFAULT 0, perms TEXT, deleted INTEGER DEFAULT 0, active INTEGER DEFAULT 0, pin_hash TEXT, address TEXT, lat REAL, lng REAL, goods_credit REAL DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT, category TEXT, mrp REAL, discount_pct REAL DEFAULT 0,
  stock INTEGER DEFAULT 0, rx INTEGER DEFAULT 0, active INTEGER DEFAULT 1, image TEXT, cost REAL DEFAULT 0, deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER, seller_id INTEGER, total REAL, credit_used REAL,
  payable REAL, status TEXT, utr TEXT, rx_ref TEXT, deliver_to TEXT, created REAL, delivered_at REAL);
CREATE TABLE IF NOT EXISTS order_items(order_id INTEGER, product_id INTEGER, qty INTEGER, unit_price REAL, cost REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, qty_left INTEGER, unit_cost REAL, expiry TEXT, batch_no TEXT, created REAL, ref TEXT);
CREATE TABLE IF NOT EXISTS commissions(id INTEGER PRIMARY KEY, order_id INTEGER, earner_id INTEGER, level INTEGER,
  track TEXT, amount REAL, status TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, upi TEXT, status TEXT, ref TEXT, created REAL);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, body TEXT, read INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS offers(id INTEGER PRIMARY KEY, title TEXT, body TEXT, active INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, user_id INTEGER, from_admin INTEGER, text TEXT, created REAL);
CREATE TABLE IF NOT EXISTS inventory(user_id INTEGER, product_id INTEGER, qty INTEGER, cost_total REAL, PRIMARY KEY(user_id, product_id));
CREATE TABLE IF NOT EXISTS stock_ledger(id INTEGER PRIMARY KEY, user_id INTEGER, product_id INTEGER, kind TEXT, qty INTEGER, unit_price REAL, cost REAL, party TEXT, created REAL, ref TEXT);
CREATE TABLE IF NOT EXISTS parties(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, ptype TEXT DEFAULT 'both', address TEXT, gstin TEXT, opening REAL DEFAULT 0, active INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS bills(id INTEGER PRIMARY KEY, kind TEXT, party_id INTEGER, party_name TEXT, bill_no TEXT, ts REAL, created REAL, subtotal REAL, discount REAL, gst_pct REAL, total REAL, paid REAL, note TEXT, by_id INTEGER, by_name TEXT, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS bill_items(id INTEGER PRIMARY KEY, bill_id INTEGER, product_id INTEGER, qty INTEGER, rate REAL, amount REAL, expiry TEXT, batch_no TEXT, consumed TEXT, batch_id INTEGER);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, party_id INTEGER, kind TEXT, amount REAL, mode TEXT, note TEXT, ts REAL, created REAL, by_id INTEGER, by_name TEXT, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL, actor_id INTEGER, actor TEXT, role TEXT, action TEXT, path TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""

@contextmanager
def tx():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    try:
        yield c; c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()

def clean(x, key=""):  # escape HTML in every text input (blocks stored XSS); images are validated separately
    if isinstance(x, str): return x if key in ("image", "doc") else html.escape(x)
    if isinstance(x, list): return [clean(i) for i in x]
    if isinstance(x, dict): return {k: clean(v, k) for k, v in x.items()}
    return x

def img_ok(d, maxlen):
    return isinstance(d, str) and len(d) <= maxlen and re.fullmatch(r"data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+", d) is not None

WAQ = queue.Queue()

def _wa_worker():
    while True:
        phone, text = WAQ.get()
        try:
            tok, pid = os.environ.get("WA_TOKEN"), os.environ.get("WA_PHONE_ID")
            if tok and pid:
                to = re.sub(r"\D", "", phone); to = "91" + to if len(to) == 10 else to
                text = html.unescape(text)[:900]; tpl = os.environ.get("WA_TEMPLATE")
                body = ({"messaging_product": "whatsapp", "to": to, "type": "template", "template": {"name": tpl, "language": {"code": os.environ.get("WA_LANG", "en")},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": text.replace("\n", " ")}]}]}} if tpl else
                        {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})
                urllib.request.urlopen(urllib.request.Request(f"https://graph.facebook.com/v20.0/{pid}/messages", json.dumps(body).encode(),
                                       {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}), timeout=15)
        except Exception as e: print("WhatsApp error:", e)
        time.sleep(0.25)
threading.Thread(target=_wa_worker, daemon=True).start()

def notify(c, uid, title, body=""):
    c.execute("INSERT INTO notifications(user_id,title,body,created) VALUES(?,?,?,?)", (uid, title, body, time.time()))
    u = c.execute("SELECT phone,wa_optin FROM users WHERE id=?", (uid,)).fetchone()
    if u and u["wa_optin"] and os.environ.get("WA_TOKEN"): WAQ.put((u["phone"], f"{title}\n{body}"))

class Ctx:
    def __init__(s, adm, uid, role=None, perms=None, name=None): s.adm, s.uid, s.role, s.perms, s.name = adm, uid, role, perms or set(), name
    def __bool__(s): return bool(s.adm) or s.role == "staff"   # admin or active staff (route-level permissions are checked in the router)

def actor(a): return "Admin" if a.adm else (a.name or f"User {a.uid}")
def actor_id(a): return 0 if a.adm else a.uid

def cat_ok(x): return re.sub(r"[^A-Za-z0-9 _-]", "", str(x or ""))[:30].strip() or "general"

def hpin(pin): return hashlib.pbkdf2_hmac("sha256", str(pin).encode(), b"sehatnet-salt", 100000).hex()

class Err(Exception):
    def __init__(s, msg, code=400): s.msg, s.code = msg, code

def settings(c):
    s = dict(DEFAULTS)
    for r in c.execute("SELECT k,v FROM settings"): s[r["k"]] = json.loads(r["v"])
    return s

def upline(c, uid, n=7):
    out = []
    for _ in range(n):
        r = c.execute("SELECT sponsor_id FROM users WHERE id=?", (uid,)).fetchone()
        if not r or not r["sponsor_id"]: break
        uid = r["sponsor_id"]; out.append(uid)
    return out

LV = """WITH RECURSIVE d(id,l) AS (SELECT id,1 FROM users WHERE sponsor_id=? UNION ALL
        SELECT u.id,d.l+1 FROM users u JOIN d ON u.sponsor_id=d.id WHERE d.l<7)"""

def level_stats(c, uid, S):
    base, mult = S["level_base"], S["level_mult"]
    rows = {r["l"]: r for r in c.execute(LV + """, v AS (SELECT user_id, SUM(total) t FROM orders WHERE status='delivered' GROUP BY user_id)
        SELECT d.l l, COUNT(*) members, SUM(CASE WHEN COALESCE(v.t,0)>=? THEN 1 ELSE 0 END) qual, COALESCE(SUM(v.t),0) vol
        FROM d LEFT JOIN v ON v.user_id=d.id GROUP BY d.l""", (uid, base)).fetchall()}
    out = []
    for n in range(1, 8):
        r = rows.get(n); tp = mult ** n; tv = tp * base
        q_ = (r["qual"] or 0) if r else 0; vol = r["vol"] if r else 0; done = q_ >= tp or vol >= tv
        out.append({"level": n, "members": r["members"] if r else 0, "qualified": q_, "volume": round(vol, 2), "target_people": tp, "target_volume": tv,
                    "done": done, "pct": 100 if done else int(min(99, max(q_ / tp, vol / tv) * 100))})
    return out

def rank_info(c, uid):
    S = settings(c); L = level_stats(c, uid, S); r = 0
    for x in L:
        if not x["done"]: break
        r += 1
    return {"rank": r, "rank_name": S["rank_names"][min(r, len(S["rank_names"]) - 1)], "levels": L, "volume": round(sum(x["volume"] for x in L), 2)}

def unlocked_levels(c, uid, S):
    r = 0
    for x in level_stats(c, uid, S):
        if not x["done"]: break
        r += 1
    return min(7, 1 + r)

def make_commissions(c, o, S):
    base = o["payable"]
    if base <= 0: return
    first = c.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND id!=? AND status IN ('paid','delivered')",
                      (o["user_id"], o["id"])).fetchone()[0] == 0
    chain = upline(c, o["user_id"])
    table = S["first_levels_pct"] if first else S["repeat_levels_pct"]
    for i, earner in enumerate(chain):
        if not c.execute('SELECT active FROM users WHERE id=?', (earner,)).fetchone()[0]: continue
        if i + 1 > unlocked_levels(c, earner, S): continue
        c.execute("INSERT INTO commissions(order_id,earner_id,level,track,amount,status) VALUES(?,?,?,?,?, 'pending')",
                  (o["id"], earner, i + 1, "first_plan" if first else "repeat_match", round(base * table[i] / 100, 2)))
    if first and chain:
        c.execute("INSERT INTO commissions(order_id,earner_id,level,track,amount,status) VALUES(?,?,?,?,?, 'pending')",
                  (o["id"], chain[0], 1, "first_direct", round(base * S["first_direct_pct"] / 100, 2)))

# ---------- handlers ----------
def register(b, c, adm):
    for f in ("name", "phone", "kyc_id"):
        if not b.get(f): raise Err(f"{f} required")
    if len(str(b.get("pin", ""))) < 4: raise Err("pin (min 4 digits) required")
    role, sp = b.get("role", "customer"), b.get("sponsor_id")
    if role not in ("franchise", "agent", "customer"): raise Err("bad role")
    if sp:
        s = c.execute("SELECT role FROM users WHERE id=?", (sp,)).fetchone()
        if not s: raise Err("sponsor not found")
        if role not in settings(c)["onboard_rules"].get(s["role"], []): raise Err(f"{s['role']} cannot onboard {role}")
    elif not adm: raise Err("sponsor required")
    try:
        cur = c.execute("INSERT INTO users(name,phone,role,sponsor_id,kyc_id,pin_hash,address,lat,lng,created,active) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (b["name"], b["phone"], role, sp, b["kyc_id"], hpin(b["pin"]), b.get("address"), b.get("lat"), b.get("lng"), time.time(), 1 if adm else 0))
    except sqlite3.IntegrityError: raise Err("one ID per person: phone/KYC already registered")
    if b.get("wa_optin"): c.execute("UPDATE users SET wa_optin=1 WHERE id=?", (cur.lastrowid,))
    if sp: notify(c, sp, "👥 Naya member juda", f"{b['name']} aapki team mein juda ({role}).")
    return {"user_id": cur.lastrowid}

def products_add(b, c, adm):
    cur = c.execute("INSERT INTO products(name,category,mrp,discount_pct,stock,rx,image,cost) VALUES(?,?,?,?,?,?,?,?)",
                    (b["name"], cat_ok(b.get("category")), b["mrp"], b.get("discount_pct", 0), b.get("stock", 0), int(b.get("rx", 0)),
                     b["image"] if img_ok(b.get("image"), 200000) else None, float(b.get("cost", 0) or 0)))
    return {"product_id": cur.lastrowid}

def products_edit(b, c, adm, pid):
    if "image" in b and not img_ok(b["image"], 200000): raise Err("photo sahi nahi")
    for k in ("name", "category", "mrp", "discount_pct", "stock", "rx", "active", "image", "cost"):
        if k in b: c.execute(f"UPDATE products SET {k}=? WHERE id=?", (cat_ok(b[k]) if k == "category" else b[k], pid))
    return {"ok": True}

def order_create(b, c, adm):
    u = c.execute("SELECT * FROM users WHERE id=?", (b.get("user_id"),)).fetchone()
    if not u: raise Err("user not found")
    if not adm:
        if adm.uid is None: raise Err("login required", 401)
        if not c.execute("SELECT active FROM users WHERE id=?", (adm.uid,)).fetchone()[0]: raise Err("Aapki ID abhi verify nahi hui", 403)
        if u["id"] != adm.uid and u["sponsor_id"] != adm.uid: raise Err("not allowed", 403)
    seller = adm.uid if u["id"] != adm.uid else None  # agent/franchise recording a sale for a customer
    items, total, needs_rx = [], 0, False
    for it in b.get("items", []):
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (it["product_id"],)).fetchone()
        q = int(it.get("qty", 1))
        if not p or q < 1 or p["stock"] < q: raise Err(f"product {it.get('product_id')} unavailable")
        price = round(p["mrp"] * (1 - p["discount_pct"] / 100), 2)
        items.append((p["id"], q, price, p["cost"] or 0)); total += price * q; needs_rx |= bool(p["rx"])
    if not items: raise Err("no items")
    if needs_rx and not b.get("rx_ref"): raise Err("prescription reference required for Rx medicines")
    total = round(total, 2); used = round(min(u["goods_credit"], total), 2); pay = round(total - used, 2)
    cur = c.execute("INSERT INTO orders(user_id,seller_id,total,credit_used,payable,status,rx_ref,deliver_to,created) VALUES(?,?,?,?,?,?,?,?,?)",
                    (u["id"], seller, total, used, pay, "paid" if pay == 0 else "awaiting_payment", b.get("rx_ref"),
                     b.get("deliver_to") or u["address"], time.time()))
    for pid, q, price, pcost in items:
        got, need = _consume(c, 0, pid, q)
        ucost = (sum(t * uc for t, uc, _, _ in got) + need * pcost) / q
        c.execute("INSERT INTO order_items(order_id,product_id,qty,unit_price,cost) VALUES(?,?,?,?,?)", (cur.lastrowid, pid, q, price, ucost))
        c.execute("UPDATE products SET stock=stock-? WHERE id=?", (q, pid))
    c.execute("UPDATE users SET goods_credit=goods_credit-? WHERE id=?", (used, u["id"]))
    return {"order_id": cur.lastrowid, "total": total, "credit_used": used, "pay_via_upi": pay}

def order_pay(b, c, adm, oid):  # admin verifies UPI payment
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "awaiting_payment": raise Err("order not awaiting payment")
    c.execute("UPDATE orders SET status='paid', utr=? WHERE id=?", (b.get("utr"), oid))
    make_commissions(c, o, settings(c))
    notify(c, o["user_id"], "✅ Payment verify ho gaya", f"Order #{oid} ka payment mil gaya. Jaldi deliver hoga.")
    for m in c.execute("SELECT earner_id,amount FROM commissions WHERE order_id=?", (oid,)).fetchall():
        notify(c, m["earner_id"], "🎁 Commission bana", f"Order #{oid} se ₹{m['amount']} pending commission (delivery par approve hoga).")
    return {"ok": True}

def order_deliver(b, c, adm, oid):
    n = c.execute("UPDATE orders SET status='delivered', delivered_at=? WHERE id=? AND status='paid'", (time.time(), oid)).rowcount
    if not n: raise Err("order not in paid state")
    c.execute("UPDATE commissions SET status='approved' WHERE order_id=? AND status='pending'", (oid,))
    bu = c.execute("SELECT user_id FROM orders WHERE id=?", (oid,)).fetchone()["user_id"]
    notify(c, bu, "🚚 Order deliver ho gaya", f"Order #{oid} deliver ho gaya. Dhanyavaad!")
    for m in c.execute("SELECT earner_id,amount FROM commissions WHERE order_id=? AND status='approved'", (oid,)).fetchall():
        notify(c, m["earner_id"], "💰 Commission approve", f"Order #{oid} ka ₹{m['amount']} commission ab nikal sakte hain.")
    return {"ok": True}

def order_return(b, c, adm, oid):  # goods credit only, commissions clawed back
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "delivered": raise Err("only delivered orders can be returned")
    if time.time() - o["delivered_at"] > settings(c)["return_days"] * 86400: raise Err("return window over")
    c.execute("UPDATE orders SET status='returned' WHERE id=?", (oid,))
    c.execute("UPDATE commissions SET status='reversed' WHERE order_id=?", (oid,))
    c.execute("UPDATE users SET goods_credit=goods_credit+? WHERE id=?", (o["total"], o["user_id"]))
    for r in c.execute("SELECT product_id,qty,cost FROM order_items WHERE order_id=?", (oid,)).fetchall():
        c.execute("UPDATE products SET stock=stock+? WHERE id=?", (r["qty"], r["product_id"]))
        c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,batch_no,created) VALUES(0,?,?,?,'RETURN',?)", (r["product_id"], r["qty"], r["cost"], time.time()))
    notify(c, o["user_id"], "↩️ Return ho gaya", f"Order #{oid} ka ₹{o['total']} saman credit mein jud gaya.")
    return {"goods_credit_added": o["total"]}

def wallet(b, c, adm, uid):
    if not adm and adm.uid != uid: raise Err("not allowed", 403)
    r = lambda q: c.execute(q, (uid,)).fetchone()[0]
    appr = r("SELECT COALESCE(SUM(amount),0) FROM commissions WHERE earner_id=? AND status='approved'")
    wd = r("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE user_id=? AND status IN ('requested','paid')")
    return {"available": round(appr - wd, 2), "withdrawn": wd, "approved": appr,
            "pending": r("SELECT COALESCE(SUM(amount),0) FROM commissions WHERE earner_id=? AND status='pending'"),
            "goods_credit": r("SELECT goods_credit FROM users WHERE id=?"),
            "levels_unlocked": unlocked_levels(c, uid, settings(c)), **rank_info(c, uid)}

def tree(b, c, adm, uid):
    if not adm and adm.uid != uid: raise Err("not allowed", 403)
    rows = c.execute("""WITH RECURSIVE d(id,l) AS (SELECT id,1 FROM users WHERE sponsor_id=? UNION ALL
        SELECT u.id,d.l+1 FROM users u JOIN d ON u.sponsor_id=d.id WHERE d.l<7)
        SELECT u.id,u.name,u.role,u.sponsor_id,u.active,d.l level FROM d JOIN users u ON u.id=d.id""", (uid,)).fetchall()
    return [dict(r) for r in rows]

def login(b, c, adm):
    u = c.execute("SELECT * FROM users WHERE phone=?", (b.get("phone"),)).fetchone()
    if not u or u["deleted"] or u["pin_hash"] != hpin(b.get("pin", "")): raise Err("phone ya pin galat", 401)
    if u["role"] == "staff" and not u["active"]: raise Err("Staff ID band hai, admin se baat karein", 403)
    t = secrets.token_hex(24); c.execute("INSERT INTO sessions VALUES(?,?,?)", (t, u["id"], time.time()))
    return {"token": t}

def me(b, c, adm):
    if adm.uid is None: raise Err("login required", 401)
    u = c.execute("SELECT id,name,phone,role,sponsor_id,address,goods_credit,active,wa_optin,perms FROM users WHERE id=?", (adm.uid,)).fetchone()
    return {**dict(u), "perms": json.loads(u["perms"] or "[]"), "upi_id": settings(c)["upi_id"], "support_wa": settings(c)["support_wa"], "can_onboard": settings(c)["onboard_rules"].get(u["role"], []),
            "unread": c.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0", (adm.uid,)).fetchone()[0]}

def my_orders(b, c, adm):
    if adm.uid is None: raise Err("login required", 401)
    return [dict(r) for r in c.execute("SELECT * FROM orders WHERE (user_id=? OR seller_id=?) AND status!='deleted' ORDER BY id DESC LIMIT 100", (adm.uid, adm.uid))]

def admin_users(b, c, adm): return [dict(r) for r in c.execute("SELECT id,name,phone,role,sponsor_id,active,(kyc_doc IS NOT NULL) has_kyc FROM users WHERE deleted=0 AND role!='staff' ORDER BY active, id DESC LIMIT 300")]

def withdraw(b, c, adm):
    if adm.uid is None: raise Err("login required", 401)
    amt, upi = float(b.get("amount", 0)), (b.get("upi") or "").strip()
    if "@" not in upi: raise Err("sahi UPI ID daalein")
    if amt < settings(c)["min_withdraw"]: raise Err(f"minimum withdrawal {settings(c)['min_withdraw']}")
    if amt > wallet(b, c, adm, adm.uid)["available"]: raise Err("balance kam hai")
    cur = c.execute("INSERT INTO withdrawals(user_id,amount,upi,status,created) VALUES(?,?,?,'requested',?)", (adm.uid, amt, upi, time.time()))
    return {"withdrawal_id": cur.lastrowid}

def my_withdrawals(b, c, adm):
    if adm.uid is None: raise Err("login required", 401)
    return [dict(r) for r in c.execute("SELECT * FROM withdrawals WHERE user_id=? AND status!='deleted' ORDER BY id DESC LIMIT 50", (adm.uid,))]

def admin_withdrawals(b, c, adm):
    return [dict(r) for r in c.execute("SELECT w.*,u.name FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.status!='deleted' ORDER BY w.id DESC LIMIT 100")]

def withdraw_pay(b, c, adm, wid):
    w = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not c.execute("UPDATE withdrawals SET status='paid', ref=? WHERE id=? AND status='requested'", (b.get("ref"), wid)).rowcount: raise Err("not pending")
    notify(c, w["user_id"], "💸 Payment bhej diya", f"₹{w['amount']} aapke UPI {w['upi']} par bhej diya gaya.")
    return {"ok": True}

def withdraw_reject(b, c, adm, wid):
    w = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
    if not c.execute("UPDATE withdrawals SET status='rejected' WHERE id=? AND status='requested'", (wid,)).rowcount: raise Err("not pending")
    notify(c, w["user_id"], "⚠️ Withdrawal reject", f"₹{w['amount']} ki request reject hui. Support se baat karein.")
    return {"ok": True}

def my_commissions(b, c, adm):
    if adm.uid is None: raise Err("login required", 401)
    return [dict(r) for r in c.execute("""SELECT m.id,m.order_id,m.level,m.track,m.amount,m.status,u.name buyer FROM commissions m
        JOIN orders o ON o.id=m.order_id JOIN users u ON u.id=o.user_id WHERE m.earner_id=? ORDER BY m.id DESC LIMIT 100""", (adm.uid,))]

def order_detail(b, c, adm, oid):
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or (not adm and adm.uid not in (o["user_id"], o["seller_id"])): raise Err("not allowed", 403)
    items = c.execute("SELECT p.name,i.qty,i.unit_price FROM order_items i JOIN products p ON p.id=i.product_id WHERE i.order_id=?", (oid,)).fetchall()
    S = settings(c); bu = c.execute("SELECT name,phone,address FROM users WHERE id=?", (o["user_id"],)).fetchone()
    return {**dict(o), "items": [dict(i) for i in items], "buyer": dict(bu),
            "biz": {"name": S["biz_name"], "gstin": S["gstin"], "gst_pct": S["gst_pct"]}}

def me_update(b, c, adm):
    if adm.uid is None: raise Err("login required", 401)
    if b.get("new_pin"):
        u = c.execute("SELECT pin_hash FROM users WHERE id=?", (adm.uid,)).fetchone()
        if u["pin_hash"] != hpin(b.get("old_pin", "")) or len(str(b["new_pin"])) < 4: raise Err("purana PIN galat ya naya chhota hai")
        c.execute("UPDATE users SET pin_hash=? WHERE id=?", (hpin(b["new_pin"]), adm.uid))
    for k in ("name", "address"):
        if b.get(k): c.execute(f"UPDATE users SET {k}=? WHERE id=?", (b[k], adm.uid))
    if "wa_optin" in b: c.execute("UPDATE users SET wa_optin=? WHERE id=?", (1 if b["wa_optin"] else 0, adm.uid))
    return {"ok": True}

def admin_stats(b, c, adm):
    q = lambda sql: c.execute(sql).fetchone()[0]
    return {"users": q("SELECT COUNT(*) FROM users"), "unverified": q("SELECT COUNT(*) FROM users WHERE active=0"),
            "orders": q("SELECT COUNT(*) FROM orders WHERE status!='deleted'"), "to_verify": q("SELECT COUNT(*) FROM orders WHERE status='awaiting_payment'"),
            "to_deliver": q("SELECT COUNT(*) FROM orders WHERE status='paid'"),
            "sales": q("SELECT COALESCE(SUM(payable),0) FROM orders WHERE status='delivered'"),
            "commission": q("SELECT COALESCE(SUM(amount),0) FROM commissions WHERE status='approved'"),
            "payouts_due": q("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='requested'"),
            "low_stock": q("SELECT COUNT(*) FROM products WHERE active=1 AND stock<=10")}

def admin_products(b, c, adm): return [dict(r) for r in c.execute("SELECT * FROM products WHERE deleted=0 ORDER BY id DESC")]

def user_active(b, c, adm, uid):
    c.execute("UPDATE users SET active=? WHERE id=?", (1 if b.get("active") else 0, uid))
    if b.get("active"): notify(c, uid, "🎉 ID verify ho gayi", "Ab aap order kar sakte hain aur commission kama sakte hain.")
    return {"ok": True}

def order_cancel(b, c, adm, oid):
    o = c.execute("SELECT * FROM orders WHERE id=? AND status='awaiting_payment'", (oid,)).fetchone()
    if not o: raise Err("sirf unpaid order cancel hota hai")
    c.execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    c.execute("UPDATE users SET goods_credit=goods_credit+? WHERE id=?", (o["credit_used"], o["user_id"]))
    for r in c.execute("SELECT product_id,qty,cost FROM order_items WHERE order_id=?", (oid,)).fetchall():
        c.execute("UPDATE products SET stock=stock+? WHERE id=?", (r["qty"], r["product_id"]))
        c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,batch_no,created) VALUES(0,?,?,?,'RETURN',?)", (r["product_id"], r["qty"], r["cost"], time.time()))
    notify(c, o["user_id"], "❌ Order cancel", f"Order #{oid} cancel ho gaya.")
    return {"ok": True}

def me_kyc(b, c, adm):
    if adm.uid is None: raise Err("login required", 401)
    d = b.get("doc", "")
    if not img_ok(d, 900000): raise Err("photo (image, 600KB tak) bhejein")
    c.execute("UPDATE users SET kyc_doc=? WHERE id=?", (d, adm.uid)); return {"ok": True}

def admin_kyc(b, c, adm, uid):
    r = c.execute("SELECT kyc_doc,kyc_id,name FROM users WHERE id=?", (uid,)).fetchone()
    return {"doc": r["kyc_doc"], "kyc_id": r["kyc_id"], "name": r["name"]}

def admin_report(b, c, adm):
    since = time.time() - 30 * 86400
    daily = [dict(r) for r in c.execute("""SELECT date(created,'unixepoch') day, COUNT(*) orders, COALESCE(SUM(payable),0) sales FROM orders
        WHERE status IN ('paid','delivered') AND created>? GROUP BY day ORDER BY day""", (since,))]
    prods = [dict(r) for r in c.execute("""SELECT p.name, SUM(i.qty) qty, SUM(i.qty*i.unit_price) amount FROM order_items i JOIN products p ON p.id=i.product_id
        JOIN orders o ON o.id=i.order_id WHERE o.status IN ('paid','delivered') GROUP BY p.id ORDER BY amount DESC LIMIT 5""")]
    earners = [dict(r) for r in c.execute("""SELECT u.name, SUM(m.amount) total FROM commissions m JOIN users u ON u.id=m.earner_id
        WHERE m.status='approved' GROUP BY u.id ORDER BY total DESC LIMIT 5""")]
    rows = c.execute("SELECT id,date(created,'unixepoch') d,user_id,total,payable,status FROM orders ORDER BY id").fetchall()
    csv = "order_id,date,user_id,total,cash_paid,status\n" + "\n".join(",".join(str(x) for x in r) for r in rows)
    return {"daily": daily, "top_products": prods, "top_earners": earners, "csv": csv}

def settings_get(b, c, adm): return settings(c)
def settings_set(b, c, adm):
    for k, v in b.items():
        if k not in DEFAULTS: raise Err(f"unknown setting {k}")
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, json.dumps(v)))
    return settings(c)
def catalog(b, c, adm): return [dict(r) for r in c.execute("SELECT * FROM products WHERE active=1 AND deleted=0")]
def orders_list(b, c, adm): return [dict(r) for r in c.execute("SELECT * FROM orders WHERE status!='deleted' ORDER BY id DESC LIMIT 200")]

# (method, regex, handler, admin_only)
ROUTES = [
    ("POST", r"/login", login, False), ("GET", r"/me", me, False), ("GET", r"/my/orders", my_orders, False),
    ("POST", r"/withdraw", withdraw, False), ("GET", r"/my/withdrawals", my_withdrawals, False),
    ("GET", r"/admin/withdrawals", admin_withdrawals, True), ("POST", r"/admin/withdrawals/(\d+)/pay", withdraw_pay, True),
    ("POST", r"/admin/withdrawals/(\d+)/reject", withdraw_reject, True),
    ("GET", r"/my/commissions", my_commissions, False), ("GET", r"/orders/(\d+)", order_detail, False), ("POST", r"/me/update", me_update, False),
    ("GET", r"/admin/stats", admin_stats, True), ("GET", r"/admin/products", admin_products, True),
    ("POST", r"/admin/users/(\d+)/active", user_active, True), ("POST", r"/admin/orders/(\d+)/cancel", order_cancel, True),
    ("POST", r"/me/kyc", me_kyc, False), ("GET", r"/admin/users/(\d+)/kyc", admin_kyc, True), ("GET", r"/admin/report", admin_report, True),
    ("GET", r"/admin/users", admin_users, True), ("POST", r"/register", register, False), ("GET", r"/catalog", catalog, False),
    ("POST", r"/orders", order_create, False), ("GET", r"/wallet/(\d+)", wallet, False), ("GET", r"/tree/(\d+)", tree, False),
    ("POST", r"/admin/products", products_add, True), ("PATCH", r"/admin/products/(\d+)", products_edit, True),
    ("POST", r"/admin/orders/(\d+)/pay", order_pay, True), ("POST", r"/admin/orders/(\d+)/deliver", order_deliver, True),
    ("POST", r"/admin/orders/(\d+)/return", order_return, True), ("GET", r"/admin/orders", orders_list, True),
    ("GET", r"/admin/settings", settings_get, True), ("POST", r"/admin/settings", settings_set, True),
]

# ---------- notifications, offers, messages ----------
def need_login(adm):
    if adm.uid is None: raise Err("login required", 401)

def my_notifications(b, c, adm):
    need_login(adm)
    items = [dict(r) for r in c.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 40", (adm.uid,))]
    return {"items": items, "unread": sum(1 for i in items if not i["read"])}

def read_notifications(b, c, adm):
    need_login(adm); c.execute("UPDATE notifications SET read=1 WHERE user_id=?", (adm.uid,)); return {"ok": True}

def offers_list(b, c, adm): return [dict(r) for r in c.execute("SELECT * FROM offers WHERE active=1 ORDER BY id DESC LIMIT 10")]

def offer_add(b, c, adm):
    if not b.get("title"): raise Err("title chahiye")
    cur = c.execute("INSERT INTO offers(title,body,created) VALUES(?,?,?)", (b["title"], b.get("body", ""), time.time()))
    if b.get("notify"):
        for u in c.execute("SELECT id FROM users WHERE active=1").fetchall(): notify(c, u["id"], "🎁 " + b["title"], b.get("body", ""))
    return {"offer_id": cur.lastrowid}

def offer_del(b, c, adm, oid):
    c.execute("UPDATE offers SET active=0 WHERE id=?", (oid,)); return {"ok": True}

def broadcast(b, c, adm):
    if not b.get("title"): raise Err("title chahiye")
    q, a = "SELECT id FROM users WHERE active=1", ()
    if b.get("role"): q, a = q + " AND role=?", (b["role"],)
    rows = c.execute(q, a).fetchall()
    for u in rows: notify(c, u["id"], "📣 " + b["title"], b.get("body", ""))
    return {"sent": len(rows)}

def my_messages(b, c, adm):
    need_login(adm); return [dict(r) for r in c.execute("SELECT * FROM messages WHERE user_id=? ORDER BY id LIMIT 100", (adm.uid,))]

def my_send(b, c, adm):
    need_login(adm)
    if not (b.get("text") or "").strip(): raise Err("message khali hai")
    c.execute("INSERT INTO messages(user_id,from_admin,text,created) VALUES(?,0,?,?)", (adm.uid, b["text"][:1000], time.time()))
    r = ai_reply(c, adm.uid)
    if r:
        esc = "[ESCALATE]" in r; r = html.escape(r.replace("[ESCALATE]", "").strip())
        if esc: r += " 👤 Maine admin team ko bata diya, wo jald jawab denge."
        c.execute("INSERT INTO messages(user_id,from_admin,text,created) VALUES(?,?,?,?)", (adm.uid, 3 if esc else 2, r, time.time()))
    return {"ok": True, "ai": bool(r)}

def admin_threads(b, c, adm):
    return [dict(r) for r in c.execute("""SELECT m.user_id,u.name,m.text,m.created,m.from_admin FROM messages m JOIN users u ON u.id=m.user_id
        WHERE m.id IN (SELECT MAX(id) FROM messages GROUP BY user_id) ORDER BY m.id DESC LIMIT 100""")]

def admin_thread(b, c, adm, uid): return [dict(r) for r in c.execute("SELECT * FROM messages WHERE user_id=? ORDER BY id LIMIT 200", (uid,))]

def admin_reply(b, c, adm, uid):
    if not (b.get("text") or "").strip(): raise Err("message khali hai")
    c.execute("INSERT INTO messages(user_id,from_admin,text,created) VALUES(?,1,?,?)", (uid, b["text"][:1000], time.time()))
    notify(c, uid, "💬 Support ka jawab", b["text"][:90]); return {"ok": True}

# ---------- internal stock: purchase / sale / transfer ----------
def stock_user(c, adm):
    need_login(adm)
    r = c.execute("SELECT role,name FROM users WHERE id=?", (adm.uid,)).fetchone()
    if r["role"] == "customer": raise Err("stock sirf agent/franchise ke liye hai", 403)
    return r

def _num(b):
    try: pid, qty, unit = int(b["product_id"]), int(b["qty"]), float(b["unit_price"])
    except Exception: raise Err("product, qty aur rate sahi daalein")
    if qty < 1 or unit < 0: raise Err("qty/rate galat")
    return pid, qty, unit

def _consume(c, uid, pid, qty):  # FEFO: earliest expiry first
    got, need = [], qty
    for b in c.execute("SELECT id,qty_left,unit_cost,expiry,batch_no FROM batches WHERE user_id=? AND product_id=? AND qty_left>0 ORDER BY expiry IS NULL, expiry, id", (uid, pid)).fetchall():
        if need <= 0: break
        t = min(need, b["qty_left"]); c.execute("UPDATE batches SET qty_left=qty_left-? WHERE id=?", (t, b["id"]))
        got.append((t, b["unit_cost"], b["expiry"], b["batch_no"])); need -= t
    return got, need

def _stock_add(c, uid, pid, qty, unit, kind, party, expiry=None, batch_no=None, batches=None):
    if not c.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone(): raise Err("product nahi mila")
    if c.execute("SELECT 1 FROM inventory WHERE user_id=? AND product_id=?", (uid, pid)).fetchone():
        c.execute("UPDATE inventory SET qty=qty+?, cost_total=cost_total+? WHERE user_id=? AND product_id=?", (qty, qty * unit, uid, pid))
    else: c.execute("INSERT INTO inventory VALUES(?,?,?,?)", (uid, pid, qty, qty * unit))
    lid = c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created) VALUES(?,?,?,?,?,?,?,?)", (uid, pid, kind, qty, unit, unit, party, time.time())).lastrowid
    for q_, ex, bn in (batches or [(qty, expiry, batch_no)]):
        c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,expiry,batch_no,created,ref) VALUES(?,?,?,?,?,?,?,?)", (uid, pid, q_, unit, ex, bn, time.time(), f"L{lid}"))
    return lid

def _stock_remove(c, uid, pid, qty, unit, kind, party):
    r = c.execute("SELECT qty,cost_total FROM inventory WHERE user_id=? AND product_id=?", (uid, pid)).fetchone()
    if not r or r["qty"] < qty: raise Err("itna stock nahi hai")
    avg = r["cost_total"] / r["qty"]
    c.execute("UPDATE inventory SET qty=qty-?, cost_total=cost_total-? WHERE user_id=? AND product_id=?", (qty, avg * qty, uid, pid))
    lid = c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created) VALUES(?,?,?,?,?,?,?,?)", (uid, pid, kind, qty, unit, avg, party, time.time())).lastrowid
    got, _ = _consume(c, uid, pid, qty)
    return [(t, ex, bn) for t, _, ex, bn in got], lid

def _exp(b):
    e = b.get("expiry") or None
    if e and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", e): raise Err("expiry date YYYY-MM-DD mein dein")
    return e, (b.get("batch_no") or "")[:30]

def stock_purchase(b, c, adm):
    stock_user(c, adm); pid, qty, unit = _num(b); ex, bn = _exp(b); _stock_add(c, adm.uid, pid, qty, unit, "purchase", b.get("party", ""), ex, bn); return {"ok": True}

def stock_sale(b, c, adm):
    stock_user(c, adm); pid, qty, unit = _num(b); _stock_remove(c, adm.uid, pid, qty, unit, "sale", b.get("party", "")); return {"ok": True}

def stock_transfer(b, c, adm):
    me_ = stock_user(c, adm); pid, qty, unit = _num(b)
    to = c.execute("SELECT id,name,role,sponsor_id,active FROM users WHERE id=?", (b.get("to_user_id"),)).fetchone()
    if not to or to["sponsor_id"] != adm.uid or to["role"] == "customer" or not to["active"]: raise Err("sirf apne active agent/franchise ko transfer kar sakte hain")
    moved, lo = _stock_remove(c, adm.uid, pid, qty, unit, "transfer_out", to["name"])
    li = _stock_add(c, to["id"], pid, qty, unit, "transfer_in", me_["name"], batches=moved or None)
    c.execute("UPDATE stock_ledger SET ref=? WHERE id=?", (f"T{li}", lo)); c.execute("UPDATE stock_ledger SET ref=? WHERE id=?", (f"T{lo}", li))
    notify(c, to["id"], "📦 Stock mila", f"{me_['name']} ne aapko {qty} piece ₹{unit} rate se diye.")
    return {"ok": True}

def stock_mine(b, c, adm):
    stock_user(c, adm); u = adm.uid
    items = [dict(r) for r in c.execute("""SELECT i.product_id,p.name,i.qty,i.cost_total/i.qty avg_cost,p.mrp FROM inventory i JOIN products p ON p.id=i.product_id
        WHERE i.user_id=? AND i.qty>0 ORDER BY p.name""", (u,))]
    ledger = [dict(r) for r in c.execute("""SELECT l.*,p.name FROM stock_ledger l JOIN products p ON p.id=l.product_id WHERE l.user_id=? ORDER BY l.id DESC LIMIT 50""", (u,))]
    q = lambda sql: c.execute(sql, (u,)).fetchone()[0]
    return {"items": items, "ledger": ledger, "summary": {
        "stock_value": round(q("SELECT COALESCE(SUM(cost_total),0) FROM inventory WHERE user_id=?"), 2),
        "bought": round(q("SELECT COALESCE(SUM(qty*unit_price),0) FROM stock_ledger WHERE user_id=? AND kind IN ('purchase','transfer_in')"), 2),
        "sold": round(q("SELECT COALESCE(SUM(qty*unit_price),0) FROM stock_ledger WHERE user_id=? AND kind IN ('sale','transfer_out')"), 2),
        "profit": round(q("SELECT COALESCE(SUM((unit_price-cost)*qty),0) FROM stock_ledger WHERE user_id=? AND kind IN ('sale','transfer_out')"), 2)}}

def admin_supply(b, c, adm):  # company -> franchise/agent (internal sale)
    pid, qty, unit = _num(b)
    u = c.execute("SELECT id,role FROM users WHERE id=?", (b.get("user_id"),)).fetchone()
    p = c.execute("SELECT stock,cost FROM products WHERE id=?", (pid,)).fetchone()
    if not u or u["role"] == "customer": raise Err("agent/franchise chunein")
    if not p or p["stock"] < qty: raise Err("company stock kam hai")
    c.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
    got, need = _consume(c, 0, pid, qty)
    avg = (sum(t * uc for t, uc, _, _ in got) + need * (p["cost"] or 0)) / qty
    cid = c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created) VALUES(0,?,'supply_out',?,?,?,?,?)", (pid, qty, unit, avg, f"user {u['id']}", time.time())).lastrowid
    lid = _stock_add(c, u["id"], pid, qty, unit, "purchase", "Company", batches=[(t, ex, bn) for t, _, ex, bn in got] or None)
    c.execute("UPDATE stock_ledger SET ref=? WHERE id=?", (f"C{cid}", lid)); c.execute("UPDATE stock_ledger SET ref=? WHERE id=?", (f"U{lid}", cid))
    notify(c, u["id"], "📦 Company se stock mila", f"{qty} piece ₹{unit} rate se aapke stock mein jude.")
    return {"ok": True}

def admin_purchase(b, c, adm):  # company buys stock from supplier (with expiry)
    pid, qty, unit = _num(b); ex, bn = _exp(b)
    if not c.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone(): raise Err("product nahi mila")
    c.execute("UPDATE products SET stock=stock+?, cost=? WHERE id=?", (qty, unit, pid))
    c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,expiry,batch_no,created) VALUES(0,?,?,?,?,?,?)", (pid, qty, unit, ex, bn, time.time()))
    c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created) VALUES(0,?,'purchase',?,?,?,?,?)", (pid, qty, unit, unit, b.get("party", ""), time.time()))
    return {"ok": True}

# ---------- reports ----------
def _since(days): return time.time() - days * 86400 if days else 0
def _R(title, cols, rows, summary=None): return {"title": title, "columns": cols, "rows": rows, "summary": summary or []}
def _n(x): return round(x or 0, 2)
AR, UR = {}, {}
def ar(f): AR[f.__name__[2:]] = f; return f
def ur(f): UR[f.__name__[2:]] = f; return f
D = "date(created,'unixepoch')"

@ar
def a_pnl(c, days):
    since = _since(days); q = lambda sql: c.execute(sql, (since,)).fetchone()[0] or 0
    rev = q("SELECT SUM(total) FROM orders WHERE status='delivered' AND delivered_at>=?")
    cogs = q("SELECT SUM(i.cost*i.qty) FROM order_items i JOIN orders o ON o.id=i.order_id WHERE o.status='delivered' AND o.delivered_at>=?")
    sup = q("SELECT SUM(qty*unit_price) FROM stock_ledger WHERE user_id=0 AND kind='supply_out' AND created>=?")
    supc = q("SELECT SUM(qty*cost) FROM stock_ledger WHERE user_id=0 AND kind='supply_out' AND created>=?")
    comm = q("SELECT SUM(m.amount) FROM commissions m JOIN orders o ON o.id=m.order_id WHERE m.status='approved' AND o.delivered_at>=?")
    ret = q("SELECT SUM(total) FROM orders WHERE status='returned' AND created>=?")
    pur = q("SELECT SUM(qty*unit_price) FROM stock_ledger WHERE user_id=0 AND kind='purchase' AND created>=?")
    brev = q("SELECT SUM(CASE kind WHEN 'sale' THEN total WHEN 'sale_return' THEN -total ELSE 0 END) FROM bills WHERE status='active' AND ts>=?")
    bcogs = q("SELECT SUM(CASE kind WHEN 'bill_sale' THEN qty*cost ELSE -qty*cost END) FROM stock_ledger WHERE user_id=0 AND kind IN ('bill_sale','sale_return') AND created>=?")
    cogs += bcogs
    gross = rev + sup + brev - cogs - supc; net = gross - comm; tot = rev + sup + brev
    rows = [["Sales (app orders, delivered)", _n(rev)], ["Counter / Bill sales", _n(brev)], ["Supply to franchise/agents", _n(sup)], ["Cost of goods sold", _n(-(cogs + supc))],
            ["GROSS PROFIT", _n(gross)], ["Commission (approved)", _n(-comm)], ["NET PROFIT / (LOSS)", _n(net)]]
    sm = [["Net profit", _n(net)], ["Margin %", round(net / tot * 100, 1) if tot else 0], ["Purchases (info)", _n(pur)], ["Returns (info)", _n(ret)]]
    if tot and not (cogs + supc): sm.append(["⚠️ Cost 0 hai", "Product cost ya purchase daalein"])
    return _R("Profit & Loss", ["Detail", "Amount ₹"], rows, sm)

@ar
def a_sales(c, days):
    r = c.execute(f"SELECT {D} d, COUNT(*), SUM(payable), SUM(total) FROM orders WHERE status IN ('paid','delivered') AND created>=? GROUP BY d ORDER BY d DESC", (_since(days),)).fetchall()
    return _R("Sales (daily)", ["Date", "Orders", "Cash ₹", "Total ₹"], [[x[0], x[1], _n(x[2]), _n(x[3])] for x in r], [["Total sales", _n(sum(x[3] or 0 for x in r))], ["Orders", sum(x[1] for x in r)]])

@ar
def a_product_sales(c, days):
    r = c.execute("""SELECT p.name, SUM(i.qty), SUM(i.qty*i.unit_price), SUM(i.qty*i.cost) FROM order_items i JOIN products p ON p.id=i.product_id
        JOIN orders o ON o.id=i.order_id WHERE o.status='delivered' AND o.delivered_at>=? GROUP BY p.id ORDER BY 3 DESC""", (_since(days),)).fetchall()
    return _R("Product-wise sales", ["Product", "Qty", "Sales ₹", "Cost ₹", "Profit ₹"], [[x[0], x[1], _n(x[2]), _n(x[3]), _n(x[2] - x[3])] for x in r])

@ar
def a_purchases(c, days):
    r = c.execute("""SELECT date(l.created,'unixepoch'), p.name, l.qty, l.unit_price, l.qty*l.unit_price, l.party FROM stock_ledger l JOIN products p ON p.id=l.product_id
        WHERE l.user_id=0 AND l.kind='purchase' AND l.created>=? ORDER BY l.id DESC""", (_since(days),)).fetchall()
    return _R("Purchase report (company)", ["Date", "Product", "Qty", "Rate ₹", "Amount ₹", "Supplier"], [list(x) for x in r], [["Total purchase", _n(sum(x[4] for x in r))]])

@ar
def a_expiry(c, days):
    w = days or 90
    r = c.execute("""SELECT CASE WHEN b.user_id=0 THEN 'Company' ELSE u.name END, p.name, b.batch_no, b.qty_left, b.expiry,
        CAST(julianday(b.expiry)-julianday('now') AS INTEGER) FROM batches b JOIN products p ON p.id=b.product_id LEFT JOIN users u ON u.id=b.user_id
        WHERE b.qty_left>0 AND b.expiry IS NOT NULL AND b.expiry<=date('now','+'||?||' day') ORDER BY b.expiry""", (w,)).fetchall()
    rows = [[x[0], x[1], x[2], x[3], x[4], x[5], "EXPIRED" if x[5] < 0 else "Jaldi" if x[5] <= 30 else "Dhyaan"] for x in r]
    return _R(f"Expiry report ({w} din)", ["Kiske paas", "Product", "Batch", "Qty", "Expiry", "Din bache", "Status"], rows,
              [["Expired qty", sum(x[3] for x in r if x[5] < 0)], ["Jaldi expire qty", sum(x[3] for x in r if x[5] >= 0)]])

@ar
def a_inventory(c, days):
    r = c.execute("SELECT name,stock,cost,stock*cost,mrp FROM products WHERE active=1 ORDER BY stock").fetchall()
    return _R("Stock valuation (company)", ["Product", "Stock", "Cost ₹", "Value ₹", "MRP ₹", "Status"], [[x[0], x[1], _n(x[2]), _n(x[3]), x[4], "LOW" if x[1] <= 10 else "ok"] for x in r],
              [["Total stock value", _n(sum(x[3] for x in r))], ["Low stock items", sum(1 for x in r if x[1] <= 10)]])

@ar
def a_team_stock(c, days):
    r = c.execute("""SELECT u.name,u.role,p.name,i.qty,i.cost_total FROM inventory i JOIN users u ON u.id=i.user_id JOIN products p ON p.id=i.product_id WHERE i.qty>0 ORDER BY u.name""").fetchall()
    return _R("Franchise/Agent stock", ["Naam", "Role", "Product", "Qty", "Value ₹"], [[x[0], x[1], x[2], x[3], _n(x[4])] for x in r], [["Total value", _n(sum(x[4] for x in r))]])

@ar
def a_commission(c, days):
    r = c.execute("""SELECT u.name,u.role,SUM(CASE WHEN m.status='approved' THEN m.amount ELSE 0 END),SUM(CASE WHEN m.status='pending' THEN m.amount ELSE 0 END),
        SUM(CASE WHEN m.status='reversed' THEN m.amount ELSE 0 END),COUNT(*) FROM commissions m JOIN users u ON u.id=m.earner_id JOIN orders o ON o.id=m.order_id
        WHERE o.created>=? GROUP BY u.id ORDER BY 3 DESC""", (_since(days),)).fetchall()
    return _R("Commission report", ["Naam", "Role", "Approved ₹", "Pending ₹", "Reversed ₹", "Entries"], [[x[0], x[1], _n(x[2]), _n(x[3]), _n(x[4]), x[5]] for x in r], [["Total approved", _n(sum(x[2] for x in r))]])

@ar
def a_customers(c, days):
    r = c.execute("""SELECT u.name,u.phone,COUNT(*),SUM(o.total) FROM orders o JOIN users u ON u.id=o.user_id WHERE o.status='delivered' AND o.delivered_at>=? GROUP BY u.id ORDER BY 4 DESC LIMIT 50""", (_since(days),)).fetchall()
    return _R("Top customers", ["Naam", "Phone", "Orders", "Total ₹"], [[x[0], x[1], x[2], _n(x[3])] for x in r])

@ar
def a_returns(c, days):
    r = c.execute("SELECT o.id,date(o.created,'unixepoch'),u.name,o.total FROM orders o JOIN users u ON u.id=o.user_id WHERE o.status='returned' AND o.created>=? ORDER BY o.id DESC", (_since(days),)).fetchall()
    return _R("Returns", ["Order", "Date", "Customer", "Amount ₹"], [[x[0], x[1], x[2], _n(x[3])] for x in r], [["Total returns", _n(sum(x[3] for x in r))]])

@ar
def a_users(c, days):
    r = c.execute(f"SELECT {D},COUNT(*) FROM users WHERE created>=? GROUP BY 1 ORDER BY 1 DESC", (_since(days),)).fetchall()
    return _R("New users", ["Date", "Naye users"], [list(x) for x in r], [["Naye users (total)", sum(x[1] for x in r)]])

@ar
def a_payouts(c, days):
    r = c.execute("SELECT date(w.created,'unixepoch'),u.name,w.amount,w.upi,w.status,w.ref FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.created>=? ORDER BY w.id DESC", (_since(days),)).fetchall()
    return _R("Payouts", ["Date", "Naam", "Amount ₹", "UPI", "Status", "Ref"], [list(x) for x in r], [["Paid", _n(sum(x[2] for x in r if x[4] == "paid"))], ["Pending", _n(sum(x[2] for x in r if x[4] == "requested"))]])

@ur
def u_pnl(c, u, days):
    since = _since(days); q = lambda k: c.execute(f"SELECT COALESCE(SUM({k}),0) FROM stock_ledger WHERE user_id=? AND created>=? AND kind IN %s", (u, since)).fetchone()[0]
    bought = c.execute("SELECT COALESCE(SUM(qty*unit_price),0) FROM stock_ledger WHERE user_id=? AND created>=? AND kind IN ('purchase','transfer_in')", (u, since)).fetchone()[0]
    sold = c.execute("SELECT COALESCE(SUM(qty*unit_price),0) FROM stock_ledger WHERE user_id=? AND created>=? AND kind IN ('sale','transfer_out')", (u, since)).fetchone()[0]
    prof = c.execute("SELECT COALESCE(SUM((unit_price-cost)*qty),0) FROM stock_ledger WHERE user_id=? AND created>=? AND kind IN ('sale','transfer_out')", (u, since)).fetchone()[0]
    comm = c.execute("SELECT COALESCE(SUM(m.amount),0) FROM commissions m JOIN orders o ON o.id=m.order_id WHERE m.earner_id=? AND m.status='approved' AND o.created>=?", (u, since)).fetchone()[0]
    rows = [["Bikri (sale + team ko)", _n(sold)], ["Khareed", _n(-bought)], ["Bikri par munafa", _n(prof)], ["Commission kamaya", _n(comm)], ["KUL MUNAFA", _n(prof + comm)]]
    return _R("Mera Profit / Loss", ["Detail", "Amount ₹"], rows, [["Kul munafa", _n(prof + comm)]])

def _ul(kinds, title, with_profit=False):
    def f(c, u, days):
        r = c.execute(f"""SELECT date(l.created,'unixepoch'),p.name,l.kind,l.qty,l.unit_price,l.qty*l.unit_price,(l.unit_price-l.cost)*l.qty,l.party FROM stock_ledger l
            JOIN products p ON p.id=l.product_id WHERE l.user_id=? AND l.created>=? AND l.kind IN ({kinds}) ORDER BY l.id DESC""", (u, _since(days))).fetchall()
        cols = ["Date", "Product", "Type", "Qty", "Rate ₹", "Amount ₹", "Munafa ₹", "Party"]
        return _R(title, cols, [[x[0], x[1], x[2], x[3], _n(x[4]), _n(x[5]), _n(x[6]), x[7]] for x in r], [["Total", _n(sum(x[5] for x in r))]] + ([["Munafa", _n(sum(x[6] for x in r))]] if with_profit else []))
    return f
UR["purchases"] = _ul("'purchase','transfer_in'", "Meri khareed")
UR["sales"] = _ul("'sale','transfer_out'", "Meri bikri", True)

@ur
def u_expiry(c, u, days):
    w = days or 90
    r = c.execute("""SELECT p.name,b.batch_no,b.qty_left,b.expiry,CAST(julianday(b.expiry)-julianday('now') AS INTEGER) FROM batches b JOIN products p ON p.id=b.product_id
        WHERE b.user_id=? AND b.qty_left>0 AND b.expiry IS NOT NULL AND b.expiry<=date('now','+'||?||' day') ORDER BY b.expiry""", (u, w)).fetchall()
    return _R(f"Mera expiry report ({w} din)", ["Product", "Batch", "Qty", "Expiry", "Din bache", "Status"], [[x[0], x[1], x[2], x[3], x[4], "EXPIRED" if x[4] < 0 else "Jaldi" if x[4] <= 30 else "Dhyaan"] for x in r])

@ur
def u_stock(c, u, days):
    r = c.execute("SELECT p.name,i.qty,i.cost_total/i.qty,i.cost_total,p.mrp FROM inventory i JOIN products p ON p.id=i.product_id WHERE i.user_id=? AND i.qty>0", (u,)).fetchall()
    return _R("Mera stock", ["Product", "Qty", "Avg cost ₹", "Value ₹", "MRP ₹"], [[x[0], x[1], _n(x[2]), _n(x[3]), x[4]] for x in r], [["Stock value", _n(sum(x[3] for x in r))]])

@ur
def u_commission(c, u, days):
    r = c.execute("""SELECT date(o.created,'unixepoch'),m.order_id,m.level,m.track,m.amount,m.status FROM commissions m JOIN orders o ON o.id=m.order_id
        WHERE m.earner_id=? AND o.created>=? ORDER BY m.id DESC""", (u, _since(days))).fetchall()
    return _R("Meri commission", ["Date", "Order", "Level", "Type", "Amount ₹", "Status"], [[x[0], x[1], x[2], x[3], _n(x[4]), x[5]] for x in r], [["Approved", _n(sum(x[4] for x in r if x[5] == "approved"))]])

def admin_report2(b, c, adm, name, days):
    if name not in AR: raise Err("report nahi mila", 404)
    return AR[name](c, days)

def my_report(b, c, adm, name, days):
    stock_user(c, adm)
    if name not in UR: raise Err("report nahi mila", 404)
    return UR[name](c, adm.uid, days)

# ---------- AI support ----------
def ai_reply(c, uid):
    key = os.environ.get("ANTHROPIC_API_KEY"); S = settings(c)
    if not key or not S.get("ai_support", True): return None
    if c.execute("SELECT COUNT(*) FROM messages WHERE user_id=? AND from_admin>=2 AND created>?", (uid, time.time() - 86400)).fetchone()[0] >= 20: return None
    u = c.execute("SELECT name,role,active FROM users WHERE id=?", (uid,)).fetchone()
    orders = c.execute("SELECT id,status,total FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 3", (uid,)).fetchall()
    hist = c.execute("SELECT from_admin,text FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,)).fetchall()[::-1]
    while hist and hist[0]["from_admin"]: hist = hist[1:]
    ctx = f"User: {u['name']} ({u['role']}), ID verified: {bool(u['active'])}. Recent orders: " + ("; ".join(f"#{o['id']} {o['status']} Rs{o['total']}" for o in orders) or "none")
    system = (f"Tum {S['biz_name']} app ke customer support assistant ho. Hinglish mein chhota (3-4 line), dosti bhara jawab do. {ctx}. "
              f"App ke rules: koi joining fee nahi; ID admin verify karta hai; payment UPI se ({S['upi_id']}) aur admin verify karta hai; delivery ke baad {S['return_days']} din tak return, "
              "return par saman credit milta hai (cash refund nahi); commission sirf delivered saman ki sale par banta hai; withdrawal Wallet tab se UPI par. "
              "Dawai ki salah, dose, bimari ya ilaaj ke sawal par mana karo aur doctor/pharmacist se milne ko kaho. Kamai ya income ki guarantee kabhi mat do. "
              "PIN ya password kabhi mat poochho. Payment/paise ka vivaad, shikayat, ya jawab na pata ho to jawab ke end mein exactly [ESCALATE] likho.")
    try:
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps({"model": os.environ.get("AI_MODEL", "claude-haiku-5-5"), "max_tokens": 300, "system": system,
              "messages": [{"role": "assistant" if m["from_admin"] else "user", "content": html.unescape(m["text"])} for m in hist]}).encode(),
              {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=25).read())["content"][0]["text"]
    except Exception as e:
        print("AI error:", e); return None

ROUTES += [
    ("GET", r"/my/notifications", my_notifications, False), ("POST", r"/my/notifications/read", read_notifications, False),
    ("GET", r"/offers", offers_list, False), ("POST", r"/admin/offers", offer_add, True), ("POST", r"/admin/offers/(\d+)/delete", offer_del, True),
    ("POST", r"/admin/broadcast", broadcast, True),
    ("GET", r"/my/messages", my_messages, False), ("POST", r"/my/messages", my_send, False),
    ("GET", r"/admin/messages", admin_threads, True), ("GET", r"/admin/messages/(\d+)", admin_thread, True), ("POST", r"/admin/messages/(\d+)", admin_reply, True),
    ("POST", r"/stock/purchase", stock_purchase, False), ("POST", r"/stock/sale", stock_sale, False), ("POST", r"/stock/transfer", stock_transfer, False),
    ("GET", r"/stock/mine", stock_mine, False), ("POST", r"/admin/stock/supply", admin_supply, True), ("POST", r"/admin/purchase", admin_purchase, True),
    ("GET", r"/admin/reports/(\w+)/(\d+)", admin_report2, True), ("GET", r"/my/reports/(\w+)/(\d+)", my_report, False),
]

# ---------- team & business details ----------
def team_biz(c, uid):
    ds = c.execute(LV + " SELECT id,l FROM d", (uid,)).fetchall()
    first = repeat = 0.0; seen = set()
    for o in c.execute(LV + " SELECT o.user_id,o.total FROM orders o JOIN d ON d.id=o.user_id WHERE o.status='delivered' ORDER BY o.id", (uid,)):
        if o["user_id"] in seen: repeat += o["total"]
        else: seen.add(o["user_id"]); first += o["total"]
    return {"team": len(ds), "direct": sum(1 for r in ds if r["l"] == 1), "first_biz": round(first, 2), "repeat_biz": round(repeat, 2)}

def team_summary(c, uid):
    S = settings(c); q = lambda st: round(c.execute("SELECT COALESCE(SUM(amount),0) FROM commissions WHERE earner_id=? AND status=?", (uid, st)).fetchone()[0], 2)
    return {**rank_info(c, uid), **team_biz(c, uid), "base": S["level_base"], "mult": S["level_mult"],
            "commission": {"approved": q("approved"), "pending": q("pending"), "reversed": q("reversed")},
            "my_sales": round(c.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE seller_id=? AND status='delivered'", (uid,)).fetchone()[0], 2),
            "my_purchase": round(c.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE user_id=? AND status='delivered'", (uid,)).fetchone()[0], 2)}

def my_team(b, c, adm):
    need_login(adm); return team_summary(c, adm.uid)

def admin_team(b, c, adm, uid):
    u = c.execute("SELECT id,name,phone,role,active,sponsor_id FROM users WHERE id=?", (uid,)).fetchone()
    if not u: raise Err("user nahi mila", 404)
    return {"user": dict(u), **team_summary(c, uid)}

def _leaders(c):
    out = []
    for r in c.execute("SELECT DISTINCT sponsor_id FROM users WHERE sponsor_id IS NOT NULL").fetchall():
        u = c.execute("SELECT id,name,role FROM users WHERE id=?", (r[0],)).fetchone()
        if not u: continue
        tb = team_biz(c, u["id"])
        out.append({"id": u["id"], "name": u["name"], "role": u["role"], "rank": rank_info(c, u["id"])["rank_name"], **tb, "business": round(tb["first_biz"] + tb["repeat_biz"], 2)})
    return sorted(out, key=lambda x: -x["business"])

def admin_dashboard(b, c, adm):
    q = lambda sql, a=(): c.execute(sql, a).fetchone()[0] or 0
    t = time.localtime(); day0 = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    counts = {r["role"]: r["n"] for r in c.execute("SELECT role,COUNT(*) n FROM users GROUP BY role")}; counts["total"] = sum(counts.values())
    first = repeat = 0.0; seen = set()
    for o in c.execute("SELECT user_id,total FROM orders WHERE status='delivered' ORDER BY id"):
        if o["user_id"] in seen: repeat += o["total"]
        else: seen.add(o["user_id"]); first += o["total"]
    comm = {r["status"]: r["s"] for r in c.execute("SELECT status,SUM(amount) s FROM commissions GROUP BY status")}
    L = _leaders(c)
    return {"counts": counts, "products": q("SELECT COUNT(*) FROM products WHERE active=1"),
        "orders": {"total": q("SELECT COUNT(*) FROM orders WHERE status!='deleted'"), "awaiting": q("SELECT COUNT(*) FROM orders WHERE status='awaiting_payment'"),
                   "paid": q("SELECT COUNT(*) FROM orders WHERE status='paid'"), "delivered": q("SELECT COUNT(*) FROM orders WHERE status='delivered'")},
        "sales": {"today": _n(q("SELECT SUM(total) FROM orders WHERE status='delivered' AND delivered_at>=?", (day0,))),
                  "month": _n(q("SELECT SUM(total) FROM orders WHERE status='delivered' AND delivered_at>=?", (time.time() - 30 * 86400,))),
                  "total": _n(q("SELECT SUM(total) FROM orders WHERE status='delivered'"))},
        "business": {"first": _n(first), "repeat": _n(repeat), "total": _n(first + repeat)},
        "commission": {"approved": _n(comm.get("approved")), "pending": _n(comm.get("pending")), "reversed": _n(comm.get("reversed"))},
        "stock": {"value": _n(q("SELECT SUM(stock*cost) FROM products WHERE active=1")), "low": q("SELECT COUNT(*) FROM products WHERE active=1 AND stock<=10"),
                  "expiring": q("SELECT COUNT(*) FROM batches WHERE user_id=0 AND qty_left>0 AND expiry IS NOT NULL AND expiry<=date('now','+30 day')")},
        "pending": [dict(r) for r in c.execute("""SELECT o.id,u.name,u.phone,o.payable,CAST((?-o.created)/86400 AS INTEGER) days FROM orders o JOIN users u ON u.id=o.user_id
                    WHERE o.status='awaiting_payment' ORDER BY o.id LIMIT 10""", (time.time(),))],
        "leaders": L[:10], "dues": _dues(c),
        "categories": [dict(r) for r in c.execute("""SELECT p.category name, SUM(i.qty*i.unit_price) amount FROM order_items i JOIN products p ON p.id=i.product_id
                    JOIN orders o ON o.id=i.order_id WHERE o.status='delivered' GROUP BY p.category ORDER BY 2 DESC""")]}

@ar
def a_team(c, days):
    rows = [[x["name"], x["role"], x["rank"], x["direct"], x["team"], x["first_biz"], x["repeat_biz"], x["business"]] for x in _leaders(c)]
    return _R("Team & repurchase", ["Naam", "Role", "Rank", "Direct", "Team", "Pehli khareed ₹", "Repurchase ₹", "Total ₹"], rows,
              [["Leaders (users)", len(rows)], ["Total repurchase", _n(sum(x[6] for x in rows))]])

ROUTES += [("GET", r"/my/team", my_team, False), ("GET", r"/admin/team/(\d+)", admin_team, True), ("GET", r"/admin/dashboard", admin_dashboard, True)]

# ================= v6: billing, parties, staff, edit/delete, audit =================
PERMS = ("billing", "items", "orders", "users", "commission", "reports", "support")
PERM_MAP = [(r"^/admin/users$", {"users", "billing", "items"}), (r"^/admin/(bills|bill|parties|payments|ledger|purchase)", {"billing"}), (r"^/admin/stock", {"billing", "items"}),
            (r"^/admin/products", {"items", "billing"}), (r"^/admin/orders", {"orders"}), (r"^/admin/users", {"users"}), (r"^/admin/withdrawals", {"commission"}),
            (r"^/admin/(report|reports|dashboard|stats|team)", {"reports"}), (r"^/admin/(offers|broadcast|messages)", {"support"})]

def route_perm(method, path):  # edit/delete/settings/staff/audit are admin-only
    if method == "PATCH" or re.search(r"/(delete|edit)$", path) or re.match(r"^/admin/(settings|staff|audit)", path): return None
    for rx, p in PERM_MAP:
        if re.match(rx, path): return p
    return None

LABELS = [(r"/admin/bills", "Bill banaya"), (r"/admin/bills/\d+/edit", "Bill edit"), (r"/admin/bills/\d+/delete", "Bill DELETE"), (r"/admin/parties", "Party jodi"),
          (r"/admin/parties/\d+/edit", "Party edit"), (r"/admin/parties/\d+/delete", "Party DELETE"), (r"/admin/payments", "Payment entry"), (r"/admin/payments/\d+/edit", "Payment edit"),
          (r"/admin/payments/\d+/delete", "Payment DELETE"), (r"/admin/products", "Item jodi"), (r"/admin/products/bulk", "Bulk items import"), (r"/admin/products/\d+/delete", "Item DELETE"),
          (r"/admin/stock/adjust", "Stock adjust"), (r"/admin/stock/supply", "Team ko stock diya"), (r"/admin/purchase", "Company purchase"),
          (r"/admin/orders/\d+/pay", "Order payment verify"), (r"/admin/orders/\d+/deliver", "Order delivered"), (r"/admin/orders/\d+/return", "Order return"),
          (r"/admin/orders/\d+/cancel", "Order cancel"), (r"/admin/orders/\d+/delete", "Order DELETE"), (r"/admin/orders/\d+/edit", "Order edit"),
          (r"/admin/users/\d+/active", "User verify/block"), (r"/admin/users/\d+/edit", "User edit"), (r"/admin/users/\d+/delete", "User DELETE"),
          (r"/admin/withdrawals/\d+/pay", "Withdrawal paid"), (r"/admin/withdrawals/\d+/reject", "Withdrawal reject"), (r"/admin/withdrawals/\d+/delete", "Withdrawal DELETE"),
          (r"/admin/ledger/\d+/edit", "Team ledger edit"), (r"/admin/ledger/\d+/delete", "Team ledger DELETE"), (r"/admin/staff", "Staff banaya"),
          (r"/admin/staff/\d+/edit", "Staff edit"), (r"/admin/staff/\d+/delete", "Staff DELETE"), (r"/admin/settings", "Settings badli"), (r"/admin/offers", "Offer lagaya"),
          (r"/admin/offers/\d+/edit", "Offer edit"), (r"/admin/offers/\d+/delete", "Offer hataya"), (r"/admin/broadcast", "Broadcast"), (r"/admin/messages/\d+", "Support reply"),
          (r"/stock/purchase", "Stock khareed"), (r"/stock/sale", "Stock bikri"), (r"/stock/transfer", "Stock transfer")]

def audit_log(ctx, method, path, body, out):
    skip = {"image", "doc", "pin", "new_pin", "old_pin", "csv"}
    d = {k: (v if not isinstance(v, (list, dict)) or k == "perms" else f"[{len(v)}]") for k, v in body.items() if k not in skip}
    if isinstance(out, dict):
        for k in ("bill_id", "user_id", "product_id", "payment_id", "party_id", "staff_id", "offer_id", "added", "mode"):
            if k in out: d["→" + k] = out[k]
    label = next((l for rx, l in LABELS if re.fullmatch(rx, path)), path)
    with tx() as c:
        c.execute("INSERT INTO audit(ts,actor_id,actor,role,action,path,detail) VALUES(?,?,?,?,?,?,?)",
                  (time.time(), actor_id(ctx), actor(ctx), "admin" if ctx.adm else ctx.role, label, f"{method} {path}", json.dumps(d, ensure_ascii=False)[:500]))

def audit_list(b, c, adm, n): return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(n, 500),))]

# ---- parties & payments ----
def _party_rows(c):
    return [dict(r) for r in c.execute("""SELECT p.*,
      p.opening + COALESCE((SELECT SUM(total-paid) FROM bills WHERE party_id=p.id AND kind='sale' AND status='active'),0) - COALESCE((SELECT SUM(total-paid) FROM bills WHERE party_id=p.id AND kind='sale_return' AND status='active'),0) - COALESCE((SELECT SUM(amount) FROM payments WHERE party_id=p.id AND kind='in' AND status='active'),0) AS recv,
      COALESCE((SELECT SUM(total-paid) FROM bills WHERE party_id=p.id AND kind='purchase' AND status='active'),0) - COALESCE((SELECT SUM(total-paid) FROM bills WHERE party_id=p.id AND kind='purchase_return' AND status='active'),0) - COALESCE((SELECT SUM(amount) FROM payments WHERE party_id=p.id AND kind='out' AND status='active'),0) AS pay
      FROM parties p WHERE p.active=1 ORDER BY p.name""")]

def _dues(c):
    rows = _party_rows(c); t = time.localtime(); d0 = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return {"recv": _n(sum(max(0, r["recv"]) for r in rows)), "pay": _n(sum(max(0, r["pay"]) for r in rows)),
            "today": c.execute("SELECT COUNT(*) FROM bills WHERE status='active' AND created>=?", (d0,)).fetchone()[0]}

def parties_list(b, c, adm): return _party_rows(c)

def _pfields(b):
    if not (b.get("name") or "").strip(): raise Err("naam daalein")
    return ((b["name"].strip())[:80], (b.get("phone") or "")[:20], b.get("ptype") if b.get("ptype") in ("customer", "supplier", "both") else "both",
            (b.get("address") or "")[:200], (b.get("gstin") or "")[:20], float(b.get("opening") or 0))

def party_add(b, c, adm):
    cur = c.execute("INSERT INTO parties(name,phone,ptype,address,gstin,opening,created) VALUES(?,?,?,?,?,?,?)", (*_pfields(b), time.time())); return {"party_id": cur.lastrowid}

def party_edit(b, c, adm, pid):
    c.execute("UPDATE parties SET name=?,phone=?,ptype=?,address=?,gstin=?,opening=? WHERE id=?", (*_pfields(b), pid)); return {"ok": True}

def party_delete(b, c, adm, pid):
    used = c.execute("SELECT (SELECT COUNT(*) FROM bills WHERE party_id=?)+(SELECT COUNT(*) FROM payments WHERE party_id=?)", (pid, pid)).fetchone()[0]
    if used: c.execute("UPDATE parties SET active=0 WHERE id=?", (pid,)); return {"mode": "soft"}
    c.execute("DELETE FROM parties WHERE id=?", (pid,)); return {"mode": "hard"}

def party_ledger(b, c, adm, pid):
    p = c.execute("SELECT * FROM parties WHERE id=?", (pid,)).fetchone()
    if not p: raise Err("party nahi mili", 404)
    ev = []
    for r in c.execute("SELECT * FROM bills WHERE party_id=? AND status='active'", (pid,)):
        d = (r["total"] - r["paid"]) * (1 if r["kind"] in ("sale", "purchase_return") else -1)
        ev.append({"ts": r["ts"], "type": r["kind"], "ref_id": r["id"], "label": f"{r['kind'].replace('_', ' ').title()} {r['bill_no']}", "amount": r["total"], "paid": r["paid"], "delta": d, "by": r["by_name"]})
    for r in c.execute("SELECT * FROM payments WHERE party_id=? AND status='active'", (pid,)):
        ev.append({"ts": r["ts"], "type": "pay_" + r["kind"], "ref_id": r["id"], "label": f"Payment {'mila' if r['kind']=='in' else 'diya'} ({r['mode'] or ''}) {r['note'] or ''}", "amount": r["amount"], "paid": 0,
                   "delta": -r["amount"] if r["kind"] == "in" else r["amount"], "by": r["by_name"]})
    ev.sort(key=lambda x: x["ts"]); bal = p["opening"]
    for e in ev: bal += e["delta"]; e["bal"] = round(bal, 2)
    return {"party": dict(p), "rows": ev, "balance": round(bal, 2)}

def payment_add(b, c, adm):
    if b.get("kind") not in ("in", "out"): raise Err("in/out chunein")
    amt = float(b.get("amount") or 0)
    if amt <= 0: raise Err("amount daalein")
    if not c.execute("SELECT 1 FROM parties WHERE id=?", (b.get("party_id"),)).fetchone(): raise Err("party chunein")
    cur = c.execute("INSERT INTO payments(party_id,kind,amount,mode,note,ts,created,by_id,by_name) VALUES(?,?,?,?,?,?,?,?,?)",
                    (b["party_id"], b["kind"], amt, (b.get("mode") or "Cash")[:20], (b.get("note") or "")[:200], _bill_ts(b), time.time(), actor_id(adm), actor(adm))); return {"payment_id": cur.lastrowid}

def payment_edit(b, c, adm, pid):
    p = c.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    if not p: raise Err("payment nahi mila", 404)
    c.execute("UPDATE payments SET amount=?,mode=?,note=?,ts=? WHERE id=?", (float(b.get("amount") or p["amount"]), (b.get("mode") or p["mode"])[:20], (b.get("note") if b.get("note") is not None else p["note"])[:200], _bill_ts(b) if b.get("date") else p["ts"], pid)); return {"ok": True}

def payment_delete(b, c, adm, pid): c.execute("UPDATE payments SET status='deleted' WHERE id=?", (pid,)); return {"ok": True}

# ---- bills (purchase / sale) with stock effects ----
def _bill_ts(b):
    d = b.get("date")
    if d:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d): raise Err("date galat")
        t = time.localtime(); return time.mktime(time.strptime(d, "%Y-%m-%d")) + t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    return time.time()

def _bitems(b, c):
    out = []
    for it in b.get("items") or []:
        try: pid, qty, rate = int(it["product_id"]), int(it["qty"]), float(it["rate"])
        except Exception: raise Err("item, qty aur rate sahi daalein")
        if qty < 1 or rate < 0: raise Err("qty/rate galat")
        if not c.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone(): raise Err("item nahi mila")
        ex = it.get("expiry") or None
        if ex and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ex): raise Err("expiry date galat")
        out.append((pid, qty, rate, ex, (it.get("batch_no") or "")[:30]))
    if not out: raise Err("kam se kam ek item daalein")
    return out

def _bparty(b, c):
    p = c.execute("SELECT id,name FROM parties WHERE id=?", (b.get("party_id"),)).fetchone() if b.get("party_id") else None
    return (p["id"] if p else None), (p["name"] if p else ((b.get("party_name") or "").strip()[:80] or "Walk-in"))

ADDS = ("purchase", "sale_return")   # kinds that bring stock IN
PFX = {"purchase": "P", "sale": "S", "sale_return": "SR", "purchase_return": "PR"}

def _bill_apply(c, bid, kind, items, party):
    for pid, qty, rate, ex, bn in items:
        consumed = bb = None
        p = c.execute("SELECT stock,cost,name FROM products WHERE id=?", (pid,)).fetchone()
        if kind in ADDS:
            ucost = rate if kind == "purchase" else (p["cost"] or 0)
            if kind == "purchase": c.execute("UPDATE products SET stock=stock+?, cost=? WHERE id=?", (qty, rate, pid))
            else: c.execute("UPDATE products SET stock=stock+? WHERE id=?", (qty, pid))
            bb = c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,expiry,batch_no,created,ref) VALUES(0,?,?,?,?,?,?,?)", (pid, qty, ucost, ex, bn, time.time(), f"B{bid}")).lastrowid
            c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created,ref) VALUES(0,?,?,?,?,?,?,?,?)", (pid, "purchase" if kind == "purchase" else "sale_return", qty, rate, ucost, party, time.time(), f"B{bid}"))
        else:  # sale / purchase_return : stock goes OUT (earliest expiry first)
            if p["stock"] < qty: raise Err(f"{p['name']}: stock kam hai (bacha {p['stock']})")
            c.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
            got, need = _consume(c, 0, pid, qty)
            avg = (sum(t * uc for t, uc, _, _ in got) + need * (p["cost"] or 0)) / qty
            consumed = json.dumps([[t, uc, e, n] for t, uc, e, n in got])
            c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created,ref) VALUES(0,?,?,?,?,?,?,?,?)", (pid, "bill_sale" if kind == "sale" else "purchase_return", qty, rate, avg, party, time.time(), f"B{bid}"))
        c.execute("INSERT INTO bill_items(bill_id,product_id,qty,rate,amount,expiry,batch_no,consumed,batch_id) VALUES(?,?,?,?,?,?,?,?,?)", (bid, pid, qty, rate, round(qty * rate, 2), ex, bn, consumed, bb))

def _bill_reverse(c, bid):
    kind = c.execute("SELECT kind FROM bills WHERE id=?", (bid,)).fetchone()["kind"]
    for it in c.execute("SELECT * FROM bill_items WHERE bill_id=?", (bid,)).fetchall():
        if kind in ADDS:
            p = c.execute("SELECT stock,name FROM products WHERE id=?", (it["product_id"],)).fetchone()
            if p["stock"] < it["qty"]: raise Err(f"{p['name']}: maal aage bik chuka hai, pehle uska bill hatayein")
            c.execute("UPDATE products SET stock=stock-? WHERE id=?", (it["qty"], it["product_id"]))
            if it["batch_id"]: c.execute("UPDATE batches SET qty_left=MAX(0,qty_left-?) WHERE id=?", (it["qty"], it["batch_id"]))
        else:
            c.execute("UPDATE products SET stock=stock+? WHERE id=?", (it["qty"], it["product_id"]))
            for t, uc, ex, bn in json.loads(it["consumed"] or "[]"):
                c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,expiry,batch_no,created,ref) VALUES(0,?,?,?,?,?,?,?)", (it["product_id"], t, uc, ex, bn, time.time(), f"REV{bid}"))
    c.execute("DELETE FROM stock_ledger WHERE ref=?", (f"B{bid}",)); c.execute("DELETE FROM bill_items WHERE bill_id=?", (bid,))

def _bhead(b, items):
    sub = round(sum(q * r for _, q, r, _, _ in items), 2); disc = float(b.get("discount") or 0); gst = float(b.get("gst_pct") or 0)
    return sub, disc, gst, round((sub - disc) * (1 + gst / 100), 2), float(b.get("paid") or 0)

def bill_create(b, c, adm):
    kind = b.get("kind")
    if kind not in PFX: raise Err("bill ka type chunein")
    items = _bitems(b, c); pid, pname = _bparty(b, c); sub, disc, gst, total, paid = _bhead(b, items)
    bid = c.execute("INSERT INTO bills(kind,party_id,party_name,ts,created,subtotal,discount,gst_pct,total,paid,note,by_id,by_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (kind, pid, pname, _bill_ts(b), time.time(), sub, disc, gst, total, paid, (b.get("note") or "")[:200], actor_id(adm), actor(adm))).lastrowid
    no = (b.get("bill_no") or "").strip()[:20] or f"{PFX[kind]}-{bid}"
    c.execute("UPDATE bills SET bill_no=? WHERE id=?", (no, bid)); _bill_apply(c, bid, kind, items, pname)
    return {"bill_id": bid, "bill_no": no, "total": total}

def bill_edit(b, c, adm, bid):
    bl = c.execute("SELECT * FROM bills WHERE id=? AND status='active'", (bid,)).fetchone()
    if not bl: raise Err("bill nahi mila", 404)
    items = _bitems(b, c); _bill_reverse(c, bid); pid, pname = _bparty(b, c); sub, disc, gst, total, paid = _bhead(b, items)
    no = (b.get("bill_no") or "").strip()[:20] or bl["bill_no"]
    c.execute("UPDATE bills SET party_id=?,party_name=?,ts=?,subtotal=?,discount=?,gst_pct=?,total=?,paid=?,note=?,bill_no=? WHERE id=?",
              (pid, pname, _bill_ts(b) if b.get("date") else bl["ts"], sub, disc, gst, total, paid, (b.get("note") or "")[:200], no, bid))
    _bill_apply(c, bid, bl["kind"], items, pname); return {"bill_id": bid, "total": total}

def bill_delete(b, c, adm, bid):
    if not c.execute("SELECT 1 FROM bills WHERE id=? AND status='active'", (bid,)).fetchone(): raise Err("bill nahi mila", 404)
    _bill_reverse(c, bid); c.execute("UPDATE bills SET status='deleted' WHERE id=?", (bid,)); return {"ok": True}

def bills_list(b, c, adm, kind, days):
    q = "SELECT * FROM bills WHERE status='active' AND ts>=?" + ("" if kind == "all" else " AND kind=?")
    a = (_since(days),) if kind == "all" else (_since(days), kind)
    return [dict(r) for r in c.execute(q + " ORDER BY ts DESC LIMIT 300", a)]

def bill_get(b, c, adm, bid):
    bl = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
    if not bl: raise Err("bill nahi mila", 404)
    items = c.execute("SELECT i.*,p.name FROM bill_items i JOIN products p ON p.id=i.product_id WHERE i.bill_id=?", (bid,)).fetchall()
    party = c.execute("SELECT * FROM parties WHERE id=?", (bl["party_id"],)).fetchone() if bl["party_id"] else None
    S = settings(c)
    return {**dict(bl), "items": [{k: i[k] for k in ("product_id", "name", "qty", "rate", "amount", "expiry", "batch_no")} for i in items], "party": dict(party) if party else None,
            "biz": {"name": S["biz_name"], "gstin": S["gstin"]}}

# ---- items: bulk add, adjust, delete ----
def products_bulk(b, c, adm):
    rows = b.get("rows") or []
    if not rows or len(rows) > 500: raise Err("1 se 500 items ek baar mein")
    added = skipped = 0; errs = []
    for i, r in enumerate(rows, 1):
        try:
            name = (r.get("name") or "").strip()[:80]; mrp = float(r.get("mrp") or 0); disc = float(r.get("discount_pct") or 0)
            stock = int(float(r.get("stock") or 0)); cost = float(r.get("cost") or 0); rx = 1 if str(r.get("rx") or "0").strip().lower() in ("1", "yes", "y", "rx", "true") else 0
        except Exception: errs.append(f"row {i}: number galat"); continue
        if not name or mrp <= 0: errs.append(f"row {i}: naam/MRP chahiye"); continue
        if c.execute("SELECT 1 FROM products WHERE lower(name)=lower(?) AND deleted=0", (name,)).fetchone(): skipped += 1; continue
        pid = c.execute("INSERT INTO products(name,category,mrp,discount_pct,stock,rx,cost) VALUES(?,?,?,?,?,?,?)", (name, cat_ok(r.get("category")), mrp, disc, stock, rx, cost)).lastrowid
        if stock > 0:
            c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,batch_no,created,ref) VALUES(0,?,?,?,'OPENING',?,'OPEN')", (pid, stock, cost, time.time()))
            c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created) VALUES(0,?,'purchase',?,?,?,'Opening stock',?)", (pid, stock, cost, cost, time.time()))
        added += 1
    return {"added": added, "skipped": skipped, "errors": errs[:20]}

def stock_adjust(b, c, adm):
    try: pid, d = int(b["product_id"]), int(b["delta"])
    except Exception: raise Err("item aur qty daalein")
    p = c.execute("SELECT stock,cost FROM products WHERE id=?", (pid,)).fetchone()
    if not p or d == 0: raise Err("item/qty galat")
    note = (b.get("note") or "adjust")[:60]
    if d > 0:
        c.execute("UPDATE products SET stock=stock+? WHERE id=?", (d, pid))
        c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,batch_no,created,ref) VALUES(0,?,?,?,'ADJ',?,'ADJ')", (pid, d, p["cost"] or 0, time.time()))
        c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created) VALUES(0,?,'adjust_in',?,?,?,?,?)", (pid, d, p["cost"] or 0, p["cost"] or 0, note, time.time()))
    else:
        if p["stock"] < -d: raise Err("itna stock nahi hai")
        c.execute("UPDATE products SET stock=stock-? WHERE id=?", (-d, pid)); got, _ = _consume(c, 0, pid, -d)
        c.execute("INSERT INTO stock_ledger(user_id,product_id,kind,qty,unit_price,cost,party,created) VALUES(0,?,'adjust_out',?,?,?,?,?)", (pid, -d, p["cost"] or 0, p["cost"] or 0, note, time.time()))
    return {"ok": True}

def product_delete(b, c, adm, pid):
    used = c.execute("SELECT (SELECT COUNT(*) FROM order_items WHERE product_id=?)+(SELECT COUNT(*) FROM bill_items WHERE product_id=?)+(SELECT COUNT(*) FROM stock_ledger WHERE product_id=?)+(SELECT COUNT(*) FROM inventory WHERE product_id=? AND qty>0)", (pid,) * 4).fetchone()[0]
    if used: c.execute("UPDATE products SET active=0, deleted=1 WHERE id=?", (pid,)); return {"mode": "soft"}
    c.execute("DELETE FROM batches WHERE product_id=?", (pid,)); c.execute("DELETE FROM products WHERE id=?", (pid,)); return {"mode": "hard"}

# ---- team ledger (franchise/agent/staff buy-sell) with edit/delete ----
def ledger_list(b, c, adm, uid, days):
    return [dict(r) for r in c.execute("""SELECT l.id,l.created,l.user_id,u.name,u.role,p.name product,l.kind,l.qty,l.unit_price,l.cost,l.party,l.ref FROM stock_ledger l
        JOIN users u ON u.id=l.user_id JOIN products p ON p.id=l.product_id WHERE l.user_id!=0 AND (?=0 OR l.user_id=?) AND l.created>=? ORDER BY l.id DESC LIMIT 300""", (uid, uid, _since(days)))]

def _undo_side(c, r):
    u, pid, q = r["user_id"], r["product_id"], r["qty"]
    if r["kind"] in ("purchase", "transfer_in"):
        inv = c.execute("SELECT qty FROM inventory WHERE user_id=? AND product_id=?", (u, pid)).fetchone()
        if not inv or inv["qty"] < q: raise Err("is maal ka stock ab nahi bacha, pehle aage ki bikri hatayein")
        c.execute("UPDATE inventory SET qty=qty-?, cost_total=cost_total-? WHERE user_id=? AND product_id=?", (q, q * r["unit_price"], u, pid))
        need = q
        for bt in c.execute("SELECT id,qty_left FROM batches WHERE ref=? ORDER BY id", (f"L{r['id']}",)).fetchall():
            t = min(need, bt["qty_left"]); c.execute("UPDATE batches SET qty_left=qty_left-? WHERE id=?", (t, bt["id"])); need -= t
    else:
        if c.execute("SELECT 1 FROM inventory WHERE user_id=? AND product_id=?", (u, pid)).fetchone():
            c.execute("UPDATE inventory SET qty=qty+?, cost_total=cost_total+? WHERE user_id=? AND product_id=?", (q, q * r["cost"], u, pid))
        else: c.execute("INSERT INTO inventory VALUES(?,?,?,?)", (u, pid, q, q * r["cost"]))
        c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,batch_no,created,ref) VALUES(?,?,?,?,'REVERSED',?,?)", (u, pid, q, r["cost"], time.time(), f"REV{r['id']}"))
    c.execute("DELETE FROM stock_ledger WHERE id=?", (r["id"],))

def ledger_delete(b, c, adm, lid):
    r = c.execute("SELECT * FROM stock_ledger WHERE id=? AND user_id!=0", (lid,)).fetchone()
    if not r: raise Err("entry nahi mili", 404)
    rows = [r]; ref = r["ref"] or ""
    if ref.startswith("T"):
        o = c.execute("SELECT * FROM stock_ledger WHERE id=?", (int(ref[1:]),)).fetchone()
        if o: rows.append(o)
    rows.sort(key=lambda x: 0 if x["kind"] in ("purchase", "transfer_in") else 1)
    for x in rows: _undo_side(c, x)
    if ref.startswith("C"):  # company supply: give stock back to company
        co = c.execute("SELECT * FROM stock_ledger WHERE id=? AND user_id=0", (int(ref[1:]),)).fetchone()
        if co:
            c.execute("UPDATE products SET stock=stock+? WHERE id=?", (co["qty"], co["product_id"]))
            c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,batch_no,created,ref) VALUES(0,?,?,?,'RETURN',?,?)", (co["product_id"], co["qty"], co["cost"], time.time(), f"REV{co['id']}"))
            c.execute("DELETE FROM stock_ledger WHERE id=?", (co["id"],))
    return {"ok": True}

def ledger_edit(b, c, adm, lid):
    r = c.execute("SELECT * FROM stock_ledger WHERE id=? AND user_id!=0", (lid,)).fetchone(); ref = (r["ref"] or "") if r else ""
    if not r or r["kind"] not in ("purchase", "sale") or ref[:1] in ("C", "T"): raise Err("sirf apni khareed/bikri ki entry edit hoti hai (rate aur party)")
    new = float(b.get("unit_price") if b.get("unit_price") not in (None, "") else r["unit_price"]); party = (b.get("party") if b.get("party") is not None else r["party"])[:80]
    if r["kind"] == "purchase":
        c.execute("UPDATE inventory SET cost_total=cost_total+? WHERE user_id=? AND product_id=?", (r["qty"] * (new - r["unit_price"]), r["user_id"], r["product_id"]))
        c.execute("UPDATE batches SET unit_cost=? WHERE ref=?", (new, f"L{lid}")); c.execute("UPDATE stock_ledger SET unit_price=?,cost=?,party=? WHERE id=?", (new, new, party, lid))
    else: c.execute("UPDATE stock_ledger SET unit_price=?,party=? WHERE id=?", (new, party, lid))
    return {"ok": True}

# ---- edit / delete for orders, users, withdrawals, offers ----
def order_delete(b, c, adm, oid):
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] == "deleted": raise Err("order nahi mila", 404)
    if o["status"] in ("awaiting_payment", "paid", "delivered"):
        for r in c.execute("SELECT product_id,qty,cost FROM order_items WHERE order_id=?", (oid,)).fetchall():
            c.execute("UPDATE products SET stock=stock+? WHERE id=?", (r["qty"], r["product_id"]))
            c.execute("INSERT INTO batches(user_id,product_id,qty_left,unit_cost,batch_no,created,ref) VALUES(0,?,?,?,'RETURN',?,?)", (r["product_id"], r["qty"], r["cost"], time.time(), f"ORD{oid}"))
        c.execute("UPDATE users SET goods_credit=goods_credit+? WHERE id=?", (o["credit_used"], o["user_id"]))
    c.execute("DELETE FROM commissions WHERE order_id=?", (oid,)); c.execute("UPDATE orders SET status='deleted' WHERE id=?", (oid,))
    notify(c, o["user_id"], "🗑 Order hata diya gaya", f"Order #{oid} admin ne hata diya."); return {"ok": True}

def order_edit(b, c, adm, oid):
    c.execute("UPDATE orders SET deliver_to=?, rx_ref=? WHERE id=?", ((b.get("deliver_to") or "")[:300], (b.get("rx_ref") or "")[:60], oid)); return {"ok": True}

def user_edit(b, c, adm, uid):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u: raise Err("user nahi mila", 404)
    role = b.get("role") or u["role"]
    if u["role"] != "staff" and role not in ("franchise", "agent", "customer"): raise Err("role galat")
    sp = b.get("sponsor_id", u["sponsor_id"]); sp = int(sp) if sp not in (None, "") else None
    if sp == uid: raise Err("khud ka sponsor nahi ho sakta")
    if sp and not c.execute("SELECT 1 FROM users WHERE id=?", (sp,)).fetchone(): raise Err("sponsor nahi mila")
    try: c.execute("UPDATE users SET name=?,phone=?,address=?,kyc_id=?,role=?,sponsor_id=? WHERE id=?", ((b.get("name") or u["name"])[:80], (b.get("phone") or u["phone"])[:20], (b.get("address") if b.get("address") is not None else u["address"]), (b.get("kyc_id") or u["kyc_id"]), role, sp, uid))
    except sqlite3.IntegrityError: raise Err("phone/KYC pehle se kisi aur ka hai")
    if b.get("new_pin"):
        if len(str(b["new_pin"])) < 4: raise Err("PIN 4+ digit ka ho")
        c.execute("UPDATE users SET pin_hash=? WHERE id=?", (hpin(b["new_pin"]), uid)); c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return {"ok": True}

def user_delete(b, c, adm, uid):
    used = c.execute("SELECT (SELECT COUNT(*) FROM orders WHERE user_id=? OR seller_id=?)+(SELECT COUNT(*) FROM commissions WHERE earner_id=?)+(SELECT COUNT(*) FROM users WHERE sponsor_id=?)+(SELECT COUNT(*) FROM stock_ledger WHERE user_id=?)", (uid,) * 5).fetchone()[0]
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    if used: c.execute("UPDATE users SET deleted=1, active=0 WHERE id=?", (uid,)); return {"mode": "soft"}
    for t in ("notifications", "messages", "withdrawals", "inventory"): c.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    c.execute("DELETE FROM users WHERE id=?", (uid,)); return {"mode": "hard"}

def withdraw_delete(b, c, adm, wid):
    c.execute("UPDATE withdrawals SET status='deleted' WHERE id=?", (wid,)); return {"ok": True}

def offer_edit(b, c, adm, oid):
    if not b.get("title"): raise Err("title chahiye")
    c.execute("UPDATE offers SET title=?, body=? WHERE id=?", (b["title"][:80], (b.get("body") or "")[:200], oid)); return {"ok": True}

# ---- staff ----
def staff_list(b, c, adm):
    return [{**{k: r[k] for k in ("id", "name", "phone", "active")}, "perms": json.loads(r["perms"] or "[]")} for r in c.execute("SELECT * FROM users WHERE role='staff' AND deleted=0 ORDER BY id DESC")]

def staff_add(b, c, adm):
    name, phone, pin = (b.get("name") or "").strip(), (b.get("phone") or "").strip(), str(b.get("pin") or "")
    if not name or not phone or len(pin) < 4: raise Err("naam, phone aur 4+ digit PIN daalein")
    try: cur = c.execute("INSERT INTO users(name,phone,role,kyc_id,pin_hash,active,perms,created) VALUES(?,?,'staff',?,?,1,?,?)", (name[:80], phone[:20], "STAFF-" + phone, hpin(pin), json.dumps([p for p in (b.get("perms") or ["billing", "items", "reports"]) if p in PERMS]), time.time()))
    except sqlite3.IntegrityError: raise Err("ye phone pehle se registered hai")
    return {"staff_id": cur.lastrowid}

def staff_edit(b, c, adm, sid):
    u = c.execute("SELECT * FROM users WHERE id=? AND role='staff'", (sid,)).fetchone()
    if not u: raise Err("staff nahi mila", 404)
    c.execute("UPDATE users SET name=?, perms=?, active=? WHERE id=?", ((b.get("name") or u["name"])[:80], json.dumps([p for p in b.get("perms", json.loads(u["perms"] or "[]")) if p in PERMS]), 1 if b.get("active", u["active"]) else 0, sid))
    if b.get("new_pin"):
        if len(str(b["new_pin"])) < 4: raise Err("PIN 4+ digit ka ho")
        c.execute("UPDATE users SET pin_hash=? WHERE id=?", (hpin(b["new_pin"]), sid))
    if not b.get("active", 1): c.execute("DELETE FROM sessions WHERE user_id=?", (sid,))
    return {"ok": True}

def staff_delete(b, c, adm, sid):
    c.execute("UPDATE users SET deleted=1, active=0 WHERE id=? AND role='staff'", (sid,)); c.execute("DELETE FROM sessions WHERE user_id=?", (sid,)); return {"ok": True}

# ---- more reports ----
def _regs(kind):
    def f(c, days):
        r = c.execute("SELECT id,strftime('%d-%m-%Y %H:%M',ts,'unixepoch','localtime'),bill_no,party_name,subtotal,discount,total,paid,by_name FROM bills WHERE kind=? AND status='active' AND ts>=? ORDER BY ts DESC", (kind, _since(days))).fetchall()
        return _R(("Sale" if kind == "sale" else "Purchase") + " register", ["ID", "Date/Time", "Bill no", "Party", "Subtotal ₹", "Discount ₹", "Total ₹", "Paid ₹", "Entry by"], [[x[0], x[1], x[2], x[3], _n(x[4]), _n(x[5]), _n(x[6]), _n(x[7]), x[8]] for x in r],
                  [["Total", _n(sum(x[6] for x in r))], ["Bills (orders)", len(r)], ["Baaki", _n(sum(x[6] - x[7] for x in r))]])
    return f
AR["sale_register"] = _regs("sale"); AR["purchase_register"] = _regs("purchase")

@ar
def a_party_dues(c, days):
    r = _party_rows(c)
    return _R("Party hisaab (lena / dena)", ["Party", "Phone", "Type", "Lena hai ₹", "Dena hai ₹"], [[x["name"], x["phone"], x["ptype"], _n(x["recv"]), _n(x["pay"])] for x in r], [["Kul lena", _n(sum(max(0, x["recv"]) for x in r))], ["Kul dena", _n(sum(max(0, x["pay"]) for x in r))]])

@ar
def a_party_sales(c, days):
    r = c.execute("""SELECT COALESCE(p.name,b.party_name) nm, SUM(CASE WHEN b.kind='sale' THEN 1 ELSE 0 END), SUM(CASE WHEN b.kind='sale' THEN b.total ELSE 0 END),
        SUM(CASE WHEN b.kind='sale_return' THEN b.total ELSE 0 END), SUM(CASE b.kind WHEN 'sale' THEN b.total ELSE -b.total END), SUM(CASE b.kind WHEN 'sale' THEN b.paid ELSE -b.paid END)
        FROM bills b LEFT JOIN parties p ON p.id=b.party_id WHERE b.status='active' AND b.kind IN ('sale','sale_return') AND b.ts>=? GROUP BY nm ORDER BY 5 DESC""", (_since(days),)).fetchall()
    return _R("Party-wise sell", ["Party", "Bills", "Sale ₹", "Return ₹", "Net sale ₹", "Mila ₹", "Baaki ₹"], [[x[0], x[1], _n(x[2]), _n(x[3]), _n(x[4]), _n(x[5]), _n(x[4] - x[5])] for x in r],
              [["Net sale", _n(sum(x[4] for x in r))], ["Return", _n(sum(x[3] for x in r))], ["Baaki", _n(sum(x[4] - x[5] for x in r))]])

@ar
def a_date_sales(c, days):
    r = c.execute("""SELECT strftime('%d-%m-%Y',ts,'unixepoch','localtime'), strftime('%Y-%m-%d',ts,'unixepoch','localtime') k, SUM(CASE WHEN kind='sale' THEN 1 ELSE 0 END),
        SUM(CASE kind WHEN 'sale' THEN total ELSE 0 END), SUM(CASE kind WHEN 'sale_return' THEN total ELSE 0 END), SUM(CASE kind WHEN 'purchase' THEN total ELSE 0 END),
        SUM(CASE kind WHEN 'purchase_return' THEN total ELSE 0 END) FROM bills WHERE status='active' AND ts>=? GROUP BY k ORDER BY k DESC""", (_since(days),)).fetchall()
    return _R("Date-wise sell", ["Date", "Sale bills", "Sale ₹", "Sale return ₹", "Net sale ₹", "Purchase ₹", "Purchase return ₹"],
              [[x[0], x[2], _n(x[3]), _n(x[4]), _n(x[3] - x[4]), _n(x[5]), _n(x[6])] for x in r], [["Net sale", _n(sum(x[3] - x[4] for x in r))], ["Net purchase", _n(sum(x[5] - x[6] for x in r))]])

ROUTES += [
    ("GET", r"/admin/parties", parties_list, True), ("POST", r"/admin/parties", party_add, True), ("POST", r"/admin/parties/(\d+)/edit", party_edit, True),
    ("POST", r"/admin/parties/(\d+)/delete", party_delete, True), ("GET", r"/admin/parties/(\d+)/ledger", party_ledger, True),
    ("POST", r"/admin/payments", payment_add, True), ("POST", r"/admin/payments/(\d+)/edit", payment_edit, True), ("POST", r"/admin/payments/(\d+)/delete", payment_delete, True),
    ("POST", r"/admin/bills", bill_create, True), ("POST", r"/admin/bills/(\d+)/edit", bill_edit, True), ("POST", r"/admin/bills/(\d+)/delete", bill_delete, True),
    ("GET", r"/admin/bills/(\w+)/(\d+)", bills_list, True), ("GET", r"/admin/bill/(\d+)", bill_get, True),
    ("POST", r"/admin/products/bulk", products_bulk, True), ("POST", r"/admin/stock/adjust", stock_adjust, True), ("POST", r"/admin/products/(\d+)/delete", product_delete, True),
    ("GET", r"/admin/ledger/(\d+)/(\d+)", ledger_list, True), ("POST", r"/admin/ledger/(\d+)/edit", ledger_edit, True), ("POST", r"/admin/ledger/(\d+)/delete", ledger_delete, True),
    ("POST", r"/admin/orders/(\d+)/delete", order_delete, True), ("POST", r"/admin/orders/(\d+)/edit", order_edit, True),
    ("POST", r"/admin/users/(\d+)/edit", user_edit, True), ("POST", r"/admin/users/(\d+)/delete", user_delete, True),
    ("POST", r"/admin/withdrawals/(\d+)/delete", withdraw_delete, True), ("POST", r"/admin/offers/(\d+)/edit", offer_edit, True),
    ("GET", r"/admin/staff", staff_list, True), ("POST", r"/admin/staff", staff_add, True), ("POST", r"/admin/staff/(\d+)/edit", staff_edit, True), ("POST", r"/admin/staff/(\d+)/delete", staff_delete, True),
    ("GET", r"/admin/audit/(\d+)", audit_list, True),
]

STATIC = {"/inventory": ("inventory.html", "text/html; charset=utf-8"), "/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/manifest.json": ("manifest.json", "application/manifest+json"), "/sw.js": ("sw.js", "text/javascript"),
          "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png")}

class H(BaseHTTPRequestHandler):
    def handle_any(self):
        try:
            fn_ = STATIC.get(self.path.split("?")[0]) if self.command == "GET" else None
            if fn_:
                d = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), fn_[0]), "rb").read()
                self.send_response(200); self.send_header("Content-Type", fn_[1]); self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(d))); self.end_headers(); self.wfile.write(d); return
            adm = self.headers.get("X-Admin-Token") == ADMIN
            uid = role = name = None; perms = set()
            if self.headers.get("X-Token"):
                with tx() as c:
                    r = c.execute("SELECT s.user_id,u.role,u.name,u.perms,u.active,u.deleted FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (self.headers["X-Token"],)).fetchone()
                    if r and not r["deleted"]:
                        uid, role, name = r["user_id"], r["role"], r["name"]
                        if role == "staff":
                            if r["active"]: perms = set(json.loads(r["perms"] or "[]"))
                            else: role = "staff_off"
            n = int(self.headers.get("Content-Length") or 0)
            body = clean(json.loads(self.rfile.read(n) or b"{}"))
            path = self.path.split("?")[0].rstrip("/") or "/"
            ctx = Ctx(adm, uid, role, perms, name)
            for m, rx, fn, admin_only in ROUTES:
                g = re.fullmatch(rx, path)
                if m == self.command and g:
                    if admin_only and not adm:
                        need = route_perm(self.command, path)
                        if not (role == "staff" and need and (perms & need)): raise Err("admin token required", 401)
                    with tx() as c: out = fn(body, c, ctx, *[int(x) if x.isdigit() else x for x in g.groups()])
                    if self.command in ("POST", "PATCH") and (admin_only or path.startswith("/stock/")): audit_log(ctx, self.command, path, body, out)
                    return self.send(200, out)
            raise Err("not found", 404)
        except Err as e: self.send(e.code, {"error": e.msg})
        except Exception as e: self.send(500, {"error": str(e)})
    do_GET = do_POST = do_PATCH = handle_any
    def send(self, code, obj):
        d = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(d)))
        self.end_headers(); self.wfile.write(d)

if __name__ == "__main__":
    with tx() as c:
        c.executescript(SCHEMA)
        try: c.execute("ALTER TABLE users ADD COLUMN active INTEGER DEFAULT 1")
        except sqlite3.OperationalError: pass
        for alt in ("ALTER TABLE products ADD COLUMN image TEXT", "ALTER TABLE products ADD COLUMN cost REAL DEFAULT 0",
                    "ALTER TABLE order_items ADD COLUMN cost REAL DEFAULT 0", "ALTER TABLE users ADD COLUMN wa_optin INTEGER DEFAULT 0", "ALTER TABLE users ADD COLUMN perms TEXT",
                    "ALTER TABLE users ADD COLUMN deleted INTEGER DEFAULT 0", "ALTER TABLE products ADD COLUMN deleted INTEGER DEFAULT 0",
                    "ALTER TABLE batches ADD COLUMN ref TEXT", "ALTER TABLE stock_ledger ADD COLUMN ref TEXT"):
            try: c.execute(alt)
            except sqlite3.OperationalError: pass
        try: c.execute("ALTER TABLE users ADD COLUMN kyc_doc TEXT")
        except sqlite3.OperationalError: pass
    print("SehatNet backend on :8000")
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8000))), H).serve_forever()
