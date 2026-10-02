"""
Regression tests for every finding of the 2026-09-21 audit.
Run:  cd backend && python3 -m pytest -q tests/   (or: python3 tests/test_audit.py)
Each test runs against the real main.py with a throw-away data folder.
"""
import os
import sys
import io
import json
import hmac
import time
import hashlib
import re
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

DATA = tempfile.mkdtemp(prefix="genius_test_")
os.environ["GENIUS_DATA_DIR"] = DATA
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
os.environ["NEW_STORE_LIMIT_PER_HOUR"] = "100000"
for k in ("ANTHROPIC_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "STRIPE_SECRET_KEY"):
    os.environ.pop(k, None)

import main  # noqa: E402

C = main.app.test_client()
ADMIN = {"X-Admin-Token": "test-admin"}
_n = [0]


def new_id():
    _n[0] += 1
    return "store_test_%06d_%04d" % (os.getpid() % 10 ** 6, _n[0])


def upload(store_id, text, token=None, name=None, preview=False, enc="utf-8"):
    qs = []
    if preview:
        qs.append("preview=1")
    if name:
        qs.append("name=" + name)
    h = {"X-Store-Token": token} if token else {}
    return C.post("/import/csv/%s?%s" % (store_id, "&".join(qs)),
                  data=text.encode(enc), headers=h, content_type="text/csv")


def daily_csv(days=60, qty=10, product="מוצר א", start=(2026, 6, 7), sep=",", fmt="%Y-%m-%d"):
    from datetime import datetime, timedelta
    d0 = datetime(*start)
    rows = ["תאריך%sמוצר%sכמות" % (sep, sep)]
    for i in range(days):
        rows.append("%s%s%s%s%d" % ((d0 + timedelta(days=i)).strftime(fmt), sep, product, sep, qty))
    return "\n".join(rows)


# ---------------- C. security ----------------

def test_admin_routes_need_admin_token():
    assert C.get("/admin").status_code == 401
    assert C.post("/admin/seed-demo").status_code == 401
    assert C.delete("/admin/seed-demo").status_code == 401
    assert C.get("/admin", headers=ADMIN).status_code == 200


def test_admin_output_has_no_token_hashes_or_raw_sales():
    sid = new_id()
    upload(sid, daily_csv())
    body = C.get("/admin", headers=ADMIN).get_data(as_text=True)
    assert "token_hash" not in body and "sales_csv" not in body


def test_store_data_needs_its_token():
    sid = new_id()
    r = upload(sid, daily_csv(), name="חנות בדיקה")
    assert r.status_code == 200, r.json
    tok = r.json["store_token"]
    for path in ("/store/", "/forecast/", "/lending/", "/trends/"):
        assert C.get(path + sid).status_code == 403, path
        assert C.get(path + sid, headers={"X-Store-Token": tok}).status_code in (200, 404), path
        assert C.get(path + sid, headers={"X-Store-Token": "wrong"}).status_code == 403, path


def test_cannot_overwrite_other_store_or_demo():
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    assert upload(sid, daily_csv(qty=99)).status_code == 403          # no token
    assert upload(sid, daily_csv(qty=99), token="nope").status_code == 403
    assert upload(sid, daily_csv(qty=99), token=tok).status_code == 200
    assert upload("demo_super", daily_csv(), name="pwned").status_code == 403
    assert C.get("/store/demo_super").json["store_name"] == "סופר דיזנגוף"


def test_stores_list_hides_real_stores():
    sid = new_id()
    upload(sid, daily_csv())
    ids = [s["store_id"] for s in C.get("/stores").json["stores"]]
    assert sid not in ids and all(i.startswith("demo") for i in ids)
    ids_admin = [s["store_id"] for s in C.get("/stores", headers=ADMIN).json["stores"]]
    assert sid in ids_admin


def test_payment_success_page_does_not_mark_paid():
    r = C.post("/onboard", json={"name": "מכולת", "phone": "052-7000001"})
    sid = r.json["store_id"]
    C.get("/payment-success/" + sid)
    assert main.load_db()["stores"][sid]["plan"] == "trial"


def _stripe_sig(payload, secret="whsec_test"):
    t = str(int(time.time()))
    v1 = hmac.new(secret.encode(), ("%s.%s" % (t, payload)).encode(), hashlib.sha256).hexdigest()
    return "t=%s,v1=%s" % (t, v1)


def test_stripe_webhook_requires_valid_signature():
    r = C.post("/onboard", json={"name": "מכולת ב", "phone": "052-7000002"})
    sid = r.json["store_id"]
    payload = json.dumps({"id": "evt_1", "object": "event", "type": "checkout.session.completed",
                          "data": {"object": {"object": "checkout.session", "payment_status": "paid",
                                              "customer": "cus_1", "metadata": {"store_id": sid}}}})
    bad = C.post("/webhook/stripe", data=payload, headers={"Stripe-Signature": "t=1,v1=00"},
                 content_type="application/json")
    assert bad.status_code == 400
    assert main.load_db()["stores"][sid]["plan"] == "trial"
    ok = C.post("/webhook/stripe", data=payload, headers={"Stripe-Signature": _stripe_sig(payload)},
                content_type="application/json")
    assert ok.status_code == 200, ok.get_data(as_text=True)
    assert main.load_db()["stores"][sid]["plan"] == "paid"


def test_stripe_webhook_non_json_is_not_500():
    r = C.post("/webhook/stripe", data="not json", content_type="text/plain")
    assert r.status_code == 400


def test_onboard_refuses_existing_phone():
    r1 = C.post("/onboard", json={"name": "א", "phone": "052-7000003"})
    assert r1.status_code == 200 and r1.json["store_token"]
    r2 = C.post("/onboard", json={"name": "ב", "phone": "0527000003"})
    assert r2.status_code == 409
    assert main.load_db()["stores"][r1.json["store_id"]]["name"] == "א"


def test_onboard_client_cannot_choose_paid_plan():
    r = C.post("/onboard", json={"name": "ג", "phone": "052-7000004", "plan": "paid"})
    assert main.load_db()["stores"][r.json["store_id"]]["plan"] == "trial"


def test_onboard_supplier_without_phone_is_skipped_not_500():
    r = C.post("/onboard", json={"name": "ד", "phone": "052-7000005",
                                 "suppliers": [{"name": "ספק בלי טלפון"}, {"name": "תנובה", "phone": "0521111111"}]})
    assert r.status_code == 200
    assert r.json["skipped_suppliers"] == ["ספק בלי טלפון"]


def test_whatsapp_webhook_requires_twilio_signature():
    r = C.post("/webhook/whatsapp", data={"From": "whatsapp:+972527000003", "Body": "1"})
    assert r.status_code == 503   # Twilio not configured -> refuse, don't act


def test_whatsapp_webhook_rules_recommendation_no_500_and_no_empty_phone_match():
    sid = new_id()
    upload(sid, daily_csv())  # imported store: phone "" and a rules analysis without "status"
    r = C.post("/webhook/whatsapp", data={"From": "whatsapp:+972529999999", "Body": "1"}, headers=ADMIN)
    assert r.status_code == 200
    r = C.post("/webhook/whatsapp", data={"From": "whatsapp:+972527000003", "Body": "1"}, headers=ADMIN)
    assert r.status_code == 200


def test_upload_size_limit():
    big = "תאריך,מוצר,כמות\n" + ("2026-01-01,x,1\n" * 400000)
    r = C.post("/import/csv/" + new_id(), data=big.encode(), content_type="text/csv")
    assert r.status_code == 413


def test_cors_allows_every_header_the_site_actually_sends():
    """A header the browser isn't told about is blocked before the request."""
    r = C.open("/supplier/sup_1/orders", method="OPTIONS", headers={
        "Origin": "https://yonahanoch.github.io",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Supplier-Token, X-Store-Token, X-Admin-Token, Content-Type"})
    allowed = (r.headers.get("Access-Control-Allow-Headers") or "").lower()
    for h in ("x-store-token", "x-admin-token", "x-supplier-token", "content-type"):
        assert h in allowed, (h, allowed)


def test_cors_only_allows_known_origins():
    r = C.get("/stores", headers={"Origin": "https://evil.example"})
    assert r.headers.get("Access-Control-Allow-Origin") is None
    r = C.get("/stores", headers={"Origin": "https://yonahanoch.github.io"})
    assert r.headers.get("Access-Control-Allow-Origin") == "https://yonahanoch.github.io"


def test_chat_needs_access_and_key():
    sid = new_id()
    upload(sid, daily_csv())
    assert C.post("/chat", json={"message": "היי", "store_id": sid}).status_code == 403
    assert C.post("/chat", json={"message": "היי", "store_id": "demo_pharm"}).status_code == 503
    assert C.post("/chat", json={"message": "היי", "role": "admin"}).status_code == 401


def test_chat_context_contains_store_data():
    db = main.load_db()
    ctx = main.store_context_for_chat(db["stores"]["demo_pharm"], main.latest_analysis(db, "demo_pharm")[0])
    assert "כהן פארם" in ctx and "חלב תנובה 3%" in ctx and "Latest analysis" in ctx


def test_network_hides_non_members_and_phones():
    sid = new_id()
    r = upload(sid, daily_csv(product="קרם הגנה SPF 50", days=60, qty=1))
    tok = r.json["store_token"]
    tr = C.get("/network/transfer-opportunities").json
    body = json.dumps(tr, ensure_ascii=False)
    assert sid not in body and "phone" not in body
    assert tr["count"] >= 1   # the demo transfer is still there
    gb = C.get("/network/group-buying").json
    gbs = json.dumps(gb, ensure_ascii=False)
    assert "phone" not in gbs
    assert all(o["discount_is_estimate"] and "לאמת" in o["discount_note"] for o in gb["opportunities"])
    # joining is an explicit choice, made with the store's own token
    assert C.post("/store/%s/settings" % sid, json={"network_opt_in": True}).status_code == 403
    # joining needs a phone, so contact can always go both ways
    r = C.post("/store/%s/settings" % sid, json={"network_opt_in": True}, headers={"X-Store-Token": tok})
    assert r.status_code == 400
    # settings can't set a phone; registration attaches it to the caller's store
    r = C.post("/store/%s/settings" % sid, json={"network_opt_in": True, "phone": "052-6660001"},
               headers={"X-Store-Token": tok})
    assert r.status_code == 400
    r = C.post("/onboard", json={"name": "x", "phone": "052-6660001", "store_id": sid},
               headers={"X-Store-Token": tok})
    assert r.status_code == 200 and r.json["attached"] is True and r.json["store_id"] == sid
    r = C.post("/store/%s/settings" % sid, json={"network_opt_in": True}, headers={"X-Store-Token": tok})
    assert r.json["network_opt_in"] is True and r.json["has_phone"] is True
    other = new_id()
    tok2 = upload(other, daily_csv()).json["store_token"]
    r = C.post("/onboard", json={"name": "y", "phone": "0526660001", "store_id": other},
               headers={"X-Store-Token": tok2})
    assert r.status_code == 409    # one phone, one store
    # attaching to someone else's store needs its token
    third = new_id()
    upload(third, daily_csv())
    r = C.post("/onboard", json={"name": "z", "phone": "0526660009", "store_id": third})
    assert r.status_code == 403


def test_notify_requires_participant_or_demo_():
    r = C.post("/network/transfer-opportunities/notify",
               json={"from_store_id": "demo_pharm", "to_store_id": "demo_super",
                     "product_name": "קרם הגנה SPF 50"})
    assert r.status_code == 200 and r.json["demo"] is True


# ---------------- B. data safety ----------------

def test_concurrent_imports_lose_nothing():
    ids = [new_id() for _ in range(20)]
    results = {}

    def go(i):
        cl = main.app.test_client()
        results[i] = cl.post("/import/csv/" + i, data=daily_csv().encode(), content_type="text/csv").status_code

    th = [threading.Thread(target=go, args=(i,)) for i in ids]
    [t.start() for t in th]
    [t.join() for t in th]
    assert all(v == 200 for v in results.values()), results
    stored = main.load_db()["stores"]
    assert all(i in stored for i in ids)


def test_two_writers_on_different_stores_do_not_clobber():
    """Row-level storage: saving store A must not roll back store B."""
    a, b = new_id(), new_id()
    ta = upload(a, daily_csv()).json["store_token"]
    tb = upload(b, daily_csv()).json["store_token"]
    va = main.load_db()      # both readers hold their own view
    vb = main.load_db()
    va["stores"][a]["name"] = "שם א"
    vb["stores"][b]["name"] = "שם ב"
    main.save_db(va)
    main.save_db(vb)
    fresh = main.load_db()["stores"]
    assert fresh[a]["name"] == "שם א" and fresh[b]["name"] == "שם ב"
    assert fresh[a]["sales_csv"] and fresh[b]["sales_csv"]
    assert ta and tb


def test_store_row_is_read_only_when_touched():
    """A request for one store must not load every other store's sales."""
    sid = new_id()
    upload(sid, daily_csv())
    db = main.load_db()
    db["stores"][sid]
    assert list(db["stores"]._cache) == [sid]


def test_migration_from_the_old_json_file():
    import subprocess, tempfile as tf, textwrap
    d = tf.mkdtemp(prefix="genius_migrate_")
    old = {"stores": {"store_old_000001": {"name": "חנות ישנה", "sales_csv": "date,product,qty\n2026-06-01,x,1"}},
           "suppliers": {"0521111111": {"name": "תנובה", "phone": "0521111111", "store_ids": []}},
           "recommendations": [{"store_id": "store_old_000001", "created": "2026-06-02", "analysis": {"summary_he": "ישן"}}]}
    with open(os.path.join(d, "genius_db.json"), "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False)
    code = textwrap.dedent("""
        import os, sys, json
        sys.path.insert(0, %r)
        import main
        db = main.load_db()
        print(json.dumps({"name": db["stores"]["store_old_000001"]["name"],
                          "sup": list(db["suppliers"]),
                          "recs": len([r for r in db["recommendations"] if r["store_id"] == "store_old_000001"]),
                          "moved": os.path.exists(os.path.join(%r, "genius_db.json.migrated"))},
                         ensure_ascii=False))
    """ % (os.path.dirname(HERE), d))
    env = dict(os.environ, GENIUS_DATA_DIR=d)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    line = [l for l in out.stdout.splitlines() if l.startswith("{")][-1]
    got = json.loads(line)
    assert got["name"] == "חנות ישנה" and got["sup"] == ["0521111111"]
    assert got["recs"] == 1 and got["moved"] is True, got   # demo stores are seeded alongside


def test_demo_delete_removes_bakery_too():
    r = C.delete("/admin/seed-demo", headers=ADMIN)
    assert "demo_bakery" in r.json["removed"]
    assert not any(s.startswith("demo") for s in main.load_db()["stores"])
    C.post("/admin/seed-demo", headers=ADMIN)
    assert "demo_bakery" in main.load_db()["stores"]


# ---------------- honest numbers ----------------

def test_forecast_without_stock_gives_no_reorder():
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    f = C.get("/forecast/" + sid, headers={"X-Store-Token": tok}).json
    assert f["stock_known"] is False
    assert all(not x["reorder_now"] and x["days_until_empty"] is None and x["order_quantity"] is None
               for x in f["forecasts"])


def test_units_never_shown_as_shekels():
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    s = C.get("/store/" + sid, headers={"X-Store-Token": tok}).json
    assert s["metric"] == "units" and s["weekly_revenue"] is None
    assert s["product_count"] == 1 and s["products"][0]["status"] == "no_stock_data"
    l = C.get("/lending/" + sid, headers={"X-Store-Token": tok}).json
    assert l["max_loan"] == 0 and l["avg_monthly_revenue"] is None and l["risk"] == "insufficient_data"


def test_lending_needs_six_full_months_for_a_figure():
    csv3 = daily_csv(days=95, start=(2026, 3, 1))
    prof = main.calculate_credit_profile(csv3, {"מוצר א": 10})
    assert prof["months_of_data"] == 3 and prof["max_loan"] == 0 and prof["score"] is not None
    csv7 = daily_csv(days=215, start=(2026, 1, 1))
    prof = main.calculate_credit_profile(csv7, {"מוצר א": 10})
    # no lending partner yet: the amount stays hidden
    assert prof["months_of_data"] >= 6 and prof["max_loan"] == 0 and prof["amount_hidden"] is True
    assert "לא הצעת אשראי" in prof["disclaimer"]
    main.LENDING_SHOW_AMOUNT = True
    try:
        assert main.calculate_credit_profile(csv7, {"מוצר א": 10})["max_loan"] > 0
    finally:
        main.LENDING_SHOW_AMOUNT = False


def test_demo_analysis_comes_from_rules():
    db = main.load_db()
    for sid in ("demo_pharm", "demo_super", "demo_makolet", "demo_bakery"):
        a, _ = main.latest_analysis(db, sid)
        assert a.get("source") == "rules", sid
        assert a == main.build_rule_analysis(db["stores"][sid]), sid


def test_overview_and_forecast_agree():
    s = C.get("/store/demo_super").json
    f = {x["product_name"]: x for x in C.get("/forecast/demo_super").json["forecasts"]}
    for p in s["products"]:
        if p["status"] == "hot":
            assert p["days_until_empty"] == f[p["name"]]["days_until_empty"]


def test_stuck_value_is_stock_times_current_price():
    s = C.get("/store/demo_makolet").json
    assert s["stuck_value"] == 80 * 15   # ממתקי פורים
    assert "potential_savings" not in s


def test_markdown_keeps_agorot():
    store = {"sales_csv": daily_csv(days=60, product="אחר"), "stock": {"שוקולד": 10}, "prices": {"שוקולד": 5}}
    a = main.build_rule_analysis(store)
    assert a["dead_products"][0]["recommended_price"] == 3.8


# ---------------- import parsing ----------------

def _parse(text):
    return main.parse_sales_table(text)


def test_csv_dot_dates_two_digit_years_timestamps():
    t = "תאריך,מוצר,כמות\n05.06.2026,א,1\n13/06/26,ב,2\n2026-06-14 08:31:00,ג,3\n14/06/2026 10:00,ד,4"
    p = _parse(t)
    got = {n: h[0][0].strftime("%Y-%m-%d") for n, h in p["sales"].items()}
    assert got == {"א": "2026-06-05", "ב": "2026-06-13", "ג": "2026-06-14", "ד": "2026-06-14"}
    assert p["info"]["rows_used"] == 4 and not p["info"]["dropped"]


def test_csv_semicolon_and_thousands():
    t = 'תאריך;מוצר;כמות יחידות\n01/06/2026;א;"1,200"\n02/06/2026;ב;3,5'
    p = _parse(t)
    assert p["info"]["delimiter"] == ";"
    assert p["sales"]["א"][0][1] == 1200 and p["sales"]["ב"][0][1] == 3.5


def test_csv_mixed_date_formats_are_reported_not_silent():
    t = "תאריך,מוצר,כמות\n13/06/2026,א,1\n06/14/2026,ב,1\n05/06/2026,ג,1"
    p = _parse(t)
    assert p["info"]["date_order"] == "mixed"
    assert p["info"]["dropped"].get("bad_date") == 1
    r = upload(new_id(), t, preview=True)
    assert r.status_code == 200 and "warning" in r.json and r.json["rows_dropped"] == 1


def test_csv_dropped_rows_reported():
    t = "תאריך,מוצר,כמות\n01/06/2026,א,1\n01/06/2026,,2\n01/06/2026,ב,abc\nלא תאריך,ג,1"
    r = upload(new_id(), t, preview=True).json
    assert r["rows_dropped"] == 3
    assert r["dropped"] == {"missing_name": 1, "bad_quantity": 1, "bad_date": 1}


def test_csv_month_first_detected():
    t = "date,product,qty\n06/13/2026,a,1\n06/14/2026,b,1"
    p = _parse(t)
    assert p["info"]["date_order"] == "month_first"
    assert p["sales"]["a"][0][0].strftime("%Y-%m-%d") == "2026-06-13"


def test_csv_prices_from_price_or_total_column():
    t = "תאריך,שם פריט,כמות,סה\"כ\n01/06/2026,א,2,15\n02/06/2026,ב,4,20"
    p = _parse(t)
    assert p["prices"] == {"א": 7.5, "ב": 5.0}


def test_csv_sku_column_not_taken_as_name():
    t = "קוד פריט,תאריך,תיאור פריט,כמות\n7290001,01/06/2026,חלב,3"
    p = _parse(t)
    assert list(p["sales"]) == ["חלב"]


def test_csv_title_lines_above_header_and_cp1255():
    t = "דוח מכירות\nסניף ראשי\nתאריך,מוצר,כמות\n01/06/2026,לחם,5"
    r = upload(new_id(), t, preview=True, enc="cp1255")
    assert r.status_code == 200 and r.json["sale_rows"] == 1


def test_names_with_commas_survive_storage():
    t = 'תאריך,מוצר,כמות\n01/06/2026,"עוגיות, שוקולד",5'
    sid = new_id()
    tok = upload(sid, t).json["store_token"]
    s = C.get("/store/" + sid, headers={"X-Store-Token": tok}).json
    assert s["products"][0]["name"] == "עוגיות, שוקולד"


def _fixed(fields, total):
    """fields: [(start_1indexed, text)] -> one fixed-width record."""
    buf = bytearray(b" " * total)
    for start, txt in fields:
        b = txt.encode("iso8859-8") if isinstance(txt, str) else txt
        buf[start - 1:start - 1 + len(b)] = b
    return bytes(buf)


def _bkmv(cancel_second=False, encoding="iso8859-8"):
    def enc(s):
        return s.encode(encoding)
    recs = [_fixed([(1, "A100"), (5, "000000001")], 95)]
    for n, (doc, cancelled) in enumerate([("1001", False), ("1002", cancel_second)], start=2):
        recs.append(_fixed([(1, "C100"), (23, "320"), (26, doc.rjust(20, "0")), (46, "20260601"),
                            (400, "1" if cancelled else " "), (401, "20260601")], 444))
        recs.append(_fixed([(1, "D110"), (23, "320"), (26, doc.rjust(20, "0")), (94, enc("חלב תנובה")),
                            (224, "+".ljust(1) + "500".rjust(12, "0") + "0000"),
                            (241, "+" + "700".rjust(14, "0")), (297, "20260601")], 339))
    recs.append(_fixed([(1, "M100"), (63, "SKU1"), (83, enc("חלב תנובה")),
                        (193, "000000004000"), (205, "000000000000"), (217, "000000001000")], 298))
    total = len(recs) + 1
    recs.append(_fixed([(1, "Z900"), (5, "000000099"), (46, str(total).rjust(15, "0"))], 110))
    return b"\r\n".join(recs)


def test_bkmv_z900_count_read_from_position_46():
    p = main.parse_bkmv(_bkmv())
    assert p["records_declared"] == p["records_seen"] == 7
    assert p["count_matches"] is True


def test_bkmv_cancelled_documents_are_not_sales():
    conv = main.bkmv_to_store_data(main.parse_bkmv(_bkmv(cancel_second=True)))
    sales = main.parse_sales_csv(conv["sales_csv"])
    assert sum(q for h in sales.values() for _, q in h) == 500   # only the valid invoice
    assert conv["cancelled_lines_skipped"] == 1


def test_bkmv_cp862_hebrew_decodes():
    raw = _bkmv(encoding="cp862")
    p = main.parse_bkmv(raw)
    assert p["encoding"] == "cp862"
    assert p["lines"][0]["description"] == "חלב תנובה"


def test_bkmv_credit_note_reduces_sales_and_not_double_listed():
    assert "330" not in main.BKMV_SALES_DOCS and "330" in main.BKMV_CREDIT_DOCS


def test_bkmv_import_needs_token_for_existing_store():
    sid = new_id()
    r = C.post("/import/bkmv/" + sid, data=_bkmv(), content_type="application/octet-stream")
    assert r.status_code == 200, r.json
    tok = r.json["store_token"]
    assert C.post("/import/bkmv/" + sid, data=_bkmv(), content_type="application/octet-stream").status_code == 403
    assert C.post("/import/bkmv/" + sid, data=_bkmv(), headers={"X-Store-Token": tok},
                  content_type="application/octet-stream").status_code == 200


# ---------------- trends & misc ----------------

def test_trends_full_sunday_weeks_only():
    sid = new_id()
    # starts on a Wednesday so first and last weeks are partial
    tok = upload(sid, daily_csv(days=40, qty=10, start=(2026, 6, 3))).json["store_token"]
    t = C.get("/trends/" + sid, headers={"X-Store-Token": tok}).json
    assert t["week_starts"] == "sunday" and t["partial_weeks_dropped"] >= 1
    assert {w["revenue"] for w in t["weekly"]} == {70}
    from datetime import datetime
    assert all(datetime.strptime(w["week"], "%Y-%m-%d").weekday() == 6 for w in t["weekly"])


def test_forecast_bad_lead_time_is_400():
    r = C.post("/forecast/x", json={"csv": daily_csv(), "lead_time_days": "abc"})
    assert r.status_code == 400


def test_demo_numbers_never_messaged():
    assert main.is_demo_phone("050-1234567")
    assert not main.is_demo_phone("052-7000003")


# ---------------- re-audit findings ----------------

def test_slow_upload_does_not_block_other_requests():
    import socket
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, main.app, threaded=True)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        slow = socket.create_connection(("127.0.0.1", port))
        slow.sendall(b"POST /import/csv/store_slowclient_x HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: text/csv\r\nContent-Length: 1000000\r\n\r\nabc")
        time.sleep(0.3)
        import urllib.request
        t0 = time.time()
        body = urllib.request.urlopen("http://127.0.0.1:%d/stores" % port, timeout=5).read()
        assert time.time() - t0 < 2 and b"stores" in body
        slow.close()
    finally:
        srv.shutdown()


def test_rate_limit_ignores_spoofed_forwarded_for():
    codes = []
    for i in range(12):
        r = C.post("/onboard", json={"name": "x", "phone": "05390000%02d" % i},
                   headers={"X-Forwarded-For": "10.9.%d.1, 203.0.113.7" % i})
        codes.append(r.status_code)
    assert 429 in codes, codes


def test_upload_cannot_squat_a_phone_number_id():
    assert upload("0547770002", daily_csv()).status_code == 403


def test_unknown_and_foreign_stores_look_the_same():
    sid = new_id()
    upload(sid, daily_csv())
    a = C.get("/store/" + sid)
    b = C.get("/store/store_nosuchstore_000")
    assert a.status_code == b.status_code == 403 and a.json == b.json


def _two_network_stores():
    """Store A has dead stock of X; store B is running out of X. Both opted in."""
    from datetime import datetime, timedelta
    A, B = new_id(), new_id()
    d0 = datetime(2026, 5, 1)
    rows_a = ["date,product,qty"] + ["%s,אחר,5" % (d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(60)]
    rows_a += ["%s,מוצר רשת,3" % (d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(10)]
    rows_b = ["date,product,qty"] + ["%s,מוצר רשת,10" % (d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(60)]
    ta = upload(A, "\n".join(rows_a)).json["store_token"]
    tb = upload(B, "\n".join(rows_b)).json["store_token"]
    with main.db_lock():
        db = main.load_db()
        db["stores"][A].update(stock={"מוצר רשת": 50}, phone="0521110001", network_opt_in=True)
        db["stores"][B].update(stock={"מוצר רשת": 5}, phone="0521110002", network_opt_in=True)
        main._save_rule_analysis(db, A)
        main._save_rule_analysis(db, B)
        main.save_db(db)
    return A, ta, B, tb


def test_contact_needs_both_sides_and_messages_are_fixed_text():
    A, ta, B, tb = _two_network_stores()
    sent = []
    orig = main.send_whatsapp
    main.send_whatsapp = lambda ph, msg: sent.append((ph, msg)) or True
    try:
        la = C.get("/network/transfer-opportunities?store_id=" + A, headers={"X-Store-Token": ta}).json
        opp = [o for o in la["opportunities"] if o["from_store_id"] == A and o["to_store_id"] == B][0]
        assert "contact_phone" not in opp
        body = {"from_store_id": A, "to_store_id": B, "product_name": opp["product_name"], "store_id": A}
        r = C.post("/network/transfer-opportunities/notify?store_id=" + A, json=body, headers={"X-Store-Token": ta})
        assert r.status_code == 200 and r.json["both_agreed"] is False
        assert sent == [("0521110002", main.MATCH_MSG)]          # fixed text, no names/phones
        lb = C.get("/network/transfer-opportunities?store_id=" + B, headers={"X-Store-Token": tb}).json
        ob = [o for o in lb["opportunities"] if o["from_store_id"] == A][0]
        assert ob["other_asked"] is True and "contact_phone" not in ob
        body["store_id"] = B
        r = C.post("/network/transfer-opportunities/notify?store_id=" + B, json=body, headers={"X-Store-Token": tb})
        assert r.json["both_agreed"] is True
        la = C.get("/network/transfer-opportunities?store_id=" + A, headers={"X-Store-Token": ta}).json
        oa = [o for o in la["opportunities"] if o["to_store_id"] == B][0]
        assert oa["contact_phone"] == "0521110002"
        # asking again sends nothing new (recorded in the db, so all workers agree)
        n = len(sent)
        body["store_id"] = A
        r = C.post("/network/transfer-opportunities/notify?store_id=" + A, json=body, headers={"X-Store-Token": ta})
        assert r.status_code == 200 and r.json["already_requested"] is True and len(sent) == n
    finally:
        main.send_whatsapp = orig


def test_expired_trial_blocks_updates_and_chat_but_not_reading():
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    with main.db_lock():
        db = main.load_db()
        db["stores"][sid]["trial_ends"] = "2020-01-01T00:00:00"
        main.save_db(db)
    assert upload(sid, daily_csv(), token=tok).status_code == 402
    assert C.post("/chat", json={"message": "x", "store_id": sid}, headers={"X-Store-Token": tok}).status_code == 402
    assert C.get("/store/" + sid, headers={"X-Store-Token": tok}).status_code == 200


def test_admin_can_reissue_and_delete():
    sid = new_id()
    old = upload(sid, daily_csv()).json["store_token"]
    assert C.post("/admin/store/%s/reset-token" % sid).status_code == 401
    new = C.post("/admin/store/%s/reset-token" % sid, headers=ADMIN).json["store_token"]
    assert C.get("/store/" + sid, headers={"X-Store-Token": old}).status_code == 403
    assert C.get("/store/" + sid, headers={"X-Store-Token": new}).status_code == 200
    assert C.delete("/admin/store/" + sid, headers=ADMIN).json["deleted"] == sid
    assert sid not in main.load_db()["stores"]


def test_trends_need_two_full_weeks():
    sid = new_id()
    tok = upload(sid, daily_csv(days=10, start=(2026, 6, 3))).json["store_token"]
    t = C.get("/trends/" + sid, headers={"X-Store-Token": tok}).json
    assert t["weekly"] == [] and t["not_enough_weeks"] is True


def test_tab_separated_with_title_lines():
    t = "דוח מכירות\tסניף 1\n\nתאריך\tמוצר\tכמות\n01/06/2026\tלחם\t5"
    p = _parse(t)
    assert p["info"]["delimiter"] == "\t" and p["sales"]["לחם"][0][1] == 5


def test_csv_after_old_stock_drops_stale_stock():
    sid = new_id()
    tok = C.post("/import/bkmv/" + sid, data=_bkmv(), content_type="application/octet-stream").json["store_token"]
    assert main.load_db()["stores"][sid]["stock"]
    r = upload(sid, daily_csv(start=(2026, 7, 1)), token=tok)   # sales newer than the stock count
    assert r.status_code == 200 and "stock_note" in r.json
    assert not main.load_db()["stores"][sid].get("stock")


def test_stock_only_bkmv_keeps_sales():
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    before = main.load_db()["stores"][sid]["sales_csv"]
    only_stock = b"\r\n".join(l for l in _bkmv().split(b"\r\n") if not l.startswith((b"C100", b"D110")))
    r = C.post("/import/bkmv/" + sid, data=only_stock, headers={"X-Store-Token": tok},
               content_type="application/octet-stream")
    assert r.status_code == 200, r.json
    st = main.load_db()["stores"][sid]
    assert st["sales_csv"] == before and st["stock"]


def test_ai_analysis_is_sanitized():
    a = main.clean_analysis({"dead_products": [{"name": "x", "stock": "12", "days_no_sale": "abc",
                                                "current_price": "₪5.5", "reason": "<b>r</b>"}],
                             "hot_products": [{"name": "", "stock": 1}, "junk"],
                             "summary_he": 5})
    assert a["dead_products"][0]["stock"] == 12 and a["dead_products"][0]["days_no_sale"] is None
    assert a["dead_products"][0]["current_price"] == 5.5 and a["hot_products"] == []
    assert a["source"] == "ai"


def test_stores_without_phone_never_join_matches():
    A, ta, B, tb = _two_network_stores()
    with main.db_lock():
        db = main.load_db()
        db["stores"][A]["phone"] = ""
        main.save_db(db)
    lb = C.get("/network/transfer-opportunities?store_id=" + B, headers={"X-Store-Token": tb}).json
    assert not any(o["from_store_id"] == A for o in lb["opportunities"])


def test_import_rejections_look_the_same():
    sid = new_id()
    upload(sid, daily_csv())
    foreign = upload(sid, daily_csv())
    phone_like = upload("0548881111", daily_csv())
    assert foreign.status_code == phone_like.status_code == 403 and foreign.json == phone_like.json


def test_trial_chat_pool_cannot_starve_paid_stores():
    main._RATE.clear()
    main.ANTHROPIC_KEY, old_key = "test", main.ANTHROPIC_KEY
    old_cap = main.CHAT_TRIAL_DAILY_CAP
    main.CHAT_TRIAL_DAILY_CAP = 2
    calls = []

    class FakeMsgs:
        def create(self, **kw):
            calls.append(kw)
            class B: type = "text"; text = "ok"
            class R: content = [B()]
            return R()

    class FakeClient:
        def __init__(self, **kw): self.messages = FakeMsgs()
    orig = main.anthropic.Anthropic
    main.anthropic.Anthropic = FakeClient
    try:
        trials = [new_id() for _ in range(3)]
        toks = [upload(t, daily_csv()).json["store_token"] for t in trials]
        codes = [C.post("/chat", json={"message": "x", "store_id": t}, headers={"X-Store-Token": k,
                        "X-Forwarded-For": "192.0.2.%d" % i}).status_code
                 for i, (t, k) in enumerate(zip(trials, toks))]
        assert codes == [200, 200, 429], codes
        paid = new_id()
        ptok = upload(paid, daily_csv()).json["store_token"]
        with main.db_lock():
            db = main.load_db(); db["stores"][paid]["plan"] = "paid"; main.save_db(db)
        r = C.post("/chat", json={"message": "x", "store_id": paid},
                   headers={"X-Store-Token": ptok, "X-Forwarded-For": "192.0.2.99"})
        assert r.status_code == 200
        assert "STORE DATA" in calls[-1]["system"]
    finally:
        main.anthropic.Anthropic = orig
        main.ANTHROPIC_KEY = old_key
        main.CHAT_TRIAL_DAILY_CAP = old_cap


def test_signup_still_works_past_the_welcome_cap():
    main._RATE.clear()
    old = main.ONBOARD_DAILY_CAP
    main.ONBOARD_DAILY_CAP = 1
    try:
        for i in range(3):
            r = C.post("/onboard", json={"name": "ש", "phone": "05344400%02d" % i},
                       headers={"X-Forwarded-For": "198.51.100.%d" % i})
            assert r.status_code == 200, r.json
    finally:
        main.ONBOARD_DAILY_CAP = old


def test_trend_insight_names_the_most_concentrated_product():
    t = C.get("/trends/demo_bakery").json
    top = max((m for m in t["movers"] if m["peak_share_pct"] >= 35), key=lambda m: m["peak_share_pct"])
    assert top["product"] == "חלה מתוקה"
    assert "%d%% מהמכירות של חלה מתוקה מרוכזות ביום שישי" % top["peak_share_pct"] in t["insight"], t["insight"]


# ---------------- holidays ----------------

def test_holiday_calendar_known_dates():
    assert main.holiday_of("2026-09-11") == ("erev", "ערב ראש השנה")
    assert main.holiday_of("2026-09-12") == ("chag", "ראש השנה")
    assert main.holiday_of("2026-09-21") == ("chag", "יום כיפור")
    assert main.holiday_of("2026-10-02") == ("erev", "ערב שמחת תורה")   # Hoshana Raba
    assert main.holiday_of("2026-04-22") == ("chag", "יום העצמאות")
    assert main.holiday_of("2025-05-01") == ("chag", "יום העצמאות")    # moved from Shabbat
    assert main.holiday_of("2027-05-12") == ("chag", "יום העצמאות")
    assert main.holiday_of("2026-12-05")[0] == "period"                 # Hanukkah
    assert main.holiday_of("2026-09-15") is None
    # the table reaches far enough ahead to be useful (checked against a
    # published calendar: Pesach 18/4/2030, Independence Day 15/4/2032)
    assert main.holiday_of("2030-04-18") == ("chag", "פסח")
    assert main.holiday_of("2032-04-15") == ("chag", "יום העצמאות")
    assert max(main.IL_HOLIDAYS) >= "2032-01-01"


def _holiday_store(boost_product="מוצר א", boost=5):
    """Flat sales every day Jul 1 - Sep 30 2026; x`boost` on holiday eves; closed on holidays."""
    from datetime import datetime, timedelta
    rows = ["date,product,qty"]
    d = datetime(2026, 7, 1)
    while d <= datetime(2026, 9, 30):
        h = main.holiday_of(d)
        if not (h and h[0] == "chag"):
            for p in ("מוצר א", "מוצר ב"):
                q = 10 * (boost if (h and h[0] == "erev" and p == boost_product) else 1)
                rows.append("%s,%s,%d" % (d.strftime("%Y-%m-%d"), p, q))
        d += timedelta(days=1)
    sid = new_id()
    tok = upload(sid, "\n".join(rows)).json["store_token"]
    return C.get("/trends/" + sid, headers={"X-Store-Token": tok}).json


def test_holidays_do_not_distort_days_or_trends():
    t = _holiday_store()
    # every ordinary day sells the same, so no weekday stands out
    assert len({d["avg_revenue"] for d in t["by_day_of_week"]}) == 1
    # a holiday spike late in the period must not make a flat product "rising"
    assert all(m["direction"] == "stable" for m in t["movers"]), t["movers"]
    assert t["holiday_days_excluded"] >= 2


def test_holiday_effects_measured_and_top_product_named_only_if_it_stands_out():
    t = _holiday_store()
    eve = [e for e in t["holiday_effects"] if e["name"] == "ערב ראש השנה"][0]
    assert eve["lift"] == 3.0 and eve["top_product"] == "מוצר א" and eve["top_product_lift"] == 5.0
    assert {c["name"] for c in t["closed_on_holidays"]} >= {"ראש השנה", "יום כיפור"}
    assert "ערב ראש השנה" in t["insight"] or "ערב יום כיפור" in t["insight"]


def test_upcoming_holidays():
    from datetime import date
    up = main.upcoming_holidays(date(2026, 9, 22), 30)
    assert up[0] == {"date": "2026-09-25", "kind": "erev", "name": "ערב סוכות", "in_days": 3}


# ---------------- daily production plan ----------------

def test_production_plan_uses_that_weekday_and_holiday_eves():
    p = C.get("/production/demo_bakery?date=2026-10-02").json      # erev Simchat Torah, a Friday
    assert p["weekday"] == "שישי" and p["holiday"]["kind"] == "erev"
    challah = [r for r in p["products"] if r["product"] == "חלה מתוקה"][0]
    assert challah["holiday_basis"] == "measured" and challah["holiday_factor"] > 1.5
    assert challah["suggested"] > challah["weekday_avg"]           # eve, so more than a normal Friday
    assert challah["range_low"] <= challah["suggested"] <= challah["range_high"]
    assert p["products"][0]["product"] == "חלה מתוקה"              # the biggest line first
    ordinary = C.get("/production/demo_bakery?date=2026-09-24").json   # a Thursday
    ch2 = [r for r in ordinary["products"] if r["product"] == "חלה מתוקה"][0]
    assert ch2["suggested"] < challah["suggested"] and ordinary["holiday"] is None


def test_production_plan_says_closed_on_a_holiday():
    p = C.get("/production/demo_bakery?date=2026-10-03").json
    assert p["holiday"]["kind"] == "chag" and "סגורה" in p["note"]
    assert all(r["suggested"] == 0 for r in p["products"])


def test_production_plan_flags_stale_data_and_bad_input():
    p = C.get("/production/demo_bakery?date=2027-01-05").json
    assert p["stale_days"] > 14 and "לא מעודכנת" in p["stale_note"]
    assert C.get("/production/demo_bakery?date=nonsense").status_code == 400
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    assert C.get("/production/" + sid).status_code == 403          # needs the store's key
    assert C.get("/production/" + sid, headers={"X-Store-Token": tok}).status_code == 200


def test_production_plan_confidence_reflects_history():
    sid = new_id()
    tok = upload(sid, daily_csv(days=10)).json["store_token"]      # ~1-2 of each weekday
    p = C.get("/production/" + sid, headers={"X-Store-Token": tok}).json
    assert all(r["confidence"] in ("low", "medium") for r in p["products"]), p["products"]


# ---------------- weekly report ----------------

def test_weekly_report_text_from_real_numbers():
    r = C.get("/report/demo_bakery").json
    assert "מאפיית לחם הארץ" in r["text"] and "מכירות השבוע" in r["text"]
    assert r["can_send"] is False and "WhatsApp" in r["reason"]     # not connected here
    assert "ההשוואה בלי ימי חג" in r["text"]                        # holidays named, not silently dropped


def test_weekly_report_compares_the_same_weekdays():
    """A week whose Friday was a holiday eve must not read as a collapse."""
    from datetime import datetime, timedelta
    rows = ["date,product,qty"]
    d = datetime(2026, 8, 24)
    while d <= datetime(2026, 9, 20):
        if not (main.holiday_of(d) or ("", ""))[0] == "chag":
            qty = 100 if (d.weekday() + 1) % 7 == 6 else 10        # Fridays are huge
            rows.append("%s,לחם,%d" % (d.strftime("%Y-%m-%d"), qty))
        d += timedelta(days=1)
    sid = new_id()
    tok = upload(sid, "\n".join(rows)).json["store_token"]
    txt = C.get("/report/" + sid, headers={"X-Store-Token": tok}).json["text"]
    pct = int(re.search(r"([0-9]+)%", txt).group(1)) if re.search(r"([0-9]+)%", txt) else 0
    assert pct <= 5, txt      # flat week over week, despite the missing Friday


def test_weekly_report_queues_until_whatsapp_is_connected():
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    r = C.post("/report/%s/send" % sid, headers={"X-Store-Token": tok}).json
    assert r["sent"] is False and r["queued"] is True and "טלפון" in r["reason"]
    assert len(C.get("/report/" + sid, headers={"X-Store-Token": tok}).json["queued"]) == 1
    for _ in range(12):
        C.post("/report/%s/send" % sid, headers={"X-Store-Token": tok})
    assert len(main.load_db()["stores"][sid]["report_queue"]) == main.REPORT_QUEUE_MAX
    assert C.delete("/report/%s/queue" % sid, headers={"X-Store-Token": tok}).json["cleared"] is True
    assert main.load_db()["stores"][sid]["report_queue"] == []


def test_weekly_report_on_a_demo_store_changes_nothing():
    r = C.post("/report/demo_makolet/send").json
    assert r["demo"] is True and r["sent"] is False and r["queued"] is False
    assert not main.load_db()["stores"]["demo_makolet"].get("report_queue")


def test_weekly_report_needs_access():
    sid = new_id()
    upload(sid, daily_csv())
    assert C.get("/report/" + sid).status_code == 403


# ---------------- suppliers and order drafts ----------------

def _store_with_reorder():
    from datetime import datetime, timedelta
    rows = ["תאריך,מוצר,כמות"]
    d = datetime(2026, 8, 1)
    while d <= datetime(2026, 9, 25):
        rows += ["%s,חלב,20" % d.strftime("%Y-%m-%d"), "%s,לחם,30" % d.strftime("%Y-%m-%d")]
        d += timedelta(days=1)
    sid = new_id()
    tok = upload(sid, "\n".join(rows)).json["store_token"]
    with main.db_lock():
        db = main.load_db()
        db["stores"][sid]["stock"] = {"חלב": 30, "לחם": 40}
        main._save_rule_analysis(db, sid)
        main.save_db(db)
    return sid, {"X-Store-Token": tok}


def test_supplier_crud_and_order_draft():
    sid, h = _store_with_reorder()
    assert C.post("/suppliers/" + sid, json={"name": "תנובה", "phone": "052-1234567",
                                             "products": ["חלב"]}).status_code == 403   # needs the key
    r = C.post("/suppliers/" + sid, json={"name": "תנובה", "phone": "052-1234567",
                                          "products": ["חלב"]}, headers=h)
    assert r.status_code == 200 and r.json["suppliers"][0]["products"] == ["חלב"]
    d = C.get("/suppliers/" + sid, headers=h).json
    draft = d["drafts"][0]
    assert draft["supplier_name"] == "תנובה" and "חלב" in draft["text"] and "יחידות" in draft["text"]
    assert [u["name"] for u in d["unassigned"]] == ["לחם"]          # nobody assigned to bread yet
    assert C.post("/suppliers/" + sid, json={"name": "x", "phone": "123"}, headers=h).status_code == 400


def test_order_is_queued_until_whatsapp_and_supplier_can_be_removed():
    sid, h = _store_with_reorder()
    sup = C.post("/suppliers/" + sid, json={"name": "תנובה", "phone": "052-1234567",
                                            "products": ["חלב"]}, headers=h).json["supplier_id"]
    r = C.post("/orders/%s/send" % sid, json={"supplier_id": sup}, headers=h).json
    assert r["queued"] is True and r["sent"] is False
    assert len(main.load_db()["stores"][sid]["order_queue"]) == 1
    assert C.delete("/suppliers/%s/%s" % (sid, sup), headers=h).json["removed"] == sup
    after = C.get("/suppliers/" + sid, headers=h).json
    assert after["suppliers"] == [] and after["drafts"] == []
    assert sup not in main.load_db()["suppliers"]                   # no other store used it


def test_demo_store_orders_are_preview_only():
    d = C.get("/suppliers/demo_pharm").json
    assert C.post("/suppliers/demo_pharm", json={"name": "x", "phone": "0521234567"}).status_code == 403
    assert d["drafts"] == [] and d["unassigned"], d                  # nothing assigned in the demo


# ---------------- settings, export, delete ----------------

def test_owner_can_export_everything_and_delete_the_store():
    sid = new_id()
    tok = upload(sid, daily_csv(), name="לייצוא").json["store_token"]
    h = {"X-Store-Token": tok}
    assert C.get("/store/%s/export" % sid).status_code == 403          # not without the key
    r = C.get("/store/%s/export" % sid, headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    data = json.loads(r.get_data(as_text=True))
    assert data["store"]["name"] == "לייצוא" and data["store"]["sales_csv"]
    assert "token_hash" not in data["store"]                            # the secret stays out
    assert C.get("/store/demo_pharm/export").status_code == 403         # demo data isn't exportable
    assert C.delete("/store/" + sid).status_code == 403
    assert C.delete("/store/" + sid, headers=h).json["deleted"] == sid
    assert sid not in main.load_db()["stores"]
    assert C.delete("/store/demo_pharm", headers=ADMIN).status_code == 403   # demo stays put


def test_store_settings_switches_are_saved():
    sid = new_id()
    tok = upload(sid, daily_csv()).json["store_token"]
    h = {"X-Store-Token": tok}
    r = C.post("/store/%s/settings" % sid, json={"name": "שם חדש", "weekly_report_enabled": False}, headers=h)
    assert r.json["name"] == "שם חדש" and r.json["weekly_report_enabled"] is False
    st = C.get("/store/" + sid, headers=h).json
    assert st["weekly_report_enabled"] is False and st["order_alerts_enabled"] is True
    assert C.get("/store/" + sid).status_code == 403


# ---------------- promotion suggestions ----------------

def test_promotions_come_from_the_data_and_carry_their_evidence():
    d = C.get("/promotions/demo_makolet").json
    kinds = {i["kind"] for i in d["ideas"]}
    assert {"markdown", "bundle"} <= kinds, kinds
    md = [i for i in d["ideas"] if i["kind"] == "markdown"][0]
    assert md["evidence"]["stock"] == 80 and "ממתקי פורים" in md["text"]
    assert md["evidence"]["tied_up"] == 80 * 15
    assert "אי אפשר לדעת מראש" in d["disclaimer"]      # no invented uplift promises


def test_promotions_do_not_suggest_a_day_the_store_is_closed():
    """A store shut on Shabbat must not be told to run a Shabbat promotion."""
    from datetime import datetime, timedelta
    rows = ["תאריך,מוצר,כמות"]
    d = datetime(2026, 7, 1)
    while d <= datetime(2026, 8, 31):
        dow = (d.weekday() + 1) % 7
        if dow != 6:                       # closed on Saturday
            rows.append("%s,לחם,%d" % (d.strftime("%Y-%m-%d"), 40 if dow == 5 else 10))
        d += timedelta(days=1)
    sid = new_id()
    tok = upload(sid, "\n".join(rows)).json["store_token"]
    ideas = C.get("/promotions/" + sid, headers={"X-Store-Token": tok}).json["ideas"]
    quiet = [i for i in ideas if i["kind"] == "quiet_day"]
    assert quiet and quiet[0]["evidence"]["quiet_day"] != "שבת", ideas
    # a store that does sell on Saturday may well be told to lift it
    bakery = [i for i in C.get("/promotions/demo_bakery").json["ideas"] if i["kind"] == "quiet_day"]
    assert bakery and bakery[0]["evidence"]["ratio"] > 1.5


def test_promotions_holiday_idea_uses_measured_lift():
    ideas = C.get("/promotions/demo_bakery").json["ideas"]
    hol = [i for i in ideas if i["kind"] == "holiday"]
    if hol:                                   # only when a holiday eve is near
        assert hol[0]["evidence"]["lift"] > 1 and hol[0]["evidence"]["product"]


def test_promotions_need_access():
    sid = new_id()
    upload(sid, daily_csv())
    assert C.get("/promotions/" + sid).status_code == 403


# ---------------- supplier portal ----------------

def test_supplier_sees_only_its_own_orders_and_can_quote():
    sid, h = _store_with_reorder()
    sup = C.post("/suppliers/" + sid, json={"name": "תנובה", "phone": "052-1234567",
                                            "products": ["חלב"]}, headers=h).json["supplier_id"]
    C.post("/orders/%s/send" % sid, json={"supplier_id": sup}, headers=h)
    assert C.get("/supplier/%s/orders" % sup).status_code == 403          # no code, no data
    code = C.post("/suppliers/%s/%s/portal-code" % (sid, sup), headers=h).json["supplier_token"]
    sh = {"X-Supplier-Token": code}
    # a supplier serving several stores sees each store's own orders
    all_orders = C.get("/supplier/%s/orders" % sup, headers=sh).json["orders"]
    mine = [o for o in all_orders if o["store_id"] == sid]
    assert len(mine) == 1 and mine[0]["status"] == "open" and mine[0]["items"][0]["name"] == "חלב"
    assert all(o["supplier_id"] == sup for o in all_orders)
    orders = mine
    oid = orders[0]["id"]
    assert C.post("/supplier/%s/orders/%s/quote" % (sup, oid), json={"price_per_unit": 0}, headers=sh).status_code == 400
    q = C.post("/supplier/%s/orders/%s/quote" % (sup, oid),
               json={"price_per_unit": "4.5", "note": "אספקה מחר"}, headers=sh).json["quote"]
    assert q["price_per_unit"] == 4.5 and q["total"] == round(4.5 * orders[0]["items"][0]["quantity"], 2)

    # a second supplier with its own code sees nothing of the first one's order
    sup2 = C.post("/suppliers/" + sid, json={"name": "אסם", "phone": "052-7654321",
                                             "products": ["לחם"]}, headers=h).json["supplier_id"]
    code2 = C.post("/suppliers/%s/%s/portal-code" % (sid, sup2), headers=h).json["supplier_token"]
    assert [o for o in C.get("/supplier/%s/orders" % sup2,
                             headers={"X-Supplier-Token": code2}).json["orders"]
            if o["store_id"] == sid] == []
    assert C.get("/supplier/%s/orders" % sup, headers={"X-Supplier-Token": code2}).status_code == 403

    # the store sees the quote and answers it
    st = C.get("/suppliers/" + sid, headers=h).json
    assert st["orders"][0]["quote"]["price_per_unit"] == 4.5
    assert C.post("/orders/%s/%s" % (sid, oid), json={"status": "accepted"}, headers=h).json["status"] == "accepted"
    assert C.post("/supplier/%s/orders/%s/quote" % (sup, oid),
                  json={"price_per_unit": 4}, headers=sh).status_code == 409   # closed


def test_portal_code_only_from_the_store_that_owns_the_supplier():
    sid, h = _store_with_reorder()
    sup = C.post("/suppliers/" + sid, json={"name": "תנובה", "phone": "052-1234567",
                                            "products": ["חלב"]}, headers=h).json["supplier_id"]
    other, oh = _store_with_reorder()
    assert C.post("/suppliers/%s/%s/portal-code" % (other, sup), headers=oh).status_code == 404
    assert C.post("/suppliers/%s/%s/portal-code" % (sid, sup)).status_code == 403


def test_trends_reports_the_real_holiday_calendar_range():
    """The site used to hardcode "2024-2028" while the table went further."""
    years = sorted({k[:4] for k in main.IL_HOLIDAYS})
    expected = years[0] + "-" + years[-1]
    assert main.holiday_calendar_years() == expected
    body = C.get("/trends/demo_bakery").json
    assert body["holiday_calendar_years"] == expected
    # every year in the claimed range really has holidays in the table
    for y in range(int(years[0]), int(years[-1]) + 1):
        assert any(k.startswith(str(y)) for k in main.IL_HOLIDAYS), y


def test_a_failed_lock_does_not_deadlock_every_later_writer():
    """
    Taking the OS file lock can fail (full disk, read-only mount). It used to
    raise with the thread lock already held and no __exit__ coming, so every
    writer in the process blocked for ever.
    """
    import builtins
    real_open = builtins.open

    def boom(path, *a, **k):
        if str(path) == main.DB_LOCKFILE:
            raise OSError("No space left on device")
        return real_open(path, *a, **k)

    builtins.open = boom
    try:
        with main.db_lock():
            raise AssertionError("expected the lock to fail")
    except OSError:
        pass
    finally:
        builtins.open = real_open

    done = []
    t = threading.Thread(target=lambda: (main.db_lock().__enter__(),
                                         done.append(True),
                                         main._lock_state.__setattr__("depth", 0),
                                         main.DB_LOCK.release()))
    t.daemon = True
    t.start()
    t.join(timeout=5)
    assert done, "the lock was never released — later writers would hang"


def test_dates_are_israeli_business_dates_not_utc():
    """
    The holiday feature turns on "what day is it in Israel". On a UTC clock,
    the three hours before midnight local belong to the previous date, so the
    site announced a holiday eve as "tomorrow" when it had already started.
    """
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return
    il = _dt.datetime.now(ZoneInfo("Asia/Jerusalem"))
    here = _dt.datetime.now()
    assert here.date() == il.date(), (here.isoformat(), il.isoformat())
    assert abs((here.hour * 60 + here.minute) - (il.hour * 60 + il.minute)) <= 1


def test_the_holiday_calendar_warns_before_it_runs_out():
    """Past the last generated year every holiday silently becomes an ordinary
    day. The server must at least be able to say when that happens."""
    from datetime import datetime as _dt, timedelta as _td
    last = max(main.IL_HOLIDAYS)
    assert main.holiday_of(last) is not None
    after = (_dt.strptime(last, "%Y-%m-%d") + _td(days=400)).date()
    assert main.holiday_of(after) is None
    assert main.holiday_calendar_years().endswith(last[:4])
    # and the API says where the table ends, so this cannot expire unnoticed
    body = C.get("/trends/demo_bakery").json
    assert body["holiday_calendar_until"] == last, body.get("holiday_calendar_until")


def test_two_days_of_sales_is_not_a_confident_forecast():
    from datetime import datetime as _dt
    hist = [(_dt(2026, 9, 1), 50), (_dt(2026, 9, 2), 50)]
    f = main.forecast_product(hist, 100)
    assert f["confidence"] == "low", f
    assert f["observed_days"] == 2 and f["span_days"] == 2, f


def test_confidence_needs_days_spread_over_time_not_just_rows():
    from datetime import datetime as _dt
    # ten sales all on one day is not ten days of evidence
    same_day = [(_dt(2026, 9, 1), 5)] * 10
    assert main.forecast_product(same_day, 100)["confidence"] == "low"
    # ten different days across three weeks is
    from datetime import timedelta as _td
    spread = [(_dt(2026, 9, 1) + _td(days=2 * i), 5) for i in range(10)]
    assert main.forecast_product(spread, 100)["confidence"] == "high"


def test_a_trend_is_measured_over_time_not_over_row_positions():
    from datetime import datetime as _dt
    # three small sales in January, one in December: splitting by row count
    # called this "rising" and inflated the reorder quantity by 25%
    lopsided = [(_dt(2026, 1, 1), 10), (_dt(2026, 1, 2), 10),
                (_dt(2026, 1, 3), 10), (_dt(2026, 12, 1), 31)]
    f = main.forecast_product(lopsided, 100)
    assert f["trend"] == "stable", f
    assert f["adjusted_daily_rate"] == f["daily_rate"], f


def test_a_thin_reorder_says_what_it_rests_on():
    sid = new_id()
    rows = ["תאריך,מוצר,כמות", "2026-09-01,קולה,50", "2026-09-02,קולה,50"]
    tok = upload(sid, "\n".join(rows)).json["store_token"]
    h = {"X-Store-Token": tok}
    with main.db_lock():
        db = main.load_db()
        db["stores"][sid]["stock"] = {"קולה": 10}
        main._save_rule_analysis(db, sid)
        main.save_db(db)
    hot = C.get("/store/%s" % sid, headers=h).json["hot_products"]
    assert hot, "expected a reorder suggestion"
    p = hot[0]
    assert p["confidence"] == "low", p
    assert p["observed_days"] == 2 and p["basis_he"], p
    rep = C.get("/report/%s" % sid, headers=h).json.get("text", "")
    assert "להזמין" in rep and ("הערכה ראשונית" in rep or "עוד מוקדם" in rep), rep


def test_no_holiday_lift_claim_from_a_single_ordinary_day():
    """A "typical Sunday" built from one Sunday is not a baseline."""
    from datetime import datetime as _dt
    rows = ["תאריך,מוצר,כמות,מחיר"]
    # one ordinary Sunday, plus a holiday eve that also falls on a Sunday
    for day, qty in [(_dt(2026, 9, 13), 10), (_dt(2026, 9, 20), 40)]:
        rows.append("%s,חלה,%d,10" % (day.strftime("%Y-%m-%d"), qty))
    from datetime import timedelta as _td
    for i in range(20):                      # other weekdays, so trends runs
        d = _dt(2026, 8, 3) + _td(days=i)
        if d.weekday() != 6:
            rows.append("%s,לחם,10,5" % d.strftime("%Y-%m-%d"))
    sid = new_id()
    tok = upload(sid, "\n".join(rows)).json["store_token"]
    body = C.get("/trends/%s" % sid, headers={"X-Store-Token": tok}).json
    for e in body.get("holiday_effects") or []:
        assert e["baseline_days"] >= 2, e
    ins = body.get("insight") or ""
    assert "פי 4.0 מ-1 ימי" not in ins, ins


def test_production_shows_no_range_from_a_single_measurement():
    from datetime import datetime as _dt
    rows = ["תאריך,מוצר,כמות,מחיר", "%s,קולה,50,7" % _dt(2026, 9, 7).strftime("%Y-%m-%d")]
    sid = new_id()
    tok = upload(sid, "\n".join(rows)).json["store_token"]
    body = C.get("/production/%s?date=2026-09-14" % sid,
                 headers={"X-Store-Token": tok}).json
    for p in body.get("products") or []:
        if p["observations"] <= 1:
            assert p["range_low"] is None and p["range_high"] is None, p


def _order_to_supplier(phone="050-1234567"):
    """A store with one real open order to the supplier at `phone`."""
    sid, h = _store_with_reorder()
    sup = C.post("/suppliers/%s" % sid,
                 json={"name": "ספק", "phone": phone, "products": ["חלב"]},
                 headers=h).json["supplier_id"]
    C.post("/orders/%s/send" % sid, json={"supplier_id": sup}, headers=h)
    return sid, h, sup


def test_supplier_code_shows_only_the_store_that_issued_it():
    """
    The whole attack, end to end: a stranger claims someone else's supplier
    phone, gets a portal code for it, and must still see nothing of theirs.
    """
    victim, vh, sup = _order_to_supplier("050-7654321")
    assert len(C.get("/supplier/%s/orders" % sup,
                     headers={"X-Supplier-Token": C.post(
                         "/suppliers/%s/%s/portal-code" % (victim, sup),
                         headers=vh).json["supplier_token"]}).json["orders"]) == 1

    # the attacker: its own store, same supplier phone, its own portal code
    att, ah = _store_with_reorder()
    same = C.post("/suppliers/%s" % att,
                  json={"name": "אני", "phone": "050-7654321", "products": ["חלב"]},
                  headers=ah).json["supplier_id"]
    assert same == sup, "the record really is shared by phone number"
    atok = C.post("/suppliers/%s/%s/portal-code" % (att, sup),
                  headers=ah).json["supplier_token"]

    seen = C.get("/supplier/%s/orders" % sup,
                 headers={"X-Supplier-Token": atok}).json["orders"]
    assert all(o["store_id"] == att for o in seen), \
        "attacker saw another store's orders: %r" % [o["store_id"] for o in seen]


def test_a_stranger_cannot_quote_on_someone_elses_order():
    victim, vh, sup = _order_to_supplier("050-7654322")
    order = C.get("/suppliers/%s" % victim, headers=vh).json["orders"][0]["id"]
    att, ah = _store_with_reorder()
    C.post("/suppliers/%s" % att,
           json={"name": "אני", "phone": "050-7654322", "products": ["חלב"]}, headers=ah)
    atok = C.post("/suppliers/%s/%s/portal-code" % (att, sup),
                  headers=ah).json["supplier_token"]
    r = C.post("/supplier/%s/orders/%s/quote" % (sup, order),
               json={"price_per_unit": 999}, headers={"X-Supplier-Token": atok})
    assert r.status_code == 404, r.status_code
    assert C.get("/suppliers/%s" % victim, headers=vh).json["orders"][0]["quote"] is None


def test_issuing_a_code_does_not_revoke_another_stores_code():
    a, ah, sup = _order_to_supplier("050-7654323")
    atok = C.post("/suppliers/%s/%s/portal-code" % (a, sup), headers=ah).json["supplier_token"]
    b, bh = _store_with_reorder()
    C.post("/suppliers/%s" % b, json={"name": "ספק", "phone": "050-7654323",
                                      "products": ["חלב"]}, headers=bh)
    C.post("/suppliers/%s/%s/portal-code" % (b, sup), headers=bh)
    assert C.get("/supplier/%s/orders" % sup,
                 headers={"X-Supplier-Token": atok}).status_code == 200


def test_one_stores_orders_cannot_evict_another_stores():
    quiet, qh, sup = _order_to_supplier("050-7654324")
    assert len(C.get("/suppliers/%s" % quiet, headers=qh).json["orders"]) == 1
    with main.db_lock():
        db = main.load_db()
        for _ in range(250):
            main.record_order(db, "store_noisy_shop_1", {"supplier_id": sup, "items": []})
        main.save_db(db)
    assert len(C.get("/suppliers/%s" % quiet, headers=qh).json["orders"]) == 1, \
        "a busy store deleted a quiet store's order"


def test_subscribe_does_not_reveal_which_stores_exist():
    """Store ids are phone numbers — this route must not confirm one."""
    sid, h = _store_with_reorder()
    mine = C.get("/subscribe/%s/basic" % sid)                 # no token
    missing = C.get("/subscribe/%s/basic" % new_id())         # no such store
    assert mine.status_code == missing.status_code, (mine.status_code, missing.status_code)
    assert mine.get_data() == missing.get_data()


def test_forwarded_for_cannot_be_used_to_reset_the_rate_limit():
    assert main.PROXY_HOPS == 0, "X-Forwarded-For must not be trusted by default"
    with main.app.test_request_context(headers={"X-Forwarded-For": "1.2.3.4"}):
        assert main.client_ip() != "1.2.3.4"


def test_restored_json_backup_is_migrated_even_after_demo_stores_exist():
    """
    Boot once with no legacy file (demo stores get seeded), then drop a
    genius_db.json in and boot again — the operator's restore after a redeploy.
    The store in that file must come back.
    """
    import subprocess, tempfile, shutil, json as _json
    d = tempfile.mkdtemp(prefix="gx_mig_")
    try:
        env = dict(os.environ, GENIUS_DATA_DIR=d, ADMIN_TOKEN="t")
        boot = [sys.executable, "-c",
                "import sys;sys.path.insert(0,%r);import main;"
                "print('STORES', sorted(main.load_db()['stores'].keys()))"
                % os.path.dirname(main.__file__)]
        first = subprocess.run(boot, env=env, capture_output=True, text=True, timeout=120)
        assert "demo_bakery" in first.stdout, first.stdout + first.stderr

        with open(os.path.join(d, "genius_db.json"), "w", encoding="utf-8") as f:
            _json.dump({"stores": {"0521111111": {"name": "חנות ששוחזרה", "active": True}}}, f)

        second = subprocess.run(boot, env=env, capture_output=True, text=True, timeout=120)
        assert "0521111111" in second.stdout, \
            "a restored backup was ignored:\n" + second.stdout + second.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_backup_is_a_real_readable_copy_of_the_data():
    import datetime as _dt
    import sqlite3 as _sq
    sid, h = _store_with_reorder()
    path = main.backup_database(day=_dt.date(2031, 1, 1))
    assert path and os.path.exists(path), path
    # the copy really contains that store, and opens as a database
    con = _sq.connect(path)
    try:
        rows = con.execute("SELECT id FROM stores").fetchall()
    finally:
        con.close()
    assert sid in [r[0] for r in rows]


def test_backup_is_once_a_day_and_old_ones_are_pruned():
    import datetime as _dt
    import shutil as _sh
    _sh.rmtree(main.BACKUP_DIR, ignore_errors=True)
    for n in range(1, 11):
        main.backup_database(keep=7, day=_dt.date(2030, 1, n))
    kept = sorted(f["file"] for f in main.list_backups())
    assert len(kept) == 7, kept
    assert kept[0] == "genius-2030-01-04.db"       # the three oldest are gone
    assert kept[-1] == "genius-2030-01-10.db"
    # asking twice on the same day does not write a second copy
    assert main.backup_database(keep=7, day=_dt.date(2030, 1, 10)) is None


def test_backup_endpoint_needs_the_admin_token():
    assert C.get("/admin/backups").status_code == 401
    r = C.get("/admin/backups", headers=ADMIN)
    assert r.status_code == 200 and isinstance(r.json["backups"], list)


def test_a_failed_backup_does_not_stop_the_nightly_job():
    real = main.backup_database
    main.backup_database = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        main.load_db()["meta"].pop("nightly_last", None)
        main.save_db(main.load_db())
        main.nightly_job()                      # must not raise
        assert main.load_db()["meta"].get("nightly_last")
    finally:
        main.backup_database = real


def test_nightly_runs_once_per_day():
    main.nightly_job()
    first = main.load_db()["meta"]["nightly_last"]
    main.nightly_job()   # second call the same day does nothing
    assert main.load_db()["meta"]["nightly_last"] == first



# ---------------- credit-profile trend ----------------

def _monthly_csv(per_day_by_month, year=2026):
    """One row per day; per_day_by_month[i] is the daily qty in month i+1."""
    import calendar
    rows = ["תאריך,מוצר,כמות"]
    for i, q in enumerate(per_day_by_month):
        for d in range(1, calendar.monthrange(year, i + 1)[1] + 1):
            rows.append("%04d-%02d-%02d,מוצר א,%d" % (year, i + 1, d, q))
    return "\n".join(rows)


def test_trend_needs_four_months_not_one_against_two():
    # the old half-split read [100, 200, 200] as one month vs two -> "growing", +20 points
    t, pct, why = main.monthly_trend([100, 200, 200])
    assert t == "unknown" and pct is None and "4" in why


def test_one_holiday_month_is_not_a_trend_wherever_it_falls():
    # old code: spike in month 3 -> "declining", in month 4 -> "growing"
    for spike_at in range(6):
        v = [100] * 6
        v[spike_at] = 180
        assert main.monthly_trend(v)[0] == "stable", (spike_at, main.monthly_trend(v))


def test_same_store_same_score_whatever_month_the_holiday_lands_in():
    a = main.calculate_credit_profile(_monthly_csv([10, 10, 18, 10, 10, 10, 10]), {"מוצר א": 10})
    b = main.calculate_credit_profile(_monthly_csv([10, 10, 10, 18, 10, 10, 10]), {"מוצר א": 10})
    assert a["trend"] == b["trend"] == "stable", (a["trend"], b["trend"])
    assert a["score"] == b["score"], (a["score"], b["score"])


def test_a_real_steady_trend_is_still_found():
    up = main.monthly_trend([100, 110, 121, 133, 146, 161])
    down = main.monthly_trend([100, 90, 81, 73, 66, 59])
    assert up[0] == "growing" and 55 <= up[1] <= 70, up
    assert down[0] == "declining" and -50 <= down[1] <= -35, down


def test_big_but_noisy_change_is_not_called_a_trend():
    t, pct, why = main.monthly_trend([100, 60, 140, 80, 150, 90])
    assert t == "stable" and abs(pct) > 15 and "תנודות" in why


def test_credit_profile_explains_its_trend():
    p = main.calculate_credit_profile(_monthly_csv([10, 11, 12, 13, 15, 16, 18]), {"מוצר א": 10})
    assert p["trend"] == "growing" and p["trend_change_pct"] > 15 and p["trend_basis_he"]
    p3 = main.calculate_credit_profile(_monthly_csv([10, 20, 20]), {"מוצר א": 10})
    assert p3["trend"] == "unknown" and p3["score"] is not None and p3["trend_basis_he"]



def test_whoami_shows_how_the_caller_is_seen():
    r = C.get("/whoami", headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"}, environ_base={"REMOTE_ADDR": "10.0.0.5"})
    j = r.json
    assert r.status_code == 200 and j["socket"] == "10.0.0.5" and j["forwarded_for"] == ["9.9.9.9", "8.8.8.8"]
    old = main.PROXY_HOPS
    try:
        main.PROXY_HOPS = 0
        assert C.get("/whoami", headers={"X-Forwarded-For": "9.9.9.9"}, environ_base={"REMOTE_ADDR": "10.0.0.5"}).json["ip"] == "10.0.0.5"
        main.PROXY_HOPS = 1
        assert C.get("/whoami", headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"}, environ_base={"REMOTE_ADDR": "10.0.0.5"}).json["ip"] == "8.8.8.8"
    finally:
        main.PROXY_HOPS = old
    # it echoes only the caller's own request: nothing about stores or tokens
    assert set(j) == {"ip", "socket", "forwarded_for", "proxy_hops"}


if __name__ == "__main__":
    fails = 0
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print("FAIL", name, "->", repr(e)[:300])
    print("\n%d passed, %d failed" % (len(tests) - fails, fails))
    sys.exit(1 if fails else 0)
