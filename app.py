import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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

APP_VERSION = "V10.28.0-admin-max-only-cloud-reset"

app = Flask(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()
SUCCESS_URL = os.environ.get("SUCCESS_URL", "").strip()
CANCEL_URL = os.environ.get("CANCEL_URL", "").strip()
FOUNDER_ADMIN_TOKEN = os.environ.get("FOUNDER_ADMIN_TOKEN", "").strip()
ALLOW_SQLITE_FALLBACK = os.environ.get("SHIPTAXREFUND_ALLOW_SQLITE_FALLBACK", "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat()


def normalize_email(value):
    return str(value or "").strip().lower()


def normalize_code(value):
    return str(value or "").strip().upper()


def founder_code_hash(value):
    return hashlib.sha256(normalize_code(value).encode("utf-8")).hexdigest()


def safe_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "active"}


def postgres_available():
    return bool(DATABASE_URL and psycopg2 is not None)


def sqlite_fallback_allowed():
    return safe_bool(ALLOW_SQLITE_FALLBACK)


def param():
    return "%s" if postgres_available() else "?"


def params(count):
    return ", ".join([param()] * int(count))


def get_base_url():
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/") + "/"
    try:
        return request.host_url
    except Exception:
        return ""


def get_success_url():
    return SUCCESS_URL or urljoin(get_base_url(), "payment-success")


def get_cancel_url():
    return CANCEL_URL or urljoin(get_base_url(), "payment-cancelled")


def get_db_connection():
    if postgres_available():
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    if sqlite_fallback_allowed():
        return sqlite3.connect(os.environ.get("SQLITE_PATH", "payments.sqlite3"))
    raise RuntimeError(
        "DATABASE_URL is required. Local SQLite fallback is disabled by default so "
        "customer/payment/refund history cannot be stored locally by accident. "
        "Set SHIPTAXREFUND_ALLOW_SQLITE_FALLBACK=1 only for developer testing."
    )


def row_to_dict(cur, row):
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "keys"):
        try:
            return {key: row[key] for key in row.keys()}
        except Exception:
            pass
    columns = [desc[0] for desc in (cur.description or [])]
    return {columns[i]: value for i, value in enumerate(row) if i < len(columns)}


def rows_to_dicts(cur, rows):
    return [row_to_dict(cur, row) for row in (rows or [])]


def scalar(row, default=0):
    if row is None:
        return default
    if isinstance(row, dict):
        try:
            return next(iter(row.values()))
        except StopIteration:
            return default
    if hasattr(row, "keys"):
        try:
            return row[0]
        except Exception:
            try:
                return next(iter(dict(row).values()))
            except Exception:
                return default
    try:
        return row[0]
    except Exception:
        return default


def table_columns(cur, table_name):
    if postgres_available():
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table_name,),
        )
        return {str(row[0]) for row in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table_name})")
    return {str(row[1]) for row in cur.fetchall()}


def add_missing_columns(cur, table_name, column_types):
    existing = table_columns(cur, table_name)
    for name, col_type in column_types.items():
        if name not in existing:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {col_type}")


def execute_one(sql, args=()):
    conn = get_db_connection()
    try:
        if not postgres_available():
            conn.row_factory = sqlite3.Row
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if postgres_available() else conn.cursor()
        cur.execute(sql, tuple(args))
        row = cur.fetchone()
        return row_to_dict(cur, row)
    finally:
        conn.close()


def execute_all(sql, args=()):
    conn = get_db_connection()
    try:
        if not postgres_available():
            conn.row_factory = sqlite3.Row
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if postgres_available() else conn.cursor()
        cur.execute(sql, tuple(args))
        return rows_to_dicts(cur, cur.fetchall())
    finally:
        conn.close()


def json_text(value):
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


def clean_text(value, max_len=1000):
    text = str(value or "").strip()
    return text[:max_len] if max_len and len(text) > max_len else text


TABLE_COLUMNS = {
    "payments": {
        "session_id": "TEXT PRIMARY KEY", "status": "TEXT DEFAULT ''", "amount_cents": "INTEGER DEFAULT 0",
        "report_id": "TEXT DEFAULT ''", "created_at": "TEXT", "paid_at": "TEXT", "recovery_total": "TEXT DEFAULT ''",
        "app_name": "TEXT DEFAULT ''", "app_version": "TEXT DEFAULT ''", "payment_status": "TEXT DEFAULT ''",
        "customer_email": "TEXT DEFAULT ''", "updated_at": "TEXT",
    },
    "founding_members": {
        "founder_code": "TEXT PRIMARY KEY", "code_hash": "TEXT DEFAULT ''", "email": "TEXT DEFAULT ''",
        "membership_type": "TEXT DEFAULT 'Founding Member'", "active": "BOOLEAN DEFAULT TRUE", "created_at": "TEXT",
        "expires_at": "TEXT", "notes": "TEXT DEFAULT ''", "last_validated_at": "TEXT", "validation_count": "INTEGER DEFAULT 0",
    },
    "report_statistics": {
        "report_id": "TEXT PRIMARY KEY", "founder_code": "TEXT DEFAULT ''", "email": "TEXT DEFAULT ''",
        "app_name": "TEXT DEFAULT ''", "app_version": "TEXT DEFAULT ''", "scan_mode": "TEXT DEFAULT ''", "generated_at": "TEXT",
        "orders_found": "INTEGER DEFAULT 0", "tracking_found": "INTEGER DEFAULT 0", "tax_identified": "TEXT DEFAULT ''",
        "paid_scan": "BOOLEAN DEFAULT FALSE", "payment_session_id": "TEXT DEFAULT ''", "generated_package": "BOOLEAN DEFAULT TRUE",
        "proof_consent": "BOOLEAN DEFAULT TRUE", "notes": "TEXT DEFAULT ''", "raw_payload": "TEXT DEFAULT ''",
    },
    "refund_outcomes": {
        "report_id": "TEXT PRIMARY KEY", "founder_code": "TEXT DEFAULT ''", "email": "TEXT DEFAULT ''",
        "amazon_submission_date": "TEXT", "status": "TEXT DEFAULT 'Not Submitted'", "refund_amount": "TEXT DEFAULT ''",
        "resolution_date": "TEXT", "verification_evidence_status": "TEXT DEFAULT ''", "notes": "TEXT DEFAULT ''",
        "created_at": "TEXT", "updated_at": "TEXT",
    },
    "customers": {
        "customer_uuid": "TEXT PRIMARY KEY", "email": "TEXT UNIQUE NOT NULL", "display_name": "TEXT DEFAULT ''",
        "source": "TEXT DEFAULT ''", "created_at": "TEXT", "updated_at": "TEXT",
    },
    "customer_forwarder_state": {
        "customer_uuid": "TEXT NOT NULL", "forwarder": "TEXT NOT NULL", "last_processed_invoice": "TEXT DEFAULT ''",
        "last_processed_tracking": "TEXT DEFAULT ''", "last_processed_order_date": "TEXT DEFAULT ''", "updated_at": "TEXT",
    },
    "customer_submission_history": {
        "history_id": "TEXT PRIMARY KEY", "customer_uuid": "TEXT NOT NULL", "forwarder": "TEXT DEFAULT ''",
        "amazon_order_id": "TEXT DEFAULT ''", "tracking_number": "TEXT DEFAULT ''", "bol": "TEXT DEFAULT ''", "invoice_number": "TEXT DEFAULT ''",
        "submitted_date": "TEXT", "refund_amount": "TEXT DEFAULT ''", "certification_id": "TEXT DEFAULT ''",
        "source_run_id": "TEXT DEFAULT ''", "raw_payload": "TEXT DEFAULT ''", "created_at": "TEXT",
    },
    "customer_runs": {
        "run_id": "TEXT PRIMARY KEY", "customer_uuid": "TEXT NOT NULL", "forwarder": "TEXT DEFAULT ''", "app_version": "TEXT DEFAULT ''",
        "scan_mode": "TEXT DEFAULT ''", "started_at": "TEXT", "completed_at": "TEXT", "status": "TEXT DEFAULT ''",
        "matched_count": "INTEGER DEFAULT 0", "excluded_prior_count": "INTEGER DEFAULT 0", "recovery_total": "TEXT DEFAULT ''",
        "raw_payload": "TEXT DEFAULT ''",
    },
    "customer_refund_lifecycle": {
        "lifecycle_id": "TEXT PRIMARY KEY", "customer_uuid": "TEXT NOT NULL", "forwarder": "TEXT DEFAULT ''",
        "profile_key": "TEXT DEFAULT ''", "amazon_account_email": "TEXT DEFAULT ''", "submission_id": "TEXT DEFAULT ''",
        "amazon_order_id": "TEXT DEFAULT ''", "current_status": "TEXT DEFAULT ''", "requested_tax_amount": "TEXT DEFAULT ''",
        "refund_amount_received": "TEXT DEFAULT ''", "refund_received_date": "TEXT DEFAULT ''", "refund_method": "TEXT DEFAULT ''",
        "action_needed": "TEXT DEFAULT ''", "status_detail": "TEXT DEFAULT ''", "tracker_payload": "TEXT DEFAULT ''",
        "source_run_id": "TEXT DEFAULT ''", "created_at": "TEXT", "updated_at": "TEXT",
    },
    "customer_refunds_received": {
        "refund_id": "TEXT PRIMARY KEY", "customer_uuid": "TEXT NOT NULL", "amazon_account_email": "TEXT DEFAULT ''",
        "amazon_order_id": "TEXT DEFAULT ''", "refund_amount_received": "TEXT DEFAULT ''", "refund_method": "TEXT DEFAULT ''",
        "refund_received_date": "TEXT DEFAULT ''", "email_date": "TEXT DEFAULT ''", "email_from": "TEXT DEFAULT ''",
        "email_to": "TEXT DEFAULT ''", "email_subject": "TEXT DEFAULT ''", "parsed_status": "TEXT DEFAULT ''",
        "source": "TEXT DEFAULT ''", "source_hash": "TEXT DEFAULT ''", "notes": "TEXT DEFAULT ''", "text_preview": "TEXT DEFAULT ''",
        "raw_payload": "TEXT DEFAULT ''", "created_at": "TEXT", "updated_at": "TEXT",
    },
    "customer_claimant_profiles": {
        "profile_key": "TEXT PRIMARY KEY", "customer_uuid": "TEXT DEFAULT ''", "amazon_account_email": "TEXT NOT NULL",
        "claimant_name": "TEXT NOT NULL", "signature_line": "TEXT DEFAULT ''", "max_shipping_email": "TEXT NOT NULL",
        "max_shipping_account_number": "TEXT NOT NULL", "amazon_ship_to_address": "TEXT NOT NULL", "export_destination": "TEXT DEFAULT ''",
        "preferred_contact_email": "TEXT DEFAULT ''", "claimant_label": "TEXT DEFAULT ''", "app_name": "TEXT DEFAULT ''",
        "app_version": "TEXT DEFAULT ''", "source": "TEXT DEFAULT ''", "raw_payload": "TEXT DEFAULT ''", "created_at": "TEXT", "updated_at": "TEXT",
    },
    "customer_claimant_profile_events": {
        "event_id": "TEXT PRIMARY KEY", "profile_key": "TEXT NOT NULL", "event_type": "TEXT DEFAULT 'profile_upsert'",
        "raw_payload": "TEXT DEFAULT ''", "created_at": "TEXT",
    },
    "customer_export_references": {
        "reference_id": "TEXT PRIMARY KEY", "customer_uuid": "TEXT NOT NULL", "forwarder": "TEXT DEFAULT ''", "profile_key": "TEXT DEFAULT ''",
        "amazon_account_email": "TEXT DEFAULT ''", "max_shipping_account_number": "TEXT DEFAULT ''", "export_year": "TEXT DEFAULT ''",
        "amazon_tracking_number": "TEXT DEFAULT ''", "export_reference": "TEXT DEFAULT ''", "reference_source": "TEXT DEFAULT ''",
        "source_run_id": "TEXT DEFAULT ''", "raw_payload": "TEXT DEFAULT ''", "created_at": "TEXT", "updated_at": "TEXT",
    },
    "customer_export_reference_sequences": {
        "customer_uuid": "TEXT NOT NULL", "forwarder": "TEXT NOT NULL", "profile_key": "TEXT NOT NULL",
        "export_year": "TEXT NOT NULL", "last_sequence_number": "INTEGER DEFAULT 0", "updated_at": "TEXT",
    },
    "admin_cloud_reset_archive": {
        "archive_id": "TEXT PRIMARY KEY", "reset_id": "TEXT NOT NULL", "reset_scope": "TEXT DEFAULT ''", "amazon_refund_email": "TEXT DEFAULT ''",
        "customer_uuid": "TEXT DEFAULT ''", "table_name": "TEXT NOT NULL", "row_primary_key": "TEXT DEFAULT ''", "row_payload": "TEXT DEFAULT ''", "created_at": "TEXT",
    },
}


