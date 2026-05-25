import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

from flask import Flask, jsonify, request
import stripe

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None
    try:
        import psycopg2_binary  # noqa: F401
    except Exception:
        pass


APP_VERSION = "V10.13-webhook-fix-db-compatible"

app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()
SUCCESS_URL = os.environ.get("SUCCESS_URL", "").strip()
CANCEL_URL = os.environ.get("CANCEL_URL", "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def postgres_available():
    return bool(DATABASE_URL and psycopg2 is not None)


def get_base_url():
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/") + "/"
    try:
        return request.host_url
    except Exception:
        return ""


def get_success_url():
    if SUCCESS_URL:
        return SUCCESS_URL
    return urljoin(get_base_url(), "payment-success")


def get_cancel_url():
    if CANCEL_URL:
        return CANCEL_URL
    return urljoin(get_base_url(), "payment-cancelled")


def get_sqlite_path():
    return os.environ.get("SQLITE_PATH", "payments.sqlite3")


def get_db_connection():
    if postgres_available():
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    return sqlite3.connect(get_sqlite_path())


def postgres_column_exists(cur, table_name, column_name):
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        (table_name, column_name),
    )
    return cur.fetchone() is not None


def sqlite_column_exists(cur, table_name, column_name):
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cur.fetchall()]
    return column_name in columns


def init_db():
    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS payments (
                        session_id TEXT PRIMARY KEY,
                        status TEXT DEFAULT '',
                        amount_cents INTEGER DEFAULT 0,
                        report_id TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        paid_at TIMESTAMPTZ
                    )
                    """
                )

                migrations = [
                    ("recovery_total", "TEXT DEFAULT ''"),
                    ("app_name", "TEXT DEFAULT ''"),
                    ("app_version", "TEXT DEFAULT ''"),
                    ("payment_status", "TEXT DEFAULT ''"),
                    ("customer_email", "TEXT DEFAULT ''"),
                    ("updated_at", "TIMESTAMPTZ"),
                ]

                for column_name, column_type in migrations:
                    if not postgres_column_exists(cur, "payments", column_name):
                        cur.execute(
                            f"ALTER TABLE payments ADD COLUMN {column_name} {column_type}"
                        )

            conn.commit()
        finally:
            conn.close()
    else:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    session_id TEXT PRIMARY KEY,
                    status TEXT DEFAULT '',
                    amount_cents INTEGER DEFAULT 0,
                    report_id TEXT DEFAULT '',
                    created_at TEXT,
                    paid_at TEXT
                )
                """
            )

            migrations = [
                ("recovery_total", "TEXT DEFAULT ''"),
                ("app_name", "TEXT DEFAULT ''"),
                ("app_version", "TEXT DEFAULT ''"),
                ("payment_status", "TEXT DEFAULT ''"),
                ("customer_email", "TEXT DEFAULT ''"),
                ("updated_at", "TEXT"),
            ]

            for column_name, column_type in migrations:
                if not sqlite_column_exists(cur, "payments", column_name):
                    cur.execute(
                        f"ALTER TABLE payments ADD COLUMN {column_name} {column_type}"
                    )

            conn.commit()
        finally:
            conn.close()


