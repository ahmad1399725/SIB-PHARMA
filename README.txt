SEHATNET - DEPLOY (Railway, ~10 min)
1. GitHub par ek repo banayein, in sabhi files ko upload karein.
2. railway.app > New Project > Deploy from GitHub repo.
3. Variables: ADMIN_TOKEN=<koi lamba secret>   DB=/data/sehatnet.db
4. Volume add karein, mount path /data  (warna redeploy par data ud jayega).
5. Settings > Networking > Generate Domain -> HTTPS link milega.
6. Ek baar Shell mein:  python seed.py   (sirf TEST data ke liye; live par mat chalayein)
Phone par: Android Chrome > menu > "Install app". iPhone Safari > Share > "Add to Home Screen".
Local test: ADMIN_TOKEN=admin123 python seed.py && ADMIN_TOKEN=admin123 python sehatnet_backend.py -> http://localhost:8000

--- v4 EXTRA (optional, Railway Variables mein daalein) ---
AI support auto-reply : ANTHROPIC_API_KEY=<aapki key>   (bina key ke chat sirf admin ko jata hai)
WhatsApp notification : WA_TOKEN=<Meta permanent token>  WA_PHONE_ID=<phone number id>
                        WA_TEMPLATE=<approved template naam, 1 variable {{1}}>  WA_LANG=hi (ya en)
  Note: WhatsApp Business Cloud API (Meta) account chahiye. Business se pehle message ke liye approved TEMPLATE zaroori hai.
  User ne register/profile mein "WhatsApp updates chahiye" tick kiya ho tabhi message jata hai.

--- v6: INVENTORY (alag page, sirf Admin + Staff) ---
Link:  <aapka-link>/inventory
Admin: Admin token se login -> "Staff (login banayein)" se staff ID banayein.
Staff: phone + PIN se login. Staff bill/items/reports kar sakta hai; edit/delete sirf Admin.
Bills: Sale, Purchase, Sale Return, Purchase Return. Reports: Party-wise Sell, Date-wise Sell (CSV + Print).
Customer app (/) alag hai; staff wahan login kare to apne aap /inventory par chala jata hai.
