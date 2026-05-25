from flask import Flask, request, jsonify
import stripe
import os
import psycopg2
from datetime import datetime

app = Flask(__name__)

# ENV VARIABLES (SET IN RENDER)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")

# -------------------------
# DATABASE CONNECTION
# -------------------------
def get_db():
    return psycopg2.connect(DATABASE_URL)

# -------------------------
# CREATE CHECKOUT SESSION
# -------------------------
@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    data = request.json
    amount = int(data.get("amount", 0))  # in cents

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": "Amazon MaxShipping Recovery Report"
                },
                "unit_amount": amount,
            },
            "quantity": 1,
        }],
        success_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
    )

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO payments (session_id, status, created_at)
        VALUES (%s, %s, %s)
    """, (session.id, "pending", datetime.utcnow()))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"id": session.id, "url": session.url})

# -------------------------
# WEBHOOK HANDLER
# -------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, WEBHOOK_SECRET
        )
    except Exception as e:
        return str(e), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE payments
            SET status='paid'
            WHERE session_id=%s
        """, (session["id"],))

        conn.commit()
        cur.close()
        conn.close()

    return "OK", 200

# -------------------------
# CHECK PAYMENT STATUS
# -------------------------
@app.route("/check-payment/<session_id>", methods=["GET"])
def check_payment(session_id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT status FROM payments WHERE session_id=%s
    """, (session_id,))

    result = cur.fetchone()

    cur.close()
    conn.close()

    if result and result[0] == "paid":
        return jsonify({"paid": True})
    else:
        return jsonify({"paid": False})

if __name__ == "__main__":
    app.run()