def upsert_payment(
    session_id,
    report_id="",
    amount_cents=0,
    recovery_total="",
    app_name="",
    app_version="",
    paid=False,
    payment_status="",
    customer_email="",
):
    init_db()
    now = utc_now_iso()
    status_value = "paid" if paid else (payment_status or "created")
    paid_at_value = now if paid else None

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO payments (
                        session_id,
                        status,
                        amount_cents,
                        report_id,
                        created_at,
                        paid_at,
                        recovery_total,
                        app_name,
                        app_version,
                        payment_status,
                        customer_email,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (session_id) DO UPDATE SET
                        status = CASE
                            WHEN EXCLUDED.status = 'paid' THEN 'paid'
                            WHEN payments.status = 'paid' THEN payments.status
                            ELSE EXCLUDED.status
                        END,
                        amount_cents = CASE
                            WHEN EXCLUDED.amount_cents > 0 THEN EXCLUDED.amount_cents
                            ELSE payments.amount_cents
                        END,
                        report_id = COALESCE(NULLIF(EXCLUDED.report_id, ''), payments.report_id),
                        paid_at = CASE
                            WHEN EXCLUDED.paid_at IS NOT NULL THEN EXCLUDED.paid_at
                            ELSE payments.paid_at
                        END,
                        recovery_total = COALESCE(NULLIF(EXCLUDED.recovery_total, ''), payments.recovery_total),
                        app_name = COALESCE(NULLIF(EXCLUDED.app_name, ''), payments.app_name),
                        app_version = COALESCE(NULLIF(EXCLUDED.app_version, ''), payments.app_version),
                        payment_status = COALESCE(NULLIF(EXCLUDED.payment_status, ''), payments.payment_status),
                        customer_email = COALESCE(NULLIF(EXCLUDED.customer_email, ''), payments.customer_email),
                        updated_at = NOW()
                    """,
                    (
                        session_id,
                        status_value,
                        int(amount_cents or 0),
                        report_id,
                        paid_at_value,
                        str(recovery_total or ""),
                        app_name,
                        app_version,
                        payment_status,
                        customer_email,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    else:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            existing = cur.execute(
                "SELECT session_id FROM payments WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE payments
                    SET
                        status = CASE
                            WHEN ? = 'paid' THEN 'paid'
                            WHEN status = 'paid' THEN status
                            ELSE ?
                        END,
                        amount_cents = CASE WHEN ? > 0 THEN ? ELSE amount_cents END,
                        report_id = CASE WHEN ? != '' THEN ? ELSE report_id END,
                        paid_at = CASE WHEN ? IS NOT NULL THEN ? ELSE paid_at END,
                        recovery_total = CASE WHEN ? != '' THEN ? ELSE recovery_total END,
                        app_name = CASE WHEN ? != '' THEN ? ELSE app_name END,
                        app_version = CASE WHEN ? != '' THEN ? ELSE app_version END,
                        payment_status = CASE WHEN ? != '' THEN ? ELSE payment_status END,
                        customer_email = CASE WHEN ? != '' THEN ? ELSE customer_email END,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        status_value,
                        status_value,
                        int(amount_cents or 0),
                        int(amount_cents or 0),
                        report_id,
                        report_id,
                        paid_at_value,
                        paid_at_value,
                        str(recovery_total or ""),
                        str(recovery_total or ""),
                        app_name,
                        app_name,
                        app_version,
                        app_version,
                        payment_status,
                        payment_status,
                        customer_email,
                        customer_email,
                        now,
                        session_id,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO payments (
                        session_id,
                        status,
                        amount_cents,
                        report_id,
                        created_at,
                        paid_at,
                        recovery_total,
                        app_name,
                        app_version,
                        payment_status,
                        customer_email,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        status_value,
                        int(amount_cents or 0),
                        report_id,
                        now,
                        paid_at_value,
                        str(recovery_total or ""),
                        app_name,
                        app_version,
                        payment_status,
                        customer_email,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def get_payment(session_id):
    init_db()

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM payments WHERE session_id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM payments WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def payment_row_is_paid(row):
    if not row:
        return False

    status = str(row.get("status", "") or "").lower()
    payment_status = str(row.get("payment_status", "") or "").lower()
    paid_at = row.get("paid_at")

    return status == "paid" or payment_status == "paid" or bool(paid_at)


@app.route("/", methods=["GET"])
def index():
    try:
        init_db()
        db_ready = True
    except Exception:
        db_ready = False

    return jsonify(
        {
            "service": "Amazon MaxShipping payment server",
            "version": APP_VERSION,
            "stripe_configured": bool(STRIPE_SECRET_KEY),
            "database_configured": bool(DATABASE_URL),
            "database_ready": db_ready,
            "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET),
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return index()


@app.route("/payment-success", methods=["GET"])
def payment_success():
    return (
        "Payment completed. Return to the Amazon-MaxShipping Tracker app and click OK to verify payment.",
        200,
    )


@app.route("/payment-cancelled", methods=["GET"])
def payment_cancelled():
    return (
        "Payment cancelled. Return to the Amazon-MaxShipping Tracker app if you want to try again.",
        200,
    )


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe secret key is not configured."}), 500

    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON payload."}), 400

    try:
        amount_cents = int(payload.get("amount_cents", 0) or 0)
    except Exception:
        amount_cents = 0

    if amount_cents <= 0:
        return jsonify({"error": "amount_cents must be greater than zero."}), 400

    report_id = str(payload.get("report_id", "") or "").strip()
    recovery_total = str(payload.get("recovery_total", "") or "").strip()
    app_name = str(payload.get("app_name", "AMAZON-MAXSHIPPING TRACKER") or "").strip()
    app_version = str(payload.get("app_version", "") or "").strip()

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {
                            "name": "Amazon MaxShipping Recovery Report",
                        },
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            success_url=get_success_url(),
            cancel_url=get_cancel_url(),
            metadata={
                "report_id": report_id,
                "recovery_total": recovery_total,
                "app_name": app_name,
                "app_version": app_version,
            },
        )

        session_id = checkout_session["id"]
        checkout_url = checkout_session["url"]

        upsert_payment(
            session_id=session_id,
            report_id=report_id,
            amount_cents=amount_cents,
            recovery_total=recovery_total,
            app_name=app_name,
            app_version=app_version,
            paid=False,
            payment_status="created",
            customer_email="",
        )

        return jsonify(
            {
                "id": session_id,
                "session_id": session_id,
                "url": checkout_url,
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc), "version": APP_VERSION}), 500


@app.route("/check-payment/<session_id>", methods=["GET"])
def check_payment(session_id):
    session_id = str(session_id or "").strip()
    if not session_id:
        return jsonify({"paid": False, "error": "Missing session_id."}), 400

    row = get_payment(session_id)
    if not row:
        return jsonify(
            {
                "paid": False,
                "session_id": session_id,
                "payment_status": "not_found",
                "version": APP_VERSION,
            }
        )

    return jsonify(
        {
            "paid": payment_row_is_paid(row),
            "session_id": session_id,
            "report_id": row.get("report_id", ""),
            "amount_cents": row.get("amount_cents", 0),
            "payment_status": row.get("payment_status", "") or row.get("status", ""),
            "customer_email": row.get("customer_email", ""),
            "updated_at": str(row.get("updated_at", "") or ""),
            "version": APP_VERSION,
        }
    )


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Stripe webhook secret is not configured.", "version": APP_VERSION}), 500

    payload = request.data
    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=signature,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        return jsonify({"error": "Invalid payload.", "version": APP_VERSION}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature.", "version": APP_VERSION}), 400
    except Exception as exc:
        return jsonify({"error": str(exc), "version": APP_VERSION}), 400

    try:
        event_type = event["type"]

        if event_type == "checkout.session.completed":
            session = event["data"]["object"]

            session_id = session["id"]
            amount_cents = int(session["amount_total"] or 0)
            payment_status = session["payment_status"] if "payment_status" in session else "paid"

            metadata = session["metadata"] if "metadata" in session else {}
            report_id = metadata["report_id"] if "report_id" in metadata else ""
            recovery_total = metadata["recovery_total"] if "recovery_total" in metadata else ""
            app_name = metadata["app_name"] if "app_name" in metadata else ""
            app_version = metadata["app_version"] if "app_version" in metadata else ""

            customer_details = session["customer_details"] if "customer_details" in session else {}
            customer_email = ""
            if customer_details and "email" in customer_details:
                customer_email = customer_details["email"] or ""

            upsert_payment(
                session_id=session_id,
                report_id=report_id,
                amount_cents=amount_cents,
                recovery_total=recovery_total,
                app_name=app_name,
                app_version=app_version,
                paid=True,
                payment_status=payment_status,
                customer_email=customer_email,
            )

        return jsonify({"received": True, "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"error": str(exc), "version": APP_VERSION}), 500


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
