import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
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


APP_VERSION = "V10.23-founder-pilot-cloud-capture"

app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()
SUCCESS_URL = os.environ.get("SUCCESS_URL", "").strip()
CANCEL_URL = os.environ.get("CANCEL_URL", "").strip()
FOUNDER_ADMIN_TOKEN = os.environ.get("FOUNDER_ADMIN_TOKEN", "").strip()
FOUNDER_CODE_SECRET = os.environ.get("FOUNDER_CODE_SECRET", FOUNDER_ADMIN_TOKEN or STRIPE_WEBHOOK_SECRET or "dev-founder-secret").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def parse_iso_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def normalize_email(value):
    return str(value or "").strip().lower()


def normalize_code(value):
    return str(value or "").strip().upper()


def code_hash(founder_code):
    normalized = normalize_code(founder_code)
    return hmac.new(
        FOUNDER_CODE_SECRET.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_founder_code(email):
    local_part = normalize_email(email).split("@")[0] or "FOUNDER"
    safe_name = "".join(ch for ch in local_part.upper() if ch.isalnum())[:12] or "FOUNDER"
    suffix = secrets.token_hex(2).upper()
    year = utc_now().year
    return f"FOUNDER-{safe_name}-{year}-{suffix}"


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


def sqlite_add_column_if_missing(cur, table_name, column_name, column_type):
    if not sqlite_column_exists(cur, table_name, column_name):
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def postgres_add_column_if_missing(cur, table_name, column_name, column_type):
    if not postgres_column_exists(cur, table_name, column_name):
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


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

                payment_migrations = [
                    ("recovery_total", "TEXT DEFAULT ''"),
                    ("app_name", "TEXT DEFAULT ''"),
                    ("app_version", "TEXT DEFAULT ''"),
                    ("payment_status", "TEXT DEFAULT ''"),
                    ("customer_email", "TEXT DEFAULT ''"),
                    ("updated_at", "TIMESTAMPTZ"),
                ]
                for column_name, column_type in payment_migrations:
                    postgres_add_column_if_missing(cur, "payments", column_name, column_type)

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS founding_members (
                        code_hash TEXT PRIMARY KEY,
                        email TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        expires_at TIMESTAMPTZ NOT NULL,
                        active BOOLEAN DEFAULT TRUE,
                        notes TEXT DEFAULT '',
                        last_validated_at TIMESTAMPTZ,
                        validation_count INTEGER DEFAULT 0
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS report_statistics (
                        report_id TEXT PRIMARY KEY,
                        email TEXT DEFAULT '',
                        founder_code_hash TEXT DEFAULT '',
                        app_name TEXT DEFAULT '',
                        app_version TEXT DEFAULT '',
                        scan_date TIMESTAMPTZ DEFAULT NOW(),
                        orders_found INTEGER DEFAULT 0,
                        tracking_found INTEGER DEFAULT 0,
                        tax_identified TEXT DEFAULT '',
                        paid_scan BOOLEAN DEFAULT FALSE,
                        generated_package BOOLEAN DEFAULT FALSE,
                        source TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS refund_outcomes (
                        report_id TEXT PRIMARY KEY,
                        email TEXT DEFAULT '',
                        amazon_submission_date TIMESTAMPTZ,
                        status TEXT DEFAULT 'Not Submitted',
                        refund_amount TEXT DEFAULT '',
                        resolution_date TIMESTAMPTZ,
                        verification_evidence_status TEXT DEFAULT '',
                        notes TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
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

            payment_migrations = [
                ("recovery_total", "TEXT DEFAULT ''"),
                ("app_name", "TEXT DEFAULT ''"),
                ("app_version", "TEXT DEFAULT ''"),
                ("payment_status", "TEXT DEFAULT ''"),
                ("customer_email", "TEXT DEFAULT ''"),
                ("updated_at", "TEXT"),
            ]
            for column_name, column_type in payment_migrations:
                sqlite_add_column_if_missing(cur, "payments", column_name, column_type)

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS founding_members (
                    code_hash TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    created_at TEXT,
                    expires_at TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    notes TEXT DEFAULT '',
                    last_validated_at TEXT,
                    validation_count INTEGER DEFAULT 0
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS report_statistics (
                    report_id TEXT PRIMARY KEY,
                    email TEXT DEFAULT '',
                    founder_code_hash TEXT DEFAULT '',
                    app_name TEXT DEFAULT '',
                    app_version TEXT DEFAULT '',
                    scan_date TEXT,
                    orders_found INTEGER DEFAULT 0,
                    tracking_found INTEGER DEFAULT 0,
                    tax_identified TEXT DEFAULT '',
                    paid_scan INTEGER DEFAULT 0,
                    generated_package INTEGER DEFAULT 0,
                    source TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS refund_outcomes (
                    report_id TEXT PRIMARY KEY,
                    email TEXT DEFAULT '',
                    amazon_submission_date TEXT,
                    status TEXT DEFAULT 'Not Submitted',
                    refund_amount TEXT DEFAULT '',
                    resolution_date TEXT,
                    verification_evidence_status TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                )
                """
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


def validate_founder_credentials(founder_code, email):
    init_db()
    founder_code = normalize_code(founder_code)
    email = normalize_email(email)
    if not founder_code or not email:
        return False, "Founder code and email are required.", None

    hashed_code = code_hash(founder_code)
    now = utc_now()

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM founding_members WHERE code_hash = %s",
                    (hashed_code,),
                )
                row = cur.fetchone()
                if not row:
                    return False, "Founder code was not found.", None
                row = dict(row)
                if normalize_email(row.get("email")) != email:
                    return False, "Founder code does not match this email.", row
                if not bool(row.get("active")):
                    return False, "Founder code is inactive.", row
                expires_at = row.get("expires_at")
                if expires_at and expires_at < now:
                    return False, "Founder code is expired.", row
                cur.execute(
                    """
                    UPDATE founding_members
                    SET last_validated_at = NOW(),
                        validation_count = COALESCE(validation_count, 0) + 1
                    WHERE code_hash = %s
                    """,
                    (hashed_code,),
                )
            conn.commit()
            return True, "Founder code is valid.", row
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute(
            "SELECT * FROM founding_members WHERE code_hash = ?",
            (hashed_code,),
        ).fetchone()
        if not row:
            return False, "Founder code was not found.", None
        row = dict(row)
        if normalize_email(row.get("email")) != email:
            return False, "Founder code does not match this email.", row
        if int(row.get("active") or 0) != 1:
            return False, "Founder code is inactive.", row
        expires_at = parse_iso_datetime(row.get("expires_at"))
        if expires_at and expires_at < now:
            return False, "Founder code is expired.", row
        cur.execute(
            """
            UPDATE founding_members
            SET last_validated_at = ?,
                validation_count = COALESCE(validation_count, 0) + 1
            WHERE code_hash = ?
            """,
            (utc_now_iso(), hashed_code),
        )
        conn.commit()
        return True, "Founder code is valid.", row
    finally:
        conn.close()


def admin_authorized():
    if not FOUNDER_ADMIN_TOKEN:
        return False
    supplied = request.headers.get("X-Admin-Token", "").strip()
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        supplied = auth_header[7:].strip()
    return hmac.compare_digest(supplied, FOUNDER_ADMIN_TOKEN)


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
            "founder_admin_configured": bool(FOUNDER_ADMIN_TOKEN),
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


@app.route("/admin/create-founder-code", methods=["POST"])
def create_founder_code():
    if not admin_authorized():
        return jsonify({"error": "Unauthorized.", "version": APP_VERSION}), 401

    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"error": "Invalid JSON payload.", "version": APP_VERSION}), 400

    email = normalize_email(payload.get("email"))
    if not email:
        return jsonify({"error": "email is required.", "version": APP_VERSION}), 400

    founder_code = normalize_code(payload.get("founder_code") or generate_founder_code(email))
    expires_at_text = str(payload.get("expires_at", "") or "").strip()
    expires_at = parse_iso_datetime(expires_at_text) if expires_at_text else utc_now() + timedelta(days=365)
    notes = str(payload.get("notes", "") or "").strip()
    hashed_code = code_hash(founder_code)

    init_db()
    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO founding_members (
                        code_hash,
                        email,
                        created_at,
                        expires_at,
                        active,
                        notes,
                        validation_count
                    )
                    VALUES (%s, %s, NOW(), %s, TRUE, %s, 0)
                    ON CONFLICT (code_hash) DO UPDATE SET
                        email = EXCLUDED.email,
                        expires_at = EXCLUDED.expires_at,
                        active = TRUE,
                        notes = EXCLUDED.notes
                    """,
                    (hashed_code, email, expires_at, notes),
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
                INSERT OR REPLACE INTO founding_members (
                    code_hash,
                    email,
                    created_at,
                    expires_at,
                    active,
                    notes,
                    validation_count
                )
                VALUES (?, ?, ?, ?, 1, ?, 0)
                """,
                (hashed_code, email, utc_now_iso(), expires_at.isoformat(), notes),
            )
            conn.commit()
        finally:
            conn.close()

    return jsonify(
        {
            "created": True,
            "founder_code": founder_code,
            "email": email,
            "expires_at": expires_at.isoformat(),
            "version": APP_VERSION,
        }
    )


@app.route("/validate-founder-code", methods=["POST"])
def validate_founder_code():
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"valid": False, "error": "Invalid JSON payload.", "version": APP_VERSION}), 400

    founder_code = payload.get("founder_code", "")
    email = payload.get("email", "")
    valid, message, row = validate_founder_credentials(founder_code, email)

    expires_at = ""
    if row and row.get("expires_at"):
        expires_at = str(row.get("expires_at"))

    return jsonify(
        {
            "valid": bool(valid),
            "message": message,
            "email": normalize_email(email),
            "expires_at": expires_at,
            "membership_type": "Founding Member" if valid else "",
            "version": APP_VERSION,
        }
    )


@app.route("/record-report-statistics", methods=["POST"])
def record_report_statistics():
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"recorded": False, "error": "Invalid JSON payload.", "version": APP_VERSION}), 400

    report_id = str(payload.get("report_id", "") or "").strip()
    if not report_id:
        return jsonify({"recorded": False, "error": "report_id is required.", "version": APP_VERSION}), 400

    email = normalize_email(payload.get("email"))
    founder_code = normalize_code(payload.get("founder_code"))
    founder_code_hash = ""
    if founder_code:
        valid, message, _row = validate_founder_credentials(founder_code, email)
        if not valid:
            return jsonify({"recorded": False, "error": message, "version": APP_VERSION}), 403
        founder_code_hash = code_hash(founder_code)

    app_name = str(payload.get("app_name", "AMAZON-MAXSHIPPING TRACKER") or "").strip()
    app_version = str(payload.get("app_version", "") or "").strip()
    source = str(payload.get("source", "desktop_app") or "").strip()
    scan_date = parse_iso_datetime(payload.get("scan_date")) or utc_now()

    try:
        orders_found = int(payload.get("orders_found", 0) or 0)
    except Exception:
        orders_found = 0
    try:
        tracking_found = int(payload.get("tracking_found", 0) or 0)
    except Exception:
        tracking_found = 0

    tax_identified = str(payload.get("tax_identified", "") or "").strip()
    paid_scan = bool(payload.get("paid_scan", False))
    generated_package = bool(payload.get("generated_package", False))

    init_db()
    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO report_statistics (
                        report_id,
                        email,
                        founder_code_hash,
                        app_name,
                        app_version,
                        scan_date,
                        orders_found,
                        tracking_found,
                        tax_identified,
                        paid_scan,
                        generated_package,
                        source,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (report_id) DO UPDATE SET
                        email = EXCLUDED.email,
                        founder_code_hash = EXCLUDED.founder_code_hash,
                        app_name = EXCLUDED.app_name,
                        app_version = EXCLUDED.app_version,
                        scan_date = EXCLUDED.scan_date,
                        orders_found = EXCLUDED.orders_found,
                        tracking_found = EXCLUDED.tracking_found,
                        tax_identified = EXCLUDED.tax_identified,
                        paid_scan = EXCLUDED.paid_scan,
                        generated_package = EXCLUDED.generated_package,
                        source = EXCLUDED.source,
                        updated_at = NOW()
                    """,
                    (
                        report_id,
                        email,
                        founder_code_hash,
                        app_name,
                        app_version,
                        scan_date,
                        orders_found,
                        tracking_found,
                        tax_identified,
                        paid_scan,
                        generated_package,
                        source,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO refund_outcomes (report_id, email, status, created_at, updated_at)
                    VALUES (%s, %s, 'Not Submitted', NOW(), NOW())
                    ON CONFLICT (report_id) DO NOTHING
                    """,
                    (report_id, email),
                )
            conn.commit()
        finally:
            conn.close()
    else:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            now = utc_now_iso()
            cur.execute(
                """
                INSERT OR REPLACE INTO report_statistics (
                    report_id,
                    email,
                    founder_code_hash,
                    app_name,
                    app_version,
                    scan_date,
                    orders_found,
                    tracking_found,
                    tax_identified,
                    paid_scan,
                    generated_package,
                    source,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM report_statistics WHERE report_id = ?), ?), ?)
                """,
                (
                    report_id,
                    email,
                    founder_code_hash,
                    app_name,
                    app_version,
                    scan_date.isoformat(),
                    orders_found,
                    tracking_found,
                    tax_identified,
                    1 if paid_scan else 0,
                    1 if generated_package else 0,
                    source,
                    report_id,
                    now,
                    now,
                ),
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO refund_outcomes (report_id, email, status, created_at, updated_at)
                VALUES (?, ?, 'Not Submitted', ?, ?)
                """,
                (report_id, email, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    return jsonify(
        {
            "recorded": True,
            "report_id": report_id,
            "email": email,
            "version": APP_VERSION,
        }
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