def create_table_sql(table, columns):
    col_sql = ",\n    ".join(f"{name} {typ}" for name, typ in columns.items())
    extras = []
    if table == "customer_forwarder_state":
        extras.append("PRIMARY KEY (customer_uuid, forwarder)")
    if table == "customer_export_reference_sequences":
        extras.append("PRIMARY KEY (customer_uuid, forwarder, profile_key, export_year)")
    if extras:
        col_sql += ",\n    " + ",\n    ".join(extras)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n    {col_sql}\n)"


def init_db():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for table, columns in TABLE_COLUMNS.items():
            cur.execute(create_table_sql(table, columns))
            add_missing_columns(cur, table, columns)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_submission_history_customer_forwarder ON customer_submission_history (customer_uuid, forwarder)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_submission_unique ON customer_submission_history (customer_uuid, forwarder, amazon_order_id, tracking_number, bol, invoice_number)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refund_lifecycle_customer_forwarder ON customer_refund_lifecycle (customer_uuid, forwarder)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refund_lifecycle_order ON customer_refund_lifecycle (amazon_order_id)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_refund_lifecycle_unique ON customer_refund_lifecycle (customer_uuid, forwarder, profile_key, submission_id, amazon_order_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refunds_received_customer ON customer_refunds_received (customer_uuid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refunds_received_order ON customer_refunds_received (amazon_order_id)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_refunds_received_unique ON customer_refunds_received (customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_customer ON customer_claimant_profiles (customer_uuid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_amazon_email ON customer_claimant_profiles (amazon_account_email)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_max_account ON customer_claimant_profiles (max_shipping_account_number)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profile_events_profile ON customer_claimant_profile_events (profile_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_export_references_customer ON customer_export_references (customer_uuid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_export_references_tracking ON customer_export_references (amazon_tracking_number)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_export_references_tracking ON customer_export_references (customer_uuid, forwarder, profile_key, export_year, amazon_tracking_number)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_cloud_reset_archive_reset ON admin_cloud_reset_archive (reset_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_cloud_reset_archive_customer ON admin_cloud_reset_archive (customer_uuid)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_founding_members_founder_code_nonblank ON founding_members (founder_code) WHERE founder_code IS NOT NULL AND founder_code <> ''")
        conn.commit()
    finally:
        conn.close()


def get_founder_member(founder_code, email=""):
    init_db()
    founder_code = normalize_code(founder_code)
    email = normalize_email(email)
    if not founder_code:
        return None
    ph = param()
    if email:
        return execute_one(f"SELECT * FROM founding_members WHERE founder_code = {ph} AND LOWER(email) = {ph}", (founder_code, email))
    return execute_one(f"SELECT * FROM founding_members WHERE founder_code = {ph}", (founder_code,))


def founder_row_is_active(row):
    if not row:
        return False, "Founder code was not found for this email."
    if not safe_bool(row.get("active")):
        return False, "Founder code is inactive."
    expires_at = row.get("expires_at")
    if not expires_at:
        return False, "Founder code has no expiration date."
    try:
        exp = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        return False, "Founder code expiration date is invalid."
    if exp < utc_now():
        return False, "Founder code has expired."
    return True, ""


def mark_founder_validated(founder_code):
    init_db()
    ph = param()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if postgres_available():
            cur.execute(f"UPDATE founding_members SET last_validated_at = {ph}, validation_count = COALESCE(validation_count, 0) + 1 WHERE founder_code = {ph}", (utc_now_iso(), normalize_code(founder_code)))
        else:
            cur.execute("UPDATE founding_members SET last_validated_at = ?, validation_count = COALESCE(validation_count, 0) + 1 WHERE founder_code = ?", (utc_now_iso(), normalize_code(founder_code)))
        conn.commit()
    finally:
        conn.close()


def verify_admin_request():
    if not FOUNDER_ADMIN_TOKEN:
        return False, "FOUNDER_ADMIN_TOKEN is not configured on the server."
    supplied = request.headers.get("X-Admin-Token", "").strip()
    if not supplied:
        supplied = str((request.get_json(silent=True) or {}).get("admin_token", "") or "").strip()
    return (supplied == FOUNDER_ADMIN_TOKEN, "" if supplied == FOUNDER_ADMIN_TOKEN else "Invalid or missing admin token.")


def upsert_payment(session_id, report_id="", amount_cents=0, recovery_total="", app_name="", app_version="", paid=False, payment_status="", customer_email=""):
    init_db()
    status = "paid" if paid else (payment_status or "created")
    now = utc_now_iso()
    paid_at = now if paid else ""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if postgres_available():
            cur.execute(
                """
                INSERT INTO payments (session_id, status, amount_cents, report_id, created_at, paid_at, recovery_total, app_name, app_version, payment_status, customer_email, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (session_id) DO UPDATE SET
                    status = CASE WHEN EXCLUDED.status='paid' THEN 'paid' WHEN payments.status='paid' THEN payments.status ELSE EXCLUDED.status END,
                    amount_cents = CASE WHEN EXCLUDED.amount_cents > 0 THEN EXCLUDED.amount_cents ELSE payments.amount_cents END,
                    report_id = COALESCE(NULLIF(EXCLUDED.report_id,''), payments.report_id),
                    paid_at = COALESCE(NULLIF(EXCLUDED.paid_at,''), payments.paid_at),
                    recovery_total = COALESCE(NULLIF(EXCLUDED.recovery_total,''), payments.recovery_total),
                    app_name = COALESCE(NULLIF(EXCLUDED.app_name,''), payments.app_name),
                    app_version = COALESCE(NULLIF(EXCLUDED.app_version,''), payments.app_version),
                    payment_status = COALESCE(NULLIF(EXCLUDED.payment_status,''), payments.payment_status),
                    customer_email = COALESCE(NULLIF(EXCLUDED.customer_email,''), payments.customer_email),
                    updated_at = EXCLUDED.updated_at
                """,
                (session_id, status, int(amount_cents or 0), report_id, now, paid_at, str(recovery_total or ""), app_name, app_version, payment_status, customer_email, now),
            )
        else:
            cur.execute(
                """
                INSERT INTO payments (session_id, status, amount_cents, report_id, created_at, paid_at, recovery_total, app_name, app_version, payment_status, customer_email, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status = CASE WHEN excluded.status='paid' THEN 'paid' WHEN status='paid' THEN status ELSE excluded.status END,
                    amount_cents = CASE WHEN excluded.amount_cents > 0 THEN excluded.amount_cents ELSE amount_cents END,
                    report_id = CASE WHEN excluded.report_id!='' THEN excluded.report_id ELSE report_id END,
                    paid_at = CASE WHEN excluded.paid_at!='' THEN excluded.paid_at ELSE paid_at END,
                    recovery_total = CASE WHEN excluded.recovery_total!='' THEN excluded.recovery_total ELSE recovery_total END,
                    app_name = CASE WHEN excluded.app_name!='' THEN excluded.app_name ELSE app_name END,
                    app_version = CASE WHEN excluded.app_version!='' THEN excluded.app_version ELSE app_version END,
                    payment_status = CASE WHEN excluded.payment_status!='' THEN excluded.payment_status ELSE payment_status END,
                    customer_email = CASE WHEN excluded.customer_email!='' THEN excluded.customer_email ELSE customer_email END,
                    updated_at = excluded.updated_at
                """,
                (session_id, status, int(amount_cents or 0), report_id, now, paid_at, str(recovery_total or ""), app_name, app_version, payment_status, customer_email, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_payment(session_id):
    init_db()
    ph = param()
    return execute_one(f"SELECT * FROM payments WHERE session_id = {ph}", (str(session_id or "").strip(),))


def payment_row_is_paid(row):
    if not row:
        return False
    return str(row.get("status", "")).lower() == "paid" or str(row.get("payment_status", "")).lower() == "paid" or bool(row.get("paid_at"))


def create_or_update_founder_code(founder_code, email, expires_at, notes="", active=True, membership_type="Founding Member"):
    init_db()
    founder_code = normalize_code(founder_code)
    email = normalize_email(email)
    now = utc_now_iso()
    active_value = bool(active)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if postgres_available():
            cur.execute(
                """
                INSERT INTO founding_members (founder_code, code_hash, email, membership_type, active, created_at, expires_at, notes, validation_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0)
                ON CONFLICT (founder_code) DO UPDATE SET email=EXCLUDED.email, membership_type=EXCLUDED.membership_type,
                    active=EXCLUDED.active, expires_at=EXCLUDED.expires_at, notes=EXCLUDED.notes, code_hash=EXCLUDED.code_hash
                """,
                (founder_code, founder_code_hash(founder_code), email, membership_type, active_value, now, expires_at, notes),
            )
        else:
            cur.execute(
                """
                INSERT INTO founding_members (founder_code, code_hash, email, membership_type, active, created_at, expires_at, notes, validation_count)
                VALUES (?,?,?,?,?,?,?,?,0)
                ON CONFLICT(founder_code) DO UPDATE SET email=excluded.email, membership_type=excluded.membership_type,
                    active=excluded.active, expires_at=excluded.expires_at, notes=excluded.notes, code_hash=excluded.code_hash
                """,
                (founder_code, founder_code_hash(founder_code), email, membership_type, 1 if active_value else 0, now, expires_at, notes),
            )
        conn.commit()
    finally:
        conn.close()


def require_json_payload():
    try:
        return request.get_json(force=True, silent=False) or {}, None
    except Exception:
        return None, "Invalid JSON payload."


def make_customer_uuid():
    return str(uuid.uuid4())


def validate_customer_access_payload(payload):
    email = normalize_email(payload.get("email", ""))
    founder_code = normalize_code(payload.get("founder_code", "") or payload.get("code", ""))
    payment_session_id = str(payload.get("payment_session_id", "") or payload.get("session_id", "")).strip()
    if not email or "@" not in email:
        return False, "Valid email is required.", ""
    if founder_code:
        row = get_founder_member(founder_code, email)
        valid, reason = founder_row_is_active(row)
        if valid:
            mark_founder_validated(founder_code)
            return True, "", email
        return False, reason, ""
    if payment_session_id:
        row = get_payment(payment_session_id)
        if not payment_row_is_paid(row):
            return False, "Payment session was not found or is not paid.", ""
        payment_email = normalize_email((row or {}).get("customer_email", ""))
        if payment_email and payment_email != email:
            return False, "Payment session email does not match requested customer email.", ""
        return True, "", email
    return False, "A valid access code or paid payment session is required.", ""


def get_or_create_customer(email, display_name="", source=""):
    init_db()
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("Valid email is required.")
    ph = param()
    existing = execute_one(f"SELECT * FROM customers WHERE LOWER(email) = {ph}", (email,))
    now = utc_now_iso()
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if postgres_available() else conn.cursor()
        if existing:
            customer_uuid = existing["customer_uuid"]
            cur.execute(f"UPDATE customers SET display_name=COALESCE(NULLIF({ph},''), display_name), source=COALESCE(NULLIF({ph},''), source), updated_at={ph} WHERE customer_uuid={ph}", (display_name, source, now, customer_uuid))
        else:
            customer_uuid = make_customer_uuid()
            cur.execute(f"INSERT INTO customers (customer_uuid,email,display_name,source,created_at,updated_at) VALUES ({params(6)})", (customer_uuid, email, display_name, source, now, now))
        conn.commit()
    finally:
        conn.close()
    return execute_one(f"SELECT * FROM customers WHERE customer_uuid = {ph}", (customer_uuid,))


def get_customer_by_uuid(customer_uuid):
    init_db()
    ph = param()
    return execute_one(f"SELECT * FROM customers WHERE customer_uuid = {ph}", (str(customer_uuid or "").strip(),))


def resolve_authorized_customer(payload, email):
    customer_uuid = str(payload.get("customer_uuid", "") or "").strip()
    customer = get_customer_by_uuid(customer_uuid) if customer_uuid else None
    if not customer:
        return get_or_create_customer(email, payload.get("display_name", "") or payload.get("customer_name", ""), payload.get("source", "desktop"))
    if normalize_email(customer.get("email", "")) != normalize_email(email):
        raise PermissionError("Customer UUID does not match requested email.")
    return customer


def get_customer_history(customer_uuid, forwarder=""):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    forwarder = str(forwarder or "").strip().lower()
    ph = param()
    if forwarder:
        rows = execute_all(f"SELECT * FROM customer_submission_history WHERE customer_uuid={ph} AND LOWER(forwarder)={ph} ORDER BY created_at ASC", (customer_uuid, forwarder))
        state = execute_all(f"SELECT * FROM customer_forwarder_state WHERE customer_uuid={ph} AND LOWER(forwarder)={ph}", (customer_uuid, forwarder))
    else:
        rows = execute_all(f"SELECT * FROM customer_submission_history WHERE customer_uuid={ph} ORDER BY created_at ASC", (customer_uuid,))
        state = execute_all(f"SELECT * FROM customer_forwarder_state WHERE customer_uuid={ph}", (customer_uuid,))
    return rows, state


def upsert_customer_forwarder_state(customer_uuid, forwarder, state):
    init_db()
    forwarder = str(forwarder or "").strip().lower()
    if not customer_uuid or not forwarder:
        return False
    values = (
        customer_uuid, forwarder, str((state or {}).get("last_processed_invoice", "") or "").strip(),
        str((state or {}).get("last_processed_tracking", "") or "").strip(),
        str((state or {}).get("last_processed_order_date", "") or "").strip(), utc_now_iso()
    )
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if postgres_available():
            cur.execute(
                """
                INSERT INTO customer_forwarder_state (customer_uuid,forwarder,last_processed_invoice,last_processed_tracking,last_processed_order_date,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (customer_uuid, forwarder) DO UPDATE SET
                  last_processed_invoice=COALESCE(NULLIF(EXCLUDED.last_processed_invoice,''), customer_forwarder_state.last_processed_invoice),
                  last_processed_tracking=COALESCE(NULLIF(EXCLUDED.last_processed_tracking,''), customer_forwarder_state.last_processed_tracking),
                  last_processed_order_date=COALESCE(NULLIF(EXCLUDED.last_processed_order_date,''), customer_forwarder_state.last_processed_order_date),
                  updated_at=EXCLUDED.updated_at
                """, values)
        else:
            cur.execute(
                """
                INSERT INTO customer_forwarder_state (customer_uuid,forwarder,last_processed_invoice,last_processed_tracking,last_processed_order_date,updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(customer_uuid, forwarder) DO UPDATE SET
                  last_processed_invoice=CASE WHEN excluded.last_processed_invoice!='' THEN excluded.last_processed_invoice ELSE last_processed_invoice END,
                  last_processed_tracking=CASE WHEN excluded.last_processed_tracking!='' THEN excluded.last_processed_tracking ELSE last_processed_tracking END,
                  last_processed_order_date=CASE WHEN excluded.last_processed_order_date!='' THEN excluded.last_processed_order_date ELSE last_processed_order_date END,
                  updated_at=excluded.updated_at
                """, values)
        conn.commit()
    finally:
        conn.close()
    return True


def upsert_customer_history(customer_uuid, records, forwarder="", source_run_id=""):
    init_db()
    if not isinstance(records, list):
        raise ValueError("records must be a list.")
    count = 0
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for record in records:
            if not isinstance(record, dict):
                continue
            fwd = str(record.get("forwarder", "") or forwarder or "").strip().lower()
            order_id = clean_text(record.get("amazon_order_id", "") or record.get("order_id", ""), 100)
            tracking = clean_text(record.get("tracking_number", "") or record.get("tracking", ""), 200)
            bol = clean_text(record.get("bol", "") or record.get("bl", ""), 500)
            invoice = clean_text(record.get("invoice_number", "") or record.get("invoice", ""), 500)
            if not any([order_id, tracking, bol, invoice]):
                continue
            values = (clean_text(record.get("history_id") or make_customer_uuid()), customer_uuid, fwd, order_id, tracking, bol, invoice,
                      clean_text(record.get("submitted_date") or utc_now_iso()), clean_text(record.get("refund_amount") or record.get("tax", "")),
                      clean_text(record.get("certification_id", "")), clean_text(record.get("source_run_id") or source_run_id), json_text(record), utc_now_iso())
            if postgres_available():
                cur.execute(
                    """
                    INSERT INTO customer_submission_history (history_id,customer_uuid,forwarder,amazon_order_id,tracking_number,bol,invoice_number,submitted_date,refund_amount,certification_id,source_run_id,raw_payload,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (customer_uuid, forwarder, amazon_order_id, tracking_number, bol, invoice_number) DO UPDATE SET
                      submitted_date=EXCLUDED.submitted_date, refund_amount=COALESCE(NULLIF(EXCLUDED.refund_amount,''), customer_submission_history.refund_amount),
                      certification_id=COALESCE(NULLIF(EXCLUDED.certification_id,''), customer_submission_history.certification_id),
                      source_run_id=COALESCE(NULLIF(EXCLUDED.source_run_id,''), customer_submission_history.source_run_id), raw_payload=EXCLUDED.raw_payload
                    """, values)
            else:
                cur.execute(
                    """
                    INSERT INTO customer_submission_history (history_id,customer_uuid,forwarder,amazon_order_id,tracking_number,bol,invoice_number,submitted_date,refund_amount,certification_id,source_run_id,raw_payload,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(customer_uuid, forwarder, amazon_order_id, tracking_number, bol, invoice_number) DO UPDATE SET
                      submitted_date=excluded.submitted_date, refund_amount=CASE WHEN excluded.refund_amount!='' THEN excluded.refund_amount ELSE refund_amount END,
                      certification_id=CASE WHEN excluded.certification_id!='' THEN excluded.certification_id ELSE certification_id END,
                      source_run_id=CASE WHEN excluded.source_run_id!='' THEN excluded.source_run_id ELSE source_run_id END, raw_payload=excluded.raw_payload
                    """, values)
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def record_customer_run(payload, customer_uuid):
    init_db()
    run_id = clean_text(payload.get("run_id") or make_customer_uuid())
    values = (run_id, customer_uuid, clean_text(payload.get("forwarder", "")).lower(), clean_text(payload.get("app_version", "")),
              clean_text(payload.get("scan_mode", "")), utc_now_iso(), utc_now_iso(), clean_text(payload.get("status", "completed") or "completed"),
              int(payload.get("matched_count", 0) or 0), int(payload.get("excluded_prior_count", 0) or 0), clean_text(payload.get("recovery_total", "")), json_text(payload))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if postgres_available():
            cur.execute(
                """
                INSERT INTO customer_runs (run_id,customer_uuid,forwarder,app_version,scan_mode,started_at,completed_at,status,matched_count,excluded_prior_count,recovery_total,raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET completed_at=EXCLUDED.completed_at,status=EXCLUDED.status,matched_count=EXCLUDED.matched_count,
                  excluded_prior_count=EXCLUDED.excluded_prior_count,recovery_total=EXCLUDED.recovery_total,raw_payload=EXCLUDED.raw_payload
                """, values)
        else:
            cur.execute(
                """
                INSERT INTO customer_runs (run_id,customer_uuid,forwarder,app_version,scan_mode,started_at,completed_at,status,matched_count,excluded_prior_count,recovery_total,raw_payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET completed_at=excluded.completed_at,status=excluded.status,matched_count=excluded.matched_count,
                  excluded_prior_count=excluded.excluded_prior_count,recovery_total=excluded.recovery_total,raw_payload=excluded.raw_payload
                """, values)
        conn.commit()
    finally:
        conn.close()
    return run_id


def field(record, *keys):
    for key in keys:
        if key in record and record.get(key) not in (None, ""):
            return clean_text(record.get(key, ""), 2000)
    return ""


def normalize_refund_lifecycle_record(record, customer_uuid, forwarder_default="", source_run_id=""):
    record = record if isinstance(record, dict) else {}
    order_id = field(record, "amazon_order_id", "Amazon Order ID", "order_id")
    if not order_id:
        return None
    forwarder = (field(record, "forwarder", "Forwarder") or forwarder_default).lower()
    profile_key = field(record, "profile_key", "Profile Key", "Customer Profile Key")
    submission_id = field(record, "submission_id", "Submission ID", "certification_id")
    lifecycle_id = field(record, "lifecycle_id", "Lifecycle ID")
    if not lifecycle_id:
        lifecycle_id = "lifecycle_" + hashlib.sha256("|".join([customer_uuid, forwarder, profile_key, submission_id, order_id]).encode("utf-8")).hexdigest()[:32]
    return {
        "lifecycle_id": lifecycle_id, "customer_uuid": customer_uuid, "forwarder": forwarder, "profile_key": profile_key,
        "amazon_account_email": normalize_email(field(record, "amazon_account_email", "Amazon Account Email")),
        "submission_id": submission_id, "amazon_order_id": order_id,
        "current_status": field(record, "current_status", "Current Status"), "requested_tax_amount": field(record, "requested_tax_amount", "Requested Tax Amount"),
        "refund_amount_received": field(record, "refund_amount_received", "Refund Amount Received"), "refund_received_date": field(record, "refund_received_date", "Refund Received Date"),
        "refund_method": field(record, "refund_method", "Refund Method"), "action_needed": field(record, "action_needed", "Action Needed"),
        "status_detail": field(record, "status_detail", "Status Detail"), "tracker_payload": json_text(record),
        "source_run_id": field(record, "source_run_id") or source_run_id,
    }


def upsert_customer_refund_lifecycle(customer_uuid, records, forwarder="", source_run_id=""):
    init_db()
    if not isinstance(records, list):
        raise ValueError("records must be a list.")
    rows = [normalize_refund_lifecycle_record(r, customer_uuid, forwarder, source_run_id) for r in records]
    rows = [r for r in rows if r]
    if not rows:
        return 0
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for row in rows:
            values = (row["lifecycle_id"], row["customer_uuid"], row["forwarder"], row["profile_key"], row["amazon_account_email"], row["submission_id"], row["amazon_order_id"],
                      row["current_status"], row["requested_tax_amount"], row["refund_amount_received"], row["refund_received_date"], row["refund_method"], row["action_needed"],
                      row["status_detail"], row["tracker_payload"], row["source_run_id"], utc_now_iso(), utc_now_iso())
            if postgres_available():
                cur.execute(
                    """
                    INSERT INTO customer_refund_lifecycle (lifecycle_id,customer_uuid,forwarder,profile_key,amazon_account_email,submission_id,amazon_order_id,current_status,requested_tax_amount,refund_amount_received,refund_received_date,refund_method,action_needed,status_detail,tracker_payload,source_run_id,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (customer_uuid, forwarder, profile_key, submission_id, amazon_order_id) DO UPDATE SET
                      current_status=COALESCE(NULLIF(EXCLUDED.current_status,''), customer_refund_lifecycle.current_status), requested_tax_amount=COALESCE(NULLIF(EXCLUDED.requested_tax_amount,''), customer_refund_lifecycle.requested_tax_amount),
                      refund_amount_received=COALESCE(NULLIF(EXCLUDED.refund_amount_received,''), customer_refund_lifecycle.refund_amount_received), refund_received_date=COALESCE(NULLIF(EXCLUDED.refund_received_date,''), customer_refund_lifecycle.refund_received_date),
                      refund_method=COALESCE(NULLIF(EXCLUDED.refund_method,''), customer_refund_lifecycle.refund_method), action_needed=COALESCE(NULLIF(EXCLUDED.action_needed,''), customer_refund_lifecycle.action_needed),
                      status_detail=COALESCE(NULLIF(EXCLUDED.status_detail,''), customer_refund_lifecycle.status_detail), tracker_payload=EXCLUDED.tracker_payload,
                      source_run_id=COALESCE(NULLIF(EXCLUDED.source_run_id,''), customer_refund_lifecycle.source_run_id), updated_at=EXCLUDED.updated_at
                    """, values)
            else:
                cur.execute(
                    """
                    INSERT INTO customer_refund_lifecycle (lifecycle_id,customer_uuid,forwarder,profile_key,amazon_account_email,submission_id,amazon_order_id,current_status,requested_tax_amount,refund_amount_received,refund_received_date,refund_method,action_needed,status_detail,tracker_payload,source_run_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(customer_uuid, forwarder, profile_key, submission_id, amazon_order_id) DO UPDATE SET
                      current_status=CASE WHEN excluded.current_status!='' THEN excluded.current_status ELSE current_status END, requested_tax_amount=CASE WHEN excluded.requested_tax_amount!='' THEN excluded.requested_tax_amount ELSE requested_tax_amount END,
                      refund_amount_received=CASE WHEN excluded.refund_amount_received!='' THEN excluded.refund_amount_received ELSE refund_amount_received END, refund_received_date=CASE WHEN excluded.refund_received_date!='' THEN excluded.refund_received_date ELSE refund_received_date END,
                      refund_method=CASE WHEN excluded.refund_method!='' THEN excluded.refund_method ELSE refund_method END, action_needed=CASE WHEN excluded.action_needed!='' THEN excluded.action_needed ELSE action_needed END,
                      status_detail=CASE WHEN excluded.status_detail!='' THEN excluded.status_detail ELSE status_detail END, tracker_payload=excluded.tracker_payload,
                      source_run_id=CASE WHEN excluded.source_run_id!='' THEN excluded.source_run_id ELSE source_run_id END, updated_at=excluded.updated_at
                    """, values)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def get_customer_refund_lifecycle(customer_uuid, forwarder="", profile_key="", amazon_account_email=""):
    init_db()
    clauses = [f"customer_uuid = {param()}"]
    args = [clean_text(customer_uuid)]
    if forwarder:
        clauses.append(f"LOWER(forwarder) = {param()}")
        args.append(clean_text(forwarder).lower())
    if profile_key:
        clauses.append(f"profile_key = {param()}")
        args.append(clean_text(profile_key))
    if amazon_account_email:
        clauses.append(f"LOWER(amazon_account_email) = {param()}")
        args.append(normalize_email(amazon_account_email))
    return execute_all("SELECT * FROM customer_refund_lifecycle WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC", args)


MAXTRACKS_REAL_REFERENCE_RE = re.compile(r"\b(\d{3,8})-(20\d{2})-MAXTRACKS\b", re.IGNORECASE)
GENERATED_MAXTRACKS_REFERENCE_RE = re.compile(r"\bA(\d{4,8})-(20\d{2})-MAXTRACKS\b", re.IGNORECASE)


def normalize_tracking_id(value):
    text = str(value or "").strip().upper()
    if text.startswith('="') and text.endswith('"'):
        text = text[2:-1]
    elif text.startswith("='") and text.endswith("'"):
        text = text[2:-1]
    return text.strip().strip("'").strip()


def normalize_export_year(value):
    match = re.search(r"\b(20\d{2}|19\d{2})\b", str(value or ""))
    return match.group(1) if match else str(utc_now().year)


def normalize_real_maxtracks_reference(value):
    match = MAXTRACKS_REAL_REFERENCE_RE.search(str(value or "").strip().upper().replace(" ", ""))
    return f"{match.group(1)}-{match.group(2)}-MAXTRACKS" if match else ""


def format_generated_maxtracks_reference(sequence_number, export_year):
    return f"A{int(sequence_number):04d}-{normalize_export_year(export_year)}-MAXTRACKS"


def export_reference_id(customer_uuid, forwarder, profile_key, export_year, amazon_tracking_number):
    raw = "|".join([clean_text(customer_uuid), clean_text(forwarder).lower(), clean_text(profile_key), clean_text(export_year), normalize_tracking_id(amazon_tracking_number)])
    return "export_ref_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def resolve_customer_export_references(customer_uuid, records, amazon_account_email="", forwarder="max_shipping", profile_key="", max_shipping_account_number="", source_run_id=""):
    init_db()
    forwarder = clean_text(forwarder or "max_shipping").lower()
    profile_key = clean_text(profile_key)
    customer_uuid = clean_text(customer_uuid)
    normalized = []
    seen = set()
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        tracking = normalize_tracking_id(record.get("amazon_tracking_number") or record.get("tracking_number") or record.get("Amazon Tracking Number"))
        if not tracking:
            continue
        year = normalize_export_year(record.get("export_year") or record.get("Export Year"))
        real = normalize_real_maxtracks_reference(record.get("real_export_reference") or record.get("maxtracks_reference") or record.get("export_reference") or record.get("Export Reference"))
        key = (forwarder, profile_key, year, tracking)
        if key in seen:
            continue
        seen.add(key)
        normalized.append((tracking, year, real, record))
    if not normalized:
        return []
    conn = get_db_connection()
    results = []
    try:
        cur = conn.cursor()
        ph = param()
        for tracking, year, real, record in normalized:
            cur.execute(f"SELECT export_reference, reference_source FROM customer_export_references WHERE customer_uuid={ph} AND forwarder={ph} AND profile_key={ph} AND export_year={ph} AND amazon_tracking_number={ph} LIMIT 1", (customer_uuid, forwarder, profile_key, year, tracking))
            existing = cur.fetchone()
            reused = False
            seq = None
            if real:
                ref = real
                source = "real_maxtracks_reference"
            elif existing:
                ref = scalar(existing, "")
                source = existing[1] if not isinstance(existing, dict) else existing.get("reference_source", "")
                source = source or "generated_fallback_reference"
                reused = True
            else:
                if postgres_available():
                    cur.execute("INSERT INTO customer_export_reference_sequences (customer_uuid,forwarder,profile_key,export_year,last_sequence_number,updated_at) VALUES (%s,%s,%s,%s,0,%s) ON CONFLICT (customer_uuid, forwarder, profile_key, export_year) DO NOTHING", (customer_uuid, forwarder, profile_key, year, utc_now_iso()))
                    cur.execute("SELECT last_sequence_number FROM customer_export_reference_sequences WHERE customer_uuid=%s AND forwarder=%s AND profile_key=%s AND export_year=%s FOR UPDATE", (customer_uuid, forwarder, profile_key, year))
                else:
                    cur.execute("INSERT OR IGNORE INTO customer_export_reference_sequences (customer_uuid,forwarder,profile_key,export_year,last_sequence_number,updated_at) VALUES (?,?,?,?,0,?)", (customer_uuid, forwarder, profile_key, year, utc_now_iso()))
                    cur.execute("SELECT last_sequence_number FROM customer_export_reference_sequences WHERE customer_uuid=? AND forwarder=? AND profile_key=? AND export_year=?", (customer_uuid, forwarder, profile_key, year))
                seq = int(scalar(cur.fetchone(), 0) or 0) + 1
                cur.execute(f"UPDATE customer_export_reference_sequences SET last_sequence_number={ph}, updated_at={ph} WHERE customer_uuid={ph} AND forwarder={ph} AND profile_key={ph} AND export_year={ph}", (seq, utc_now_iso(), customer_uuid, forwarder, profile_key, year))
                ref = format_generated_maxtracks_reference(seq, year)
                source = "generated_fallback_reference"
            values = (export_reference_id(customer_uuid, forwarder, profile_key, year, tracking), customer_uuid, forwarder, profile_key, normalize_email(amazon_account_email), clean_text(max_shipping_account_number), year, tracking, ref, source, clean_text(source_run_id), json_text(record), utc_now_iso(), utc_now_iso())
            if postgres_available():
                cur.execute("""
                    INSERT INTO customer_export_references (reference_id,customer_uuid,forwarder,profile_key,amazon_account_email,max_shipping_account_number,export_year,amazon_tracking_number,export_reference,reference_source,source_run_id,raw_payload,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (customer_uuid, forwarder, profile_key, export_year, amazon_tracking_number) DO UPDATE SET
                      amazon_account_email=COALESCE(NULLIF(EXCLUDED.amazon_account_email,''), customer_export_references.amazon_account_email),
                      max_shipping_account_number=COALESCE(NULLIF(EXCLUDED.max_shipping_account_number,''), customer_export_references.max_shipping_account_number),
                      export_reference=COALESCE(NULLIF(EXCLUDED.export_reference,''), customer_export_references.export_reference), reference_source=COALESCE(NULLIF(EXCLUDED.reference_source,''), customer_export_references.reference_source),
                      source_run_id=COALESCE(NULLIF(EXCLUDED.source_run_id,''), customer_export_references.source_run_id), raw_payload=EXCLUDED.raw_payload, updated_at=EXCLUDED.updated_at
                """, values)
            else:
                cur.execute("""
                    INSERT INTO customer_export_references (reference_id,customer_uuid,forwarder,profile_key,amazon_account_email,max_shipping_account_number,export_year,amazon_tracking_number,export_reference,reference_source,source_run_id,raw_payload,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(customer_uuid, forwarder, profile_key, export_year, amazon_tracking_number) DO UPDATE SET
                      amazon_account_email=CASE WHEN excluded.amazon_account_email!='' THEN excluded.amazon_account_email ELSE amazon_account_email END,
                      max_shipping_account_number=CASE WHEN excluded.max_shipping_account_number!='' THEN excluded.max_shipping_account_number ELSE max_shipping_account_number END,
                      export_reference=CASE WHEN excluded.export_reference!='' THEN excluded.export_reference ELSE export_reference END, reference_source=CASE WHEN excluded.reference_source!='' THEN excluded.reference_source ELSE reference_source END,
                      source_run_id=CASE WHEN excluded.source_run_id!='' THEN excluded.source_run_id ELSE source_run_id END, raw_payload=excluded.raw_payload, updated_at=excluded.updated_at
                """, values)
            results.append({"amazon_tracking_number": tracking, "export_year": year, "export_reference": ref, "reference_source": source, "reused_existing": reused, "sequence_number": seq})
        conn.commit()
        return results
    finally:
        conn.close()


def normalize_refunds_received_record(record, customer_uuid):
    record = record if isinstance(record, dict) else {}
    order_id = field(record, "amazon_order_id", "Amazon Order ID", "order_id")
    if not order_id:
        return None
    amount = field(record, "refund_amount_received", "Refund Amount Received", "refund_amount")
    source_hash = field(record, "source_hash", "Source Hash")
    email = normalize_email(field(record, "amazon_account_email", "Amazon Account Email"))
    rid = field(record, "refund_id", "Refund ID") or "refund_" + hashlib.sha256("|".join([customer_uuid, email, order_id, amount, source_hash]).encode("utf-8")).hexdigest()[:32]
    return {
        "refund_id": rid, "customer_uuid": customer_uuid, "amazon_account_email": email, "amazon_order_id": order_id,
        "refund_amount_received": amount, "refund_method": field(record, "refund_method", "Refund Method"),
        "refund_received_date": field(record, "refund_received_date", "Refund Received Date"), "email_date": field(record, "email_date", "Email Date"),
        "email_from": field(record, "email_from", "Email From", "from"), "email_to": field(record, "email_to", "Email To", "to"),
        "email_subject": field(record, "email_subject", "Email Subject", "subject"), "parsed_status": field(record, "parsed_status", "Parsed Status"),
        "source": field(record, "source", "Source"), "source_hash": source_hash, "notes": field(record, "notes", "Notes"),
        "text_preview": field(record, "text_preview", "Text Preview"), "raw_payload": json_text(record)
    }


def upsert_customer_refunds_received(customer_uuid, records):
    init_db()
    rows = [normalize_refunds_received_record(r, customer_uuid) for r in (records if isinstance(records, list) else [])]
    rows = [r for r in rows if r]
    if not rows:
        return 0
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for r in rows:
            values = (r["refund_id"], r["customer_uuid"], r["amazon_account_email"], r["amazon_order_id"], r["refund_amount_received"], r["refund_method"], r["refund_received_date"], r["email_date"], r["email_from"], r["email_to"], r["email_subject"], r["parsed_status"], r["source"], r["source_hash"], r["notes"], r["text_preview"], r["raw_payload"], utc_now_iso(), utc_now_iso())
            if postgres_available():
                cur.execute("""
                    INSERT INTO customer_refunds_received (refund_id,customer_uuid,amazon_account_email,amazon_order_id,refund_amount_received,refund_method,refund_received_date,email_date,email_from,email_to,email_subject,parsed_status,source,source_hash,notes,text_preview,raw_payload,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash) DO UPDATE SET raw_payload=EXCLUDED.raw_payload, updated_at=EXCLUDED.updated_at
                """, values)
            else:
                cur.execute("""
                    INSERT INTO customer_refunds_received (refund_id,customer_uuid,amazon_account_email,amazon_order_id,refund_amount_received,refund_method,refund_received_date,email_date,email_from,email_to,email_subject,parsed_status,source,source_hash,notes,text_preview,raw_payload,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash) DO UPDATE SET raw_payload=excluded.raw_payload, updated_at=excluded.updated_at
                """, values)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def get_customer_refunds_received(customer_uuid, amazon_account_email=""):
    init_db()
    clauses = [f"customer_uuid = {param()}"]
    args = [clean_text(customer_uuid)]
    if amazon_account_email:
        clauses.append(f"LOWER(amazon_account_email) = {param()}")
        args.append(normalize_email(amazon_account_email))
    return execute_all("SELECT * FROM customer_refunds_received WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC", args)


def normalize_identity_part(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def make_claimant_profile_key(customer_uuid, amazon_account_email, max_shipping_account_number, amazon_ship_to_address):
    raw = "|".join([clean_text(customer_uuid), normalize_email(amazon_account_email), normalize_identity_part(max_shipping_account_number), normalize_identity_part(amazon_ship_to_address)])
    return "cp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalize_claimant_profile_payload(payload, customer_uuid):
    amazon_email = normalize_email(payload.get("amazon_account_email", "") or payload.get("amazon_email", ""))
    claimant = clean_text(payload.get("claimant_name", "") or payload.get("customer_name", "") or payload.get("display_name", ""), 200)
    max_email = normalize_email(payload.get("max_shipping_email", "") or payload.get("forwarder_login_email", ""))
    max_acct = clean_text(payload.get("max_shipping_account_number", "") or payload.get("max_account_number", ""), 120)
    ship_to = clean_text(payload.get("amazon_ship_to_address", "") or payload.get("ship_to_address", ""), 1000)
    missing = []
    if not amazon_email or "@" not in amazon_email: missing.append("amazon_account_email")
    if not claimant: missing.append("claimant_name")
    if not max_email or "@" not in max_email: missing.append("max_shipping_email")
    if not max_acct: missing.append("max_shipping_account_number")
    if not ship_to: missing.append("amazon_ship_to_address")
    if missing:
        raise ValueError("Missing or invalid claimant profile field(s): " + ", ".join(missing))
    profile_key = clean_text(payload.get("profile_key", ""), 80) or make_claimant_profile_key(customer_uuid, amazon_email, max_acct, ship_to)
    return {"profile_key": profile_key, "customer_uuid": customer_uuid, "amazon_account_email": amazon_email, "claimant_name": claimant,
            "signature_line": clean_text(payload.get("signature_line", ""), 300), "max_shipping_email": max_email, "max_shipping_account_number": max_acct,
            "amazon_ship_to_address": ship_to, "export_destination": clean_text(payload.get("export_destination", ""), 300),
            "preferred_contact_email": normalize_email(payload.get("preferred_contact_email", "") or payload.get("email", "")),
            "claimant_label": clean_text(payload.get("claimant_label", "") or payload.get("profile_label", ""), 200),
            "app_name": clean_text(payload.get("app_name", ""), 120), "app_version": clean_text(payload.get("app_version", ""), 120),
            "source": clean_text(payload.get("source", "desktop"), 120), "raw_payload": json_text(payload)}


def upsert_claimant_profile(profile):
    init_db()
    values = (profile["profile_key"], profile["customer_uuid"], profile["amazon_account_email"], profile["claimant_name"], profile["signature_line"], profile["max_shipping_email"], profile["max_shipping_account_number"], profile["amazon_ship_to_address"], profile["export_destination"], profile["preferred_contact_email"], profile["claimant_label"], profile["app_name"], profile["app_version"], profile["source"], profile["raw_payload"], utc_now_iso(), utc_now_iso())
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if postgres_available() else conn.cursor()
        if postgres_available():
            cur.execute("""
                INSERT INTO customer_claimant_profiles (profile_key,customer_uuid,amazon_account_email,claimant_name,signature_line,max_shipping_email,max_shipping_account_number,amazon_ship_to_address,export_destination,preferred_contact_email,claimant_label,app_name,app_version,source,raw_payload,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (profile_key) DO UPDATE SET customer_uuid=EXCLUDED.customer_uuid, amazon_account_email=EXCLUDED.amazon_account_email, claimant_name=EXCLUDED.claimant_name,
                  signature_line=EXCLUDED.signature_line, max_shipping_email=EXCLUDED.max_shipping_email, max_shipping_account_number=EXCLUDED.max_shipping_account_number,
                  amazon_ship_to_address=EXCLUDED.amazon_ship_to_address, export_destination=EXCLUDED.export_destination, preferred_contact_email=EXCLUDED.preferred_contact_email,
                  claimant_label=EXCLUDED.claimant_label, app_name=EXCLUDED.app_name, app_version=EXCLUDED.app_version, source=EXCLUDED.source, raw_payload=EXCLUDED.raw_payload, updated_at=EXCLUDED.updated_at
                RETURNING *
            """, values)
            saved = dict(cur.fetchone())
        else:
            cur.execute("""
                INSERT INTO customer_claimant_profiles (profile_key,customer_uuid,amazon_account_email,claimant_name,signature_line,max_shipping_email,max_shipping_account_number,amazon_ship_to_address,export_destination,preferred_contact_email,claimant_label,app_name,app_version,source,raw_payload,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_key) DO UPDATE SET customer_uuid=excluded.customer_uuid, amazon_account_email=excluded.amazon_account_email, claimant_name=excluded.claimant_name,
                  signature_line=excluded.signature_line, max_shipping_email=excluded.max_shipping_email, max_shipping_account_number=excluded.max_shipping_account_number,
                  amazon_ship_to_address=excluded.amazon_ship_to_address, export_destination=excluded.export_destination, preferred_contact_email=excluded.preferred_contact_email,
                  claimant_label=excluded.claimant_label, app_name=excluded.app_name, app_version=excluded.app_version, source=excluded.source, raw_payload=excluded.raw_payload, updated_at=excluded.updated_at
            """, values)
            cur.execute("SELECT * FROM customer_claimant_profiles WHERE profile_key=?", (profile["profile_key"],))
            saved = row_to_dict(cur, cur.fetchone())
        cur.execute(f"INSERT INTO customer_claimant_profile_events (event_id,profile_key,event_type,raw_payload,created_at) VALUES ({params(5)})", (str(uuid.uuid4()), profile["profile_key"], "profile_upsert", profile["raw_payload"], utc_now_iso()))
        conn.commit()
        return saved
    finally:
        conn.close()


def get_claimant_profiles(customer_uuid, amazon_account_email="", max_shipping_account_number=""):
    init_db()
    clauses = [f"customer_uuid = {param()}"]
    args = [clean_text(customer_uuid)]
    if amazon_account_email:
        clauses.append(f"LOWER(amazon_account_email) = {param()}")
        args.append(normalize_email(amazon_account_email))
    if max_shipping_account_number:
        clauses.append(f"max_shipping_account_number = {param()}")
        args.append(clean_text(max_shipping_account_number, 120))
    return execute_all("SELECT * FROM customer_claimant_profiles WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC", args)


def upsert_report_statistics(payload):
    init_db()
    report_id = clean_text(payload.get("report_id", ""))
    if not report_id:
        raise ValueError("report_id is required.")
    values = (report_id, normalize_code(payload.get("founder_code", "")), normalize_email(payload.get("email", "")), clean_text(payload.get("app_name", "")), clean_text(payload.get("app_version", "")), clean_text(payload.get("scan_mode", "")), clean_text(payload.get("generated_at") or utc_now_iso()), int(payload.get("orders_found", 0) or 0), int(payload.get("tracking_found", 0) or 0), clean_text(payload.get("tax_identified") or payload.get("recovery_total", "")), safe_bool(payload.get("paid_scan", False)), clean_text(payload.get("payment_session_id", "")), safe_bool(payload.get("generated_package", True)), safe_bool(payload.get("proof_consent", True)), clean_text(payload.get("notes", "")), json_text(payload))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if postgres_available():
            cur.execute("""
                INSERT INTO report_statistics (report_id,founder_code,email,app_name,app_version,scan_mode,generated_at,orders_found,tracking_found,tax_identified,paid_scan,payment_session_id,generated_package,proof_consent,notes,raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (report_id) DO UPDATE SET founder_code=COALESCE(NULLIF(EXCLUDED.founder_code,''), report_statistics.founder_code), email=COALESCE(NULLIF(EXCLUDED.email,''), report_statistics.email), app_name=COALESCE(NULLIF(EXCLUDED.app_name,''), report_statistics.app_name), app_version=COALESCE(NULLIF(EXCLUDED.app_version,''), report_statistics.app_version), scan_mode=COALESCE(NULLIF(EXCLUDED.scan_mode,''), report_statistics.scan_mode), generated_at=EXCLUDED.generated_at, orders_found=EXCLUDED.orders_found, tracking_found=EXCLUDED.tracking_found, tax_identified=COALESCE(NULLIF(EXCLUDED.tax_identified,''), report_statistics.tax_identified), paid_scan=EXCLUDED.paid_scan, payment_session_id=COALESCE(NULLIF(EXCLUDED.payment_session_id,''), report_statistics.payment_session_id), generated_package=EXCLUDED.generated_package, proof_consent=EXCLUDED.proof_consent, notes=COALESCE(NULLIF(EXCLUDED.notes,''), report_statistics.notes), raw_payload=EXCLUDED.raw_payload
            """, values)
            cur.execute("INSERT INTO refund_outcomes (report_id,founder_code,email,status,created_at,updated_at) VALUES (%s,%s,%s,'Not Submitted',%s,%s) ON CONFLICT (report_id) DO NOTHING", (report_id, values[1], values[2], utc_now_iso(), utc_now_iso()))
        else:
            cur.execute("""
                INSERT INTO report_statistics (report_id,founder_code,email,app_name,app_version,scan_mode,generated_at,orders_found,tracking_found,tax_identified,paid_scan,payment_session_id,generated_package,proof_consent,notes,raw_payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(report_id) DO UPDATE SET founder_code=CASE WHEN excluded.founder_code!='' THEN excluded.founder_code ELSE founder_code END, email=CASE WHEN excluded.email!='' THEN excluded.email ELSE email END, app_name=CASE WHEN excluded.app_name!='' THEN excluded.app_name ELSE app_name END, app_version=CASE WHEN excluded.app_version!='' THEN excluded.app_version ELSE app_version END, scan_mode=CASE WHEN excluded.scan_mode!='' THEN excluded.scan_mode ELSE scan_mode END, generated_at=excluded.generated_at, orders_found=excluded.orders_found, tracking_found=excluded.tracking_found, tax_identified=CASE WHEN excluded.tax_identified!='' THEN excluded.tax_identified ELSE tax_identified END, paid_scan=excluded.paid_scan, payment_session_id=CASE WHEN excluded.payment_session_id!='' THEN excluded.payment_session_id ELSE payment_session_id END, generated_package=excluded.generated_package, proof_consent=excluded.proof_consent, notes=CASE WHEN excluded.notes!='' THEN excluded.notes ELSE notes END, raw_payload=excluded.raw_payload
            """, values)
            cur.execute("INSERT OR IGNORE INTO refund_outcomes (report_id,founder_code,email,status,created_at,updated_at) VALUES (?,?,?,'Not Submitted',?,?)", (report_id, values[1], values[2], utc_now_iso(), utc_now_iso()))
        conn.commit()
    finally:
        conn.close()


def refund_tracker_email_from_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    email = normalize_email(payload.get("amazon_refund_email", "") or payload.get("amazon_account_email", "") or payload.get("amazon_email", "") or payload.get("email", ""))
    return email if email and "@" in email else ""


def resolve_free_refund_tracker_customer(payload):
    email = refund_tracker_email_from_payload(payload)
    if not email:
        raise PermissionError("Amazon refund email / Amazon account email is required.")
    return get_or_create_customer(email, payload.get("display_name") or payload.get("customer_name") or payload.get("claimant_name") or "", "free_refund_tracker"), email


def enrich_refund_tracker_records_with_email(records, amazon_refund_email):
    out = []
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict):
            row = dict(record)
            if not normalize_email(row.get("amazon_account_email") or row.get("Amazon Account Email")):
                row["amazon_account_email"] = amazon_refund_email
            out.append(row)
    return out


@app.route("/", methods=["GET"])
def index():
    try:
        init_db()
        db_ready = True
    except Exception:
        db_ready = False
    return jsonify({
        "service": "Amazon MaxShipping payment server",
        "version": APP_VERSION,
        "stripe_configured": bool(STRIPE_SECRET_KEY),
        "database_configured": bool(DATABASE_URL),
        "sqlite_fallback_allowed": sqlite_fallback_allowed(),
        "database_ready": db_ready,
        "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET),
        "founder_admin_token_configured": bool(FOUNDER_ADMIN_TOKEN),
        "cloud_customer_history_enabled": True,
        "cloud_refund_lifecycle_enabled": True,
        "cloud_refunds_received_enabled": True,
        "cloud_export_references_enabled": True,
        "admin_max_only_reset_enabled": True,
    })


@app.route("/health", methods=["GET"])
def health():
    return index()


@app.route("/payment-success", methods=["GET"])
def payment_success():
    return "Payment completed. Return to the Amazon-MaxShipping Tracker app and click OK to verify payment.", 200


@app.route("/payment-cancelled", methods=["GET"])
def payment_cancelled():
    return "Payment cancelled. Return to the Amazon-MaxShipping Tracker app if you want to try again.", 200


@app.route("/admin/create-founder-code", methods=["POST"])
def admin_create_founder_code():
    ok, error = verify_admin_request()
    if not ok:
        return jsonify({"created": False, "error": error, "version": APP_VERSION}), 401
    payload, error = require_json_payload()
    if error:
        return jsonify({"created": False, "error": error, "version": APP_VERSION}), 400
    founder_code = normalize_code(payload.get("founder_code", ""))
    email = normalize_email(payload.get("email", ""))
    if not founder_code:
        return jsonify({"created": False, "error": "founder_code is required.", "version": APP_VERSION}), 400
    if not email or "@" not in email:
        return jsonify({"created": False, "error": "Valid email is required.", "version": APP_VERSION}), 400
    expires_at = clean_text(payload.get("expires_at", "")) or (utc_now() + timedelta(days=int(payload.get("days_valid", 365) or 365))).isoformat()
    try:
        create_or_update_founder_code(founder_code, email, expires_at, clean_text(payload.get("notes", "")), safe_bool(payload.get("active", True)), clean_text(payload.get("membership_type", "Founding Member")) or "Founding Member")
        return jsonify({"created": True, "founder_code": founder_code, "email": email, "expires_at": expires_at, "active": safe_bool(payload.get("active", True)), "membership_type": clean_text(payload.get("membership_type", "Founding Member")) or "Founding Member", "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"created": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/validate-founder-code", methods=["POST"])
def validate_founder_code():
    payload, error = require_json_payload()
    if error:
        return jsonify({"valid": False, "error": error, "version": APP_VERSION}), 400
    founder_code = normalize_code(payload.get("founder_code", "") or payload.get("code", ""))
    email = normalize_email(payload.get("email", "") or payload.get("max_login", ""))
    if not founder_code:
        return jsonify({"valid": False, "error": "founder_code is required.", "version": APP_VERSION}), 400
    if not email:
        return jsonify({"valid": False, "error": "email is required.", "version": APP_VERSION}), 400
    row = get_founder_member(founder_code, email)
    valid, reason = founder_row_is_active(row)
    if not valid:
        return jsonify({"valid": False, "reason": reason, "founder_code": founder_code, "email": email, "version": APP_VERSION})
    mark_founder_validated(founder_code)
    return jsonify({"valid": True, "founder_code": founder_code, "email": normalize_email(row.get("email", email)), "expires_at": str(row.get("expires_at", "") or ""), "membership_type": row.get("membership_type", "Founding Member"), "version": APP_VERSION})


@app.route("/record-report-statistics", methods=["POST"])
def record_report_statistics():
    payload, error = require_json_payload()
    if error:
        return jsonify({"recorded": False, "error": error, "version": APP_VERSION}), 400
    founder_code = normalize_code(payload.get("founder_code", ""))
    email = normalize_email(payload.get("email", ""))
    if founder_code or email:
        valid, reason = founder_row_is_active(get_founder_member(founder_code, email))
        if not valid:
            return jsonify({"recorded": False, "error": reason, "version": APP_VERSION}), 403
    try:
        upsert_report_statistics(payload)
        return jsonify({"recorded": True, "report_id": clean_text(payload.get("report_id", "")), "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"recorded": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/resolve", methods=["POST"])
def customer_resolve():
    payload, error = require_json_payload()
    if error:
        return jsonify({"resolved": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"resolved": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = get_or_create_customer(email, payload.get("display_name") or payload.get("customer_name") or "", payload.get("source", "desktop"))
        return jsonify({"resolved": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "display_name": customer.get("display_name", ""), "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"resolved": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/history", methods=["POST"])
def customer_history():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        forwarder = clean_text(payload.get("forwarder", "")).lower()
        history, state = get_customer_history(customer.get("customer_uuid", ""), forwarder)
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "forwarder": forwarder, "history": history, "forwarder_state": state, "history_count": len(history), "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/history/sync", methods=["POST"])
def customer_history_sync():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        forwarder = clean_text(payload.get("forwarder", "")).lower()
        run_id = clean_text(payload.get("run_id", ""))
        synced = upsert_customer_history(customer.get("customer_uuid", ""), payload.get("history", payload.get("records", [])), forwarder, run_id)
        if isinstance(payload.get("forwarder_state"), dict) and forwarder:
            upsert_customer_forwarder_state(customer.get("customer_uuid", ""), forwarder, payload.get("forwarder_state"))
        saved_run_id = ""
        if isinstance(payload.get("run"), dict) and payload.get("run"):
            run_payload = dict(payload["run"])
            run_payload.setdefault("forwarder", forwarder)
            run_payload.setdefault("run_id", run_id or make_customer_uuid())
            saved_run_id = record_customer_run(run_payload, customer.get("customer_uuid", ""))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "forwarder": forwarder, "synced_count": synced, "run_id": saved_run_id, "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/export-references/resolve", methods=["POST"])
def free_export_references_resolve():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    try:
        customer, email = resolve_free_refund_tracker_customer(payload)
        resolved = resolve_customer_export_references(customer.get("customer_uuid", ""), payload.get("records", []), email, payload.get("forwarder", "max_shipping") or "max_shipping", payload.get("profile_key", ""), payload.get("max_shipping_account_number", "") or payload.get("max_account_number", ""), payload.get("run_id", "") or payload.get("source_run_id", ""))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "amazon_refund_email": email, "forwarder": clean_text(payload.get("forwarder", "max_shipping") or "max_shipping").lower(), "resolved_references": resolved, "resolved_count": len(resolved), "access_model": "free_refund_tracker_amazon_email", "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refund-tracker/lifecycle", methods=["POST"])
def free_refund_tracker_lifecycle_list():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    try:
        customer, email = resolve_free_refund_tracker_customer(payload)
        rows = get_customer_refund_lifecycle(customer.get("customer_uuid", ""), payload.get("forwarder", ""), payload.get("profile_key", ""), email)
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "amazon_refund_email": email, "lifecycle": rows, "lifecycle_count": len(rows), "access_model": "free_refund_tracker_amazon_email", "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refund-tracker/lifecycle/sync", methods=["POST"])
def free_refund_tracker_lifecycle_sync():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    try:
        customer, email = resolve_free_refund_tracker_customer(payload)
        forwarder = clean_text(payload.get("forwarder", "")).lower()
        records = enrich_refund_tracker_records_with_email(payload.get("lifecycle", payload.get("records", [])), email)
        synced = upsert_customer_refund_lifecycle(customer.get("customer_uuid", ""), records, forwarder, clean_text(payload.get("run_id", "")))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "amazon_refund_email": email, "forwarder": forwarder, "synced_count": synced, "access_model": "free_refund_tracker_amazon_email", "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refund-tracker/refunds-received", methods=["POST"])
def free_refund_tracker_refunds_received_list():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    try:
        customer, email = resolve_free_refund_tracker_customer(payload)
        rows = get_customer_refunds_received(customer.get("customer_uuid", ""), email)
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "amazon_refund_email": email, "refunds_received": rows, "refund_count": len(rows), "access_model": "free_refund_tracker_amazon_email", "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refund-tracker/refunds-received/sync", methods=["POST"])
def free_refund_tracker_refunds_received_sync():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    try:
        customer, email = resolve_free_refund_tracker_customer(payload)
        records = enrich_refund_tracker_records_with_email(payload.get("refunds_received", payload.get("records", [])), email)
        synced = upsert_customer_refunds_received(customer.get("customer_uuid", ""), records)
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "amazon_refund_email": email, "synced_count": synced, "access_model": "free_refund_tracker_amazon_email", "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refund-lifecycle", methods=["POST"])
def customer_refund_lifecycle_list():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        rows = get_customer_refund_lifecycle(customer.get("customer_uuid", ""), payload.get("forwarder", ""), payload.get("profile_key", ""), payload.get("amazon_account_email", "") or payload.get("amazon_email", ""))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "lifecycle": rows, "lifecycle_count": len(rows), "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refund-lifecycle/sync", methods=["POST"])
def customer_refund_lifecycle_sync():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        synced = upsert_customer_refund_lifecycle(customer.get("customer_uuid", ""), payload.get("lifecycle", payload.get("records", [])), clean_text(payload.get("forwarder", "")).lower(), clean_text(payload.get("run_id", "")))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "forwarder": clean_text(payload.get("forwarder", "")).lower(), "synced_count": synced, "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refunds-received", methods=["POST"])
def customer_refunds_received_list():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        rows = get_customer_refunds_received(customer.get("customer_uuid", ""), payload.get("amazon_account_email", "") or payload.get("amazon_email", ""))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "refunds_received": rows, "refund_count": len(rows), "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/refunds-received/sync", methods=["POST"])
def customer_refunds_received_sync():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        synced = upsert_customer_refunds_received(customer.get("customer_uuid", ""), payload.get("refunds_received", payload.get("records", [])))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "synced_count": synced, "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/claimant-profile", methods=["POST"])
def customer_claimant_profile_upsert():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        saved = upsert_claimant_profile(normalize_claimant_profile_payload(payload, customer.get("customer_uuid", "")))
        profiles = get_claimant_profiles(customer.get("customer_uuid", ""))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "profile": saved, "profile_key": saved.get("profile_key", ""), "profiles": profiles, "profile_count": len(profiles), "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/customer/claimant-profiles", methods=["POST"])
def customer_claimant_profiles_list():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    ok, reason, email = validate_customer_access_payload(payload)
    if not ok:
        return jsonify({"ok": False, "error": reason, "version": APP_VERSION}), 403
    try:
        customer = resolve_authorized_customer(payload, email)
        profiles = get_claimant_profiles(customer.get("customer_uuid", ""), payload.get("amazon_account_email", "") or payload.get("amazon_email", ""), payload.get("max_shipping_account_number", "") or payload.get("max_account_number", ""))
        return jsonify({"ok": True, "customer_uuid": customer.get("customer_uuid", ""), "email": customer.get("email", email), "profiles": profiles, "profile_count": len(profiles), "version": APP_VERSION})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


MAX_ONLY_RESET_CONFIRM_PHRASE = "RESET MAX ONLY KEEP NAVIVAN"


def max_only_reset_specs(customer_uuid):
    ph = param()
    return [
        {"table": "customer_refund_lifecycle", "pk": "lifecycle_id", "where": f"customer_uuid = {ph} AND NOT (LOWER(COALESCE(forwarder,'')) = 'navivan' OR COALESCE(submission_id,'') LIKE 'NVR-%')", "args": [customer_uuid], "purpose": "Remove active non-Navivan lifecycle rows, including old Max submitted/follow-up tracker rows."},
        {"table": "customer_submission_history", "pk": "history_id", "where": f"customer_uuid = {ph} AND NOT (LOWER(COALESCE(forwarder,'')) = 'navivan' OR COALESCE(certification_id,'') LIKE 'NVR-%')", "args": [customer_uuid], "purpose": "Remove old Max cloud submission history while keeping Navivan history."},
        {"table": "customer_forwarder_state", "pk": "forwarder", "where": f"customer_uuid = {ph} AND LOWER(COALESCE(forwarder,'')) <> 'navivan'", "args": [customer_uuid], "purpose": "Clear non-Navivan forwarder state so Max starts fresh."},
        {"table": "customer_runs", "pk": "run_id", "where": f"customer_uuid = {ph} AND LOWER(COALESCE(forwarder,'')) <> 'navivan'", "args": [customer_uuid], "purpose": "Archive old non-Navivan run rows."},
        {"table": "customer_export_references", "pk": "reference_id", "where": f"customer_uuid = {ph} AND LOWER(COALESCE(forwarder,'')) IN ('max_shipping','maxshipping','max shipping','max')", "args": [customer_uuid], "purpose": "Clear old Max cloud export-reference mappings so invoice-first rebuild can assign clean evidence references."},
        {"table": "customer_export_reference_sequences", "pk": "forwarder", "where": f"customer_uuid = {ph} AND LOWER(COALESCE(forwarder,'')) IN ('max_shipping','maxshipping','max shipping','max')", "args": [customer_uuid], "purpose": "Clear old Max fallback-reference sequence state."},
    ]


@app.route("/admin/max-only-reset", methods=["POST"])
def admin_max_only_reset():
    ok, error = verify_admin_request()
    if not ok:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 401
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400
    amazon_refund_email = refund_tracker_email_from_payload(payload)
    if not amazon_refund_email:
        return jsonify({"ok": False, "error": "amazon_refund_email is required.", "version": APP_VERSION}), 400
    dry_run = safe_bool(payload.get("dry_run", True))
    if not dry_run and clean_text(payload.get("confirm_phrase", "")) != MAX_ONLY_RESET_CONFIRM_PHRASE:
        return jsonify({"ok": False, "error": "Exact confirm_phrase is required for a live reset.", "required_confirm_phrase": MAX_ONLY_RESET_CONFIRM_PHRASE, "version": APP_VERSION}), 400
    customer = execute_one(f"SELECT * FROM customers WHERE LOWER(email) = {param()} LIMIT 1", (amazon_refund_email,))
    if not customer:
        return jsonify({"ok": False, "error": "No existing cloud customer was found for that Amazon refund email. Nothing was reset.", "amazon_refund_email": amazon_refund_email, "version": APP_VERSION}), 404
    customer_uuid = clean_text(customer.get("customer_uuid", ""))
    reset_id = "max_only_reset_" + utc_now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    protected = ["customers", "customer_claimant_profiles", "customer_claimant_profile_events", "customer_refunds_received", "payments", "founding_members", "report_statistics", "refund_outcomes"]
    conn = get_db_connection()
    try:
        if not postgres_available():
            conn.row_factory = sqlite3.Row
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if postgres_available() else conn.cursor()
        try:
            specs = max_only_reset_specs(customer_uuid)
            cur.execute(f"SELECT COUNT(*) FROM customer_refund_lifecycle WHERE customer_uuid = {param()} AND (LOWER(COALESCE(forwarder,'')) = 'navivan' OR COALESCE(submission_id,'') LIKE 'NVR-%')", (customer_uuid,))
            preserved_navivan = int(scalar(cur.fetchone(), 0) or 0)
            counts = []
            for spec in specs:
                cur.execute(f"SELECT COUNT(*) FROM {spec['table']} WHERE {spec['where']}", tuple(spec["args"]))
                counts.append({"table": spec["table"], "matching_rows": int(scalar(cur.fetchone(), 0) or 0), "purpose": spec["purpose"]})
            if dry_run:
                conn.rollback()
                return jsonify({"ok": True, "dry_run": True, "would_reset": True, "reset_id": reset_id, "amazon_refund_email": amazon_refund_email, "customer_uuid": customer_uuid, "preserved_navivan_lifecycle_rows": preserved_navivan, "matching_counts": counts, "protected_tables_not_touched": protected, "required_confirm_phrase_for_live_reset": MAX_ONLY_RESET_CONFIRM_PHRASE, "version": APP_VERSION})
            archived_counts = []
            deleted_counts = []
            for spec in specs:
                cur.execute(f"SELECT * FROM {spec['table']} WHERE {spec['where']}", tuple(spec["args"]))
                rows = rows_to_dicts(cur, cur.fetchall())
                for row in rows:
                    payload_text = json_text(row)
                    archive_id = "archive_" + hashlib.sha256("|".join([reset_id, spec["table"], clean_text(row.get(spec["pk"], "")), payload_text]).encode("utf-8")).hexdigest()[:32]
                    cur.execute(f"INSERT INTO admin_cloud_reset_archive (archive_id,reset_id,reset_scope,amazon_refund_email,customer_uuid,table_name,row_primary_key,row_payload,created_at) VALUES ({params(9)}) ON CONFLICT (archive_id) DO NOTHING" if postgres_available() else "INSERT OR IGNORE INTO admin_cloud_reset_archive (archive_id,reset_id,reset_scope,amazon_refund_email,customer_uuid,table_name,row_primary_key,row_payload,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (archive_id, reset_id, "max_only_keep_navivan", amazon_refund_email, customer_uuid, spec["table"], clean_text(row.get(spec["pk"], "")), payload_text, utc_now_iso()))
                archived_counts.append({"table": spec["table"], "archived_rows": len(rows)})
                cur.execute(f"DELETE FROM {spec['table']} WHERE {spec['where']}", tuple(spec["args"]))
                deleted_counts.append({"table": spec["table"], "deleted_rows": int(cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0)})
            conn.commit()
            return jsonify({"ok": True, "dry_run": False, "reset_complete": True, "reset_id": reset_id, "amazon_refund_email": amazon_refund_email, "customer_uuid": customer_uuid, "preserved_navivan_lifecycle_rows": preserved_navivan, "matching_counts_before_reset": counts, "archived_counts": archived_counts, "deleted_counts": deleted_counts, "protected_tables_not_touched": protected, "archive_table": "admin_cloud_reset_archive", "version": APP_VERSION})
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500
    finally:
        conn.close()


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe secret key is not configured."}), 500
    payload, error = require_json_payload()
    if error:
        return jsonify({"error": error}), 400
    try:
        amount_cents = int(payload.get("amount_cents", 0) or 0)
    except Exception:
        amount_cents = 0
    if amount_cents <= 0:
        return jsonify({"error": "amount_cents must be greater than zero."}), 400
    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "usd", "product_data": {"name": "Amazon MaxShipping Recovery Report"}, "unit_amount": amount_cents}, "quantity": 1}],
            success_url=get_success_url(),
            cancel_url=get_cancel_url(),
            metadata={"report_id": clean_text(payload.get("report_id", "")), "recovery_total": clean_text(payload.get("recovery_total", "")), "app_name": clean_text(payload.get("app_name", "AMAZON-MAXSHIPPING TRACKER")), "app_version": clean_text(payload.get("app_version", ""))},
        )
        session_id = checkout_session["id"]
        url = checkout_session["url"]
        upsert_payment(session_id, payload.get("report_id", ""), amount_cents, payload.get("recovery_total", ""), payload.get("app_name", "AMAZON-MAXSHIPPING TRACKER"), payload.get("app_version", ""), False, "created", "")
        return jsonify({"id": session_id, "session_id": session_id, "url": url})
    except Exception as exc:
        return jsonify({"error": str(exc), "version": APP_VERSION}), 500


