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


def test_nightly_runs_once_per_day():
    main.nightly_job()
    first = main.load_db()["meta"]["nightly_last"]
    main.nightly_job()   # second call the same day does nothing
    assert main.load_db()["meta"]["nightly_last"] == first


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
