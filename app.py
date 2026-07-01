import hashlib
import json
import os
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


APP_VERSION = "V10.26.1-free-amazon-refund-tracker"

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
    """Return the legacy code_hash value expected by older production schemas."""
    return hashlib.sha256(normalize_code(value).encode("utf-8")).hexdigest()


def safe_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "active"}


def postgres_available():
    return bool(DATABASE_URL and psycopg2 is not None)


def sqlite_fallback_allowed():
    """Allow local SQLite only for explicit developer testing.

    Production/customer deployments must use DATABASE_URL so payment, customer,
    submission-history, refund-lifecycle, and refunds-received records are stored
    in the hosted database instead of silently landing in a local sqlite file.
    """
    return safe_bool(ALLOW_SQLITE_FALLBACK)


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
    if sqlite_fallback_allowed():
        return sqlite3.connect(get_sqlite_path())
    raise RuntimeError(
        "DATABASE_URL is required. Local SQLite fallback is disabled by default "
        "so customer/payment/refund history cannot be stored locally by accident. "
        "Set SHIPTAXREFUND_ALLOW_SQLITE_FALLBACK=1 only for developer testing."
    )


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


def add_column_if_missing(cur, table_name, column_name, column_type):
    if postgres_available():
        if not postgres_column_exists(cur, table_name, column_name):
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
    else:
        if not sqlite_column_exists(cur, table_name, column_name):
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def get_table_columns(cur, table_name):
    if postgres_available():
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table_name,),
        )
        return {str(row[0]) for row in cur.fetchall()}
    cur.execute(f"PRAGMA table_info({table_name})")
    return {str(row[1]) for row in cur.fetchall()}