@app.route("/check-payment/<session_id>", methods=["GET"])
def check_payment(session_id):
    session_id = clean_text(session_id)
    if not session_id:
        return jsonify({"paid": False, "error": "Missing session_id."}), 400
    row = get_payment(session_id)
    if not row:
        return jsonify({"paid": False, "session_id": session_id, "payment_status": "not_found", "version": APP_VERSION})
    return jsonify({"paid": payment_row_is_paid(row), "session_id": session_id, "report_id": row.get("report_id", ""), "amount_cents": row.get("amount_cents", 0), "payment_status": row.get("payment_status", "") or row.get("status", ""), "customer_email": row.get("customer_email", ""), "updated_at": str(row.get("updated_at", "") or ""), "version": APP_VERSION})


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Stripe webhook secret is not configured.", "version": APP_VERSION}), 500
    try:
        event = stripe.Webhook.construct_event(payload=request.data, sig_header=request.headers.get("Stripe-Signature", ""), secret=STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return jsonify({"error": "Invalid payload.", "version": APP_VERSION}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature.", "version": APP_VERSION}), 400
    except Exception as exc:
        return jsonify({"error": str(exc), "version": APP_VERSION}), 400
    try:
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            metadata = session["metadata"] if "metadata" in session else {}
            customer_details = session["customer_details"] if "customer_details" in session else {}
            upsert_payment(session["id"], metadata.get("report_id", ""), int(session["amount_total"] or 0), metadata.get("recovery_total", ""), metadata.get("app_name", ""), metadata.get("app_version", ""), True, session["payment_status"] if "payment_status" in session else "paid", customer_details.get("email", "") if customer_details else "")
        return jsonify({"received": True, "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"error": str(exc), "version": APP_VERSION}), 500


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
