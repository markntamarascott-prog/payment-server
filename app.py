import os
from datetime import datetime, timezone

import psycopg2
import stripe
from flask import Flask, jsonify, request

app = Flask(__name__)

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL)


def now_utc():
    return datetime.now(timezone.utc)


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "service": "amazon-maxshipping-payment-server",
        "version": "V10.13",
        "stripe_configured": bool(stripe.api_key),
        "database_configured": bool(DATABASE_URL),
        "webhook_secret_configured": bool(WEBHOOK_SECRET and WEBHOOK_SECRET != "pending"),
    })


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    if not stripe.api_key:
        return jsonify({"error": "STRIPE_SECRET_KEY is not configured"}), 500

    data = request.get_json(silent=True) or {}

    try:
        amount_cents = int(data.get("amount_cents", 0))
    except Exception:
        return jsonify({"error": "amount_cents must be an integer"}), 400

    report_id = str(data.get("report_id", "")).strip()
    recovery_total = str(data.get("recovery_total", "")).strip()
    app_name = str(data.get("app_name", "AMAZON-MAXSHIPPING TRACKER")).strip()
    app_version = str(data.get("app_version", "")).strip()

    if amount_cents <= 0:
        return jsonify({"error": "amount_cents must be greater than zero"}), 400

    if not report_id:
        return jsonify({"error": "report_id is required"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": "Amazon MaxShipping Recovery Report",
                            "description": "Verified report generation fee",
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url="https://payment-server-r56z.onrender.com/payment-success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://payment-server-r56z.onrender.com/payment-cancelled",
            metadata={
                "report_id": report_id,
                "recovery_total": recovery_total,
                "app_name": app_name,
                "app_version": app_version,
            },
        )
    except Exception as e:
        return jsonify({"error": f"Stripe checkout session creation failed: {e}"}), 500

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO payments (session_id, status, amount_cents, report_id, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (session_id)
            DO UPDATE SET
                status = EXCLUDED.status,
                amount_cents = EXCLUDED.amount_cents,
                report_id = EXCLUDED.report_id,
                created_at = EXCLUDED.created_at
            """,
            (session.id, "pending", amount_cents, report_id),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": f"Database insert failed: {e}"}), 500

    return jsonify({
        "session_id": session.id,
        "id": session.id,
        "url": session.url,
        "amount_cents": amount_cents,
        "report_id": report_id,
    })


@app.route("/check-payment/<session_id>", methods=["GET"])
def check_payment(session_id):
    session_id = str(session_id or "").strip()
    if not session_id:
        return jsonify({"paid": False, "error": "session_id is required"}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT session_id, status, amount_cents, report_id, created_at, paid_at
            FROM payments
            WHERE session_id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"paid": False, "error": f"Database lookup failed: {e}"}), 500

    if not row:
        return jsonify({"paid": False, "status": "not_found", "session_id": session_id})

    return jsonify({
        "paid": row[1] == "paid",
        "session_id": row[0],
        "status": row[1],
        "amount_cents": row[2],
        "report_id": row[3],
        "created_at": row[4].isoformat() if row[4] else None,
        "paid_at": row[5].isoformat() if row[5] else None,
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    if not WEBHOOK_SECRET or WEBHOOK_SECRET == "pending":
        return "STRIPE_WEBHOOK_SECRET is not configured", 500

    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400
    except Exception as e:
        return f"Webhook verification failed: {e}", 400

    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id")
        amount_total = int(session.get("amount_total") or 0)
        report_id = (session.get("metadata") or {}).get("report_id", "")

        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO payments (session_id, status, amount_cents, report_id, created_at, paid_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (session_id)
                DO UPDATE SET
                    status = 'paid',
                    amount_cents = EXCLUDED.amount_cents,
                    report_id = EXCLUDED.report_id,
                    paid_at = NOW()
                """,
                (session_id, "paid", amount_total, report_id),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            return f"Database update failed: {e}", 500

    return "OK", 200


@app.route("/payment-success", methods=["GET"])
def payment_success():
    return "Payment received. You may return to the Amazon-MaxShipping Tracker app and click OK to verify payment."


@app.route("/payment-cancelled", methods=["GET"])
def payment_cancelled():
    return "Payment was cancelled. You may return to the Amazon-MaxShipping Tracker app."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