def ensure_founder_schema(cur):
    """Bring older founding_members tables forward without dropping data.

    Earlier payment-server deployments used a legacy founding_members table.
    CREATE TABLE IF NOT EXISTS does not modify an existing table, so V10.25
    must explicitly add the columns it now queries before founder/access-code
    validation can be used by the cloud customer-history endpoints.
    """
    if postgres_available():
        required_columns = [
            ("founder_code", "TEXT DEFAULT ''"),
            ("code_hash", "TEXT DEFAULT ''"),
            ("email", "TEXT DEFAULT ''"),
            ("membership_type", "TEXT DEFAULT 'Founding Member'"),
            ("active", "BOOLEAN DEFAULT TRUE"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
            ("expires_at", "TIMESTAMPTZ"),
            ("notes", "TEXT DEFAULT ''"),
            ("last_validated_at", "TIMESTAMPTZ"),
            ("validation_count", "INTEGER DEFAULT 0"),
        ]
        for column_name, column_type in required_columns:
            add_column_if_missing(cur, "founding_members", column_name, column_type)

        columns = get_table_columns(cur, "founding_members")
        for legacy_column in ["code", "access_code", "passcode", "owner_code", "founder_passcode"]:
            if legacy_column in columns and legacy_column != "founder_code":
                cur.execute(
                    f"""
                    UPDATE founding_members
                    SET founder_code = UPPER(COALESCE(NULLIF(founder_code, ''), {legacy_column}))
                    WHERE COALESCE(founder_code, '') = ''
                      AND COALESCE({legacy_column}, '') != ''
                    """
                )
        cur.execute(
            """
            UPDATE founding_members
            SET expires_at = NOW() + INTERVAL '10 years'
            WHERE expires_at IS NULL
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_founding_members_founder_code_nonblank
            ON founding_members (founder_code)
            WHERE founder_code IS NOT NULL AND founder_code <> ''
            """
        )
        return

    required_columns = [
        ("founder_code", "TEXT DEFAULT ''"),
        ("code_hash", "TEXT DEFAULT ''"),
        ("email", "TEXT DEFAULT ''"),
        ("membership_type", "TEXT DEFAULT 'Founding Member'"),
        ("active", "INTEGER DEFAULT 1"),
        ("created_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("notes", "TEXT DEFAULT ''"),
        ("last_validated_at", "TEXT"),
        ("validation_count", "INTEGER DEFAULT 0"),
    ]
    for column_name, column_type in required_columns:
        add_column_if_missing(cur, "founding_members", column_name, column_type)

    columns = get_table_columns(cur, "founding_members")
    for legacy_column in ["code", "access_code", "passcode", "owner_code", "founder_passcode"]:
        if legacy_column in columns and legacy_column != "founder_code":
            cur.execute(
                f"""
                UPDATE founding_members
                SET founder_code = UPPER(COALESCE(NULLIF(founder_code, ''), {legacy_column}))
                WHERE COALESCE(founder_code, '') = ''
                  AND COALESCE({legacy_column}, '') != ''
                """
            )
    cur.execute(
        """
        UPDATE founding_members
        SET expires_at = ?
        WHERE COALESCE(expires_at, '') = ''
        """,
        ((utc_now() + timedelta(days=3650)).isoformat(),),
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_founding_members_founder_code_nonblank ON founding_members (founder_code) WHERE founder_code IS NOT NULL AND founder_code <> ''"
    )


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
                for column_name, column_type in [
                    ("recovery_total", "TEXT DEFAULT ''"),
                    ("app_name", "TEXT DEFAULT ''"),
                    ("app_version", "TEXT DEFAULT ''"),
                    ("payment_status", "TEXT DEFAULT ''"),
                    ("customer_email", "TEXT DEFAULT ''"),
                    ("updated_at", "TIMESTAMPTZ"),
                ]:
                    add_column_if_missing(cur, "payments", column_name, column_type)

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS founding_members (
                        founder_code TEXT PRIMARY KEY,
                        email TEXT NOT NULL,
                        membership_type TEXT DEFAULT 'Founding Member',
                        active BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        expires_at TIMESTAMPTZ NOT NULL,
                        notes TEXT DEFAULT '',
                        last_validated_at TIMESTAMPTZ,
                        validation_count INTEGER DEFAULT 0
                    )
                    """
                )

                ensure_founder_schema(cur)

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS report_statistics (
                        report_id TEXT PRIMARY KEY,
                        founder_code TEXT DEFAULT '',
                        email TEXT DEFAULT '',
                        app_name TEXT DEFAULT '',
                        app_version TEXT DEFAULT '',
                        scan_mode TEXT DEFAULT '',
                        generated_at TIMESTAMPTZ DEFAULT NOW(),
                        orders_found INTEGER DEFAULT 0,
                        tracking_found INTEGER DEFAULT 0,
                        tax_identified TEXT DEFAULT '',
                        paid_scan BOOLEAN DEFAULT FALSE,
                        payment_session_id TEXT DEFAULT '',
                        generated_package BOOLEAN DEFAULT TRUE,
                        proof_consent BOOLEAN DEFAULT TRUE,
                        notes TEXT DEFAULT '',
                        raw_payload TEXT DEFAULT ''
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS refund_outcomes (
                        report_id TEXT PRIMARY KEY,
                        founder_code TEXT DEFAULT '',
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

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customers (
                        customer_uuid TEXT PRIMARY KEY,
                        email TEXT UNIQUE NOT NULL,
                        display_name TEXT DEFAULT '',
                        source TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_forwarder_state (
                        customer_uuid TEXT NOT NULL,
                        forwarder TEXT NOT NULL,
                        last_processed_invoice TEXT DEFAULT '',
                        last_processed_tracking TEXT DEFAULT '',
                        last_processed_order_date TEXT DEFAULT '',
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (customer_uuid, forwarder)
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_submission_history (
                        history_id TEXT PRIMARY KEY,
                        customer_uuid TEXT NOT NULL,
                        forwarder TEXT DEFAULT '',
                        amazon_order_id TEXT DEFAULT '',
                        tracking_number TEXT DEFAULT '',
                        bol TEXT DEFAULT '',
                        invoice_number TEXT DEFAULT '',
                        submitted_date TIMESTAMPTZ DEFAULT NOW(),
                        refund_amount TEXT DEFAULT '',
                        certification_id TEXT DEFAULT '',
                        source_run_id TEXT DEFAULT '',
                        raw_payload TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_submission_history_customer_forwarder ON customer_submission_history (customer_uuid, forwarder)"
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_submission_unique ON customer_submission_history (customer_uuid, forwarder, amazon_order_id, tracking_number, bol, invoice_number)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_runs (
                        run_id TEXT PRIMARY KEY,
                        customer_uuid TEXT NOT NULL,
                        forwarder TEXT DEFAULT '',
                        app_version TEXT DEFAULT '',
                        scan_mode TEXT DEFAULT '',
                        started_at TIMESTAMPTZ DEFAULT NOW(),
                        completed_at TIMESTAMPTZ,
                        status TEXT DEFAULT '',
                        matched_count INTEGER DEFAULT 0,
                        excluded_prior_count INTEGER DEFAULT 0,
                        recovery_total TEXT DEFAULT '',
                        raw_payload TEXT DEFAULT ''
                    )
                    """
                )


                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_refund_lifecycle (
                        lifecycle_id TEXT PRIMARY KEY,
                        customer_uuid TEXT NOT NULL,
                        forwarder TEXT DEFAULT '',
                        profile_key TEXT DEFAULT '',
                        amazon_account_email TEXT DEFAULT '',
                        submission_id TEXT DEFAULT '',
                        amazon_order_id TEXT DEFAULT '',
                        current_status TEXT DEFAULT '',
                        requested_tax_amount TEXT DEFAULT '',
                        refund_amount_received TEXT DEFAULT '',
                        refund_received_date TEXT DEFAULT '',
                        refund_method TEXT DEFAULT '',
                        action_needed TEXT DEFAULT '',
                        status_detail TEXT DEFAULT '',
                        tracker_payload TEXT DEFAULT '',
                        source_run_id TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_refund_lifecycle_customer_forwarder ON customer_refund_lifecycle (customer_uuid, forwarder)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_refund_lifecycle_order ON customer_refund_lifecycle (amazon_order_id)"
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_refund_lifecycle_unique ON customer_refund_lifecycle (customer_uuid, forwarder, profile_key, submission_id, amazon_order_id)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_refunds_received (
                        refund_id TEXT PRIMARY KEY,
                        customer_uuid TEXT NOT NULL,
                        amazon_account_email TEXT DEFAULT '',
                        amazon_order_id TEXT DEFAULT '',
                        refund_amount_received TEXT DEFAULT '',
                        refund_method TEXT DEFAULT '',
                        refund_received_date TEXT DEFAULT '',
                        email_date TEXT DEFAULT '',
                        email_from TEXT DEFAULT '',
                        email_to TEXT DEFAULT '',
                        email_subject TEXT DEFAULT '',
                        parsed_status TEXT DEFAULT '',
                        source TEXT DEFAULT '',
                        source_hash TEXT DEFAULT '',
                        notes TEXT DEFAULT '',
                        text_preview TEXT DEFAULT '',
                        raw_payload TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_refunds_received_customer ON customer_refunds_received (customer_uuid)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_refunds_received_order ON customer_refunds_received (amazon_order_id)"
                )
                cur.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_refunds_received_unique ON customer_refunds_received (customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash)"
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_claimant_profiles (
                        profile_key TEXT PRIMARY KEY,
                        customer_uuid TEXT DEFAULT '',
                        amazon_account_email TEXT NOT NULL,
                        claimant_name TEXT NOT NULL,
                        signature_line TEXT DEFAULT '',
                        max_shipping_email TEXT NOT NULL,
                        max_shipping_account_number TEXT NOT NULL,
                        amazon_ship_to_address TEXT NOT NULL,
                        export_destination TEXT DEFAULT '',
                        preferred_contact_email TEXT DEFAULT '',
                        claimant_label TEXT DEFAULT '',
                        app_name TEXT DEFAULT '',
                        app_version TEXT DEFAULT '',
                        source TEXT DEFAULT '',
                        raw_payload TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS customer_claimant_profile_events (
                        event_id TEXT PRIMARY KEY,
                        profile_key TEXT NOT NULL,
                        event_type TEXT DEFAULT 'profile_upsert',
                        raw_payload TEXT DEFAULT '',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_customer ON customer_claimant_profiles (customer_uuid)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_amazon_email ON customer_claimant_profiles (amazon_account_email)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_max_account ON customer_claimant_profiles (max_shipping_account_number)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_customer_claimant_profile_events_profile ON customer_claimant_profile_events (profile_key)"
                )

            conn.commit()
        finally:
            conn.close()
        return

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
        for column_name, column_type in [
            ("recovery_total", "TEXT DEFAULT ''"),
            ("app_name", "TEXT DEFAULT ''"),
            ("app_version", "TEXT DEFAULT ''"),
            ("payment_status", "TEXT DEFAULT ''"),
            ("customer_email", "TEXT DEFAULT ''"),
            ("updated_at", "TEXT"),
        ]:
            add_column_if_missing(cur, "payments", column_name, column_type)

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS founding_members (
                founder_code TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                membership_type TEXT DEFAULT 'Founding Member',
                active INTEGER DEFAULT 1,
                created_at TEXT,
                expires_at TEXT NOT NULL,
                notes TEXT DEFAULT '',
                last_validated_at TEXT,
                validation_count INTEGER DEFAULT 0
            )
            """
        )

        ensure_founder_schema(cur)

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS report_statistics (
                report_id TEXT PRIMARY KEY,
                founder_code TEXT DEFAULT '',
                email TEXT DEFAULT '',
                app_name TEXT DEFAULT '',
                app_version TEXT DEFAULT '',
                scan_mode TEXT DEFAULT '',
                generated_at TEXT,
                orders_found INTEGER DEFAULT 0,
                tracking_found INTEGER DEFAULT 0,
                tax_identified TEXT DEFAULT '',
                paid_scan INTEGER DEFAULT 0,
                payment_session_id TEXT DEFAULT '',
                generated_package INTEGER DEFAULT 1,
                proof_consent INTEGER DEFAULT 1,
                notes TEXT DEFAULT '',
                raw_payload TEXT DEFAULT ''
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS refund_outcomes (
                report_id TEXT PRIMARY KEY,
                founder_code TEXT DEFAULT '',
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

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                customer_uuid TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT DEFAULT '',
                source TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_forwarder_state (
                customer_uuid TEXT NOT NULL,
                forwarder TEXT NOT NULL,
                last_processed_invoice TEXT DEFAULT '',
                last_processed_tracking TEXT DEFAULT '',
                last_processed_order_date TEXT DEFAULT '',
                updated_at TEXT,
                PRIMARY KEY (customer_uuid, forwarder)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_submission_history (
                history_id TEXT PRIMARY KEY,
                customer_uuid TEXT NOT NULL,
                forwarder TEXT DEFAULT '',
                amazon_order_id TEXT DEFAULT '',
                tracking_number TEXT DEFAULT '',
                bol TEXT DEFAULT '',
                invoice_number TEXT DEFAULT '',
                submitted_date TEXT,
                refund_amount TEXT DEFAULT '',
                certification_id TEXT DEFAULT '',
                source_run_id TEXT DEFAULT '',
                raw_payload TEXT DEFAULT '',
                created_at TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_customer_submission_history_customer_forwarder ON customer_submission_history (customer_uuid, forwarder)"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_submission_unique ON customer_submission_history (customer_uuid, forwarder, amazon_order_id, tracking_number, bol, invoice_number)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_runs (
                run_id TEXT PRIMARY KEY,
                customer_uuid TEXT NOT NULL,
                forwarder TEXT DEFAULT '',
                app_version TEXT DEFAULT '',
                scan_mode TEXT DEFAULT '',
                started_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT '',
                matched_count INTEGER DEFAULT 0,
                excluded_prior_count INTEGER DEFAULT 0,
                recovery_total TEXT DEFAULT '',
                raw_payload TEXT DEFAULT ''
            )
            """
        )


        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_refund_lifecycle (
                lifecycle_id TEXT PRIMARY KEY,
                customer_uuid TEXT NOT NULL,
                forwarder TEXT DEFAULT '',
                profile_key TEXT DEFAULT '',
                amazon_account_email TEXT DEFAULT '',
                submission_id TEXT DEFAULT '',
                amazon_order_id TEXT DEFAULT '',
                current_status TEXT DEFAULT '',
                requested_tax_amount TEXT DEFAULT '',
                refund_amount_received TEXT DEFAULT '',
                refund_received_date TEXT DEFAULT '',
                refund_method TEXT DEFAULT '',
                action_needed TEXT DEFAULT '',
                status_detail TEXT DEFAULT '',
                tracker_payload TEXT DEFAULT '',
                source_run_id TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refund_lifecycle_customer_forwarder ON customer_refund_lifecycle (customer_uuid, forwarder)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refund_lifecycle_order ON customer_refund_lifecycle (amazon_order_id)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_refund_lifecycle_unique ON customer_refund_lifecycle (customer_uuid, forwarder, profile_key, submission_id, amazon_order_id)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_refunds_received (
                refund_id TEXT PRIMARY KEY,
                customer_uuid TEXT NOT NULL,
                amazon_account_email TEXT DEFAULT '',
                amazon_order_id TEXT DEFAULT '',
                refund_amount_received TEXT DEFAULT '',
                refund_method TEXT DEFAULT '',
                refund_received_date TEXT DEFAULT '',
                email_date TEXT DEFAULT '',
                email_from TEXT DEFAULT '',
                email_to TEXT DEFAULT '',
                email_subject TEXT DEFAULT '',
                parsed_status TEXT DEFAULT '',
                source TEXT DEFAULT '',
                source_hash TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                text_preview TEXT DEFAULT '',
                raw_payload TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refunds_received_customer ON customer_refunds_received (customer_uuid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_refunds_received_order ON customer_refunds_received (amazon_order_id)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_refunds_received_unique ON customer_refunds_received (customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_claimant_profiles (
                profile_key TEXT PRIMARY KEY,
                customer_uuid TEXT DEFAULT '',
                amazon_account_email TEXT NOT NULL,
                claimant_name TEXT NOT NULL,
                signature_line TEXT DEFAULT '',
                max_shipping_email TEXT NOT NULL,
                max_shipping_account_number TEXT NOT NULL,
                amazon_ship_to_address TEXT NOT NULL,
                export_destination TEXT DEFAULT '',
                preferred_contact_email TEXT DEFAULT '',
                claimant_label TEXT DEFAULT '',
                app_name TEXT DEFAULT '',
                app_version TEXT DEFAULT '',
                source TEXT DEFAULT '',
                raw_payload TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_claimant_profile_events (
                event_id TEXT PRIMARY KEY,
                profile_key TEXT NOT NULL,
                event_type TEXT DEFAULT 'profile_upsert',
                raw_payload TEXT DEFAULT '',
                created_at TEXT
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_customer ON customer_claimant_profiles (customer_uuid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_amazon_email ON customer_claimant_profiles (amazon_account_email)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profiles_max_account ON customer_claimant_profiles (max_shipping_account_number)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_customer_claimant_profile_events_profile ON customer_claimant_profile_events (profile_key)")
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
                        session_id, status, amount_cents, report_id, created_at, paid_at,
                        recovery_total, app_name, app_version, payment_status, customer_email, updated_at
                    )
                    VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (session_id) DO UPDATE SET
                        status = CASE
                            WHEN EXCLUDED.status = 'paid' THEN 'paid'
                            WHEN payments.status = 'paid' THEN payments.status
                            ELSE EXCLUDED.status
                        END,
                        amount_cents = CASE WHEN EXCLUDED.amount_cents > 0 THEN EXCLUDED.amount_cents ELSE payments.amount_cents END,
                        report_id = COALESCE(NULLIF(EXCLUDED.report_id, ''), payments.report_id),
                        paid_at = CASE WHEN EXCLUDED.paid_at IS NOT NULL THEN EXCLUDED.paid_at ELSE payments.paid_at END,
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
        return

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        existing = cur.execute("SELECT session_id FROM payments WHERE session_id = ?", (session_id,)).fetchone()
        if existing:
            cur.execute(
                """
                UPDATE payments
                SET
                    status = CASE WHEN ? = 'paid' THEN 'paid' WHEN status = 'paid' THEN status ELSE ? END,
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
                    session_id, status, amount_cents, report_id, created_at, paid_at,
                    recovery_total, app_name, app_version, payment_status, customer_email, updated_at
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
                cur.execute("SELECT * FROM payments WHERE session_id = %s", (session_id,))
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM payments WHERE session_id = ?", (session_id,)).fetchone()
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


def verify_admin_request():
    if not FOUNDER_ADMIN_TOKEN:
        return False, "FOUNDER_ADMIN_TOKEN is not configured on the server."
    supplied = request.headers.get("X-Admin-Token", "").strip()
    if not supplied:
        supplied = str((request.get_json(silent=True) or {}).get("admin_token", "") or "").strip()
    if supplied != FOUNDER_ADMIN_TOKEN:
        return False, "Invalid or missing admin token."
    return True, ""


def get_founder_member(founder_code, email=""):
    init_db()
    founder_code = normalize_code(founder_code)
    email = normalize_email(email)
    if not founder_code:
        return None

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if email:
                    cur.execute(
                        "SELECT * FROM founding_members WHERE founder_code = %s AND LOWER(email) = %s",
                        (founder_code, email),
                    )
                else:
                    cur.execute("SELECT * FROM founding_members WHERE founder_code = %s", (founder_code,))
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if email:
            row = cur.execute(
                "SELECT * FROM founding_members WHERE founder_code = ? AND LOWER(email) = ?",
                (founder_code, email),
            ).fetchone()
        else:
            row = cur.execute(
                "SELECT * FROM founding_members WHERE founder_code = ?",
                (founder_code,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def founder_row_is_active(row):
    if not row:
        return False, "Founder code was not found for this email."
    if not safe_bool(row.get("active")):
        return False, "Founder code is inactive."

    expires_at = row.get("expires_at")
    if not expires_at:
        return False, "Founder code has no expiration date."

    try:
        if isinstance(expires_at, datetime):
            exp = expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        else:
            exp_text = str(expires_at).replace("Z", "+00:00")
            exp = datetime.fromisoformat(exp_text)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        return False, "Founder code expiration date is invalid."

    if exp < utc_now():
        return False, "Founder code has expired."

    return True, ""


def mark_founder_validated(founder_code):
    founder_code = normalize_code(founder_code)
    now = utc_now_iso()
    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE founding_members
                    SET last_validated_at = NOW(), validation_count = COALESCE(validation_count, 0) + 1
                    WHERE founder_code = %s
                    """,
                    (founder_code,),
                )
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE founding_members
            SET last_validated_at = ?, validation_count = COALESCE(validation_count, 0) + 1
            WHERE founder_code = ?
            """,
            (now, founder_code),
        )
        conn.commit()
    finally:
        conn.close()


def create_or_update_founder_code(founder_code, email, expires_at, notes="", active=True, membership_type="Founding Member"):
    """Create or update an access/founder code without relying on ON CONFLICT.

    Some existing production databases were created before founder_code had a
    formal UNIQUE/PRIMARY KEY constraint. PostgreSQL will reject
    ON CONFLICT(founder_code) unless that exact constraint exists. This manual
    select-then-update/insert path is safer for migrated databases and avoids
    destructive schema changes.
    """
    init_db()
    founder_code = normalize_code(founder_code)
    email = normalize_email(email)
    notes = str(notes or "").strip()
    membership_type = str(membership_type or "Founding Member").strip()
    active_value = bool(active)
    code_hash_value = founder_code_hash(founder_code)

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT founder_code FROM founding_members WHERE founder_code = %s LIMIT 1",
                    (founder_code,),
                )
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        UPDATE founding_members
                        SET email = %s,
                            membership_type = %s,
                            active = %s,
                            expires_at = %s,
                            notes = %s,
                            code_hash = %s
                        WHERE founder_code = %s
                        """,
                        (email, membership_type, active_value, expires_at, notes, code_hash_value, founder_code),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO founding_members (
                            founder_code, code_hash, email, membership_type, active, created_at, expires_at, notes, validation_count
                        )
                        VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, 0)
                        """,
                        (founder_code, code_hash_value, email, membership_type, active_value, expires_at, notes),
                    )
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT founder_code FROM founding_members WHERE founder_code = ? LIMIT 1",
            (founder_code,),
        ).fetchone()
        if existing:
            cur.execute(
                """
                UPDATE founding_members
                SET email = ?,
                    membership_type = ?,
                    active = ?,
                    expires_at = ?,
                    notes = ?,
                    code_hash = ?
                WHERE founder_code = ?
                """,
                (email, membership_type, 1 if active_value else 0, expires_at, notes, code_hash_value, founder_code),
            )
        else:
            cur.execute(
                """
                INSERT INTO founding_members (
                    founder_code, code_hash, email, membership_type, active, created_at, expires_at, notes, validation_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (founder_code, code_hash_value, email, membership_type, 1 if active_value else 0, utc_now_iso(), expires_at, notes),
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
    """
    V10.24 customer-history endpoints require either an active founder/access code
    for the email or a paid Stripe checkout session tied to that email. This keeps
    the new cloud history API from becoming an open unauthenticated customer-data API.
    """
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
    display_name = str(display_name or "").strip()
    source = str(source or "").strip()
    if not email or "@" not in email:
        raise ValueError("Valid email is required.")

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM customers WHERE LOWER(email) = %s", (email,))
                row = cur.fetchone()
                if row:
                    customer_uuid = row["customer_uuid"]
                    cur.execute(
                        """
                        UPDATE customers
                        SET display_name = COALESCE(NULLIF(%s, ''), display_name),
                            source = COALESCE(NULLIF(%s, ''), source),
                            updated_at = NOW()
                        WHERE customer_uuid = %s
                        """,
                        (display_name, source, customer_uuid),
                    )
                    conn.commit()
                    cur.execute("SELECT * FROM customers WHERE customer_uuid = %s", (customer_uuid,))
                    return dict(cur.fetchone())

                customer_uuid = make_customer_uuid()
                cur.execute(
                    """
                    INSERT INTO customers (customer_uuid, email, display_name, source, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, NOW(), NOW())
                    """,
                    (customer_uuid, email, display_name, source),
                )
                conn.commit()
                cur.execute("SELECT * FROM customers WHERE customer_uuid = %s", (customer_uuid,))
                return dict(cur.fetchone())
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM customers WHERE LOWER(email) = ?", (email,)).fetchone()
        now = utc_now_iso()
        if row:
            customer_uuid = row["customer_uuid"]
            cur.execute(
                """
                UPDATE customers
                SET display_name = CASE WHEN ? != '' THEN ? ELSE display_name END,
                    source = CASE WHEN ? != '' THEN ? ELSE source END,
                    updated_at = ?
                WHERE customer_uuid = ?
                """,
                (display_name, display_name, source, source, now, customer_uuid),
            )
            conn.commit()
            row = cur.execute("SELECT * FROM customers WHERE customer_uuid = ?", (customer_uuid,)).fetchone()
            return dict(row)

        customer_uuid = make_customer_uuid()
        cur.execute(
            """
            INSERT INTO customers (customer_uuid, email, display_name, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (customer_uuid, email, display_name, source, now, now),
        )
        conn.commit()
        row = cur.execute("SELECT * FROM customers WHERE customer_uuid = ?", (customer_uuid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_customer_by_uuid(customer_uuid):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    if not customer_uuid:
        return None

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM customers WHERE customer_uuid = %s", (customer_uuid,))
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM customers WHERE customer_uuid = ?", (customer_uuid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_customer_history(customer_uuid, forwarder=""):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    forwarder = str(forwarder or "").strip().lower()

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if forwarder:
                    cur.execute(
                        """
                        SELECT * FROM customer_submission_history
                        WHERE customer_uuid = %s AND LOWER(forwarder) = %s
                        ORDER BY created_at ASC
                        """,
                        (customer_uuid, forwarder),
                    )
                else:
                    cur.execute(
                        """
                        SELECT * FROM customer_submission_history
                        WHERE customer_uuid = %s
                        ORDER BY created_at ASC
                        """,
                        (customer_uuid,),
                    )
                rows = [dict(row) for row in cur.fetchall()]

                if forwarder:
                    cur.execute(
                        "SELECT * FROM customer_forwarder_state WHERE customer_uuid = %s AND LOWER(forwarder) = %s",
                        (customer_uuid, forwarder),
                    )
                else:
                    cur.execute("SELECT * FROM customer_forwarder_state WHERE customer_uuid = %s", (customer_uuid,))
                state_rows = [dict(row) for row in cur.fetchall()]
                return rows, state_rows
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if forwarder:
            rows = cur.execute(
                """
                SELECT * FROM customer_submission_history
                WHERE customer_uuid = ? AND LOWER(forwarder) = ?
                ORDER BY created_at ASC
                """,
                (customer_uuid, forwarder),
            ).fetchall()
            state_rows = cur.execute(
                "SELECT * FROM customer_forwarder_state WHERE customer_uuid = ? AND LOWER(forwarder) = ?",
                (customer_uuid, forwarder),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT * FROM customer_submission_history WHERE customer_uuid = ? ORDER BY created_at ASC",
                (customer_uuid,),
            ).fetchall()
            state_rows = cur.execute(
                "SELECT * FROM customer_forwarder_state WHERE customer_uuid = ?",
                (customer_uuid,),
            ).fetchall()
        return [dict(row) for row in rows], [dict(row) for row in state_rows]
    finally:
        conn.close()


def upsert_customer_history(customer_uuid, records, forwarder="", source_run_id=""):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    forwarder_default = str(forwarder or "").strip().lower()
    source_run_id = str(source_run_id or "").strip()
    if not customer_uuid:
        raise ValueError("customer_uuid is required.")
    if not isinstance(records, list):
        raise ValueError("records must be a list.")

    inserted_or_updated = 0
    now = utc_now_iso()

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    forwarder_value = str(record.get("forwarder", "") or forwarder_default).strip().lower()
                    amazon_order_id = str(record.get("amazon_order_id", "") or record.get("order_id", "") or "").strip()
                    tracking_number = str(record.get("tracking_number", "") or record.get("tracking", "") or "").strip()
                    bol = str(record.get("bol", "") or record.get("bl", "") or "").strip()
                    invoice_number = str(record.get("invoice_number", "") or record.get("invoice", "") or "").strip()
                    if not any([amazon_order_id, tracking_number, bol, invoice_number]):
                        continue
                    history_id = str(record.get("history_id", "") or make_customer_uuid()).strip()
                    submitted_date = str(record.get("submitted_date", "") or now).strip()
                    refund_amount = str(record.get("refund_amount", "") or record.get("tax", "") or "").strip()
                    certification_id = str(record.get("certification_id", "") or "").strip()
                    raw_payload = str(record)
                    cur.execute(
                        """
                        INSERT INTO customer_submission_history (
                            history_id, customer_uuid, forwarder, amazon_order_id, tracking_number, bol,
                            invoice_number, submitted_date, refund_amount, certification_id, source_run_id,
                            raw_payload, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (customer_uuid, forwarder, amazon_order_id, tracking_number, bol, invoice_number)
                        DO UPDATE SET
                            submitted_date = EXCLUDED.submitted_date,
                            refund_amount = COALESCE(NULLIF(EXCLUDED.refund_amount, ''), customer_submission_history.refund_amount),
                            certification_id = COALESCE(NULLIF(EXCLUDED.certification_id, ''), customer_submission_history.certification_id),
                            source_run_id = COALESCE(NULLIF(EXCLUDED.source_run_id, ''), customer_submission_history.source_run_id),
                            raw_payload = EXCLUDED.raw_payload
                        """,
                        (
                            history_id,
                            customer_uuid,
                            forwarder_value,
                            amazon_order_id,
                            tracking_number,
                            bol,
                            invoice_number,
                            submitted_date,
                            refund_amount,
                            certification_id,
                            source_run_id,
                            raw_payload,
                        ),
                    )
                    inserted_or_updated += 1
            conn.commit()
        finally:
            conn.close()
        return inserted_or_updated

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for record in records:
            if not isinstance(record, dict):
                continue
            forwarder_value = str(record.get("forwarder", "") or forwarder_default).strip().lower()
            amazon_order_id = str(record.get("amazon_order_id", "") or record.get("order_id", "") or "").strip()
            tracking_number = str(record.get("tracking_number", "") or record.get("tracking", "") or "").strip()
            bol = str(record.get("bol", "") or record.get("bl", "") or "").strip()
            invoice_number = str(record.get("invoice_number", "") or record.get("invoice", "") or "").strip()
            if not any([amazon_order_id, tracking_number, bol, invoice_number]):
                continue
            history_id = str(record.get("history_id", "") or make_customer_uuid()).strip()
            submitted_date = str(record.get("submitted_date", "") or now).strip()
            refund_amount = str(record.get("refund_amount", "") or record.get("tax", "") or "").strip()
            certification_id = str(record.get("certification_id", "") or "").strip()
            raw_payload = str(record)
            cur.execute(
                """
                INSERT INTO customer_submission_history (
                    history_id, customer_uuid, forwarder, amazon_order_id, tracking_number, bol,
                    invoice_number, submitted_date, refund_amount, certification_id, source_run_id,
                    raw_payload, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_uuid, forwarder, amazon_order_id, tracking_number, bol, invoice_number)
                DO UPDATE SET
                    submitted_date = excluded.submitted_date,
                    refund_amount = CASE WHEN excluded.refund_amount != '' THEN excluded.refund_amount ELSE refund_amount END,
                    certification_id = CASE WHEN excluded.certification_id != '' THEN excluded.certification_id ELSE certification_id END,
                    source_run_id = CASE WHEN excluded.source_run_id != '' THEN excluded.source_run_id ELSE source_run_id END,
                    raw_payload = excluded.raw_payload
                """,
                (
                    history_id,
                    customer_uuid,
                    forwarder_value,
                    amazon_order_id,
                    tracking_number,
                    bol,
                    invoice_number,
                    submitted_date,
                    refund_amount,
                    certification_id,
                    source_run_id,
                    raw_payload,
                    now,
                ),
            )
            inserted_or_updated += 1
        conn.commit()
    finally:
        conn.close()
    return inserted_or_updated


def upsert_customer_forwarder_state(customer_uuid, forwarder, state):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    forwarder = str(forwarder or "").strip().lower()
    state = state if isinstance(state, dict) else {}
    if not customer_uuid or not forwarder:
        return False

    last_processed_invoice = str(state.get("last_processed_invoice", "") or "").strip()
    last_processed_tracking = str(state.get("last_processed_tracking", "") or "").strip()
    last_processed_order_date = str(state.get("last_processed_order_date", "") or "").strip()

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO customer_forwarder_state (
                        customer_uuid, forwarder, last_processed_invoice,
                        last_processed_tracking, last_processed_order_date, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (customer_uuid, forwarder) DO UPDATE SET
                        last_processed_invoice = COALESCE(NULLIF(EXCLUDED.last_processed_invoice, ''), customer_forwarder_state.last_processed_invoice),
                        last_processed_tracking = COALESCE(NULLIF(EXCLUDED.last_processed_tracking, ''), customer_forwarder_state.last_processed_tracking),
                        last_processed_order_date = COALESCE(NULLIF(EXCLUDED.last_processed_order_date, ''), customer_forwarder_state.last_processed_order_date),
                        updated_at = NOW()
                    """,
                    (customer_uuid, forwarder, last_processed_invoice, last_processed_tracking, last_processed_order_date),
                )
            conn.commit()
        finally:
            conn.close()
        return True

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO customer_forwarder_state (
                customer_uuid, forwarder, last_processed_invoice,
                last_processed_tracking, last_processed_order_date, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_uuid, forwarder) DO UPDATE SET
                last_processed_invoice = CASE WHEN excluded.last_processed_invoice != '' THEN excluded.last_processed_invoice ELSE last_processed_invoice END,
                last_processed_tracking = CASE WHEN excluded.last_processed_tracking != '' THEN excluded.last_processed_tracking ELSE last_processed_tracking END,
                last_processed_order_date = CASE WHEN excluded.last_processed_order_date != '' THEN excluded.last_processed_order_date ELSE last_processed_order_date END,
                updated_at = excluded.updated_at
            """,
            (customer_uuid, forwarder, last_processed_invoice, last_processed_tracking, last_processed_order_date, utc_now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def record_customer_run(payload, customer_uuid):
    init_db()
    run_id = str(payload.get("run_id", "") or make_customer_uuid()).strip()
    forwarder = str(payload.get("forwarder", "") or "").strip().lower()
    app_version = str(payload.get("app_version", "") or "").strip()
    scan_mode = str(payload.get("scan_mode", "") or "").strip()
    status = str(payload.get("status", "completed") or "completed").strip()
    recovery_total = str(payload.get("recovery_total", "") or "").strip()
    raw_payload = str(payload)
    try:
        matched_count = int(payload.get("matched_count", 0) or 0)
    except Exception:
        matched_count = 0
    try:
        excluded_prior_count = int(payload.get("excluded_prior_count", 0) or 0)
    except Exception:
        excluded_prior_count = 0

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO customer_runs (
                        run_id, customer_uuid, forwarder, app_version, scan_mode,
                        started_at, completed_at, status, matched_count, excluded_prior_count,
                        recovery_total, raw_payload
                    )
                    VALUES (%s, %s, %s, %s, %s, NOW(), NOW(), %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO UPDATE SET
                        completed_at = NOW(),
                        status = EXCLUDED.status,
                        matched_count = EXCLUDED.matched_count,
                        excluded_prior_count = EXCLUDED.excluded_prior_count,
                        recovery_total = EXCLUDED.recovery_total,
                        raw_payload = EXCLUDED.raw_payload
                    """,
                    (
                        run_id,
                        customer_uuid,
                        forwarder,
                        app_version,
                        scan_mode,
                        status,
                        matched_count,
                        excluded_prior_count,
                        recovery_total,
                        raw_payload,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return run_id

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        now = utc_now_iso()
        cur.execute(
            """
            INSERT INTO customer_runs (
                run_id, customer_uuid, forwarder, app_version, scan_mode,
                started_at, completed_at, status, matched_count, excluded_prior_count,
                recovery_total, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                completed_at = excluded.completed_at,
                status = excluded.status,
                matched_count = excluded.matched_count,
                excluded_prior_count = excluded.excluded_prior_count,
                recovery_total = excluded.recovery_total,
                raw_payload = excluded.raw_payload
            """,
            (
                run_id,
                customer_uuid,
                forwarder,
                app_version,
                scan_mode,
                now,
                now,
                status,
                matched_count,
                excluded_prior_count,
                recovery_total,
                raw_payload,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id




def _record_value(record, *keys):
    for key in keys:
        if key in record and record.get(key) not in (None, ""):
            return str(record.get(key, "") or "").strip()
    return ""


def _json_payload(record):
    try:
        return json.dumps(record, sort_keys=True, default=str)
    except Exception:
        return str(record)


def normalize_refund_lifecycle_record(record, customer_uuid, forwarder_default="", source_run_id=""):
    record = record if isinstance(record, dict) else {}
    forwarder = _record_value(record, "forwarder", "Forwarder") or forwarder_default
    forwarder = str(forwarder or "").strip().lower()
    profile_key = _record_value(record, "profile_key", "Profile Key", "Customer Profile Key")
    amazon_account_email = normalize_email(_record_value(record, "amazon_account_email", "Amazon Account Email"))
    submission_id = _record_value(record, "submission_id", "Submission ID", "certification_id")
    amazon_order_id = _record_value(record, "amazon_order_id", "Amazon Order ID", "order_id")
    if not amazon_order_id:
        return None
    lifecycle_id = _record_value(record, "lifecycle_id", "Lifecycle ID")
    if not lifecycle_id:
        digest_source = "|".join([customer_uuid, forwarder, profile_key, submission_id, amazon_order_id])
        lifecycle_id = "lifecycle_" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32]
    return {
        "lifecycle_id": lifecycle_id,
        "customer_uuid": customer_uuid,
        "forwarder": forwarder,
        "profile_key": profile_key,
        "amazon_account_email": amazon_account_email,
        "submission_id": submission_id,
        "amazon_order_id": amazon_order_id,
        "current_status": _record_value(record, "current_status", "Current Status"),
        "requested_tax_amount": _record_value(record, "requested_tax_amount", "Requested Tax Amount"),
        "refund_amount_received": _record_value(record, "refund_amount_received", "Refund Amount Received"),
        "refund_received_date": _record_value(record, "refund_received_date", "Refund Received Date"),
        "refund_method": _record_value(record, "refund_method", "Refund Method"),
        "action_needed": _record_value(record, "action_needed", "Action Needed"),
        "status_detail": _record_value(record, "status_detail", "Status Detail"),
        "tracker_payload": _json_payload(record),
        "source_run_id": _record_value(record, "source_run_id") or source_run_id,
    }


def upsert_customer_refund_lifecycle(customer_uuid, records, forwarder="", source_run_id=""):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    if not customer_uuid:
        raise ValueError("customer_uuid is required.")
    if not isinstance(records, list):
        raise ValueError("records must be a list.")

    normalized_records = []
    for record in records:
        normalized = normalize_refund_lifecycle_record(record, customer_uuid, forwarder, source_run_id)
        if normalized:
            normalized_records.append(normalized)

    if not normalized_records:
        return 0

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                for row in normalized_records:
                    cur.execute(
                        """
                        INSERT INTO customer_refund_lifecycle (
                            lifecycle_id, customer_uuid, forwarder, profile_key, amazon_account_email,
                            submission_id, amazon_order_id, current_status, requested_tax_amount,
                            refund_amount_received, refund_received_date, refund_method, action_needed,
                            status_detail, tracker_payload, source_run_id, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT (customer_uuid, forwarder, profile_key, submission_id, amazon_order_id)
                        DO UPDATE SET
                            current_status = COALESCE(NULLIF(EXCLUDED.current_status, ''), customer_refund_lifecycle.current_status),
                            requested_tax_amount = COALESCE(NULLIF(EXCLUDED.requested_tax_amount, ''), customer_refund_lifecycle.requested_tax_amount),
                            refund_amount_received = COALESCE(NULLIF(EXCLUDED.refund_amount_received, ''), customer_refund_lifecycle.refund_amount_received),
                            refund_received_date = COALESCE(NULLIF(EXCLUDED.refund_received_date, ''), customer_refund_lifecycle.refund_received_date),
                            refund_method = COALESCE(NULLIF(EXCLUDED.refund_method, ''), customer_refund_lifecycle.refund_method),
                            action_needed = COALESCE(NULLIF(EXCLUDED.action_needed, ''), customer_refund_lifecycle.action_needed),
                            status_detail = COALESCE(NULLIF(EXCLUDED.status_detail, ''), customer_refund_lifecycle.status_detail),
                            tracker_payload = EXCLUDED.tracker_payload,
                            source_run_id = COALESCE(NULLIF(EXCLUDED.source_run_id, ''), customer_refund_lifecycle.source_run_id),
                            updated_at = NOW()
                        """,
                        (
                            row["lifecycle_id"], row["customer_uuid"], row["forwarder"], row["profile_key"],
                            row["amazon_account_email"], row["submission_id"], row["amazon_order_id"],
                            row["current_status"], row["requested_tax_amount"], row["refund_amount_received"],
                            row["refund_received_date"], row["refund_method"], row["action_needed"],
                            row["status_detail"], row["tracker_payload"], row["source_run_id"],
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
        return len(normalized_records)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        now = utc_now_iso()
        for row in normalized_records:
            cur.execute(
                """
                INSERT INTO customer_refund_lifecycle (
                    lifecycle_id, customer_uuid, forwarder, profile_key, amazon_account_email,
                    submission_id, amazon_order_id, current_status, requested_tax_amount,
                    refund_amount_received, refund_received_date, refund_method, action_needed,
                    status_detail, tracker_payload, source_run_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_uuid, forwarder, profile_key, submission_id, amazon_order_id)
                DO UPDATE SET
                    current_status = CASE WHEN excluded.current_status != '' THEN excluded.current_status ELSE current_status END,
                    requested_tax_amount = CASE WHEN excluded.requested_tax_amount != '' THEN excluded.requested_tax_amount ELSE requested_tax_amount END,
                    refund_amount_received = CASE WHEN excluded.refund_amount_received != '' THEN excluded.refund_amount_received ELSE refund_amount_received END,
                    refund_received_date = CASE WHEN excluded.refund_received_date != '' THEN excluded.refund_received_date ELSE refund_received_date END,
                    refund_method = CASE WHEN excluded.refund_method != '' THEN excluded.refund_method ELSE refund_method END,
                    action_needed = CASE WHEN excluded.action_needed != '' THEN excluded.action_needed ELSE action_needed END,
                    status_detail = CASE WHEN excluded.status_detail != '' THEN excluded.status_detail ELSE status_detail END,
                    tracker_payload = excluded.tracker_payload,
                    source_run_id = CASE WHEN excluded.source_run_id != '' THEN excluded.source_run_id ELSE source_run_id END,
                    updated_at = excluded.updated_at
                """,
                (
                    row["lifecycle_id"], row["customer_uuid"], row["forwarder"], row["profile_key"],
                    row["amazon_account_email"], row["submission_id"], row["amazon_order_id"],
                    row["current_status"], row["requested_tax_amount"], row["refund_amount_received"],
                    row["refund_received_date"], row["refund_method"], row["action_needed"],
                    row["status_detail"], row["tracker_payload"], row["source_run_id"], now, now,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return len(normalized_records)


def get_customer_refund_lifecycle(customer_uuid, forwarder="", profile_key="", amazon_account_email=""):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    forwarder = str(forwarder or "").strip().lower()
    profile_key = str(profile_key or "").strip()
    amazon_account_email = normalize_email(amazon_account_email)

    clauses = ["customer_uuid = %s" if postgres_available() else "customer_uuid = ?"]
    params = [customer_uuid]
    if forwarder:
        clauses.append("LOWER(forwarder) = %s" if postgres_available() else "LOWER(forwarder) = ?")
        params.append(forwarder)
    if profile_key:
        clauses.append("profile_key = %s" if postgres_available() else "profile_key = ?")
        params.append(profile_key)
    if amazon_account_email:
        clauses.append("LOWER(amazon_account_email) = %s" if postgres_available() else "LOWER(amazon_account_email) = ?")
        params.append(amazon_account_email)
    sql = "SELECT * FROM customer_refund_lifecycle WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC"

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, tuple(params))
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        return [dict(row) for row in cur.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def normalize_refunds_received_record(record, customer_uuid):
    record = record if isinstance(record, dict) else {}
    amazon_account_email = normalize_email(_record_value(record, "amazon_account_email", "Amazon Account Email"))
    amazon_order_id = _record_value(record, "amazon_order_id", "Amazon Order ID", "order_id")
    refund_amount_received = _record_value(record, "refund_amount_received", "Refund Amount Received", "refund_amount")
    source_hash = _record_value(record, "source_hash", "Source Hash")
    if not amazon_order_id:
        return None
    refund_id = _record_value(record, "refund_id", "Refund ID")
    if not refund_id:
        digest_source = "|".join([customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash])
        refund_id = "refund_" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32]
    return {
        "refund_id": refund_id,
        "customer_uuid": customer_uuid,
        "amazon_account_email": amazon_account_email,
        "amazon_order_id": amazon_order_id,
        "refund_amount_received": refund_amount_received,
        "refund_method": _record_value(record, "refund_method", "Refund Method"),
        "refund_received_date": _record_value(record, "refund_received_date", "Refund Received Date"),
        "email_date": _record_value(record, "email_date", "Email Date"),
        "email_from": _record_value(record, "email_from", "Email From", "from"),
        "email_to": _record_value(record, "email_to", "Email To", "to"),
        "email_subject": _record_value(record, "email_subject", "Email Subject", "subject"),
        "parsed_status": _record_value(record, "parsed_status", "Parsed Status"),
        "source": _record_value(record, "source", "Source"),
        "source_hash": source_hash,
        "notes": _record_value(record, "notes", "Notes"),
        "text_preview": _record_value(record, "text_preview", "Text Preview"),
        "raw_payload": _json_payload(record),
    }


def upsert_customer_refunds_received(customer_uuid, records):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    if not customer_uuid:
        raise ValueError("customer_uuid is required.")
    if not isinstance(records, list):
        raise ValueError("records must be a list.")

    normalized_records = []
    for record in records:
        normalized = normalize_refunds_received_record(record, customer_uuid)
        if normalized:
            normalized_records.append(normalized)

    if not normalized_records:
        return 0

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                for row in normalized_records:
                    cur.execute(
                        """
                        INSERT INTO customer_refunds_received (
                            refund_id, customer_uuid, amazon_account_email, amazon_order_id,
                            refund_amount_received, refund_method, refund_received_date, email_date,
                            email_from, email_to, email_subject, parsed_status, source, source_hash,
                            notes, text_preview, raw_payload, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT (customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash)
                        DO UPDATE SET
                            refund_method = COALESCE(NULLIF(EXCLUDED.refund_method, ''), customer_refunds_received.refund_method),
                            refund_received_date = COALESCE(NULLIF(EXCLUDED.refund_received_date, ''), customer_refunds_received.refund_received_date),
                            email_date = COALESCE(NULLIF(EXCLUDED.email_date, ''), customer_refunds_received.email_date),
                            email_from = COALESCE(NULLIF(EXCLUDED.email_from, ''), customer_refunds_received.email_from),
                            email_to = COALESCE(NULLIF(EXCLUDED.email_to, ''), customer_refunds_received.email_to),
                            email_subject = COALESCE(NULLIF(EXCLUDED.email_subject, ''), customer_refunds_received.email_subject),
                            parsed_status = COALESCE(NULLIF(EXCLUDED.parsed_status, ''), customer_refunds_received.parsed_status),
                            source = COALESCE(NULLIF(EXCLUDED.source, ''), customer_refunds_received.source),
                            notes = COALESCE(NULLIF(EXCLUDED.notes, ''), customer_refunds_received.notes),
                            text_preview = COALESCE(NULLIF(EXCLUDED.text_preview, ''), customer_refunds_received.text_preview),
                            raw_payload = EXCLUDED.raw_payload,
                            updated_at = NOW()
                        """,
                        (
                            row["refund_id"], row["customer_uuid"], row["amazon_account_email"], row["amazon_order_id"],
                            row["refund_amount_received"], row["refund_method"], row["refund_received_date"], row["email_date"],
                            row["email_from"], row["email_to"], row["email_subject"], row["parsed_status"], row["source"],
                            row["source_hash"], row["notes"], row["text_preview"], row["raw_payload"],
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
        return len(normalized_records)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        now = utc_now_iso()
        for row in normalized_records:
            cur.execute(
                """
                INSERT INTO customer_refunds_received (
                    refund_id, customer_uuid, amazon_account_email, amazon_order_id,
                    refund_amount_received, refund_method, refund_received_date, email_date,
                    email_from, email_to, email_subject, parsed_status, source, source_hash,
                    notes, text_preview, raw_payload, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(customer_uuid, amazon_account_email, amazon_order_id, refund_amount_received, source_hash)
                DO UPDATE SET
                    refund_method = CASE WHEN excluded.refund_method != '' THEN excluded.refund_method ELSE refund_method END,
                    refund_received_date = CASE WHEN excluded.refund_received_date != '' THEN excluded.refund_received_date ELSE refund_received_date END,
                    email_date = CASE WHEN excluded.email_date != '' THEN excluded.email_date ELSE email_date END,
                    email_from = CASE WHEN excluded.email_from != '' THEN excluded.email_from ELSE email_from END,
                    email_to = CASE WHEN excluded.email_to != '' THEN excluded.email_to ELSE email_to END,
                    email_subject = CASE WHEN excluded.email_subject != '' THEN excluded.email_subject ELSE email_subject END,
                    parsed_status = CASE WHEN excluded.parsed_status != '' THEN excluded.parsed_status ELSE parsed_status END,
                    source = CASE WHEN excluded.source != '' THEN excluded.source ELSE source END,
                    notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE notes END,
                    text_preview = CASE WHEN excluded.text_preview != '' THEN excluded.text_preview ELSE text_preview END,
                    raw_payload = excluded.raw_payload,
                    updated_at = excluded.updated_at
                """,
                (
                    row["refund_id"], row["customer_uuid"], row["amazon_account_email"], row["amazon_order_id"],
                    row["refund_amount_received"], row["refund_method"], row["refund_received_date"], row["email_date"],
                    row["email_from"], row["email_to"], row["email_subject"], row["parsed_status"], row["source"],
                    row["source_hash"], row["notes"], row["text_preview"], row["raw_payload"], now, now,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return len(normalized_records)


def get_customer_refunds_received(customer_uuid, amazon_account_email=""):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    amazon_account_email = normalize_email(amazon_account_email)

    clauses = ["customer_uuid = %s" if postgres_available() else "customer_uuid = ?"]
    params = [customer_uuid]
    if amazon_account_email:
        clauses.append("LOWER(amazon_account_email) = %s" if postgres_available() else "LOWER(amazon_account_email) = ?")
        params.append(amazon_account_email)
    sql = "SELECT * FROM customer_refunds_received WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC"

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, tuple(params))
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        return [dict(row) for row in cur.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def normalize_identity_part(value):
    text = str(value or "").strip().lower()
    text = re_sub_non_alnum(text)
    return text

def re_sub_non_alnum(text):
    import re
    return re.sub(r"[^a-z0-9]+", "", text or "")

def make_claimant_profile_key(customer_uuid, amazon_account_email, max_shipping_account_number, amazon_ship_to_address):
    base = "|".join([
        str(customer_uuid or "").strip(),
        normalize_email(amazon_account_email),
        normalize_identity_part(max_shipping_account_number),
        normalize_identity_part(amazon_ship_to_address),
    ])
    return "cp_" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]

def clean_text(value, max_len=1000):
    text = str(value or "").strip()
    if max_len and len(text) > max_len:
        return text[:max_len]
    return text

def normalize_claimant_profile_payload(payload, customer_uuid):
    amazon_account_email = normalize_email(payload.get("amazon_account_email", "") or payload.get("amazon_email", ""))
    claimant_name = clean_text(payload.get("claimant_name", "") or payload.get("customer_name", "") or payload.get("display_name", ""), 200)
    signature_line = clean_text(payload.get("signature_line", ""), 300)
    max_shipping_email = normalize_email(payload.get("max_shipping_email", "") or payload.get("forwarder_login_email", ""))
    max_shipping_account_number = clean_text(payload.get("max_shipping_account_number", "") or payload.get("max_account_number", ""), 120)
    amazon_ship_to_address = clean_text(payload.get("amazon_ship_to_address", "") or payload.get("ship_to_address", ""), 1000)
    export_destination = clean_text(payload.get("export_destination", ""), 300)
    preferred_contact_email = normalize_email(payload.get("preferred_contact_email", "") or payload.get("email", ""))
    claimant_label = clean_text(payload.get("claimant_label", "") or payload.get("profile_label", ""), 200)
    app_name = clean_text(payload.get("app_name", ""), 120)
    app_version = clean_text(payload.get("app_version", ""), 120)
    source = clean_text(payload.get("source", "desktop"), 120)

    missing = []
    if not amazon_account_email or "@" not in amazon_account_email:
        missing.append("amazon_account_email")
    if not claimant_name:
        missing.append("claimant_name")
    if not max_shipping_email or "@" not in max_shipping_email:
        missing.append("max_shipping_email")
    if not max_shipping_account_number:
        missing.append("max_shipping_account_number")
    if not amazon_ship_to_address:
        missing.append("amazon_ship_to_address")
    if missing:
        raise ValueError("Missing or invalid claimant profile field(s): " + ", ".join(missing))

    profile_key = clean_text(payload.get("profile_key", ""), 80)
    if not profile_key:
        profile_key = make_claimant_profile_key(customer_uuid, amazon_account_email, max_shipping_account_number, amazon_ship_to_address)

    return {
        "profile_key": profile_key,
        "customer_uuid": customer_uuid,
        "amazon_account_email": amazon_account_email,
        "claimant_name": claimant_name,
        "signature_line": signature_line,
        "max_shipping_email": max_shipping_email,
        "max_shipping_account_number": max_shipping_account_number,
        "amazon_ship_to_address": amazon_ship_to_address,
        "export_destination": export_destination,
        "preferred_contact_email": preferred_contact_email,
        "claimant_label": claimant_label,
        "app_name": app_name,
        "app_version": app_version,
        "source": source,
        "raw_payload": json.dumps(payload, sort_keys=True, default=str),
    }

def resolve_authorized_customer(payload, email):
    customer_uuid = str(payload.get("customer_uuid", "") or "").strip()
    customer = get_customer_by_uuid(customer_uuid) if customer_uuid else None
    if not customer:
        customer = get_or_create_customer(
            email=email,
            display_name=str(payload.get("display_name", "") or payload.get("customer_name", "") or "").strip(),
            source=str(payload.get("source", "desktop") or "desktop").strip(),
        )
    elif normalize_email(customer.get("email", "")) != email:
        raise PermissionError("Customer UUID does not match requested email.")
    return customer

def upsert_claimant_profile(profile):
    init_db()
    now = utc_now_iso()
    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO customer_claimant_profiles (
                        profile_key, customer_uuid, amazon_account_email, claimant_name, signature_line,
                        max_shipping_email, max_shipping_account_number, amazon_ship_to_address, export_destination,
                        preferred_contact_email, claimant_label, app_name, app_version, source, raw_payload,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (profile_key) DO UPDATE SET
                        customer_uuid = EXCLUDED.customer_uuid,
                        amazon_account_email = EXCLUDED.amazon_account_email,
                        claimant_name = EXCLUDED.claimant_name,
                        signature_line = EXCLUDED.signature_line,
                        max_shipping_email = EXCLUDED.max_shipping_email,
                        max_shipping_account_number = EXCLUDED.max_shipping_account_number,
                        amazon_ship_to_address = EXCLUDED.amazon_ship_to_address,
                        export_destination = EXCLUDED.export_destination,
                        preferred_contact_email = EXCLUDED.preferred_contact_email,
                        claimant_label = EXCLUDED.claimant_label,
                        app_name = EXCLUDED.app_name,
                        app_version = EXCLUDED.app_version,
                        source = EXCLUDED.source,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = NOW()
                    RETURNING *
                    """,
                    (profile["profile_key"], profile["customer_uuid"], profile["amazon_account_email"], profile["claimant_name"],
                     profile["signature_line"], profile["max_shipping_email"], profile["max_shipping_account_number"],
                     profile["amazon_ship_to_address"], profile["export_destination"], profile["preferred_contact_email"],
                     profile["claimant_label"], profile["app_name"], profile["app_version"], profile["source"], profile["raw_payload"]),
                )
                row = dict(cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO customer_claimant_profile_events (event_id, profile_key, event_type, raw_payload, created_at)
                    VALUES (%s, %s, 'profile_upsert', %s, NOW())
                    """,
                    (str(uuid.uuid4()), profile["profile_key"], profile["raw_payload"]),
                )
            conn.commit()
            return row
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO customer_claimant_profiles (
                profile_key, customer_uuid, amazon_account_email, claimant_name, signature_line,
                max_shipping_email, max_shipping_account_number, amazon_ship_to_address, export_destination,
                preferred_contact_email, claimant_label, app_name, app_version, source, raw_payload,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_key) DO UPDATE SET
                customer_uuid = excluded.customer_uuid,
                amazon_account_email = excluded.amazon_account_email,
                claimant_name = excluded.claimant_name,
                signature_line = excluded.signature_line,
                max_shipping_email = excluded.max_shipping_email,
                max_shipping_account_number = excluded.max_shipping_account_number,
                amazon_ship_to_address = excluded.amazon_ship_to_address,
                export_destination = excluded.export_destination,
                preferred_contact_email = excluded.preferred_contact_email,
                claimant_label = excluded.claimant_label,
                app_name = excluded.app_name,
                app_version = excluded.app_version,
                source = excluded.source,
                raw_payload = excluded.raw_payload,
                updated_at = excluded.updated_at
            """,
            (profile["profile_key"], profile["customer_uuid"], profile["amazon_account_email"], profile["claimant_name"],
             profile["signature_line"], profile["max_shipping_email"], profile["max_shipping_account_number"],
             profile["amazon_ship_to_address"], profile["export_destination"], profile["preferred_contact_email"],
             profile["claimant_label"], profile["app_name"], profile["app_version"], profile["source"], profile["raw_payload"], now, now),
        )
        cur.execute(
            """
            INSERT INTO customer_claimant_profile_events (event_id, profile_key, event_type, raw_payload, created_at)
            VALUES (?, ?, 'profile_upsert', ?, ?)
            """,
            (str(uuid.uuid4()), profile["profile_key"], profile["raw_payload"], now),
        )
        conn.commit()
        row = cur.execute("SELECT * FROM customer_claimant_profiles WHERE profile_key = ?", (profile["profile_key"],)).fetchone()
        return dict(row)
    finally:
        conn.close()

def get_claimant_profiles(customer_uuid, amazon_account_email="", max_shipping_account_number=""):
    init_db()
    customer_uuid = str(customer_uuid or "").strip()
    amazon_account_email = normalize_email(amazon_account_email)
    max_shipping_account_number = clean_text(max_shipping_account_number, 120)

    clauses = ["customer_uuid = %s" if postgres_available() else "customer_uuid = ?"]
    params = [customer_uuid]
    if amazon_account_email:
        clauses.append("LOWER(amazon_account_email) = %s" if postgres_available() else "LOWER(amazon_account_email) = ?")
        params.append(amazon_account_email)
    if max_shipping_account_number:
        clauses.append("max_shipping_account_number = %s" if postgres_available() else "max_shipping_account_number = ?")
        params.append(max_shipping_account_number)
    where = " AND ".join(clauses)
    sql = f"SELECT * FROM customer_claimant_profiles WHERE {where} ORDER BY updated_at DESC"

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, tuple(params))
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    conn = get_db_connection()
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        return [dict(row) for row in cur.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()

def upsert_report_statistics(payload):
    init_db()
    report_id = str(payload.get("report_id", "") or "").strip()
    if not report_id:
        raise ValueError("report_id is required.")

    founder_code = normalize_code(payload.get("founder_code", ""))
    email = normalize_email(payload.get("email", ""))
    app_name = str(payload.get("app_name", "") or "").strip()
    app_version = str(payload.get("app_version", "") or "").strip()
    scan_mode = str(payload.get("scan_mode", "") or "").strip()
    payment_session_id = str(payload.get("payment_session_id", "") or "").strip()
    tax_identified = str(payload.get("tax_identified", "") or payload.get("recovery_total", "") or "").strip()
    notes = str(payload.get("notes", "") or "").strip()
    generated_at = str(payload.get("generated_at", "") or utc_now_iso()).strip()
    raw_payload = str(payload)

    try:
        orders_found = int(payload.get("orders_found", 0) or 0)
    except Exception:
        orders_found = 0
    try:
        tracking_found = int(payload.get("tracking_found", 0) or 0)
    except Exception:
        tracking_found = 0

    paid_scan = safe_bool(payload.get("paid_scan", False))
    generated_package = safe_bool(payload.get("generated_package", True))
    proof_consent = safe_bool(payload.get("proof_consent", True))

    if postgres_available():
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO report_statistics (
                        report_id, founder_code, email, app_name, app_version, scan_mode, generated_at,
                        orders_found, tracking_found, tax_identified, paid_scan, payment_session_id,
                        generated_package, proof_consent, notes, raw_payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (report_id) DO UPDATE SET
                        founder_code = COALESCE(NULLIF(EXCLUDED.founder_code, ''), report_statistics.founder_code),
                        email = COALESCE(NULLIF(EXCLUDED.email, ''), report_statistics.email),
                        app_name = COALESCE(NULLIF(EXCLUDED.app_name, ''), report_statistics.app_name),
                        app_version = COALESCE(NULLIF(EXCLUDED.app_version, ''), report_statistics.app_version),
                        scan_mode = COALESCE(NULLIF(EXCLUDED.scan_mode, ''), report_statistics.scan_mode),
                        generated_at = EXCLUDED.generated_at,
                        orders_found = EXCLUDED.orders_found,
                        tracking_found = EXCLUDED.tracking_found,
                        tax_identified = COALESCE(NULLIF(EXCLUDED.tax_identified, ''), report_statistics.tax_identified),
                        paid_scan = EXCLUDED.paid_scan,
                        payment_session_id = COALESCE(NULLIF(EXCLUDED.payment_session_id, ''), report_statistics.payment_session_id),
                        generated_package = EXCLUDED.generated_package,
                        proof_consent = EXCLUDED.proof_consent,
                        notes = COALESCE(NULLIF(EXCLUDED.notes, ''), report_statistics.notes),
                        raw_payload = EXCLUDED.raw_payload
                    """,
                    (
                        report_id,
                        founder_code,
                        email,
                        app_name,
                        app_version,
                        scan_mode,
                        generated_at,
                        orders_found,
                        tracking_found,
                        tax_identified,
                        paid_scan,
                        payment_session_id,
                        generated_package,
                        proof_consent,
                        notes,
                        raw_payload,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO refund_outcomes (report_id, founder_code, email, status, created_at, updated_at)
                    VALUES (%s, %s, %s, 'Not Submitted', NOW(), NOW())
                    ON CONFLICT (report_id) DO NOTHING
                    """,
                    (report_id, founder_code, email),
                )
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO report_statistics (
                report_id, founder_code, email, app_name, app_version, scan_mode, generated_at,
                orders_found, tracking_found, tax_identified, paid_scan, payment_session_id,
                generated_package, proof_consent, notes, raw_payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_id) DO UPDATE SET
                founder_code = CASE WHEN excluded.founder_code != '' THEN excluded.founder_code ELSE founder_code END,
                email = CASE WHEN excluded.email != '' THEN excluded.email ELSE email END,
                app_name = CASE WHEN excluded.app_name != '' THEN excluded.app_name ELSE app_name END,
                app_version = CASE WHEN excluded.app_version != '' THEN excluded.app_version ELSE app_version END,
                scan_mode = CASE WHEN excluded.scan_mode != '' THEN excluded.scan_mode ELSE scan_mode END,
                generated_at = excluded.generated_at,
                orders_found = excluded.orders_found,
                tracking_found = excluded.tracking_found,
                tax_identified = CASE WHEN excluded.tax_identified != '' THEN excluded.tax_identified ELSE tax_identified END,
                paid_scan = excluded.paid_scan,
                payment_session_id = CASE WHEN excluded.payment_session_id != '' THEN excluded.payment_session_id ELSE payment_session_id END,
                generated_package = excluded.generated_package,
                proof_consent = excluded.proof_consent,
                notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE notes END,
                raw_payload = excluded.raw_payload
            """,
            (
                report_id,
                founder_code,
                email,
                app_name,
                app_version,
                scan_mode,
                generated_at,
                orders_found,
                tracking_found,
                tax_identified,
                1 if paid_scan else 0,
                payment_session_id,
                1 if generated_package else 0,
                1 if proof_consent else 0,
                notes,
                raw_payload,
            ),
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO refund_outcomes (
                report_id, founder_code, email, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 'Not Submitted', ?, ?)
            """,
            (report_id, founder_code, email, utc_now_iso(), utc_now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


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
            "sqlite_fallback_allowed": sqlite_fallback_allowed(),
            "database_ready": db_ready,
            "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET),
            "founder_admin_token_configured": bool(FOUNDER_ADMIN_TOKEN),
            "cloud_customer_history_enabled": True,
            "cloud_refund_lifecycle_enabled": True,
            "cloud_refunds_received_enabled": True,
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
def admin_create_founder_code():
    ok, error = verify_admin_request()
    if not ok:
        return jsonify({"created": False, "error": error, "version": APP_VERSION}), 401

    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"created": False, "error": "Invalid JSON payload.", "version": APP_VERSION}), 400

    founder_code = normalize_code(payload.get("founder_code", ""))
    email = normalize_email(payload.get("email", ""))
    notes = str(payload.get("notes", "") or "").strip()
    membership_type = str(payload.get("membership_type", "Founding Member") or "Founding Member").strip()
    active = safe_bool(payload.get("active", True))

    if not founder_code:
        return jsonify({"created": False, "error": "founder_code is required.", "version": APP_VERSION}), 400
    if not email or "@" not in email:
        return jsonify({"created": False, "error": "Valid email is required.", "version": APP_VERSION}), 400

    expires_at = str(payload.get("expires_at", "") or "").strip()
    if not expires_at:
        days_valid = int(payload.get("days_valid", 365) or 365)
        expires_at = (utc_now() + timedelta(days=days_valid)).isoformat()

    try:
        create_or_update_founder_code(
            founder_code=founder_code,
            email=email,
            expires_at=expires_at,
            notes=notes,
            active=active,
            membership_type=membership_type,
        )
        return jsonify(
            {
                "created": True,
                "founder_code": founder_code,
                "email": email,
                "expires_at": expires_at,
                "active": active,
                "membership_type": membership_type,
                "version": APP_VERSION,
            }
        )
    except Exception as exc:
        return jsonify({"created": False, "error": str(exc), "version": APP_VERSION}), 500


@app.route("/validate-founder-code", methods=["POST"])
def validate_founder_code():
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"valid": False, "error": "Invalid JSON payload.", "version": APP_VERSION}), 400

    founder_code = normalize_code(payload.get("founder_code", "") or payload.get("code", ""))
    email = normalize_email(payload.get("email", "") or payload.get("max_login", ""))

    if not founder_code:
        return jsonify({"valid": False, "error": "founder_code is required.", "version": APP_VERSION}), 400
    if not email:
        return jsonify({"valid": False, "error": "email is required.", "version": APP_VERSION}), 400

    row = get_founder_member(founder_code, email)
    valid, reason = founder_row_is_active(row)
    if not valid:
        return jsonify(
            {
                "valid": False,
                "reason": reason,
                "founder_code": founder_code,
                "email": email,
                "version": APP_VERSION,
            }
        )

    mark_founder_validated(founder_code)
    return jsonify(
        {
            "valid": True,
            "founder_code": founder_code,
            "email": normalize_email(row.get("email", email)),
            "expires_at": str(row.get("expires_at", "") or ""),
            "membership_type": row.get("membership_type", "Founding Member"),
            "version": APP_VERSION,
        }
    )


@app.route("/record-report-statistics", methods=["POST"])
def record_report_statistics():
    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"recorded": False, "error": "Invalid JSON payload.", "version": APP_VERSION}), 400

    founder_code = normalize_code(payload.get("founder_code", ""))
    email = normalize_email(payload.get("email", ""))

    if founder_code or email:
        row = get_founder_member(founder_code, email)
        valid, reason = founder_row_is_active(row)
        if not valid:
            return jsonify({"recorded": False, "error": reason, "version": APP_VERSION}), 403

    try:
        upsert_report_statistics(payload)
        return jsonify(
            {
                "recorded": True,
                "report_id": str(payload.get("report_id", "") or ""),
                "version": APP_VERSION,
            }
        )
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
        customer = get_or_create_customer(
            email=email,
            display_name=str(payload.get("display_name", "") or payload.get("customer_name", "") or "").strip(),
            source=str(payload.get("source", "desktop") or "desktop").strip(),
        )
        return jsonify(
            {
                "resolved": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "display_name": customer.get("display_name", ""),
                "version": APP_VERSION,
            }
        )
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
        customer_uuid = str(payload.get("customer_uuid", "") or "").strip()
        customer = get_customer_by_uuid(customer_uuid) if customer_uuid else None
        if not customer:
            customer = get_or_create_customer(
                email=email,
                display_name=str(payload.get("display_name", "") or payload.get("customer_name", "") or "").strip(),
                source=str(payload.get("source", "desktop") or "desktop").strip(),
            )
        elif normalize_email(customer.get("email", "")) != email:
            return jsonify({"ok": False, "error": "Customer UUID does not match requested email.", "version": APP_VERSION}), 403

        forwarder = str(payload.get("forwarder", "") or "").strip().lower()
        records, state_rows = get_customer_history(customer.get("customer_uuid", ""), forwarder)
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "forwarder": forwarder,
                "history": records,
                "forwarder_state": state_rows,
                "history_count": len(records),
                "version": APP_VERSION,
            }
        )
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
        customer_uuid = str(payload.get("customer_uuid", "") or "").strip()
        customer = get_customer_by_uuid(customer_uuid) if customer_uuid else None
        if not customer:
            customer = get_or_create_customer(
                email=email,
                display_name=str(payload.get("display_name", "") or payload.get("customer_name", "") or "").strip(),
                source=str(payload.get("source", "desktop") or "desktop").strip(),
            )
        elif normalize_email(customer.get("email", "")) != email:
            return jsonify({"ok": False, "error": "Customer UUID does not match requested email.", "version": APP_VERSION}), 403

        forwarder = str(payload.get("forwarder", "") or "").strip().lower()
        source_run_id = str(payload.get("run_id", "") or "").strip()
        records = payload.get("history", payload.get("records", []))
        synced_count = upsert_customer_history(customer.get("customer_uuid", ""), records, forwarder, source_run_id)

        state = payload.get("forwarder_state", {})
        if isinstance(state, dict) and forwarder:
            upsert_customer_forwarder_state(customer.get("customer_uuid", ""), forwarder, state)

        run_payload = payload.get("run", {})
        run_id = ""
        if isinstance(run_payload, dict) and run_payload:
            run_payload.setdefault("forwarder", forwarder)
            run_payload.setdefault("run_id", source_run_id or make_customer_uuid())
            run_id = record_customer_run(run_payload, customer.get("customer_uuid", ""))

        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "forwarder": forwarder,
                "synced_count": synced_count,
                "run_id": run_id,
                "version": APP_VERSION,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500





def refund_tracker_email_from_payload(payload):
    """Return the Amazon refund email used to identify the free refund tracker.

    The Refund Tracker is not a paid feature. It is keyed to the Amazon account
    email where Amazon refund correspondence is received, not to a founder code
    or Stripe payment session.
    """
    payload = payload if isinstance(payload, dict) else {}
    email = normalize_email(
        payload.get("amazon_refund_email", "")
        or payload.get("amazon_account_email", "")
        or payload.get("amazon_email", "")
        or payload.get("email", "")
    )
    if not email or "@" not in email:
        return ""
    return email


def resolve_free_refund_tracker_customer(payload):
    """Create or return the free Refund Tracker cloud customer record."""
    email = refund_tracker_email_from_payload(payload)
    if not email:
        raise PermissionError("Amazon refund email / Amazon account email is required.")
    display_name = str(
        payload.get("display_name", "")
        or payload.get("customer_name", "")
        or payload.get("claimant_name", "")
        or ""
    ).strip()
    customer = get_or_create_customer(email, display_name=display_name, source="free_refund_tracker")
    return customer, email


def enrich_refund_tracker_records_with_email(records, amazon_refund_email):
    """Attach the Amazon refund email to uploaded tracker/refund rows when missing."""
    output = []
    if not isinstance(records, list):
        return output
    for record in records:
        if not isinstance(record, dict):
            continue
        row = dict(record)
        if not normalize_email(row.get("amazon_account_email", "") or row.get("Amazon Account Email", "")):
            row["amazon_account_email"] = amazon_refund_email
        output.append(row)
    return output


@app.route("/customer/refund-tracker/lifecycle", methods=["POST"])
def free_refund_tracker_lifecycle_list():
    payload, error = require_json_payload()
    if error:
        return jsonify({"ok": False, "error": error, "version": APP_VERSION}), 400

    try:
        customer, amazon_refund_email = resolve_free_refund_tracker_customer(payload)
        rows = get_customer_refund_lifecycle(
            customer.get("customer_uuid", ""),
            forwarder=payload.get("forwarder", ""),
            profile_key=payload.get("profile_key", ""),
            amazon_account_email=amazon_refund_email,
        )
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", amazon_refund_email),
                "amazon_refund_email": amazon_refund_email,
                "lifecycle": rows,
                "lifecycle_count": len(rows),
                "access_model": "free_refund_tracker_amazon_email",
                "version": APP_VERSION,
            }
        )
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
        customer, amazon_refund_email = resolve_free_refund_tracker_customer(payload)
        forwarder = str(payload.get("forwarder", "") or "").strip().lower()
        source_run_id = str(payload.get("run_id", "") or "").strip()
        records = payload.get("lifecycle", payload.get("records", []))
        records = enrich_refund_tracker_records_with_email(records, amazon_refund_email)
        synced_count = upsert_customer_refund_lifecycle(customer.get("customer_uuid", ""), records, forwarder, source_run_id)
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", amazon_refund_email),
                "amazon_refund_email": amazon_refund_email,
                "forwarder": forwarder,
                "synced_count": synced_count,
                "access_model": "free_refund_tracker_amazon_email",
                "version": APP_VERSION,
            }
        )
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
        customer, amazon_refund_email = resolve_free_refund_tracker_customer(payload)
        rows = get_customer_refunds_received(
            customer.get("customer_uuid", ""),
            amazon_account_email=amazon_refund_email,
        )
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", amazon_refund_email),
                "amazon_refund_email": amazon_refund_email,
                "refunds_received": rows,
                "refund_count": len(rows),
                "access_model": "free_refund_tracker_amazon_email",
                "version": APP_VERSION,
            }
        )
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
        customer, amazon_refund_email = resolve_free_refund_tracker_customer(payload)
        records = payload.get("refunds_received", payload.get("records", []))
        records = enrich_refund_tracker_records_with_email(records, amazon_refund_email)
        synced_count = upsert_customer_refunds_received(customer.get("customer_uuid", ""), records)
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", amazon_refund_email),
                "amazon_refund_email": amazon_refund_email,
                "synced_count": synced_count,
                "access_model": "free_refund_tracker_amazon_email",
                "version": APP_VERSION,
            }
        )
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
        rows = get_customer_refund_lifecycle(
            customer.get("customer_uuid", ""),
            forwarder=payload.get("forwarder", ""),
            profile_key=payload.get("profile_key", ""),
            amazon_account_email=payload.get("amazon_account_email", "") or payload.get("amazon_email", ""),
        )
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "lifecycle": rows,
                "lifecycle_count": len(rows),
                "version": APP_VERSION,
            }
        )
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
        forwarder = str(payload.get("forwarder", "") or "").strip().lower()
        source_run_id = str(payload.get("run_id", "") or "").strip()
        records = payload.get("lifecycle", payload.get("records", []))
        synced_count = upsert_customer_refund_lifecycle(customer.get("customer_uuid", ""), records, forwarder, source_run_id)
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "forwarder": forwarder,
                "synced_count": synced_count,
                "version": APP_VERSION,
            }
        )
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
        rows = get_customer_refunds_received(
            customer.get("customer_uuid", ""),
            amazon_account_email=payload.get("amazon_account_email", "") or payload.get("amazon_email", ""),
        )
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "refunds_received": rows,
                "refund_count": len(rows),
                "version": APP_VERSION,
            }
        )
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
        records = payload.get("refunds_received", payload.get("records", []))
        synced_count = upsert_customer_refunds_received(customer.get("customer_uuid", ""), records)
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "synced_count": synced_count,
                "version": APP_VERSION,
            }
        )
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
        profile = normalize_claimant_profile_payload(payload, customer.get("customer_uuid", ""))
        saved = upsert_claimant_profile(profile)
        profiles = get_claimant_profiles(customer.get("customer_uuid", ""))
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "profile": saved,
                "profile_key": saved.get("profile_key", ""),
                "profiles": profiles,
                "profile_count": len(profiles),
                "version": APP_VERSION,
            }
        )
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
        profiles = get_claimant_profiles(
            customer.get("customer_uuid", ""),
            amazon_account_email=payload.get("amazon_account_email", "") or payload.get("amazon_email", ""),
            max_shipping_account_number=payload.get("max_shipping_account_number", "") or payload.get("max_account_number", ""),
        )
        return jsonify(
            {
                "ok": True,
                "customer_uuid": customer.get("customer_uuid", ""),
                "email": customer.get("email", email),
                "profiles": profiles,
                "profile_count": len(profiles),
                "version": APP_VERSION,
            }
        )
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "version": APP_VERSION}), 500


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
                        "product_data": {"name": "Amazon MaxShipping Recovery Report"},
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

        return jsonify({"id": session_id, "session_id": session_id, "url": checkout_url})
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
        event = stripe.Webhook.construct_event(payload=payload, sig_header=signature, secret=STRIPE_WEBHOOK_SECRET)
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
