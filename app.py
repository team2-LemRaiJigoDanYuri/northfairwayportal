import os
import re
import random
import string
import json
import html
import time
import secrets
from io import BytesIO
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, send_file
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import pymysql
import pymysql.cursors

from reportlab.lib.pagesizes import LETTER
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                 TableStyle, HRFlowable, Image, KeepTogether)
from reportlab.lib.enums import TA_CENTER, TA_RIGHT

load_dotenv()

app = Flask(__name__)
_configured_secret = os.getenv('SECRET_KEY')
app.secret_key = _configured_secret or secrets.token_hex(32)
if not _configured_secret:
    app.logger.warning('SECRET_KEY is not set. Using an ephemeral development key; set SECRET_KEY in .env before deployment.')

# Safe defaults for the session cookie. Secure cookies remain opt-in locally so
# the project still works over http://127.0.0.1 during capstone development.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'False').lower() in {'1','true','yes','on'}

# ---------------------------------------------------------------------------
# Session inactivity security
# ---------------------------------------------------------------------------
# Production defaults: 30 minutes of inactivity with a warning during the
# final 5 minutes. Environment overrides are intentionally limited to timing
# values so local testing can use shorter intervals without changing code.
SESSION_INACTIVITY_MINUTES = max(1, int(os.getenv('SESSION_INACTIVITY_MINUTES', '30')))
SESSION_WARNING_MINUTES = max(1, int(os.getenv('SESSION_WARNING_MINUTES', '5')))
if SESSION_WARNING_MINUTES >= SESSION_INACTIVITY_MINUTES:
    SESSION_WARNING_MINUTES = max(1, SESSION_INACTIVITY_MINUTES - 1)

SESSION_INACTIVITY_SECONDS = SESSION_INACTIVITY_MINUTES * 60
SESSION_WARNING_SECONDS = SESSION_WARNING_MINUTES * 60
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(seconds=SESSION_INACTIVITY_SECONDS)
# Background requests must not refresh the cookie simply because a request was
# made. Real activity updates the session explicitly below.
app.config['SESSION_REFRESH_EACH_REQUEST'] = False

# Endpoints that run automatically in the background and therefore must never
# extend an authenticated session.
SESSION_BACKGROUND_ENDPOINTS = {'live_metrics'}


UPLOAD_FOLDER = os.path.join(app.static_folder, 'uploads')
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

def _detected_image_ext(file_storage):
    """Validate uploaded image content using file signatures, without a new dependency."""
    pos=file_storage.stream.tell()
    head=file_storage.stream.read(16)
    file_storage.stream.seek(pos)
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
SIGNATURE_FOLDER = os.path.join(app.instance_path, 'protected_uploads', 'esignatures')
SIGNATURE_SNAPSHOT_FOLDER = os.path.join(app.instance_path, 'protected_uploads', 'signature_snapshots')
os.makedirs(SIGNATURE_FOLDER, exist_ok=True)
os.makedirs(SIGNATURE_SNAPSHOT_FOLDER, exist_ok=True)
app.config['SIGNATURE_FOLDER'] = SIGNATURE_FOLDER
app.config['SIGNATURE_SNAPSHOT_FOLDER'] = SIGNATURE_SNAPSHOT_FOLDER

EXECUTIVE_ROLES = ['President', 'Vice President', 'Board Director']
OFFICER_ROLES = ['Secretary', 'Treasurer', *EXECUTIVE_ROLES, 'Admin']
HOA_NAME = "NORTH FAIRWAY HOMES HOMEOWNERS ASSOCIATION INC."
HOA_ADDRESS = "Sitio Pastol, Brgy. Muzon West, City of San Jose del Monte, Bulacan 3023"
HOA_REG = "HLURB Registration Number: NTR-20986-R | TIN No: 486-778-923-000"


DOCUMENT_SIGNATURE_RULES = {
    'cert-improvement': ['Secretary','President'],
    'proof-residency': ['Secretary','President'],
    'move-in': ['Secretary','President'],
    'move-out': ['Secretary','President'],
    'cert-membership': ['Secretary','President'],
    'gate-pass': ['Treasurer','President'],
    'vehicle-sticker': [],
    'tenant-form': [],
}

# Human-readable labels used by the Admin template editor. The database keeps
# compact internal tokens, while administrators edit familiar field names.
TEMPLATE_FIELD_LABELS = {
    'homeowner': 'Homeowner Full Name',
    'block': 'Block Number',
    'lot': 'Lot Number',
    'address': 'Complete Address',
    'mobile': 'Contact Number',
    'email': 'Email Address',
    'request_id': 'Request ID',
    'request_type': 'Request Type',
    'purpose': 'Purpose of Request',
    'request_date': 'Request Date',
    'issue_date': 'Issue Date',
    'materials': 'Materials',
    'permit_no': 'Construction Permit Number',
    'vehicle_plate': 'Vehicle Plate Number',
    'vehicle_brand': 'Vehicle Brand',
    'vehicle_model': 'Vehicle Model',
    'vehicle_color': 'Vehicle Color',
    'vehicle_type': 'Vehicle Type',
    'secretary_name': 'Secretary Name',
    'treasurer_name': 'Treasurer Name',
    'president_name': 'President Name',
}

def template_text_to_display(text):
    out=str(text or '')
    for token,label in TEMPLATE_FIELD_LABELS.items():
        out=out.replace('{'+token+'}', '['+label+']')
    return out

def template_text_to_internal(text):
    out=str(text or '')
    for token,label in TEMPLATE_FIELD_LABELS.items():
        out=out.replace('['+label+']', '{'+token+'}')
    return out

def _signature_roles(template):
    raw=(template or {}).get('required_signatures') if template else None
    if raw:
        roles=[x.strip() for x in str(raw).split(',') if x.strip()]
        if roles: return roles
    return DOCUMENT_SIGNATURE_RULES.get((template or {}).get('template_key'), ['Secretary','President'])

def _current_role_signature(cursor, role):
    cursor.execute("""SELECT id, CONCAT(first_name,' ',last_name) AS full_name, e_signature_path
                      FROM users WHERE role=%s AND status='Active' ORDER BY id ASC LIMIT 1""", (role,))
    return cursor.fetchone()

def _signature_abs(path_value, snapshot=False):
    if not path_value: return None
    base=app.config['SIGNATURE_SNAPSHOT_FOLDER'] if snapshot else app.config['SIGNATURE_FOLDER']
    p=os.path.join(base, os.path.basename(path_value))
    return p if os.path.isfile(p) else None

def snapshot_request_signatures(cursor, request_id, template):
    """Copy current authorized e-signatures at issuance so historical PDFs never change."""
    import shutil
    for role in _signature_roles(template):
        cursor.execute("SELECT id FROM request_signature_snapshots WHERE request_id=%s AND role=%s", (request_id, role))
        if cursor.fetchone(): continue
        officer=_current_role_signature(cursor, role)
        if not officer or not officer.get('e_signature_path'): continue
        src=_signature_abs(officer.get('e_signature_path'))
        if not src: continue
        ext=os.path.splitext(src)[1].lower() or '.png'
        safe_role=re.sub(r'[^A-Za-z0-9]+','_',role).strip('_').lower()
        filename=secure_filename(f"{request_id}_{safe_role}_{int(time.time())}{ext}")
        dst=os.path.join(app.config['SIGNATURE_SNAPSHOT_FOLDER'], filename)
        shutil.copy2(src,dst)
        cursor.execute("""INSERT INTO request_signature_snapshots
            (request_id,role,officer_id,officer_name,signature_path,captured_at) VALUES (%s,%s,%s,%s,%s,NOW())""",
            (request_id,role,officer['id'],officer['full_name'],filename))

def request_signature_assets(cursor, request_id, template):
    result={}
    roles=_signature_roles(template)
    for role in roles:
        cursor.execute("SELECT officer_name,signature_path FROM request_signature_snapshots WHERE request_id=%s AND role=%s", (request_id,role))
        snap=cursor.fetchone()
        if snap:
            result[role]={'name':snap.get('officer_name') or role,'path':_signature_abs(snap.get('signature_path'), snapshot=True)}
            continue
        officer=_current_role_signature(cursor, role)
        result[role]={'name':(officer or {}).get('full_name') or f'NFH-HOA {role}', 'path':_signature_abs((officer or {}).get('e_signature_path'))}
    return result

# ---------------------------------------------------------------------------
# Request-type-specific approval routing
# ---------------------------------------------------------------------------
# Keep these names aligned with the request titles currently emitted by the
# Homeowner request forms. These sets are the single backend source of truth
# for who may perform the initial checking stage.
SECRETARY_FIRST_REQUEST_TYPES = {
    'Certificate of Improvement',
    'Proof of Residency',
    'Move-in Gate Pass',
    'Move-out Gate Pass',
    'Certificate of Membership',
}
TREASURER_FIRST_REQUEST_TYPES = {
    'Gate Pass',
}
SHARED_INITIAL_CHECK_REQUEST_TYPES = {
    'Vehicle Sticker Application',
    'Renters/Tenants Information Form',
}


def get_service_config(cursor, request_type, active_only=False):
    """Return the authoritative service/workflow configuration for a request type."""
    sql = """SELECT id, service_name, category, fee, initial_reviewer,
                    requires_executive, requires_payment, requires_dues_clearance, purpose_required,
                    release_role, form_template, description, status
             FROM service_types WHERE service_name=%s"""
    params = [request_type]
    if active_only:
        sql += " AND status='Active'"
    cursor.execute(sql, tuple(params))
    return cursor.fetchone()


def service_reviewer_roles(config, request_type=None):
    if config:
        reviewer = config.get('initial_reviewer')
        if reviewer == 'Treasurer':
            return {'Treasurer'}
        if reviewer == 'Shared':
            return {'Secretary', 'Treasurer'}
        return {'Secretary'}
    if request_type in SECRETARY_FIRST_REQUEST_TYPES:
        return {'Secretary'}
    if request_type in TREASURER_FIRST_REQUEST_TYPES:
        return {'Treasurer'}
    if request_type in SHARED_INITIAL_CHECK_REQUEST_TYPES:
        return {'Secretary', 'Treasurer'}
    return {'Secretary'}


def initial_checker_roles(request_type):
    # Administrator-managed configuration is the primary source of truth.
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            config = get_service_config(cursor, request_type, active_only=True)
        if config:
            return service_reviewer_roles(config, request_type)
    except Exception:
        # This compatibility fallback lets an older database start long enough
        # to run migrate.sql without breaking legacy request routing.
        pass
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    return service_reviewer_roles(None, request_type)


def release_roles_for_request(config, assigned_officer=None):
    """Resolve which operational role may release/issue a configured service."""
    if not config:
        return {'Secretary'}
    release_role = config.get('release_role') or 'Secretary'
    if release_role == 'Treasurer':
        return {'Treasurer'}
    if release_role == 'Initial Reviewer':
        if assigned_officer in {'Secretary', 'Treasurer'}:
            return {assigned_officer}
        return service_reviewer_roles(config, config.get('service_name'))
    return {'Secretary'}


def role_can_initial_check(role, request_type, assigned_officer=None):
    if role == 'Admin':
        return True
    allowed = initial_checker_roles(request_type)
    if role not in allowed:
        return False
    # A dynamically configured Shared service can initially be seen by either
    # authorized reviewer. Once assigned, keep it with the same checker so the
    # request does not bounce between Secretary and Treasurer.
    if len(allowed) > 1 and assigned_officer in {'Secretary', 'Treasurer'}:
        return role == assigned_officer
    return True


def request_is_check_actionable(req, role):
    return (
        req.get('status') in {'Submitted', 'Under Review'} and
        role_can_initial_check(role, req.get('request_type'), req.get('assigned_officer'))
    )



# ---------------------------------------------------------------------------
# Inactivity enforcement and shared UI configuration
# ---------------------------------------------------------------------------
def _session_expired_response():
    session.clear()
    if request.path.startswith('/api/') or request.headers.get('Accept', '').lower().find('application/json') >= 0:
        response = jsonify({
            'status': 'error',
            'message': 'Your session expired after 30 minutes of inactivity. Please sign in again.',
            'session_expired': True,
        })
        response.status_code = 401
        response.headers['X-Session-Expired'] = '1'
        return response
    return redirect(url_for('register_page', session='expired'))


@app.before_request
def enforce_session_inactivity():
    """Expire authenticated sessions after 30 minutes without real activity.

    Automatic live-metric polling is checked for expiration but intentionally
    does not refresh last_activity. Normal page/API actions do refresh it, so
    server-side enforcement still works when JavaScript is unavailable.
    """
    if request.endpoint == 'static' or 'user_id' not in session:
        return None

    now = time.time()
    last_activity = float(session.get('last_activity', now))
    if now - last_activity >= SESSION_INACTIVITY_SECONDS:
        return _session_expired_response()

    if request.endpoint not in SESSION_BACKGROUND_ENDPOINTS and request.endpoint != 'session_keepalive':
        session['last_activity'] = now
        session.permanent = True
        session.modified = True
    return None


def _get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
        session.modified = True
    return token


@app.context_processor
def inject_session_timeout_config():
    return {
        'session_timeout_seconds': SESSION_INACTIVITY_SECONDS,
        'session_warning_seconds': SESSION_WARNING_SECONDS,
        'csrf_token': _get_csrf_token(),
    }


@app.before_request
def enforce_csrf_for_authenticated_changes():
    """Protect authenticated state-changing requests without adding a dependency.

    Browser forms receive the token through static/csrf.js, and same-origin fetch
    calls receive it through the same helper. Authentication/bootstrap endpoints
    remain usable before a user session exists.
    """
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'} or 'user_id' not in session:
        return None
    if request.endpoint in {'session_keepalive'}:
        return None
    expected = session.get('_csrf_token') or ''
    supplied = request.headers.get('X-CSRF-Token') or request.form.get('_csrf_token') or ''
    if expected and supplied and secrets.compare_digest(expected, supplied):
        return None
    if request.path.startswith('/api/') or 'application/json' in request.headers.get('Accept', '').lower():
        return jsonify({'status': 'error', 'message': 'Security token expired. Refresh the page and try again.'}), 400
    flash('Your security token expired. Please refresh the page and try again.', 'error')
    return redirect(request.referrer or _role_home_url(session.get('role')))


@app.route('/api/session/keepalive', methods=['POST'])
def session_keepalive():
    if 'user_id' not in session:
        response = jsonify({'status': 'error', 'message': 'Unauthorized', 'session_expired': True})
        response.status_code = 401
        response.headers['X-Session-Expired'] = '1'
        return response
    now = time.time()
    session['last_activity'] = now
    session.permanent = True
    session.modified = True
    return jsonify({
        'status': 'success',
        'expires_in': SESSION_INACTIVITY_SECONDS,
        'server_time': int(now),
    })


# ---------------------------------------------------------------------------
# Jinja filter
# ---------------------------------------------------------------------------
@app.template_filter('format_details')
def format_details(details):
    if not details:
        return 'None'
    try:
        parsed = json.loads(details) if isinstance(details, str) else details
        if isinstance(parsed, dict):
            items = []
            for k, v in parsed.items():
                if k == 'vehicles' and isinstance(v, list):
                    items.append(f"Vehicles: {len(v)}")
                else:
                    items.append(f"{k.replace('_', ' ').title()}: {v}")
            return ', '.join(items)
    except Exception:
        pass
    return str(details)


# ---------------------------------------------------------------------------
# Database configuration
# ---------------------------------------------------------------------------
DB_HOST = os.getenv('MYSQL_HOST', 'localhost')
DB_USER = os.getenv('MYSQL_USER', 'root')
DB_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
DB_NAME = os.getenv('MYSQL_DB', 'nfhsystem')
DB_PORT = int(os.getenv('MYSQL_PORT', 3306))

app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER') or os.getenv('MAIL_USERNAME')
DEV_SHOW_OTP = os.getenv('DEV_SHOW_OTP', 'False').lower() in {'1','true','yes','on'}

mail = Mail(app)

VERIFICATION_CODES = {}
RESET_CODES = {}


def get_db_connection():
    return pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
        port=DB_PORT, cursorclass=pymysql.cursors.DictCursor, autocommit=True
    )


def now_str():
    """NFH convention: DD/MM/YYYY - HH:MM"""
    return datetime.now().strftime('%d/%m/%Y - %H:%M')


def dues_summary(cursor, user_id):
    """Return a compact, read-only summary of a homeowner's unpaid monthly dues."""
    cursor.execute("""SELECT COUNT(*) AS unpaid_months, COALESCE(SUM(amount),0) AS outstanding_balance,
                              MIN(due_month) AS oldest_unpaid_due
                       FROM monthly_dues WHERE user_id=%s AND status='Unpaid'""", (user_id,))
    row = cursor.fetchone() or {}
    cursor.execute("""SELECT due_month, amount FROM monthly_dues
                       WHERE user_id=%s AND status='Unpaid' ORDER BY due_month ASC""", (user_id,))
    unpaid_rows = cursor.fetchall() or []
    count = int(row.get('unpaid_months') or 0)
    oldest = row.get('oldest_unpaid_due')
    if count <= 0 or not oldest:
        period = 'Clear'
    else:
        today = datetime.now().date().replace(day=1)
        if isinstance(oldest, datetime): oldest = oldest.date()
        months = max(1, (today.year-oldest.year)*12 + today.month-oldest.month + 1)
        years, rem = divmod(months, 12)
        parts=[]
        if years: parts.append(f"{years} year{'s' if years != 1 else ''}")
        if rem: parts.append(f"{rem} month{'s' if rem != 1 else ''}")
        period = ' and '.join(parts) or '1 month'
    return {
        'unpaid_months': count,
        'outstanding_balance': float(row.get('outstanding_balance') or 0),
        'oldest_unpaid_due': oldest.strftime('%B %Y') if oldest else None,
        'unpaid_periods': [r['due_month'].strftime('%B %Y') if hasattr(r.get('due_month'), 'strftime') else str(r.get('due_month')) for r in unpaid_rows],
        'outstanding_period': period,
        'dues_status': 'Outstanding' if count else 'Clear'
    }


def format_pdf_datetime(value):
    """Human-readable presentation for database datetimes used in PDFs."""
    if not value:
        return ''
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        dt = None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S'):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
        if dt is None:
            return text
    return dt.strftime('%B %d, %Y • %I:%M %p').replace(' 0', ' ')


def generate_code(length=6):
    return ''.join(secrets.choice(string.digits) for _ in range(length))


OTP_TTL_SECONDS = max(120, int(os.getenv('OTP_TTL_SECONDS', '600')))
OTP_RESEND_COOLDOWN_SECONDS = max(15, int(os.getenv('OTP_RESEND_COOLDOWN_SECONDS', '60')))
OTP_MAX_ATTEMPTS = max(3, int(os.getenv('OTP_MAX_ATTEMPTS', '5')))


def _issue_one_time_code(store, key):
    """Create a short-lived code with resend throttling for this process."""
    now = time.time()
    current = store.get(key)
    if isinstance(current, dict):
        remaining = OTP_RESEND_COOLDOWN_SECONDS - int(now - float(current.get('sent_at', 0)))
        if remaining > 0 and not current.get('verified'):
            return None, remaining
    code = generate_code()
    store[key] = {
        'code': code,
        'expires_at': now + OTP_TTL_SECONDS,
        'sent_at': now,
        'attempts': 0,
        'verified': False,
    }
    return code, 0


def _verify_one_time_code(store, key, submitted_code, mark_verified=False):
    entry = store.get(key)
    if not isinstance(entry, dict):
        return False, 'No active verification code was found. Request a new code.'
    now = time.time()
    if now > float(entry.get('expires_at', 0)):
        store.pop(key, None)
        return False, 'The verification code has expired. Request a new code.'
    if int(entry.get('attempts', 0)) >= OTP_MAX_ATTEMPTS:
        store.pop(key, None)
        return False, 'Too many incorrect attempts. Request a new code.'
    if not secrets.compare_digest(str(entry.get('code') or ''), str(submitted_code or '')):
        entry['attempts'] = int(entry.get('attempts', 0)) + 1
        remaining = max(0, OTP_MAX_ATTEMPTS - entry['attempts'])
        return False, f'Invalid verification code. {remaining} attempt(s) remaining.'
    if mark_verified:
        entry['verified'] = True
        entry['code'] = None
        # Keep a verified registration token alive only for the original TTL.
    return True, None


def _registration_email_verified(email):
    entry = VERIFICATION_CODES.get(email)
    return bool(isinstance(entry, dict) and entry.get('verified') and time.time() <= float(entry.get('expires_at', 0)))


# ---------------------------------------------------------------------------
# Validation helpers (also enforced server-side so URL/API bypass is blocked)
# ---------------------------------------------------------------------------
COLOR_RE = re.compile(r'^[A-Za-z\s\-]+$')


def validate_vehicle_list(vehicles):
    """Returns an error message string, or None if all vehicles are valid."""
    if not vehicles:
        return 'At least one vehicle is required.'
    for idx, v in enumerate(vehicles, start=1):
        brand = (v.get('brand') or '').strip()
        model = (v.get('model') or '').strip()
        color = (v.get('color') or '').strip()
        plate = (v.get('plate') or '').strip()
        if not plate:
            return f'Vehicle #{idx}: Plate number is required.'
        if not brand:
            return f'Vehicle #{idx}: Vehicle brand is required.'
        if len(model) < 4:
            return f'Vehicle #{idx}: Vehicle Model must be at least 4 characters.'
        if not color or not COLOR_RE.match(color):
            return f'Vehicle #{idx}: Vehicle Color must contain letters only.'
    return None


# ---------------------------------------------------------------------------
# HOA settings (key/value store) — used for Treasurer contact info
# ---------------------------------------------------------------------------
def get_settings(keys=None):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if keys:
                fmt = ','.join(['%s'] * len(keys))
                cursor.execute(f"SELECT setting_key, setting_value FROM hoa_settings WHERE setting_key IN ({fmt})", tuple(keys))
            else:
                cursor.execute("SELECT setting_key, setting_value FROM hoa_settings")
            rows = cursor.fetchall()
        return {r['setting_key']: r['setting_value'] for r in rows}
    finally:
        conn.close()


def set_setting(key, value, updated_by=None):
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""INSERT INTO hoa_settings (setting_key, setting_value, updated_by)
                              VALUES (%s,%s,%s)
                              ON DUPLICATE KEY UPDATE setting_value=%s, updated_by=%s""",
                           (key, value, updated_by, value, updated_by))
    finally:
        conn.close()


def get_treasurer_contact():
    s = get_settings(['treasurer_name', 'treasurer_phone', 'treasurer_email', 'treasurer_office_hours'])
    return {
        'name': s.get('treasurer_name') or 'Office of the Treasurer',
        'phone': s.get('treasurer_phone') or 'N/A',
        'email': s.get('treasurer_email') or 'N/A',
        'office_hours': s.get('treasurer_office_hours') or 'N/A',
    }


# ---------------------------------------------------------------------------
# Audit + notifications
# ---------------------------------------------------------------------------
def log_audit_action(username, role, action, item, prev_val=None, new_val=None):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO audit_logs (username, role, action, item, prev_val, new_val)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (username, role, action, item,
                  str(prev_val) if prev_val is not None else None,
                  str(new_val) if new_val is not None else None))
        conn.close()
    except Exception as e:
        print(f"Audit log error: {e}")




def record_request_history(request_id, action, previous_status=None, new_status=None, remarks=None, actor_name=None, actor_role=None):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""INSERT INTO request_history
                (request_id,action,previous_status,new_status,actor_name,actor_role,remarks)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (request_id, action, previous_status, new_status,
                 actor_name or session.get('full_name') or session.get('username') or 'System',
                 actor_role or session.get('role') or 'System', remarks))
        conn.close()
    except Exception as exc:
        app.logger.warning('Request history insert failed for %s: %s', request_id, exc)




def send_in_app_notification(user_id, subject, body, related_type=None, related_id=None, dedupe_key=None):
    """Create an in-app workflow notification without sending email.

    Used for face-to-face payment/release events where the homeowner is already
    physically present and the portal record is the useful confirmation.
    """
    try:
        if dedupe_key and notification_already_sent(dedupe_key):
            return {'created': False}
        create_alert(user_id, f"{subject}: {body}")
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""INSERT INTO notification_log
                (user_id, channel, subject, body, related_type, related_id, dedupe_key, status)
                VALUES (%s,'In-App',%s,%s,%s,%s,%s,'Sent')""",
                (user_id, subject, body, related_type, related_id, dedupe_key))
        conn.close()
        return {'created': True}
    except Exception as exc:
        app.logger.warning('In-app notification failed for %s: %s', related_id, exc)
        return {'created': False}


def get_active_document_template(cursor, request_type=None, template_key=None):
    """Return the newest active version of the requested official soft-copy template."""
    if template_key:
        cursor.execute("""SELECT * FROM document_templates
                          WHERE template_key=%s AND status='Active'
                          ORDER BY version_no DESC LIMIT 1""", (template_key,))
        return cursor.fetchone()
    if request_type:
        cursor.execute("""SELECT dt.* FROM document_templates dt
                          LEFT JOIN service_types s ON s.service_name=%s
                          WHERE dt.status='Active'
                            AND (dt.service_name=%s OR dt.template_key=s.form_template)
                          ORDER BY (dt.service_name=%s) DESC, dt.version_no DESC LIMIT 1""",
                       (request_type, request_type, request_type))
        return cursor.fetchone()
    return None


def assign_request_document_template(cursor, request_id, request_type):
    """Freeze the template version used by a request so later Admin edits do not rewrite history."""
    cursor.execute("SELECT document_template_id FROM requests WHERE id=%s", (request_id,))
    row = cursor.fetchone()
    if row and row.get('document_template_id'):
        return row.get('document_template_id')
    template = get_active_document_template(cursor, request_type=request_type)
    if template:
        cursor.execute("""UPDATE requests SET document_template_id=%s, document_prepared_at=COALESCE(document_prepared_at,NOW())
                          WHERE id=%s""", (template['id'], request_id))
        return template['id']
    return None


def _request_workflow_after_approval(cursor, req):
    """Resolve the next stage after initial/executive approval using actual HOA procedure."""
    service = get_service_config(cursor, req['request_type'], active_only=False)
    requires_dues = True if not service else bool(service.get('requires_dues_clearance'))
    requires_payment = bool(float(req.get('fee') or 0) > 0) if not service else bool(service.get('requires_payment')) and float(req.get('fee') or 0) > 0
    dues = dues_summary(cursor, req['user_id']) if requires_dues else {'unpaid_months': 0}
    if dues.get('unpaid_months', 0) > 0:
        return 'Awaiting Dues Clearance', requires_payment
    if requires_payment:
        return 'Ready for Payment & Release', True
    return 'Ready for Release', False


def _ensure_payment_reference(cursor, transaction_id):
    """Create an internal payment-confirmation reference for request tracking.

    This is not an accounting invoice/receipt number. Official financial records
    remain under the HOA Treasurer's existing financial procedure.
    """
    year = datetime.now().year
    ref = f"NFH-PAY-{year}-{transaction_id:05d}"
    cursor.execute("""UPDATE payment_transactions
                      SET system_reference=COALESCE(system_reference,%s)
                      WHERE id=%s""", (ref, transaction_id))
    return ref

def generate_homeowner_public_id(cursor, block, lot):
    b = re.sub(r'[^A-Za-z0-9]', '', str(block or '')).upper()
    l = re.sub(r'[^A-Za-z0-9]', '', str(lot or '')).upper()
    base = f"NFH-B{b or 'NA'}-L{l or 'NA'}"
    cursor.execute("SELECT COUNT(*) AS total FROM users WHERE homeowner_public_id LIKE %s", (base + '%',))
    n = (cursor.fetchone() or {}).get('total', 0)
    return base if n == 0 else f"{base}-{n + 1}"


PURPOSE_REQUIRED_REQUEST_TYPES = {
    'Gate Pass',
    'Proof of Residency',
    'Certificate of Improvement',
    'Certificate of Membership',
}


def complete_missing_request_profile(cursor, user_id, user, form_data):
    """Fill only missing core property fields from the request form.

    New registrations already capture these values. This compatibility path keeps
    older homeowner accounts from being forced to leave the request screen and
    visit Profile before they can proceed.
    """
    user = dict(user or {})
    candidates = {
        'block': str(form_data.get('profileBlock') or form_data.get('blockNum') or '').strip(),
        'lot': str(form_data.get('profileLot') or form_data.get('lotNum') or '').strip(),
        'address_line': str(form_data.get('profileAddressLine') or '').strip(),
    }
    updates = {}
    for field, value in candidates.items():
        if not str(user.get(field) or '').strip() and value:
            updates[field] = value
            user[field] = value
    if updates:
        assignments = ', '.join(f"{field}=%s" for field in updates)
        cursor.execute(f"UPDATE users SET {assignments} WHERE id=%s", (*updates.values(), user_id))
    return user


def validate_request_payload(title, form_data, user, service_config=None):
    """Server-side request validation for every supported request template."""
    missing = []
    if not str(user.get('block') or '').strip(): missing.append('Block Number')
    if not str(user.get('lot') or '').strip(): missing.append('Lot Number')
    if not str(user.get('address_line') or '').strip(): missing.append('Complete Address')
    if missing:
        return 'Please provide the missing property details shown in the request form: ' + ', '.join(missing) + '.'

    config = service_config or {}
    form_template = str(config.get('form_template') or '').strip() or {
        'Gate Pass': 'gate-pass',
        'Proof of Residency': 'proof-residency',
        'Certificate of Improvement': 'cert-improvement',
        'Certificate of Membership': 'cert-membership',
        'Move-in Gate Pass': 'move-in',
        'Move-out Gate Pass': 'move-out',
        'Vehicle Sticker Application': 'vehicle-sticker',
        'Renters/Tenants Information Form': 'tenant-form',
    }.get(title, 'generic')

    purpose_required = bool(config.get('purpose_required')) if 'purpose_required' in config else title in PURPOSE_REQUIRED_REQUEST_TYPES
    if purpose_required:
        purpose = str(form_data.get('purpose') or '').strip()
        if not purpose:
            return 'Please select the reason or purpose for this request.'
        if purpose == 'Other' and not str(form_data.get('purposeDetails') or '').strip():
            return 'Please briefly specify the reason for this request.'

    def required(key, label, minimum=1):
        value = str(form_data.get(key) or '').strip()
        if len(value) < minimum:
            return f'{label} is required.'
        return None

    if form_template == 'gate-pass':
        err = required('materialsList', 'List of materials', 3)
        if err: return err
    elif form_template == 'proof-residency':
        for key, label in [('verifiedOwner', 'Verified owner'), ('requestDate', 'Date requested')]:
            err = required(key, label)
            if err: return err
    elif form_template == 'cert-improvement':
        err = required('requestDate', 'Request date')
        if err: return err
    elif form_template == 'cert-membership':
        err = required('applicationDate', 'Application date')
        if err: return err
    elif form_template == 'move-in':
        err = required('moveInDate', 'Move-in date')
        if err: return err
    elif form_template == 'move-out':
        err = required('moveOutDate', 'Move-out date')
        if err: return err
    elif form_template == 'vehicle-sticker':
        err = validate_vehicle_list(form_data.get('vehicles', []))
        if err: return err
        for i, vehicle in enumerate(form_data.get('vehicles', []), start=1):
            plate = str(vehicle.get('plate') or '').strip().upper()
            if not re.match(r'^[A-Z0-9][A-Z0-9 -]{2,9}$', plate):
                return f'Vehicle #{i}: enter a valid plate number using 3-10 letters/numbers.'
    elif form_template == 'tenant-form':
        for key, label in [
            ('fullName', 'Tenant full name'), ('dob', 'Date of birth'), ('phoneNum', 'Phone number'),
            ('previousAddress', 'Previous address'), ('rentedAddress', 'Rented unit address'),
            ('ownerName', 'Unit owner name'), ('ownerPhone', 'Owner contact number'),
            ('emergencyName', 'Emergency contact person'), ('emergencyPhone', 'Emergency contact number')]:
            err = required(key, label)
            if err: return err
        phone_fields = [('phoneNum', 'Phone number'), ('ownerPhone', 'Owner contact number'), ('emergencyPhone', 'Emergency contact number')]
        for key, label in phone_fields:
            digits = re.sub(r'\D', '', str(form_data.get(key) or ''))
            if len(digits) < 10 or len(digits) > 11:
                return f'{label} must contain a valid 10-11 digit number.'
        if not form_data.get('oathAgreed'):
            return 'You must accept the Tenant Oath before submitting.'
    else:
        # Administrator-created generic services still require meaningful details
        # so the reviewing officer receives more than an empty transaction shell.
        details = str(form_data.get('requestDetails') or '').strip()
        if len(details) < 5:
            return 'Please provide brief details for this request.'
    return None


def create_alert(user_id, text):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO alerts (user_id, text) VALUES (%s, %s)", (user_id, text))
        conn.close()
    except Exception as e:
        print(f"Alert insert error: {e}")


def notification_already_sent(dedupe_key):
    if not dedupe_key:
        return False
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM notification_log WHERE dedupe_key=%s", (dedupe_key,))
            row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def send_alert_and_email(user_id, subject, body, related_type=None, related_id=None, dedupe_key=None, important=True):
    """Persist the in-app notification and send the matching workflow email.

    Workflow mail deliberately uses the same initialized Flask-Mail ``mail``
    object and ``Message`` path as the already-working registration-code mail.
    Business actions are committed by their routes before this helper runs, so
    SMTP failure can never roll back an HOA decision.
    """
    user = None
    email_sent = False
    email_error = None
    in_app_exists = False
    email_already_sent = False

    # Dedupe channels independently.  An earlier in-app notification (or an
    # earlier FAILED email attempt) must never prevent a later legitimate SMTP
    # attempt.  Only a previously successful Email/Sent row suppresses mail.
    if dedupe_key:
        try:
            conn = get_db_connection()
            with conn.cursor() as cursor:
                cursor.execute("""SELECT channel, status FROM notification_log
                                  WHERE dedupe_key=%s""", (dedupe_key,))
                prior = cursor.fetchall()
            conn.close()
            in_app_exists = any(r.get('channel') == 'In-App' and r.get('status') == 'Sent' for r in prior)
            email_already_sent = any(r.get('channel') == 'Email' and r.get('status') == 'Sent' for r in prior)
        except Exception as exc:
            app.logger.warning('[EMAIL] Dedupe lookup failed for %s: %s', related_id, exc)

    if not in_app_exists:
        create_alert(user_id, f"{subject}: {body}")

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            # Registered email is the only field required for critical workflow
            # delivery.  Older live NFH databases may not yet have the optional
            # email_notifications preference column; verification mail works on
            # those databases because it also depends only on the email address.
            cursor.execute("SELECT email FROM users WHERE id=%s", (user_id,))
            user = cursor.fetchone()
        conn.close()

        # Important workflow events always email.  This intentionally avoids
        # making correction/approval/rejection delivery depend on an optional
        # preference column that may be absent in an upgraded database.
        should_email = bool(important)
        recipient = (user.get('email') or '').strip() if user else ''

        if email_already_sent:
            email_sent = True
            email_error = 'already_sent'
        elif recipient and should_email:
            # Match the proven registration verification path: construct a
            # Flask-Mail Message and call the SAME global mail.send().  Do not
            # add a second mail object or frontend recipient source.
            if not app.config.get('MAIL_USERNAME'):
                email_error = 'Mail sender is not configured.'
                app.logger.warning('[EMAIL FAILURE] %s for %s: MAIL_USERNAME is not configured', subject, related_id)
            else:
                try:
                    msg = Message(subject, recipients=[recipient])
                    msg.body = body
                    mail.send(msg)
                    email_sent = True
                    app.logger.info('[EMAIL SUCCESS] %s for %s sent to registered homeowner email %s',
                                    subject, related_id, recipient)
                    print(f"[EMAIL SUCCESS] {subject} for {related_id} sent to {recipient}")
                except Exception as exc:
                    email_error = f'{type(exc).__name__}: {exc}'
                    app.logger.warning('[EMAIL FAILURE] %s for %s to %s: %s',
                                       subject, related_id, recipient, email_error)
                    print(f"[EMAIL FAILURE] {subject} for {related_id} to {recipient}: {email_error}")
        elif not recipient:
            email_error = 'No registered homeowner email address is available.'
            app.logger.warning('[EMAIL FAILURE] %s for %s: homeowner has no registered email', subject, related_id)
        else:
            email_error = 'Email notifications are disabled for this account.'
    except Exception as exc:
        email_error = f'{type(exc).__name__}: {exc}'
        app.logger.warning('[EMAIL FAILURE] Recipient lookup for user_id=%s related_id=%s: %s',
                           user_id, related_id, email_error)

    # Record only channel attempts that were not already successfully logged.
    # Logging errors do not change the already-completed workflow action.
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if not email_already_sent:
                cursor.execute("""INSERT INTO notification_log
                    (user_id, channel, subject, body, recipient, related_type, related_id, dedupe_key, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                               (user_id, 'Email', subject, body,
                                user.get('email') if user else None,
                                related_type, related_id, dedupe_key,
                                'Sent' if email_sent else 'Failed'))
            if not in_app_exists:
                cursor.execute("""INSERT INTO notification_log
                    (user_id, channel, subject, body, related_type, related_id, dedupe_key, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                               (user_id, 'In-App', subject, body,
                                related_type, related_id, dedupe_key, 'Sent'))
        conn.close()
    except Exception as exc:
        app.logger.warning('Notification log insert failed for user_id=%s related_id=%s: %s',
                           user_id, related_id, exc)

    return {'created': not in_app_exists, 'email_sent': email_sent,
            'reason': email_error, 'recipient': user.get('email') if user else None}


def _set_login_session(user):
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['full_name'] = f"{user['first_name']} {user['last_name']}".strip()
    session['role'] = user['role']
    session['email'] = user.get('email', '')
    session['last_activity'] = time.time()
    session.permanent = True


def _role_home_url(role):
    if role in ['Secretary', 'Treasurer', *EXECUTIVE_ROLES]:
        return url_for('officer')
    if role == 'Admin':
        return url_for('admin')
    return url_for('homeowner')


# Prevent stale login/protected HTML from being restored as a usable page via browser cache.
# Static assets keep their normal caching behavior.
@app.after_request
def set_auth_page_cache_headers(response):
    if request.endpoint in {'index', 'register_page', 'homeowner', 'officer', 'admin', 'logout'}:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


# ===========================================================================
# PAGE ROUTES
# ===========================================================================
@app.route('/')
def index():
    if 'user_id' in session:
        role = session.get('role', 'Homeowner')
        if role == 'Homeowner':
            return redirect(url_for('homeowner'))
        elif role in ['Secretary', 'Treasurer', *EXECUTIVE_ROLES]:
            return redirect(url_for('officer'))
        elif role == 'Admin':
            return redirect(url_for('admin'))
    return render_template('index.html')


@app.route('/register')
def register_page():
    if 'user_id' in session:
        role = session.get('role', 'Homeowner')
        if role == 'Homeowner':
            return redirect(url_for('homeowner'))
        elif role in ['Secretary', 'Treasurer', *EXECUTIVE_ROLES]:
            return redirect(url_for('officer'))
        elif role == 'Admin':
            return redirect(url_for('admin'))
    return render_template('register.html')


@app.route('/home')
def homeowner():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    if session.get('role') != 'Homeowner':
        return redirect(url_for('index'))
    return render_template('homeowner.html', treasurer=get_treasurer_contact())



# ---------------------------------------------------------------------------
# Officer route (queries homeowners for Treasurer dropdown)
# ---------------------------------------------------------------------------
@app.route('/officer')
def officer():
    if 'user_id' not in session or session.get('role') not in OFFICER_ROLES:
        return redirect(url_for('index'))

    role = session.get('role', 'Secretary')
    username = session.get('username')
    ready_release_requests = []

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, first_name, last_name, username,
                       CONCAT(first_name, ' ', last_name) AS full_name
                FROM users
                WHERE role = 'Homeowner' AND status = 'Active'
                ORDER BY last_name ASC, first_name ASC
            """)
            homeowners = cursor.fetchall()

            cursor.execute("SELECT item_name, amount, proposed_amount, status FROM fees ORDER BY item_name ASC")
            fees_rows = cursor.fetchall()
            fees = {
                row['item_name']: {
                    'amount': float(row['amount']),
                    'proposed_amount': float(row['proposed_amount']) if row.get('proposed_amount') is not None else None,
                    'status': row['status']
                } for row in fees_rows
            }
            official_monthly_due = fees.get('Monthly Dues', {}).get('amount', 100.00)

            cursor.execute("SELECT * FROM service_types")
            service_rows = cursor.fetchall()
            service_map = {row['service_name']: row for row in service_rows}

            cursor.execute("""
                SELECT r.id, CONCAT(u.first_name,' ',u.last_name) AS homeowner, u.block, u.lot, r.request_type,
                       r.category, r.submission_type, r.assigned_officer, r.status, r.payment_status, r.fee,
                       r.details, r.attachment, r.remarks, r.purpose, r.purpose_details,
                       r.payment_reference, r.official_receipt_no, r.payment_date, r.ready_for_release_at,
                       r.issued_at, r.issued_by, r.sticker_valid_from, r.sticker_valid_until,
                       DATE_FORMAT(r.date_submitted,'%Y-%m-%d %H:%i') AS date_submitted,
                       DATE_FORMAT(r.last_updated,'%Y-%m-%d %H:%i') AS last_updated, r.user_id
                FROM requests r JOIN users u ON r.user_id=u.id
                ORDER BY r.date_submitted DESC
            """)
            all_requests = cursor.fetchall()

            cursor.execute("SELECT * FROM payment_transactions ORDER BY created_at DESC")
            payment_tx_rows = cursor.fetchall()
            payment_tx_map = {row['request_id']: row for row in payment_tx_rows}

            # Cache dues once per homeowner rather than once per request.
            dues_cache = {}
            for uid in {r['user_id'] for r in all_requests}:
                dues_cache[uid] = dues_summary(cursor, uid)
            for req in all_requests:
                req['dues'] = dues_cache.get(req['user_id'], {'unpaid_months':0,'outstanding_balance':0,'dues_status':'Clear'})
                req['service_config'] = service_map.get(req['request_type'])
                req['payment_tx'] = payment_tx_map.get(req['id'])

            active_requests = [r for r in all_requests if r['status'] != 'Cancelled']

            def actionable_for_role(req):
                if req.get('status') not in {'Submitted','Under Review'}:
                    return False
                cfg = service_map.get(req.get('request_type'))
                allowed = service_reviewer_roles(cfg, req.get('request_type'))
                if role == 'Admin':
                    return True
                if role not in allowed:
                    return False
                if cfg and cfg.get('initial_reviewer') == 'Shared' and req.get('assigned_officer') in {'Secretary','Treasurer'}:
                    return role == req.get('assigned_officer')
                return True

            checking_requests = [r for r in active_requests if actionable_for_role(r)]
            if role == 'Secretary':
                assigned_requests = checking_requests
            elif role == 'Treasurer':
                assigned_requests = [r for r in active_requests
                                     if r['status'] in ('Ready for Payment & Release','Awaiting Payment Verification','Approved')
                                     and r['payment_status'] == 'Unpaid']
            elif role in EXECUTIVE_ROLES:
                assigned_requests = [r for r in active_requests if r['status'] == 'Pending Executive Approval']
            else:
                assigned_requests = active_requests

            if role in {'Secretary','Treasurer','Admin'}:
                for req in active_requests:
                    if req['status'] != 'Ready for Release':
                        continue
                    cfg = service_map.get(req['request_type'])
                    if role == 'Admin' or role in release_roles_for_request(cfg, req.get('assigned_officer')):
                        ready_release_requests.append(req)

            cursor.execute("SELECT id, CONCAT(first_name,' ',last_name) AS full_name, username, email, mobile, role, status, profile_image, e_signature_path, e_signature_updated_at, created_at FROM users WHERE id=%s", (session['user_id'],))
            current_user = cursor.fetchone()
            if role in EXECUTIVE_ROLES:
                cursor.execute("SELECT * FROM audit_logs WHERE role IN ('President','Vice President','Board Director') ORDER BY timestamp ASC")
            else:
                cursor.execute("SELECT * FROM audit_logs WHERE role=%s OR username=%s ORDER BY timestamp ASC", (role, username))
            audits_rows = cursor.fetchall()

            concerns = []
            if role in ('Secretary','Treasurer'):
                cursor.execute("""SELECT c.*, r.request_type, r.assigned_officer, u.block, u.lot,
                                  CONCAT(u.first_name,' ',u.last_name) AS homeowner
                           FROM request_concerns c
                           JOIN requests r ON r.id=c.request_id
                           JOIN users u ON u.id=c.user_id
                           WHERE c.assigned_role=%s
                           ORDER BY c.updated_at DESC""", (role,))
                concerns = cursor.fetchall()

            executive_history = []
            if role in EXECUTIVE_ROLES:
                cursor.execute("""SELECT r.id, r.request_type, r.status, r.executive_decision_reason,
                                  r.executive_decision_details, r.executive_decided_by,
                                  r.executive_decided_role, r.executive_decided_at, u.block, u.lot,
                                  CONCAT(u.first_name,' ',u.last_name) AS homeowner
                           FROM requests r JOIN users u ON u.id=r.user_id
                           WHERE r.executive_decided_at IS NOT NULL
                           ORDER BY r.executive_decided_at DESC LIMIT 50""")
                executive_history = cursor.fetchall()

            cursor.execute("""SELECT dt.*, s.fee AS service_fee, s.requires_payment AS service_requires_payment,
                                      s.form_template AS service_form_template
                              FROM document_templates dt
                              JOIN (SELECT template_key, MAX(version_no) AS max_version
                                    FROM document_templates WHERE status='Active' GROUP BY template_key) x
                                ON x.template_key=dt.template_key AND x.max_version=dt.version_no
                              LEFT JOIN service_types s ON s.service_name=dt.service_name
                              WHERE dt.status='Active' AND dt.template_key NOT IN ('service-invoice','ack-receipt','dues-commitment') ORDER BY dt.title""")
            document_templates = cursor.fetchall()
            for _t in document_templates:
                _t['display_body_text'] = template_text_to_display(_t.get('body_text'))
                _t['display_footer_text'] = template_text_to_display(_t.get('footer_text'))

            financial_adjustments = []
            if role in ['Treasurer', 'Admin']:
                cursor.execute("SELECT * FROM financial_adjustments ORDER BY created_at DESC")
                financial_adjustments = cursor.fetchall()
        conn.close()
    except Exception as e:
        app.logger.exception('Officer route error')
        return render_template('officer.html', role=role, requests=[], all_requests=[], assigned_requests=[], checking_requests=[],
                               ready_release_requests=[], fees={}, audits=[], financial_adjustments=[], homeowners=[],
                               official_monthly_due=100.00, treasurer=get_treasurer_contact(), current_user=None,
                               concerns=[], executive_history=[], document_templates=[])

    if 'full_name' not in session:
        session['full_name'] = username

    return render_template('officer.html', role=role, requests=all_requests, all_requests=all_requests,
                           assigned_requests=assigned_requests, checking_requests=checking_requests,
                           ready_release_requests=ready_release_requests, fees=fees, audits=audits_rows,
                           financial_adjustments=financial_adjustments, homeowners=homeowners,
                           official_monthly_due=official_monthly_due, treasurer=get_treasurer_contact(),
                           current_user=current_user, concerns=concerns, executive_history=executive_history,
                           document_templates=document_templates)


# ---------------------------------------------------------------------------
# Admin route

# ---------------------------------------------------------------------------
@app.route('/admin')
def admin():
    if 'user_id' not in session or session.get('role') != 'Admin':
        flash('Unauthorized access. Admin privileges required.', 'error')
        return redirect(url_for('index'))

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM users WHERE role='Homeowner'")
            total_homeowners = cursor.fetchone()['total']
            cursor.execute("SELECT COUNT(*) AS total FROM requests")
            total_requests = cursor.fetchone()['total']
            cursor.execute(
                "SELECT COUNT(*) AS total FROM requests WHERE status IN ('Submitted','Under Review','Checked','For Correction')")
            pending_requests = cursor.fetchone()['total']
            cursor.execute("SELECT COUNT(*) AS total FROM requests WHERE status='Pending Executive Approval'")
            pending_executive = cursor.fetchone()['total']
            cursor.execute(
                "SELECT COUNT(*) AS total FROM users WHERE role IN ('Secretary','Treasurer','President','Vice President','Board Director') AND status='Active'")
            active_officers = cursor.fetchone()['total']
            cursor.execute("SELECT COUNT(*) AS total FROM fees WHERE status='Pending Executive Approval'")
            pending_fee_changes = cursor.fetchone()['total']

            cursor.execute("SELECT * FROM audit_logs WHERE role IN ('Admin','Secretary','Treasurer','President','Vice President','Board Director') ORDER BY timestamp ASC")
            audits_rows = cursor.fetchall()

            cursor.execute("""
                SELECT id, CONCAT(first_name,' ',last_name) AS full_name, username, email, mobile, role, status, created_at
                FROM users WHERE role IN ('Secretary','Treasurer','President','Vice President','Board Director') ORDER BY created_at DESC
            """)
            officers = cursor.fetchall()

            cursor.execute("""
                SELECT r.id, CONCAT(u.first_name,' ',u.last_name) AS homeowner, u.block, u.lot, r.request_type, r.category,
                       r.submission_type, r.assigned_officer, r.status, r.payment_status,
                       DATE_FORMAT(r.date_submitted,'%Y-%m-%d %H:%i') AS date_submitted,
                       DATE_FORMAT(r.last_updated,'%Y-%m-%d %H:%i') AS last_updated
                FROM requests r JOIN users u ON r.user_id=u.id ORDER BY r.date_submitted DESC
            """)
            requests_list = cursor.fetchall()

            cursor.execute("SELECT item_name, amount, proposed_amount, status FROM fees ORDER BY item_name ASC")
            fees_rows = cursor.fetchall()
            fees = {
                row['item_name']: {
                    'amount': float(row['amount']),
                    'proposed_amount': float(row['proposed_amount']) if row.get(
                        'proposed_amount') is not None else None,
                    'status': row['status']
                } for row in fees_rows
            }

            cursor.execute("SELECT * FROM requests WHERE status='Cancelled' ORDER BY last_updated DESC")
            cancellations = cursor.fetchall()
            cursor.execute("SELECT * FROM financial_adjustments ORDER BY created_at DESC")
            financial_adjustments = cursor.fetchall()
            cursor.execute("SELECT * FROM notification_log ORDER BY sent_at DESC LIMIT 200")
            notifications_log = cursor.fetchall()
            cursor.execute("SELECT id, CONCAT(first_name,' ',last_name) AS full_name, username, email, mobile, role, status, profile_image, e_signature_path, e_signature_updated_at, created_at FROM users WHERE id=%s", (session['user_id'],))
            current_user = cursor.fetchone()
            cursor.execute("""SELECT id, homeowner_public_id, CONCAT(first_name,' ',last_name) AS full_name,
                              email, mobile, block, lot, address_line, verification_status, created_at
                              FROM users WHERE role='Homeowner' AND verification_status='Pending Verification'
                              ORDER BY created_at ASC""")
            pending_homeowners = cursor.fetchall()
            cursor.execute("SELECT * FROM service_types ORDER BY category, service_name")
            service_types = cursor.fetchall()
            cursor.execute("""SELECT dt.* FROM document_templates dt
                              JOIN (SELECT template_key, MAX(version_no) AS max_version
                                    FROM document_templates WHERE status='Active' GROUP BY template_key) x
                                ON x.template_key=dt.template_key AND x.max_version=dt.version_no
                              WHERE dt.status='Active' AND dt.template_key NOT IN ('service-invoice','ack-receipt','dues-commitment') ORDER BY dt.title""")
            document_templates = cursor.fetchall()
            for _t in document_templates:
                _t['display_body_text'] = template_text_to_display(_t.get('body_text'))
                _t['display_footer_text'] = template_text_to_display(_t.get('footer_text'))
            cursor.execute("SELECT status, COUNT(*) AS total FROM requests GROUP BY status ORDER BY status")
            report_status_summary = cursor.fetchall()
            cursor.execute("""SELECT r.request_type,
                       COUNT(*) AS completed_requests,
                       ROUND(AVG(TIMESTAMPDIFF(MINUTE, r.date_submitted, r.issued_at))/60, 2) AS avg_total_hours,
                       ROUND(AVG(TIMESTAMPDIFF(MINUTE, r.date_submitted, h.reviewed_at))/60, 2) AS avg_review_hours,
                       ROUND(AVG(CASE WHEN r.executive_decided_at IS NOT NULL AND r.ready_for_release_at IS NOT NULL
                                      THEN TIMESTAMPDIFF(MINUTE, r.executive_decided_at, r.ready_for_release_at) END)/60, 2) AS avg_approval_to_release_hours
                FROM requests r
                LEFT JOIN (
                    SELECT request_id, MIN(created_at) AS reviewed_at
                    FROM request_history WHERE action='Initial Review' GROUP BY request_id
                ) h ON h.request_id=r.id
                WHERE r.status='Issued / Completed' AND r.issued_at IS NOT NULL
                GROUP BY r.request_type ORDER BY r.request_type""")
            report_processing_summary = cursor.fetchall()
            cursor.execute("""SELECT COUNT(*) AS completed_requests,
                       ROUND(AVG(TIMESTAMPDIFF(MINUTE,date_submitted,issued_at))/60,2) AS avg_total_hours
                       FROM requests WHERE status='Issued / Completed' AND issued_at IS NOT NULL""")
            report_processing_overall = cursor.fetchone() or {'completed_requests':0,'avg_total_hours':None}

        conn.close()
    except Exception as e:
        print(f"Admin Route Error: {e}")
        return render_template('admin.html',
                               metrics={'total_homeowners': 0, 'total_requests': 0, 'pending_requests': 0,
                                        'pending_executive': 0, 'active_officers': 0, 'pending_fee_changes': 0},
                               audits=[], officers=[], requests=[], fees={}, cancellations=[],
                               financial_adjustments=[], notifications_log=[], current_user=None, pending_homeowners=[], service_types=[], document_templates=[], report_status_summary=[], report_processing_summary=[], report_processing_overall={'completed_requests':0,'avg_total_hours':None})

    if 'full_name' not in session:
        session['full_name'] = session.get('username', 'System Admin')

    metrics = {
        'total_homeowners': total_homeowners, 'total_requests': total_requests,
        'pending_requests': pending_requests, 'pending_executive': pending_executive,
        'active_officers': active_officers, 'pending_fee_changes': pending_fee_changes
    }

    return render_template('admin.html', metrics=metrics, audits=audits_rows,
                           officers=officers, requests=requests_list, fees=fees,
                           cancellations=cancellations, financial_adjustments=financial_adjustments,
                           notifications_log=notifications_log, current_user=current_user, pending_homeowners=pending_homeowners,
                           service_types=service_types, document_templates=document_templates,
                           report_status_summary=report_status_summary, report_processing_summary=report_processing_summary,
                           report_processing_overall=report_processing_overall)


# ===========================================================================
# OFFICER WORKFLOW ENDPOINTS
# ===========================================================================
@app.route('/officer/check_request/<req_id>', methods=['POST'])
def check_request(req_id):
    if 'user_id' not in session or session.get('role') not in ['Secretary', 'Treasurer', 'Admin']:
        return redirect(url_for('index'))

    role = session.get('role')
    action = request.form.get('action')
    remarks = request.form.get('remarks', '').strip()
    if action not in ('check', 'correction', 'reject'):
        flash('Invalid request review action. Please try again.', 'error')
        return redirect(url_for('officer'))
    rejection_reason=(request.form.get('rejection_reason') or '').strip()
    rejection_details=(request.form.get('rejection_details') or '').strip()
    if action == 'correction' and not remarks:
        flash('Officer Remarks are required when returning a request for correction.', 'error')
        return redirect(url_for('officer'))
    allowed_reject_reasons={'Invalid supporting document','Request information is inconsistent','Applicant is not eligible for the requested service','Duplicate request','Incorrect property information','HOA policy restriction','Other'}
    if action=='reject':
        if rejection_reason not in allowed_reject_reasons:
            flash('Select a valid rejection reason.', 'error'); return redirect(url_for('officer'))
        if rejection_reason=='Other' and not rejection_details:
            flash('Please provide the rejection reason.', 'error'); return redirect(url_for('officer'))

    correction_delivery = None
    homeowner_notice = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""SELECT status, request_type, user_id, assigned_officer, fee, payment_status
                              FROM requests WHERE id=%s""", (req_id,))
            prev_req = cursor.fetchone()
            if not prev_req:
                conn.close(); return redirect(url_for('officer'))
            prev_status = prev_req['status']
            if prev_status == 'Cancelled':
                conn.close(); flash('Cannot process a cancelled request.', 'error'); return redirect(url_for('officer'))
            if prev_status not in ('Submitted', 'Under Review'):
                conn.close(); flash('This request is no longer available for initial officer checking.', 'error'); return redirect(url_for('officer'))

            service = get_service_config(cursor, prev_req['request_type'], active_only=False)
            allowed = service_reviewer_roles(service, prev_req['request_type'])
            if role != 'Admin' and role not in allowed:
                conn.close(); flash(f'{role} is not authorized to perform the initial checking for {prev_req["request_type"]}.', 'error'); return redirect(url_for('officer'))
            if role != 'Admin' and service and service.get('initial_reviewer') == 'Shared' and prev_req.get('assigned_officer') in {'Secretary','Treasurer'} and role != prev_req.get('assigned_officer'):
                conn.close(); flash('This shared request is already assigned to another initial reviewer.', 'error'); return redirect(url_for('officer'))

            if role in {'Secretary', 'Treasurer'}:
                checker = role
            elif prev_req.get('assigned_officer') in {'Secretary', 'Treasurer'}:
                checker = prev_req.get('assigned_officer')
            elif service and service.get('initial_reviewer') == 'Treasurer':
                checker = 'Treasurer'
            else:
                # Admin override on a Secretary/Shared service retains a stable
                # operational owner for correction and release routing.
                checker = 'Secretary'
            ready_now = False
            if action == 'correction':
                new_status = 'For Correction'
            elif action == 'reject':
                new_status = 'Rejected'
                remarks = rejection_details if rejection_reason == 'Other' else rejection_reason
            else:
                requires_executive = True if not service else bool(service.get('requires_executive'))
                requires_payment = bool(float(prev_req.get('fee') or 0) > 0) if not service else bool(service.get('requires_payment')) and float(prev_req.get('fee') or 0) > 0
                requires_dues = True if not service else bool(service.get('requires_dues_clearance'))
                dues = dues_summary(cursor, prev_req['user_id']) if requires_dues else {'unpaid_months': 0}
                if requires_executive:
                    new_status = 'Pending Executive Approval'
                elif dues.get('unpaid_months', 0) > 0:
                    new_status = 'Awaiting Dues Clearance'
                    homeowner_notice = ('Request Waiting for Dues Clearance',
                        f"Your {prev_req['request_type']} request {req_id} passed initial review but cannot proceed until outstanding monthly dues are cleared.")
                elif requires_payment:
                    new_status = 'Ready for Payment & Release'
                    homeowner_notice = ('Request Ready for Payment & Release',
                        f"Your {prev_req['request_type']} request {req_id} passed review. The document is being prepared and may be paid for and released during one face-to-face visit with the Treasurer.")
                else:
                    new_status = 'Ready for Release'
                    ready_now = True
                    homeowner_notice = ('Request Ready for Release',
                        f"Your {prev_req['request_type']} request {req_id} passed review and is now Ready for Release.")

            sql = "UPDATE requests SET status=%s, remarks=%s, assigned_officer=%s"
            params = [new_status, remarks, checker]
            if ready_now:
                sql += ", ready_for_release_at=NOW(), payment_status='Not Required'"
            sql += " WHERE id=%s AND status IN ('Submitted','Under Review')"
            params.append(req_id)
            cursor.execute(sql, tuple(params))
            if action == 'check':
                assign_request_document_template(cursor, req_id, prev_req['request_type'])
            if cursor.rowcount != 1:
                conn.close(); flash('This request was already processed by another officer. Refresh the queue to see its current status.', 'warning'); return redirect(url_for('officer'))
        conn.close()

        record_request_history(req_id, 'Initial Review' if action == 'check' else ('Returned for Correction' if action == 'correction' else 'Officer Rejected'), prev_status, new_status, remarks)
        log_audit_action(session.get('username'), session.get('role'),
                         f'Request {action.title()}', req_id, prev_status, new_status)

        if new_status == 'For Correction':
            homeowner_name = 'Homeowner'
            try:
                c2 = get_db_connection()
                with c2.cursor() as cursor:
                    cursor.execute("SELECT first_name, last_name FROM users WHERE id=%s", (prev_req['user_id'],))
                    homeowner = cursor.fetchone()
                c2.close()
                if homeowner:
                    homeowner_name = f"{homeowner.get('first_name') or ''} {homeowner.get('last_name') or ''}".strip() or 'Homeowner'
            except Exception as exc:
                app.logger.warning('Could not resolve homeowner name for correction email %s: %s', req_id, exc)
            subject = f'NFH-HOA Request Requires Correction — {req_id}'
            body = (
                f"Dear {homeowner_name},\n\nYour {prev_req['request_type']} request ({req_id}) requires correction.\n\n"
                f"Status: For Correction\nOfficer Remarks: {remarks}\n\n"
                "Please sign in to the NFH-HOA system to review the remarks, make the necessary corrections, and resubmit your request.\n\n"
                "Thank you,\nNorth Fairway Homes Homeowners' Association"
            )
            correction_delivery = send_alert_and_email(prev_req['user_id'], subject, body,
                related_type='request', related_id=req_id,
                dedupe_key=f"correction-{req_id}-{prev_status}-{int(time.time())}")
        elif new_status == 'Rejected':
            body=(f"Your {prev_req['request_type']} request {req_id} was rejected during officer review.\n\nReason: {remarks}\n\nPlease sign in to the NFH-HOA portal to review the recorded decision.")
            send_alert_and_email(prev_req['user_id'], f'NFH-HOA Request Rejected — {req_id}', body, related_type='request', related_id=req_id, dedupe_key=f'officer-reject-{req_id}-{int(time.time())}')
        elif homeowner_notice:
            send_alert_and_email(prev_req['user_id'], homeowner_notice[0], homeowner_notice[1],
                                 related_type='request', related_id=req_id,
                                 dedupe_key=f"initial-review-{req_id}-{new_status}")

        flash(f'Request {req_id} updated to {new_status}.', 'success')
        if correction_delivery and not correction_delivery.get('email_sent'):
            flash('The homeowner was notified in the system, but the correction email could not be delivered. Please check the email/SMTP configuration.', 'warning')
    except Exception:
        app.logger.exception('Initial review failed for %s', req_id)
        flash('Unable to update the request. Please try again.', 'error')
    return redirect(url_for('officer'))


@app.route('/officer/update_payment/<req_id>', methods=['POST'])
def update_payment(req_id):
    """Record face-to-face cash payment and optionally confirm document release in the same visit."""
    if 'user_id' not in session or session.get('role') not in ['Treasurer','Admin']:
        return redirect(url_for('index'))
    payment_date=(request.form.get('payment_date') or '').strip() or datetime.now().strftime('%Y-%m-%d')
    or_no=(request.form.get('official_receipt_no') or '').strip()
    release_now=str(request.form.get('document_released') or '').lower() in {'1','true','yes','on'}
    remarks=(request.form.get('release_remarks') or '').strip()
    sticker_from=(request.form.get('sticker_valid_from') or '').strip()
    sticker_until=(request.form.get('sticker_valid_until') or '').strip()
    try:
        paid_date=datetime.strptime(payment_date,'%Y-%m-%d').date()
    except ValueError:
        flash('Enter a valid payment date.','error')
        return redirect(url_for('officer') + '#payments')

    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""SELECT r.*, CONCAT(u.first_name,' ',u.last_name) AS homeowner
                              FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=%s""",(req_id,))
            req=cursor.fetchone()
            if not req:
                conn.close(); flash('Request not found.','error'); return redirect(url_for('officer') + '#payments')
            if req['payment_status'] == 'Paid':
                conn.close(); flash('Payment for this request has already been recorded.','warning'); return redirect(url_for('officer') + '#payments')
            if req['status'] not in ('Ready for Payment & Release','Awaiting Payment Verification','Approved'):
                conn.close(); flash('This request is not yet ready for face-to-face payment and release.','error'); return redirect(url_for('officer') + '#payments')
            service=get_service_config(cursor,req['request_type'],active_only=False)
            requires_payment=(float(req.get('fee') or 0)>0) if not service else (bool(service.get('requires_payment')) and float(req.get('fee') or 0)>0)
            if not requires_payment:
                conn.close(); flash('This request does not require a payment transaction.','error'); return redirect(url_for('officer') + '#payments')
            assign_request_document_template(cursor, req_id, req['request_type'])

            valid_from=None; valid_until=None
            is_vehicle = bool(service and service.get('form_template')=='vehicle-sticker') or req['request_type']=='Vehicle Sticker Application'
            if release_now and is_vehicle:
                if not sticker_from or not sticker_until:
                    conn.close(); flash('Enter the vehicle sticker validity dates before confirming release.','error'); return redirect(url_for('officer') + '#payments')
                try:
                    valid_from=datetime.strptime(sticker_from,'%Y-%m-%d').date()
                    valid_until=datetime.strptime(sticker_until,'%Y-%m-%d').date()
                except ValueError:
                    conn.close(); flash('Enter valid sticker validity dates.','error'); return redirect(url_for('officer') + '#payments')
                if valid_until < valid_from:
                    conn.close(); flash('Sticker expiration date cannot be earlier than its start date.','error'); return redirect(url_for('officer') + '#payments')

            receiver=session.get('full_name') or session.get('username') or 'Treasurer'
            cursor.execute("""INSERT INTO payment_transactions
                (request_id,user_id,amount,payment_method,official_receipt_no,payment_date,received_by,
                 document_released,released_by,released_at,remarks)
                VALUES (%s,%s,%s,'Cash',%s,%s,%s,%s,%s,%s,%s)""",
                (req_id,req['user_id'],float(req.get('fee') or 0),or_no or None,paid_date,receiver,
                 1 if release_now else 0, receiver if release_now else None,
                 datetime.now() if release_now else None, remarks or None))
            tx_id=cursor.lastrowid
            system_ref=_ensure_payment_reference(cursor, tx_id)
            new_status='Issued / Completed' if release_now else 'Paid - Awaiting Release'
            cursor.execute("""UPDATE requests SET payment_status='Paid',status=%s,
                              payment_reference=%s,official_receipt_no=%s,payment_date=%s,
                              ready_for_release_at=COALESCE(ready_for_release_at,NOW()),
                              issued_at=%s,issued_by=%s,sticker_valid_from=%s,sticker_valid_until=%s
                              WHERE id=%s""",
                           (new_status,system_ref,or_no or None,paid_date,
                            datetime.now() if release_now else None, receiver if release_now else None,
                            valid_from,valid_until,req_id))
            if release_now:
                template=get_active_document_template(cursor,request_type=req['request_type'])
                snapshot_request_signatures(cursor,req_id,template)
        conn.close()

        record_request_history(req_id,'Cash Payment Received',req['status'],new_status,
                               f'Cash payment ₱{float(req.get("fee") or 0):.2f}; Reference {system_ref}.')
        log_audit_action(session.get('username'),session.get('role'),'Record Cash Payment',req_id,req['status'],new_status)
        if release_now:
            release_note='Document physically handed to homeowner during face-to-face payment.'
            if remarks: release_note += f' {remarks}'
            record_request_history(req_id,'Document Released',new_status,'Issued / Completed',release_note)
            log_audit_action(session.get('username'),session.get('role'),'Confirm Payment & Release',req_id,'Payment Received','Issued / Completed')
            send_in_app_notification(req['user_id'],'Payment and Release Completed',
                f'Your {req["request_type"]} request {req_id} was paid and physically released. Reference: {system_ref}.',
                related_type='request',related_id=req_id,dedupe_key=f'pay-release-{req_id}')
            flash(f'Payment recorded and document released. Reference {system_ref}.','success')
        else:
            send_in_app_notification(req['user_id'],'Payment Recorded',
                f'Your cash payment for {req["request_type"]} request {req_id} was recorded. The document is still awaiting physical release.',
                related_type='request',related_id=req_id,dedupe_key=f'payment-only-{req_id}')
            flash(f'Payment recorded. Request is now Paid - Awaiting Release. Reference {system_ref}.','success')
    except pymysql.IntegrityError:
        app.logger.exception('Duplicate payment transaction for %s', req_id)
        flash('A payment transaction for this request already exists. Refresh the queue before trying again.','warning')
    except Exception:
        app.logger.exception('Face-to-face payment/release failed for %s', req_id)
        flash('Unable to record the payment/release right now. Please try again.','error')
    return redirect(url_for('officer') + '#payments')


@app.route('/officer/complete_release/<req_id>', methods=['POST'])
def complete_paid_release(req_id):
    """Confirm physical hand-over when payment was recorded earlier."""
    if 'user_id' not in session or session.get('role') not in ['Treasurer','Admin','Secretary']:
        return redirect(url_for('index'))
    remarks=(request.form.get('release_remarks') or '').strip()
    sticker_from=(request.form.get('sticker_valid_from') or '').strip()
    sticker_until=(request.form.get('sticker_valid_until') or '').strip()
    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM requests WHERE id=%s",(req_id,)); req=cursor.fetchone()
            if not req or req['status']!='Paid - Awaiting Release' or req['payment_status']!='Paid':
                conn.close(); flash('This request is not waiting for release confirmation.','error'); return redirect(url_for('officer')+'#payments')
            service=get_service_config(cursor,req['request_type'],active_only=False)
            valid_from=None; valid_until=None
            is_vehicle=bool(service and service.get('form_template')=='vehicle-sticker') or req['request_type']=='Vehicle Sticker Application'
            if is_vehicle:
                if not sticker_from or not sticker_until:
                    conn.close(); flash('Sticker validity dates are required before release.','error'); return redirect(url_for('officer')+'#payments')
                valid_from=datetime.strptime(sticker_from,'%Y-%m-%d').date(); valid_until=datetime.strptime(sticker_until,'%Y-%m-%d').date()
                if valid_until < valid_from:
                    conn.close(); flash('Sticker expiration date cannot be earlier than its start date.','error'); return redirect(url_for('officer')+'#payments')
            releaser=session.get('full_name') or session.get('username') or session.get('role')
            cursor.execute("""UPDATE requests SET status='Issued / Completed',issued_at=NOW(),issued_by=%s,
                              sticker_valid_from=%s,sticker_valid_until=%s WHERE id=%s""",
                           (releaser,valid_from,valid_until,req_id))
            cursor.execute("""UPDATE payment_transactions SET document_released=1,released_by=%s,released_at=NOW(),
                              remarks=CONCAT_WS(' | ',remarks,%s) WHERE request_id=%s""",(releaser,remarks or None,req_id))
            template=get_active_document_template(cursor,request_type=req['request_type'])
            snapshot_request_signatures(cursor,req_id,template)
        conn.close()
        record_request_history(req_id,'Document Released','Paid - Awaiting Release','Issued / Completed',remarks or 'Physical document handed to homeowner.')
        log_audit_action(session.get('username'),session.get('role'),'Confirm Document Release',req_id,'Paid - Awaiting Release','Issued / Completed')
        send_in_app_notification(req['user_id'],'Document Released',
            f'Your {req["request_type"]} request {req_id} has been physically released and is now completed.',
            related_type='request',related_id=req_id,dedupe_key=f'release-after-payment-{req_id}')
        flash('Document release confirmed. Request is now Issued / Completed.','success')
    except Exception:
        app.logger.exception('Paid release confirmation failed for %s',req_id)
        flash('Unable to confirm release right now.','error')
    return redirect(url_for('officer')+'#payments')


@app.route('/officer/propose_fee', methods=['POST'])
def propose_fee():
    if 'user_id' not in session or session.get('role') not in ['Treasurer', 'Admin']:
        return redirect(url_for('index'))
    fee_name = (request.form.get('fee_name') or '').strip()
    amount_raw = (request.form.get('amount') or '').strip()
    try:
        amount = float(amount_raw)
        if amount < 0:
            raise ValueError
    except (TypeError, ValueError):
        flash('Enter a valid non-negative proposed fee.', 'error')
        return redirect(url_for('officer'))
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT amount FROM fees WHERE item_name=%s", (fee_name,))
            row = cursor.fetchone()
            if not row:
                conn.close(); flash('Fee item not found.', 'error'); return redirect(url_for('officer'))
            prev = float(row['amount'])
            cursor.execute("UPDATE fees SET proposed_amount=%s, status='Pending Executive Approval' WHERE item_name=%s",
                           (amount, fee_name))
        conn.close()
        log_audit_action(session.get('username'), session.get('role'), 'Propose Fee Update', fee_name, prev, amount)
        flash(f'Proposed fee for {fee_name} submitted for Executive approval.', 'success')
    except Exception as e:
        app.logger.exception('Fee proposal failed')
        flash('Unable to submit the fee proposal right now.', 'error')
    return redirect(url_for('officer'))


@app.route('/officer/update-treasurer-contact', methods=['POST'])
def update_treasurer_contact():
    if 'user_id' not in session or session.get('role') not in ['Treasurer', 'Admin']:
        return redirect(url_for('index'))
    name = request.form.get('treasurer_name', '').strip()
    phone = request.form.get('treasurer_phone', '').strip()
    email = request.form.get('treasurer_email', '').strip()
    office_hours = request.form.get('treasurer_office_hours', '').strip()
    if not name or not phone:
        flash('Treasurer name and contact number are required.', 'error')
        return redirect(url_for('officer'))
    try:
        who = session.get('username')
        set_setting('treasurer_name', name, who)
        set_setting('treasurer_phone', phone, who)
        set_setting('treasurer_email', email, who)
        set_setting('treasurer_office_hours', office_hours, who)
        log_audit_action(who, session.get('role'), 'Update Treasurer Contact Info', 'hoa_settings', None, name)
        flash('Treasurer contact information updated.', 'success')
    except Exception as e:
        app.logger.exception('Treasurer contact update failed')
        flash('Unable to update the Treasurer contact information right now.', 'error')
    return redirect(url_for('officer'))


@app.route('/officer/president_action/<req_id>', methods=['POST'])
def president_action(req_id):
    if 'user_id' not in session or session.get('role') not in EXECUTIVE_ROLES + ['Admin']:
        return redirect(url_for('index'))
    action = request.form.get('action')
    reason = (request.form.get('rejection_reason') or '').strip()
    details = (request.form.get('rejection_details') or '').strip()
    if action not in ('approve', 'reject'):
        flash('Invalid Executive decision. Please try again.', 'error')
        return redirect(url_for('officer'))
    if action == 'reject' and reason not in ('Outstanding Dues','Invalid supporting document','Request information is inconsistent','Applicant is not eligible for the requested service','Duplicate request','Incorrect property information','HOA policy restriction','Other'):
        flash('Select a valid rejection reason.', 'error')
        return redirect(url_for('officer'))
    if action == 'reject' and reason == 'Other' and not details:
        flash('Please provide the reason for rejection.', 'error')
        return redirect(url_for('officer'))
    new_status = 'Rejected'
    mail_result = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""SELECT r.status, r.payment_status, r.request_type, r.user_id,
                              CONCAT(u.first_name,' ',u.last_name) AS homeowner, r.fee
                       FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=%s""", (req_id,))
            req = cursor.fetchone()
            if not req or req['status'] != 'Pending Executive Approval':
                conn.close(); flash('This request has already been decided. Refresh the page to view the latest status.', 'error'); return redirect(url_for('officer'))
            service = get_service_config(cursor, req['request_type'], active_only=False)
            requires_dues = True if not service else bool(service.get('requires_dues_clearance'))
            requires_payment = bool(float(req.get('fee') or 0) > 0) if not service else bool(service.get('requires_payment')) and float(req.get('fee') or 0) > 0
            dues = dues_summary(cursor, req['user_id']) if requires_dues else {'unpaid_months': 0}
            if action == 'reject' and reason == 'Outstanding Dues' and dues['unpaid_months'] <= 0:
                conn.close(); flash('This homeowner currently has no outstanding monthly dues. Choose Other if there is another rejection reason.', 'error'); return redirect(url_for('officer'))
            prev_status=req['status']
            if action == 'approve':
                if dues.get('unpaid_months', 0) > 0 and requires_dues:
                    new_status = 'Awaiting Dues Clearance'
                elif requires_payment:
                    new_status = 'Ready for Payment & Release'
                else:
                    new_status = 'Ready for Release'
                assign_request_document_template(cursor, req_id, req['request_type'])
            decision_note = details if reason == 'Other' else reason if action == 'reject' else None
            sql = """UPDATE requests SET status=%s, remarks=%s,
                              executive_decision_reason=%s, executive_decision_details=%s,
                              executive_decided_by=%s, executive_decided_role=%s, executive_decided_at=NOW()"""
            params = [new_status, decision_note, reason if action=='reject' else None,
                      details if action=='reject' else None, session.get('full_name') or session.get('username'),
                      session.get('role')]
            if action == 'approve' and not requires_payment and new_status == 'Ready for Release':
                sql += ", payment_status='Not Required', ready_for_release_at=NOW()"
            elif action == 'approve' and new_status == 'Ready for Payment & Release':
                sql += ", ready_for_release_at=NOW()"
            sql += " WHERE id=%s"
            params.append(req_id)
            cursor.execute(sql, tuple(params))
        conn.close()
        if action == 'approve':
            mail_result = send_approval_email(
                req_id, req['request_type'], req['user_id'],
                session.get('full_name') or session.get('username') or 'HOA Executive',
                session.get('role') or 'Executive'
            )
            if new_status == 'Ready for Release':
                send_alert_and_email(req['user_id'], 'Request Ready for Release',
                    f"Your {req['request_type']} request {req_id} has completed approval and is now Ready for Release.",
                    related_type='request', related_id=req_id, dedupe_key=f'ready-release-{req_id}')
            elif new_status == 'Ready for Payment & Release':
                send_alert_and_email(req['user_id'], 'Ready for Payment & Release',
                    f"Your {req['request_type']} request {req_id} is approved. Please visit the Treasurer for face-to-face payment and document release.",
                    related_type='request', related_id=req_id, dedupe_key=f'pay-release-ready-{req_id}')
            elif new_status == 'Awaiting Dues Clearance':
                send_alert_and_email(req['user_id'], 'Request Approved — Dues Clearance Required',
                    f"Your {req['request_type']} request {req_id} is approved but must clear outstanding monthly dues with the Treasurer before payment/release.",
                    related_type='request', related_id=req_id, dedupe_key=f'dues-clearance-{req_id}')
        else:
            reason_text = reason + (f"\nDetails: {details}" if details else '')
            body=(f"Dear {req['homeowner']},\n\nYour {req['request_type']} request ({req_id}) has been rejected.\n\n"
                  f"Reason: {reason_text}\n\nThis decision was recorded by {session.get('full_name') or session.get('username')}, {session.get('role')}.\n\n"
                  f"Please review your NFH-HOA account and the information shown in the system.\n\nThank you,\nNorth Fairway Homes Homeowners' Association")
            mail_result=send_alert_and_email(req['user_id'], f"NFH-HOA Request Rejected — {req_id}", body,
                                             related_type='request', related_id=req_id, dedupe_key=f'rejection-{req_id}')
        record_request_history(req_id, 'Executive Approved' if action=='approve' else 'Executive Rejected', prev_status, new_status, decision_note)
        log_audit_action(session.get('username'), session.get('role'), f'Executive Decision ({action.title()})', req_id,
                         prev_status, f"{new_status}" + (f" — {reason}: {details}" if action=='reject' and details else f" — {reason}" if action=='reject' else ''))
        flash('Request approved successfully.' if action == 'approve' else 'Request rejected successfully.', 'success')
        if mail_result and not mail_result.get('email_sent'):
            event_label = 'approval' if action == 'approve' else 'rejection'
            flash(f'The {event_label} was saved and the in-app notification was created, but the email could not be delivered.', 'warning')
    except Exception:
        app.logger.exception('Executive decision error for %s', req_id)
        flash('Unable to record the Executive decision. Please try again.', 'error')
    return redirect(url_for('officer'))


@app.route('/officer/president_fee_action', methods=['POST'])
def president_fee_action():
    if 'user_id' not in session or session.get('role') not in EXECUTIVE_ROLES + ['Admin']:
        return redirect(url_for('index'))
    fee_name = (request.form.get('fee_name') or '').strip()
    action = request.form.get('action')
    if action not in ('approve', 'reject'):
        flash('Invalid fee decision.', 'error')
        return redirect(url_for('officer'))
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT amount, proposed_amount, status FROM fees WHERE item_name=%s", (fee_name,))
            fee = cursor.fetchone()
            if not fee or fee.get('proposed_amount') is None:
                conn.close(); flash('No pending fee proposal was found for this item.', 'warning'); return redirect(url_for('officer'))
            proposed = float(fee['proposed_amount'])
            if action == 'approve':
                cursor.execute(
                    "UPDATE fees SET amount=%s, proposed_amount=NULL, status='Active' WHERE item_name=%s",
                    (proposed, fee_name))
                # service_types is the authoritative request configuration; keep
                # any matching service fee synchronized with the approved fee.
                cursor.execute("UPDATE service_types SET fee=%s WHERE service_name=%s", (proposed, fee_name))
                log_audit_action(session.get('username'), session.get('role'), 'Approve Fee Update', fee_name,
                                 fee['amount'], proposed)
                flash(f'Fee update for {fee_name} approved and activated.', 'success')
            else:
                cursor.execute("UPDATE fees SET proposed_amount=NULL, status='Active' WHERE item_name=%s", (fee_name,))
                log_audit_action(session.get('username'), session.get('role'), 'Reject Fee Update', fee_name,
                                 fee['proposed_amount'], 'Rejected')
                flash(f'Fee proposal for {fee_name} rejected.', 'info')
        conn.close()
    except Exception as e:
        app.logger.exception('Fee approval failed for %s', fee_name)
        flash('Unable to process the fee decision right now.', 'error')
    return redirect(url_for('officer'))


# ===========================================================================
# OWNER APIS

# ===========================================================================


# ===========================================================================
# ROLE-AWARE LIVE DASHBOARD METRICS (lightweight polling endpoint)
# ===========================================================================
@app.route('/api/live-metrics', methods=['GET'])
def live_metrics():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    role = session.get('role')
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            def count(sql, params=()):
                cursor.execute(sql, params)
                return int(cursor.fetchone()['total'] or 0)

            cursor.execute("SELECT service_name,initial_reviewer,release_role FROM service_types WHERE status='Active'")
            services = cursor.fetchall()
            secretary_types = [s['service_name'] for s in services if s['initial_reviewer'] in ('Secretary','Shared')]
            treasurer_types = [s['service_name'] for s in services if s['initial_reviewer'] in ('Treasurer','Shared')]

            if role == 'Admin':
                metrics = {
                    'total_homeowners': count("SELECT COUNT(*) total FROM users WHERE role='Homeowner'"),
                    'total_requests': count("SELECT COUNT(*) total FROM requests"),
                    'pending_requests': count("SELECT COUNT(*) total FROM requests WHERE status IN ('Submitted','Under Review','For Correction','Awaiting Dues Clearance')"),
                    'pending_executive': count("SELECT COUNT(*) total FROM requests WHERE status='Pending Executive Approval'"),
                    'active_officers': count("SELECT COUNT(*) total FROM users WHERE role IN ('Secretary','Treasurer','President','Vice President','Board Director') AND status='Active'"),
                    'pending_fee_changes': count("SELECT COUNT(*) total FROM fees WHERE status='Pending Executive Approval'"),
                    'approved_requests': count("SELECT COUNT(*) total FROM requests WHERE status IN ('Ready for Payment & Release','Paid - Awaiting Release','Ready for Release','Issued / Completed')"),
                    'paid_requests': count("SELECT COUNT(*) total FROM requests WHERE payment_status='Paid'"),
                    'missing_dues': count("SELECT COUNT(*) total FROM monthly_dues WHERE status='Unpaid'"),
                }
            elif role == 'Secretary':
                types = secretary_types or list(SECRETARY_FIRST_REQUEST_TYPES | SHARED_INITIAL_CHECK_REQUEST_TYPES)
                placeholders = ','.join(['%s']*len(types))
                base_params = tuple(types)
                metrics = {
                    'pending_requests': count(f"SELECT COUNT(*) total FROM requests WHERE status IN ('Submitted','Under Review') AND request_type IN ({placeholders}) AND (assigned_officer IS NULL OR assigned_officer IN ('Secretary',''))", base_params),
                    'for_correction': count(f"SELECT COUNT(*) total FROM requests WHERE status='For Correction' AND request_type IN ({placeholders}) AND (assigned_officer IS NULL OR assigned_officer='Secretary')", base_params),
                    'ready_for_release': count("SELECT COUNT(*) total FROM requests r LEFT JOIN service_types s ON s.service_name=r.request_type WHERE r.status='Ready for Release' AND (s.release_role='Secretary' OR s.release_role IS NULL OR (s.release_role='Initial Reviewer' AND (r.assigned_officer='Secretary' OR (r.assigned_officer IS NULL AND s.initial_reviewer IN ('Secretary','Shared')))))"),
                    'pending_executive': count("SELECT COUNT(*) total FROM requests WHERE status='Pending Executive Approval'"),
                    'recent_submitted': count("SELECT COUNT(*) total FROM requests WHERE status='Submitted' AND date_submitted >= NOW() - INTERVAL 7 DAY"),
                }
            elif role in EXECUTIVE_ROLES:
                metrics = {
                    'pending_request_approvals': count("SELECT COUNT(*) total FROM requests WHERE status='Pending Executive Approval'"),
                    'pending_fee_approvals': count("SELECT COUNT(*) total FROM fees WHERE status='Pending Executive Approval'"),
                    'recently_approved': count("SELECT COUNT(*) total FROM requests WHERE executive_decided_at IS NOT NULL AND executive_decision_reason IS NULL AND executive_decided_at >= NOW() - INTERVAL 7 DAY"),
                    'for_correction': count("SELECT COUNT(*) total FROM requests WHERE status='For Correction'"),
                }
            elif role == 'Treasurer':
                cursor.execute("SELECT COALESCE(SUM(amount),0) total FROM monthly_dues WHERE status='Unpaid'")
                outstanding = float(cursor.fetchone()['total'] or 0)
                types = treasurer_types or list(TREASURER_FIRST_REQUEST_TYPES | SHARED_INITIAL_CHECK_REQUEST_TYPES)
                placeholders = ','.join(['%s']*len(types))
                base_params = tuple(types)
                metrics = {
                    'requests_for_checking': count(f"SELECT COUNT(*) total FROM requests WHERE status IN ('Submitted','Under Review') AND request_type IN ({placeholders}) AND (assigned_officer IS NULL OR assigned_officer='Treasurer')", base_params),
                    'approved_unpaid': count("SELECT COUNT(*) total FROM requests WHERE status='Ready for Payment & Release' AND payment_status='Unpaid'"),
                    'paid_requests': count("SELECT COUNT(*) total FROM requests WHERE payment_status='Paid'"),
                    'missing_dues': count("SELECT COUNT(*) total FROM monthly_dues WHERE status='Unpaid'"),
                    'outstanding_dues_total': outstanding,
                    'recently_paid': count("SELECT COUNT(*) total FROM requests WHERE payment_status='Paid' AND last_updated >= NOW() - INTERVAL 7 DAY"),
                }
            else:
                metrics = {}
        conn.close()
        return jsonify({'status':'success','role':role,'metrics':metrics,'updated_at':datetime.now().strftime('%H:%M:%S')})
    except Exception as e:
        app.logger.exception('Live metrics error')
        return jsonify({'status':'error','message':'Unable to refresh dashboard metrics.'}), 500


@app.route('/api/user-data', methods=['GET'])
def get_user_data():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    user_id = session['user_id']
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            user = cursor.fetchone()
            cursor.execute("SELECT item_name, amount FROM fees WHERE status='Active'")
            fees_rows = cursor.fetchall()
            fees_dict = {f['item_name']: float(f['amount']) for f in fees_rows}
            cursor.execute("""SELECT id,service_name,category,fee,initial_reviewer,requires_executive,
                                      requires_payment,requires_dues_clearance,purpose_required,
                                      release_role,form_template,description,status
                               FROM service_types WHERE status='Active'
                               ORDER BY category,service_name""")
            service_rows = cursor.fetchall()
            services=[]
            for svc in service_rows:
                svc=dict(svc)
                svc['fee']=float(svc.get('fee') or 0)
                services.append(svc)
            cursor.execute("SELECT * FROM requests WHERE user_id=%s ORDER BY date_submitted DESC", (user_id,))
            requests_rows = cursor.fetchall()
            cursor.execute("""SELECT h.* FROM request_history h JOIN requests r ON r.id=h.request_id
                              WHERE r.user_id=%s ORDER BY h.created_at ASC""", (user_id,))
            history_by_request={}
            for hist in cursor.fetchall():
                hist['created_at']=hist['created_at'].strftime('%Y-%m-%d %H:%M') if hist.get('created_at') else ''
                history_by_request.setdefault(hist['request_id'],[]).append(hist)
            cursor.execute("SELECT * FROM alerts WHERE user_id=%s ORDER BY time DESC", (user_id,))
            alerts_rows = cursor.fetchall()
            cursor.execute("""SELECT * FROM monthly_dues WHERE user_id=%s AND status='Unpaid'
                              ORDER BY due_month ASC""", (user_id,))
            missing_dues_rows = cursor.fetchall()
            cursor.execute("SELECT * FROM payment_transactions WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
            payment_tx_rows = cursor.fetchall()
            payment_tx_map = {row['request_id']: row for row in payment_tx_rows}
        conn.close()

        if user:
            user.pop('password_hash', None)
            if user.get('profile_image'):
                user['profile_image'] = f"/static/uploads/{os.path.basename(user['profile_image'])}"
            if user.get('dob'):
                user['dob'] = user['dob'].strftime('%Y-%m-%d')
            if user.get('date_joined'):
                user['date_joined'] = user['date_joined'].strftime('%Y-%m-%d')
            user['emergency'] = {
                'name': user.get('emergency_contact_name'),
                'number': user.get('emergency_contact_number'),
                'relationship': user.get('emergency_contact_relationship'),
            }

        req_list, active_count, outstanding_request_fees = [], 0, 0.0
        for r in requests_rows:
            status = r['status']
            payment_status = r['payment_status']
            fee_val = float(r['fee'])
            if status in ['Submitted','Under Review','Checked','Pending Executive Approval','For Correction','Approved','Awaiting Dues Clearance','Awaiting Payment Verification','Ready for Payment & Release','Paid - Awaiting Release','Ready for Release']:
                active_count += 1
            if payment_status == 'Unpaid' and status not in ['Rejected', 'Cancelled', 'Issued / Completed']:
                outstanding_request_fees += fee_val
            req_list.append({
                'id': r['id'], 'type': r['request_type'], 'title': r['request_type'],
                'category': r.get('category') or 'Document Request',
                'date': r['date_submitted'].strftime('%Y-%m-%d %H:%M') if r.get('date_submitted') else '',
                'fee': f"₱{fee_val:.2f}", 'payment_status': payment_status, 'status': status,
                'remarks': r.get('remarks') or '', 'details': r.get('details') or '{}',
                'purpose': r.get('purpose') or '', 'history': history_by_request.get(r['id'], []),
                'issued_at': r.get('issued_at').strftime('%Y-%m-%d %H:%M') if r.get('issued_at') else '',
                'sticker_valid_from': r.get('sticker_valid_from').strftime('%Y-%m-%d') if r.get('sticker_valid_from') else '',
                'sticker_valid_until': r.get('sticker_valid_until').strftime('%Y-%m-%d') if r.get('sticker_valid_until') else '',
                'document_available': status == 'Issued / Completed',
                'payment_transaction': ({
                    'system_reference': payment_tx_map[r['id']].get('system_reference'),
                    'payment_date': payment_tx_map[r['id']].get('payment_date').strftime('%Y-%m-%d') if payment_tx_map[r['id']].get('payment_date') else '',
                    'official_receipt_no': payment_tx_map[r['id']].get('official_receipt_no') or '',
                    'document_released': bool(payment_tx_map[r['id']].get('document_released')),
                    'released_at': payment_tx_map[r['id']].get('released_at').strftime('%Y-%m-%d %H:%M') if payment_tx_map[r['id']].get('released_at') else ''
                } if r['id'] in payment_tx_map else None),
            })

        alert_list = [{'id': a['id'], 'text': a['text'],
                       'time': a['time'].strftime('%Y-%m-%d %H:%M') if a.get('time') else '',
                       'unread': bool(a['unread'])} for a in alerts_rows]
        unread_alerts_count = sum(1 for a in alert_list if a['unread'])

        missing_dues_total = sum(float(m['amount']) for m in missing_dues_rows)
        if missing_dues_rows:
            oldest = missing_dues_rows[0].get('due_month')
            today = datetime.now().date().replace(day=1)
            if isinstance(oldest, datetime): oldest = oldest.date()
            months_elapsed = max(1, (today.year-oldest.year)*12 + today.month-oldest.month + 1) if oldest else len(missing_dues_rows)
            years, rem = divmod(months_elapsed, 12)
            period_parts = ([f"{years} year{'s' if years != 1 else ''}"] if years else []) + ([f"{rem} month{'s' if rem != 1 else ''}"] if rem else [])
            outstanding_period = ' and '.join(period_parts) or '1 month'
            oldest_unpaid_due = oldest.strftime('%B %Y') if oldest else None
        else:
            outstanding_period = 'Clear'
            oldest_unpaid_due = None
        missing_dues_list = [{
            'due_month': m['due_month'].strftime('%Y-%m') if m.get('due_month') else '',
            'amount': f"₱{float(m['amount']):.2f}"
        } for m in missing_dues_rows]

        return jsonify({'status': 'success', 'user': user, 'fees': fees_dict, 'services': services, 'dashboard': {
            'active_requests': active_count,
            'outstanding_dues': missing_dues_total,
            'outstanding_request_fees': outstanding_request_fees,
            'unread_alerts': unread_alerts_count, 'requests': req_list, 'alerts': alert_list,
            'missing_dues_count': len(missing_dues_rows), 'missing_dues_total': missing_dues_total,
            'oldest_unpaid_due': oldest_unpaid_due, 'outstanding_period': outstanding_period,
            'dues_status': 'Outstanding' if missing_dues_rows else 'Clear', 'missing_dues': missing_dues_list
        }, 'treasurer': get_treasurer_contact()})
    except Exception as e:
        app.logger.exception('Homeowner data load failed')
        return jsonify({'status': 'error', 'message': 'Unable to load homeowner data right now.'}), 500


@app.route('/api/profile/update', methods=['POST'])
def update_profile():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    if session.get('role') != 'Homeowner':
        return jsonify({'status':'error','message':'Officer/Admin identity details are managed by the Administrator.'}),403
    data = request.get_json() or {}
    user_id = session['user_id']
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""UPDATE users SET
                first_name=%s, middle_name=%s, last_name=%s, suffix=%s,
                gender=%s, dob=%s, civil_status=%s, block=%s, lot=%s, address_line=%s,
                email=%s, mobile=%s, household=%s WHERE id=%s""",
                           (data.get('first_name'), data.get('middle_name'), data.get('last_name'),
                            data.get('suffix'), data.get('gender'), data.get('dob') or None,
                            data.get('civil_status'), data.get('block'), data.get('lot'), data.get('address_line'),
                            data.get('email'), data.get('mobile'), data.get('household', 1), user_id))
        conn.close()
        return jsonify({'status': 'success', 'message': 'Profile updated successfully.'})
    except Exception as e:
        app.logger.exception('Homeowner API operation failed')
        return jsonify({'status': 'error', 'message': 'Unable to complete this operation right now. Please try again.'}), 500


@app.route('/api/profile/photo', methods=['POST'])
def upload_profile_photo():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file uploaded.'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'status': 'error', 'message': 'No file selected.'}), 400

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        return jsonify({'status': 'error', 'message': 'File too large. Maximum 5MB.'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'status': 'error', 'message': 'Invalid format. Use JPG, JPEG, PNG, or WEBP.'}), 400
    detected=_detected_image_ext(file)
    normalized_declared='jpg' if ext in {'jpg','jpeg'} else ext
    if not detected or detected != normalized_declared:
        return jsonify({'status':'error','message':'The selected file is not a valid image of the chosen type.'}),400

    filename = secure_filename(f"{session['user_id']}_{int(time.time())}.{ext}")
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(save_path)

    image_url = f"/static/uploads/{filename}"
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET profile_image=%s WHERE id=%s", (image_url, session['user_id']))
        conn.close()
    except Exception as e:
        app.logger.exception('Homeowner API operation failed')
        return jsonify({'status': 'error', 'message': 'Unable to complete this operation right now. Please try again.'}), 500

    
    if session.get('role') in OFFICER_ROLES:
        log_audit_action(session.get('username'),session.get('role'),'Update Profile Picture','Account',None,image_url)
    return jsonify({'status': 'success', 'message': 'Profile picture updated.', 'image_url': image_url})


@app.route('/api/account/esign', methods=['POST'])
def upload_esignature():
    if 'user_id' not in session or session.get('role') not in OFFICER_ROLES:
        return jsonify({'status':'error','message':'Unauthorized'}),401
    if 'file' not in request.files or not request.files['file'].filename:
        return jsonify({'status':'error','message':'Select an e-signature image first.'}),400
    f=request.files['file']; ext=f.filename.rsplit('.',1)[-1].lower() if '.' in f.filename else ''
    if ext not in {'png','jpg','jpeg'}:
        return jsonify({'status':'error','message':'Use PNG, JPG, or JPEG for the e-signature.'}),400
    detected=_detected_image_ext(f)
    normalized_declared='jpg' if ext in {'jpg','jpeg'} else ext
    if not detected or detected != normalized_declared:
        return jsonify({'status':'error','message':'The selected e-signature file is not a valid PNG/JPG image.'}),400
    f.seek(0,os.SEEK_END); size=f.tell(); f.seek(0)
    if size > 3*1024*1024:
        return jsonify({'status':'error','message':'E-signature image must be 3MB or smaller.'}),400
    filename=secure_filename(f"esign_{session['user_id']}_{int(time.time())}.{ext}")
    path=os.path.join(app.config['SIGNATURE_FOLDER'],filename); f.save(path)
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT e_signature_path FROM users WHERE id=%s",(session['user_id'],)); old=(cursor.fetchone() or {}).get('e_signature_path')
            cursor.execute("UPDATE users SET e_signature_path=%s,e_signature_updated_at=NOW() WHERE id=%s",(filename,session['user_id']))
        if old and old!=filename:
            oldp=_signature_abs(old)
            if oldp:
                try: os.remove(oldp)
                except OSError: pass
        log_audit_action(session.get('username'),session.get('role'),'Upload E-Signature','Account',old or None,filename)
        return jsonify({'status':'success','message':'E-signature updated. It will be used only on documents that require your role.','preview_url':url_for('view_esignature',user_id=session['user_id'])})
    finally: conn.close()

@app.route('/api/account/esign', methods=['DELETE'])
def remove_esignature():
    if 'user_id' not in session or session.get('role') not in OFFICER_ROLES:
        return jsonify({'status':'error','message':'Unauthorized'}),401
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT e_signature_path FROM users WHERE id=%s",(session['user_id'],)); row=cursor.fetchone() or {}; old=row.get('e_signature_path')
            cursor.execute("UPDATE users SET e_signature_path=NULL,e_signature_updated_at=NOW() WHERE id=%s",(session['user_id'],))
        oldp=_signature_abs(old)
        if oldp:
            try: os.remove(oldp)
            except OSError: pass
        log_audit_action(session.get('username'),session.get('role'),'Remove E-Signature','Account',old or None,None)
        return jsonify({'status':'success','message':'E-signature removed.'})
    finally: conn.close()

@app.route('/account/esignature/<int:user_id>')
def view_esignature(user_id):
    if 'user_id' not in session or session.get('role') not in OFFICER_ROLES:
        return ('Unauthorized',401)
    if session.get('role')!='Admin' and session.get('user_id')!=user_id:
        return ('Forbidden',403)
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT e_signature_path FROM users WHERE id=%s",(user_id,)); row=cursor.fetchone()
        path=_signature_abs((row or {}).get('e_signature_path'))
        if not path: return ('Not found',404)
        return send_file(path,conditional=True)
    finally: conn.close()


@app.route('/api/settings/update', methods=['POST'])
def update_settings():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    data = request.get_json() or {}
    user_id = session['user_id']
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if 'emergency' in data:
                em = data['emergency']
                cursor.execute("""UPDATE users SET
                    emergency_contact_name=%s, emergency_contact_number=%s,
                    emergency_contact_relationship=%s WHERE id=%s""",
                               (em.get('name'), em.get('number'), em.get('relationship'), user_id))
            if 'preferences' in data:
                cursor.execute("UPDATE users SET email_notifications=%s WHERE id=%s",
                               (1 if data['preferences'].get('email') else 0, user_id))
        conn.close()
        return jsonify({'status': 'success', 'message': 'Settings updated.'})
    except Exception as e:
        app.logger.exception('Homeowner API operation failed')
        return jsonify({'status': 'error', 'message': 'Unable to complete this operation right now. Please try again.'}), 500


@app.route('/api/settings/password', methods=['POST'])
def update_password():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    data = request.get_json() or {}
    if not all([data.get('current_password'), data.get('new_password')]):
        return jsonify({'status': 'error', 'message': 'All fields required.'}), 400
    if len(data.get('new_password','')) < 8:
        return jsonify({'status': 'error', 'message': 'New password must be at least 8 characters.'}), 400
    if data['new_password'] != data.get('confirm_password'):
        return jsonify({'status': 'error', 'message': 'Passwords do not match.'}), 400
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT password_hash FROM users WHERE id=%s", (session['user_id'],))
            u = cursor.fetchone()
            if not u or not check_password_hash(u['password_hash'], data['current_password']):
                conn.close()
                return jsonify({'status': 'error', 'message': 'Current password incorrect.'}), 400
            cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                           (generate_password_hash(data['new_password'], method='pbkdf2:sha256'), session['user_id']))
        conn.close()
        return jsonify({'status': 'success', 'message': 'Password changed successfully.'})
    except Exception as e:
        app.logger.exception('Homeowner API operation failed')
        return jsonify({'status': 'error', 'message': 'Unable to complete this operation right now. Please try again.'}), 500


# ---------------------------------------------------------------------------
# Submit request
# ---------------------------------------------------------------------------
VALID_CATEGORIES = ['Document Request', 'Property & Moving', 'Vehicle Services', 'Tenant Services']


@app.route('/api/requests/submit', methods=['POST'])
def submit_request():
    if 'user_id' not in session or session.get('role') != 'Homeowner':
        return jsonify({'status':'error','message':'Unauthorized'}),401
    data=request.get_json() or {}
    title=(data.get('title') or '').strip()
    form_data=data.get('formData') or {}
    user_id=session['user_id']
    if not title:
        return jsonify({'status':'error','message':'Select a valid service before submitting.'}),400
    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id=%s",(user_id,))
            user=cursor.fetchone()
            if not user or user.get('verification_status') != 'Verified':
                conn.close()
                return jsonify({'status':'error','message':'Your homeowner account must be verified before submitting requests.'}),403

            # Global HOA eligibility rule: a homeowner with any outstanding monthly dues
            # cannot create a NEW document request. Existing correction/resubmission flows
            # remain available so historical requests are not stranded.
            dues = dues_summary(cursor, user_id)
            if dues.get('unpaid_months', 0) > 0:
                conn.close()
                return jsonify({
                    'status':'error', 'code':'OUTSTANDING_DUES',
                    'message':'Document requests are temporarily unavailable while you have outstanding monthly HOA dues. Please coordinate with the Treasurer first.',
                    'outstanding_balance': float(dues.get('outstanding_balance') or 0),
                    'unpaid_months': int(dues.get('unpaid_months') or 0)
                }), 403

            service = get_service_config(cursor, title, active_only=False)
            if service and service.get('status') != 'Active':
                conn.close()
                return jsonify({'status':'error','message':'This service is currently inactive. Please choose another available service or contact the HOA office.'}),400
            if not service:
                # Compatibility for an older database that has not yet run the
                # service-type migration. Unknown services are never accepted.
                legacy_titles = SECRETARY_FIRST_REQUEST_TYPES | TREASURER_FIRST_REQUEST_TYPES | SHARED_INITIAL_CHECK_REQUEST_TYPES
                if title not in legacy_titles:
                    conn.close()
                    return jsonify({'status':'error','message':'This service is not configured in the NFH-HOA system.'}),400
                cursor.execute("SELECT amount FROM fees WHERE item_name=%s AND status='Active'", (title,))
                fee_row = cursor.fetchone()
                service = {
                    'service_name': title,
                    'category': data.get('category') if data.get('category') in VALID_CATEGORIES else 'Document Request',
                    'fee': float(fee_row['amount']) if fee_row else 0.0,
                    'initial_reviewer': next(iter(initial_checker_roles(title))),
                    'requires_executive': 1,
                    'requires_payment': 1,
                    'requires_dues_clearance': 1,
                    'release_role': 'Secretary',
                    'form_template': {
                        'Gate Pass':'gate-pass','Proof of Residency':'proof-residency','Certificate of Improvement':'cert-improvement',
                        'Certificate of Membership':'cert-membership','Move-in Gate Pass':'move-in','Move-out Gate Pass':'move-out',
                        'Vehicle Sticker Application':'vehicle-sticker','Renters/Tenants Information Form':'tenant-form'
                    }.get(title,'generic'),
                    'purpose_required': 1 if title in PURPOSE_REQUIRED_REQUEST_TYPES else 0,
                }

            user=complete_missing_request_profile(cursor,user_id,user,form_data)
            err=validate_request_payload(title,form_data,user,service)
            if err:
                conn.close()
                return jsonify({'status':'error','message':err}),400

            category=service.get('category') or 'Document Request'
            fee_amount=float(service.get('fee') or 0)
            if service.get('form_template') == 'vehicle-sticker':
                cursor.execute("SELECT item_name,amount FROM fees WHERE status='Active'")
                fmap={x['item_name']:float(x['amount']) for x in cursor.fetchall()}
                fee_amount=sum(
                    fmap.get('Vehicle Sticker (4 Wheels)',200.0) if v.get('type')=='4 Wheels'
                    else fmap.get('Vehicle Sticker (2/3 Wheels)',100.0)
                    for v in form_data.get('vehicles',[])
                )
            requires_payment=bool(service.get('requires_payment')) and fee_amount > 0
            if not bool(service.get('requires_payment')):
                fee_amount=0.0
            payment_status='Unpaid' if requires_payment else 'Not Required'

            req_id=f"REQ-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000,9999)}"
            # Avoid a rare collision without exposing database errors to the user.
            for _ in range(5):
                cursor.execute("SELECT id FROM requests WHERE id=%s", (req_id,))
                if not cursor.fetchone():
                    break
                req_id=f"REQ-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000,9999)}"
            purpose=(form_data.get('purpose') or '').strip()
            purpose_details=(form_data.get('purposeDetails') or '').strip()
            cursor.execute("""INSERT INTO requests
                (id,user_id,request_type,category,submission_type,fee,details,purpose,purpose_details,status,payment_status)
                VALUES (%s,%s,%s,%s,'Query',%s,%s,%s,%s,'Submitted',%s)""",
                (req_id,user_id,title,category,fee_amount,json.dumps(form_data),purpose,purpose_details,payment_status))
            assign_request_document_template(cursor, req_id, title)
        conn.close()
        history_note = f'Purpose: {purpose}' if purpose else 'Homeowner submitted the request.'
        record_request_history(req_id,'Submitted',None,'Submitted',history_note,session.get('full_name') or session.get('username'),'Homeowner')
        return jsonify({'status':'success','message':f'Request {req_id} submitted successfully.'})
    except Exception as e:
        app.logger.exception('Request submission failed')
        return jsonify({'status':'error','message':'Unable to submit the request right now. Please try again.'}),500


@app.route('/api/requests/concern', methods=['POST'])
def submit_request_concern():
    if 'user_id' not in session or session.get('role') != 'Homeowner':
        return jsonify({'status':'error','message':'Unauthorized'}), 401
    data=request.get_json() or {}; req_id=(data.get('request_id') or '').strip(); message=(data.get('message') or '').strip(); subject=(data.get('subject') or 'Request Concern').strip()[:150]
    if not req_id or len(message) < 5:
        return jsonify({'status':'error','message':'Please enter a clear concern.'}), 400
    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT request_type, assigned_officer FROM requests WHERE id=%s AND user_id=%s", (req_id,session['user_id']))
            req=cursor.fetchone()
            if not req: conn.close(); return jsonify({'status':'error','message':'Request not found.'}),404
            assigned=req.get('assigned_officer') if req.get('assigned_officer') in ('Secretary','Treasurer') else sorted(initial_checker_roles(req['request_type']))[0]
            if assigned not in ('Secretary','Treasurer'): assigned='Secretary'
            cursor.execute("""INSERT INTO request_concerns (request_id,user_id,assigned_role,subject,concern_text,status)
                              VALUES (%s,%s,%s,%s,%s,'Open')""",(req_id,session['user_id'],assigned,subject,message))
        conn.close()
        log_audit_action(session.get('username'),'Homeowner','Request Concern Submitted',req_id,None,f'Assigned to {assigned}')
        return jsonify({'status':'success','message':f'Your concern for {req_id} was submitted to the {assigned}.'})
    except Exception:
        app.logger.exception('Request concern submission failed for %s', req_id)
        return jsonify({'status':'error','message':'Unable to submit the concern right now. Please try again.'}),500

@app.route('/api/requests/concerns', methods=['GET'])
def homeowner_concerns():
    if 'user_id' not in session or session.get('role') != 'Homeowner': return jsonify({'status':'error'}),401
    conn=get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute("""SELECT c.id,c.request_id,c.assigned_role,c.subject,c.concern_text,c.response_text,c.status,c.responded_by, r.request_type,
                          DATE_FORMAT(c.created_at,'%%Y-%%m-%%d %%H:%%i') created_at,
                          DATE_FORMAT(c.responded_at,'%%Y-%%m-%%d %%H:%%i') responded_at
                          FROM request_concerns c JOIN requests r ON r.id=c.request_id WHERE c.user_id=%s ORDER BY c.updated_at DESC""",(session['user_id'],))
        rows=cursor.fetchall()
    conn.close(); return jsonify({'status':'success','concerns':rows})

@app.route('/officer/concerns/<int:concern_id>/respond', methods=['POST'])
def respond_request_concern(concern_id):
    if 'user_id' not in session or session.get('role') not in ('Secretary','Treasurer'):
        return redirect(url_for('index'))
    response=(request.form.get('response') or '').strip()
    if not response: flash('Please enter a response to the homeowner.', 'error'); return redirect(url_for('officer'))
    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM request_concerns WHERE id=%s AND assigned_role=%s",(concern_id,session['role']))
            c=cursor.fetchone()
            if not c: conn.close(); flash('Concern not found or not assigned to your role.','error'); return redirect(url_for('officer'))
            cursor.execute("UPDATE request_concerns SET response_text=%s,status='Responded',responded_by=%s,responded_at=NOW() WHERE id=%s",
                           (response,session.get('full_name') or session.get('username'),concern_id))
        conn.close()
        # Resolve request/homeowner context server-side for the notification email.
        homeowner_name = 'Homeowner'
        request_type = 'Request'
        try:
            nconn = get_db_connection()
            with nconn.cursor() as cursor:
                cursor.execute("""SELECT r.request_type, CONCAT(u.first_name,' ',u.last_name) AS homeowner
                                  FROM requests r JOIN users u ON u.id=r.user_id
                                  WHERE r.id=%s AND u.id=%s""", (c['request_id'], c['user_id']))
                nctx = cursor.fetchone()
            nconn.close()
            if nctx:
                homeowner_name = nctx.get('homeowner') or homeowner_name
                request_type = nctx.get('request_type') or request_type
        except Exception as exc:
            app.logger.warning('Could not resolve concern email context for concern_id=%s: %s', concern_id, exc)
        body=(f"Dear {homeowner_name},\n\nYour concern regarding {request_type} request {c['request_id']} has received a response.\n\n"
              f"Officer Response: {response}\n\n"
              "Please sign in to the NFH-HOA Homeowner Portal to review the complete concern and response.\n\n"
              "Thank you,\nNorth Fairway Homes Homeowners' Association")
        result=send_alert_and_email(c['user_id'],f"NFH-HOA Concern Response — {c['request_id']}",body,'request_concern',str(concern_id),f'concern-response-{concern_id}')
        log_audit_action(session.get('username'),session.get('role'),'Request Concern Responded',c['request_id'],c.get('status') or 'Open','Responded')
        flash('Response saved and the homeowner was notified.','success')
        if not result.get('email_sent'): flash('The response was saved, but email delivery was unavailable.','warning')
    except Exception:
        app.logger.exception('Request concern response failed for %s', concern_id)
        flash('Unable to save the concern response right now. Please try again.','error')
    return redirect(url_for('officer'))

@app.route('/api/requests/concerns/<int:concern_id>/resolve', methods=['POST'])
def resolve_homeowner_concern(concern_id):
    if 'user_id' not in session or session.get('role') != 'Homeowner':
        return jsonify({'status':'error','message':'Unauthorized'}),401
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT request_id,status FROM request_concerns WHERE id=%s AND user_id=%s",(concern_id,session['user_id']))
            row=cursor.fetchone()
            if not row: return jsonify({'status':'error','message':'Concern not found.'}),404
            cursor.execute("UPDATE request_concerns SET status='Resolved' WHERE id=%s",(concern_id,))
        log_audit_action(session.get('username'),'Homeowner','Resolve Request Concern',row['request_id'],row['status'],'Resolved')
        return jsonify({'status':'success','message':'Concern marked as resolved.'})
    finally:
        conn.close()


@app.route('/api/requests/resubmit', methods=['POST'])
def resubmit_request():
    """Homeowner updates and resubmits a request returned for correction."""
    if 'user_id' not in session or session.get('role') != 'Homeowner':
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    data = request.get_json() or {}
    request_id = data.get('request_id')
    form_data = data.get('formData', {})
    user_id = session['user_id']

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT status, request_type FROM requests WHERE id=%s AND user_id=%s",
                           (request_id, user_id))
            req = cursor.fetchone()
            if not req:
                conn.close()
                return jsonify({'status': 'error', 'message': 'Request not found.'}), 404
            if req['status'] != 'For Correction':
                conn.close()
                return jsonify({'status': 'error', 'message': 'Only requests marked "For Correction" can be resubmitted.'}), 400

            # Deactivating a service blocks new requests, but must not strand an
            # existing transaction that an officer already returned for correction.
            # Use the historical service configuration to validate the resubmission.
            service = get_service_config(cursor, req['request_type'], active_only=False)

            cursor.execute("SELECT * FROM users WHERE id=%s",(user_id,))
            homeowner=cursor.fetchone()
            homeowner=complete_missing_request_profile(cursor,user_id,homeowner or {},form_data)
            err=validate_request_payload(req['request_type'],form_data,homeowner,service)
            if err:
                conn.close()
                return jsonify({'status':'error','message':err}),400

            cursor.execute("""UPDATE requests SET details=%s, purpose=%s, purpose_details=%s, status='Submitted', remarks=NULL
                              WHERE id=%s""", (json.dumps(form_data), form_data.get('purpose'), form_data.get('purposeDetails'), request_id))
        conn.close()
        record_request_history(request_id,'Resubmitted','For Correction','Submitted','Homeowner corrected and resubmitted the request.')
        log_audit_action(session.get('username'), session.get('role'), 'Resubmit Request',
                         request_id, 'For Correction', 'Submitted')
        return jsonify({'status': 'success', 'message': f'Request {request_id} resubmitted for review.'})
    except Exception:
        app.logger.exception('Request resubmission failed for %s', request_id)
        return jsonify({'status': 'error', 'message': 'Unable to resubmit the request right now.'}), 500


# ---------------------------------------------------------------------------
# Cancel request

# ---------------------------------------------------------------------------
@app.route('/api/requests/cancel', methods=['POST'])
def cancel_request():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    data = request.get_json() or {}
    request_id = data.get('request_id')
    reason = (data.get('reason') or '').strip()
    user_id = session['user_id']
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT status, request_type FROM requests WHERE id=%s AND user_id=%s",
                           (request_id, user_id))
            req = cursor.fetchone()
            if not req:
                conn.close()
                return jsonify({'status': 'error', 'message': 'Request not found.'}), 404
            if req['status'] not in ['Submitted', 'Under Review', 'For Correction']:
                conn.close()
                return jsonify({'status': 'error', 'message': 'This request can no longer be cancelled.'}), 400
            cursor.execute("""UPDATE requests SET status='Cancelled', cancellation_reason=%s, cancelled_by=%s
                              WHERE id=%s""", (reason, session.get('username'), request_id))
        conn.close()
        record_request_history(request_id,'Cancelled',req['status'],'Cancelled',reason)
        log_audit_action(session.get('username'), session.get('role'), 'Cancel Request',
                         request_id, req['status'], 'Cancelled')
        return jsonify({'status': 'success', 'message': 'Request cancelled successfully.'})
    except Exception as e:
        app.logger.exception('Homeowner API operation failed')
        return jsonify({'status': 'error', 'message': 'Unable to complete this operation right now. Please try again.'}), 500


@app.route('/api/alerts/read', methods=['POST'])
def read_alerts():
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE alerts SET unread=0 WHERE user_id=%s", (session['user_id'],))
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        app.logger.exception('Homeowner API operation failed')
        return jsonify({'status': 'error', 'message': 'Unable to complete this operation right now. Please try again.'}), 500


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'status': 'success', 'message': 'Logged out successfully.'})


# ===========================================================================
# TREASURER — MISSING MONTHLY DUES (applied directly; no Executive approval)
# ===========================================================================
@app.route('/api/treasurer/adjustment', methods=['POST'])
def treasurer_submit_adjustment():
    if 'user_id' not in session or session.get('role') not in ['Treasurer', 'Admin']:
        return jsonify({'status': 'error', 'message': 'Permission denied.'}), 403
    data = request.get_json() or {}
    user_id = data.get('user_id')
    try:
        missing_months = int(data.get('missing_months') or 0)
    except (TypeError, ValueError):
        missing_months = 0
    reason = (data.get('reason') or '').strip()
    if not user_id:
        return jsonify({'status': 'error', 'message': 'Please select a homeowner.'}), 400
    if missing_months <= 0 or missing_months > 120:
        return jsonify({'status': 'error', 'message': 'Missing months must be between 1 and 120.'}), 400
    if len(reason) < 3:
        return jsonify({'status': 'error', 'message': 'Provide a short reason for the missing dues entry.'}), 400

    try:
        conn = get_db_connection()
        inserted_months = []
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, CONCAT(first_name,' ',last_name) AS name FROM users WHERE id=%s AND role='Homeowner'", (user_id,))
            u = cursor.fetchone()
            if not u:
                conn.close(); return jsonify({'status': 'error', 'message': 'Homeowner not found.'}), 404
            cursor.execute("SELECT amount FROM fees WHERE item_name='Monthly Dues' AND status='Active'")
            due_row = cursor.fetchone()
            monthly_due = float(due_row['amount']) if due_row else 100.0
            month_cursor = datetime.now().date().replace(day=1)
            for i in range(missing_months):
                due_month = month_cursor - relativedelta(months=i)
                cursor.execute("""INSERT IGNORE INTO monthly_dues (user_id, due_month, amount, status)
                                  VALUES (%s,%s,%s,'Unpaid')""", (user_id, due_month, monthly_due))
                if cursor.rowcount == 1:
                    inserted_months.append(due_month)
            if not inserted_months:
                conn.close()
                return jsonify({'status':'error','message':'Those monthly dues are already recorded for this homeowner.'}),400
            total = monthly_due * len(inserted_months)
            cursor.execute("""INSERT INTO financial_adjustments
                (user_id, homeowner_name, monthly_due, missing_months, proposed_adjustment,
                 previous_balance, new_balance, reason, submitted_by, status, decided_by, decided_at)
                VALUES (%s,%s,%s,%s,%s,0.00,%s,%s,%s,'Applied',%s,%s)""",
                (user_id, u['name'], monthly_due, len(inserted_months), total,
                 total, reason, session.get('username'), session.get('username'), datetime.now()))
            adj_id = cursor.lastrowid
        conn.close()

        skipped = missing_months - len(inserted_months)
        log_audit_action(session.get('username'), session.get('role'),
                         'Missing Dues Adjustment Applied', f'Adjustment-{adj_id}', None,
                         f'{len(inserted_months)} month(s), ₱{total:.2f}')
        treasurer = get_treasurer_contact()
        periods = ', '.join(d.strftime('%B %Y') for d in sorted(inserted_months))
        send_alert_and_email(
            user_id, 'Missing Monthly Dues Notice',
            f"Our records show {len(inserted_months)} unpaid monthly due(s) at ₱{monthly_due:.2f} each, totaling ₱{total:.2f}.\n\n"
            f"Recorded period(s): {periods}\nReason on file: {reason}\n\n"
            f"Please settle this balance with the Treasurer ({treasurer['name']}, {treasurer['phone']}, {treasurer['email']}).",
            related_type='financial_adjustment', related_id=str(adj_id), dedupe_key=f'missing-dues-{adj_id}')
        message = 'Missing dues recorded and the homeowner was notified.'
        if skipped:
            message += f' {skipped} duplicate month(s) were skipped.'
        return jsonify({'status': 'success', 'message': message, 'recorded_months': len(inserted_months), 'total': total})
    except Exception as e:
        app.logger.exception('Missing dues adjustment failed')
        return jsonify({'status': 'error', 'message': 'Unable to record the missing dues right now.'}), 500


# ===========================================================================
# TREASURER — CLEAR / SETTLE MONTHLY DUES
# ===========================================================================
@app.route('/api/treasurer/settle-dues', methods=['POST'])
def treasurer_settle_dues():
    if 'user_id' not in session or session.get('role') not in ['Treasurer', 'Admin']:
        return jsonify({'status': 'error', 'message': 'Permission denied.'}), 403
    data = request.get_json() or {}
    homeowner_id = data.get('user_id')
    if not homeowner_id:
        return jsonify({'status': 'error', 'message': 'Please select a homeowner.'}), 400
    progressed = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT CONCAT(first_name,' ',last_name) AS name FROM users WHERE id=%s AND role='Homeowner'", (homeowner_id,))
            homeowner = cursor.fetchone()
            if not homeowner:
                conn.close(); return jsonify({'status':'error','message':'Homeowner not found.'}), 404
            before = dues_summary(cursor, homeowner_id)
            if before['unpaid_months'] == 0:
                conn.close(); return jsonify({'status':'error','message':'This homeowner has no unpaid monthly dues.'}), 400
            cursor.execute("""UPDATE monthly_dues SET status='Paid', settled_at=NOW(), settled_by=%s
                              WHERE user_id=%s AND status='Unpaid'""", (session.get('username'), homeowner_id))

            # Requests that were intentionally waiting only for dues clearance
            # can now continue automatically using their configured payment rule.
            cursor.execute("""SELECT r.id, r.request_type, r.fee, r.payment_status,
                                      s.requires_payment
                               FROM requests r
                               LEFT JOIN service_types s ON s.service_name=r.request_type
                               WHERE r.user_id=%s AND r.status='Awaiting Dues Clearance'""", (homeowner_id,))
            waiting = cursor.fetchall()
            for req in waiting:
                needs_payment = bool(req.get('requires_payment')) and float(req.get('fee') or 0) > 0
                next_status = 'Ready for Payment & Release' if needs_payment else 'Ready for Release'
                if next_status == 'Ready for Release':
                    cursor.execute("""UPDATE requests SET status=%s, payment_status='Not Required', ready_for_release_at=NOW()
                                      WHERE id=%s""", (next_status, req['id']))
                else:
                    cursor.execute("UPDATE requests SET status=%s WHERE id=%s", (next_status, req['id']))
                progressed.append((req['id'], req['request_type'], next_status))
        conn.close()
        log_audit_action(session.get('username'), session.get('role'), 'Monthly Dues Cleared', homeowner['name'],
                         f"{before['unpaid_months']} unpaid month(s), ₱{before['outstanding_balance']:.2f}", 'Paid')
        send_alert_and_email(homeowner_id, 'Monthly Dues Cleared',
            'Your outstanding monthly dues have been marked as paid by the Treasurer. Requests that were waiting only for dues clearance can now continue automatically.',
            related_type='monthly_dues', related_id=str(homeowner_id), dedupe_key=f"dues-cleared-{homeowner_id}-{int(time.time())}")
        for req_id, req_type, next_status in progressed:
            record_request_history(req_id, 'Dues Clearance Confirmed', 'Awaiting Dues Clearance', next_status,
                                   'Outstanding monthly dues were cleared by the Treasurer.')
            send_alert_and_email(homeowner_id, f'Request Updated — {req_id}',
                f'Your {req_type} request is now {next_status}.', related_type='request', related_id=req_id,
                dedupe_key=f'dues-progress-{req_id}-{next_status}')
        return jsonify({'status':'success','message':f'Outstanding monthly dues marked as paid. {len(progressed)} waiting request(s) advanced automatically.'})
    except Exception as e:
        app.logger.exception('Settle dues failed for homeowner %s', homeowner_id)
        return jsonify({'status':'error','message':'Unable to settle the monthly dues right now.'}), 500


# ===========================================================================
# EVALUATOR REVISION — HOMEOWNER VERIFICATION / SERVICE CONFIG / ISSUANCE
# ===========================================================================
@app.route('/admin/homeowner-verification/<int:user_id>', methods=['POST'])
def admin_homeowner_verification(user_id):
    if 'user_id' not in session or session.get('role')!='Admin': return redirect(url_for('index'))
    action=(request.form.get('action') or '').lower()
    if action not in ('verify','reject'):
        flash('Invalid verification action.','error'); return redirect(url_for('admin'))
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT username,verification_status FROM users WHERE id=%s AND role='Homeowner'",(user_id,)); u=cursor.fetchone()
            if not u: flash('Homeowner account not found.','error'); return redirect(url_for('admin'))
            new='Verified' if action=='verify' else 'Rejected'; status='Active' if action=='verify' else 'Inactive'
            cursor.execute("UPDATE users SET verification_status=%s,status=%s,verified_by=%s,verified_at=NOW() WHERE id=%s",(new,status,session.get('username'),user_id))
        log_audit_action(session.get('username'),'Admin','Homeowner Verification',u['username'],u['verification_status'],new)
        flash(f'Homeowner registration {new.lower()}.','success' if action=='verify' else 'warning')
    finally: conn.close()
    return redirect(url_for('admin')+'#verification')

@app.route('/admin/service-types/save', methods=['POST'])
def save_service_type():
    if 'user_id' not in session or session.get('role') != 'Admin':
        return redirect(url_for('index'))

    service_id_raw = (request.form.get('service_id') or '').strip()
    name = (request.form.get('service_name') or '').strip()
    category = (request.form.get('category') or 'Document Request').strip()
    reviewer = (request.form.get('initial_reviewer') or 'Secretary').strip()
    release_role = (request.form.get('release_role') or 'Secretary').strip()
    form_template = (request.form.get('form_template') or 'generic').strip()
    description = (request.form.get('description') or '').strip()[:255]
    fee_raw = (request.form.get('fee') or '0').strip()
    requires_executive = int(bool(request.form.get('requires_executive')))
    requires_payment = int(bool(request.form.get('requires_payment')))
    requires_dues_clearance = int(bool(request.form.get('requires_dues_clearance')))
    purpose_required = int(bool(request.form.get('purpose_required')))

    valid_categories = set(VALID_CATEGORIES)
    valid_reviewers = {'Secretary', 'Treasurer', 'Shared'}
    valid_release_roles = {'Secretary', 'Treasurer', 'Initial Reviewer'}
    valid_templates = {'generic','gate-pass','proof-residency','cert-improvement','cert-membership','move-in','move-out','vehicle-sticker','tenant-form'}
    if not name or category not in valid_categories or reviewer not in valid_reviewers or release_role not in valid_release_roles or form_template not in valid_templates:
        flash('Provide a valid service name, category, reviewer, release role and form template.', 'error')
        return redirect(url_for('admin') + '#services')
    try:
        fee = float(fee_raw)
        if fee < 0:
            raise ValueError
    except (TypeError, ValueError):
        flash('Service fee must be a valid non-negative amount.', 'error')
        return redirect(url_for('admin') + '#services')
    if not requires_payment and form_template != 'vehicle-sticker':
        fee = 0.0

    service_id = None
    if service_id_raw:
        try:
            service_id = int(service_id_raw)
        except ValueError:
            flash('Invalid service record selected.', 'error')
            return redirect(url_for('admin') + '#services')

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            if service_id is not None:
                cursor.execute('SELECT * FROM service_types WHERE id=%s', (service_id,))
                old = cursor.fetchone()
                if not old:
                    flash('Service type not found.', 'error')
                    return redirect(url_for('admin') + '#services')
                cursor.execute('SELECT id FROM service_types WHERE LOWER(service_name)=LOWER(%s) AND id<>%s', (name, service_id))
                if cursor.fetchone():
                    flash('Another service already uses that name.', 'error')
                    return redirect(url_for('admin') + '#services')
                old_name = old['service_name']
                if old_name != name:
                    # Historical requests store the service name. Renaming a service
                    # that already has transactions would detach those requests from
                    # their workflow configuration, so preserve the identifier and
                    # require a new service record instead.
                    cursor.execute('SELECT COUNT(*) AS total FROM requests WHERE request_type=%s', (old_name,))
                    request_total = int((cursor.fetchone() or {}).get('total') or 0)
                    if request_total > 0:
                        flash('This service already has request history and cannot be renamed. Create a new service and deactivate the old service instead.', 'error')
                        return redirect(url_for('admin') + '#services')
                    cursor.execute('SELECT item_name FROM fees WHERE item_name=%s', (name,))
                    if cursor.fetchone():
                        flash('A fee record already uses the new service name. Choose another name.', 'error')
                        return redirect(url_for('admin') + '#services')
                cursor.execute("""UPDATE service_types
                                  SET service_name=%s, category=%s, fee=%s, initial_reviewer=%s,
                                      requires_executive=%s, requires_payment=%s, requires_dues_clearance=%s,
                                      purpose_required=%s, release_role=%s, form_template=%s, description=%s
                                  WHERE id=%s""",
                               (name, category, fee, reviewer, requires_executive, requires_payment,
                                requires_dues_clearance, purpose_required, release_role, form_template, description, service_id))
                if old_name != name:
                    cursor.execute('UPDATE fees SET item_name=%s, amount=%s WHERE item_name=%s', (name, fee, old_name))
                    if cursor.rowcount == 0 and form_template != 'vehicle-sticker':
                        cursor.execute("INSERT INTO fees(item_name,amount,status) VALUES(%s,%s,'Active')", (name, fee))
                elif form_template != 'vehicle-sticker':
                    cursor.execute("INSERT INTO fees(item_name,amount,status) VALUES(%s,%s,'Active') ON DUPLICATE KEY UPDATE amount=VALUES(amount)", (name, fee))
                previous = f"{old['service_name']} | {old['category']} | {old['fee']} | {old['initial_reviewer']}"
                current = f"{name} | {category} | {fee:.2f} | {reviewer} | exec={requires_executive} | payment={requires_payment} | dues={requires_dues_clearance} | release={release_role}"
                log_audit_action(session.get('username'), 'Admin', 'Edit Service Type', name, previous, current)
                flash('Service configuration updated successfully.', 'success')
            else:
                cursor.execute('SELECT id FROM service_types WHERE LOWER(service_name)=LOWER(%s)', (name,))
                if cursor.fetchone():
                    flash('That service already exists. Use Edit to update it.', 'error')
                    return redirect(url_for('admin') + '#services')
                cursor.execute("""INSERT INTO service_types
                                  (service_name,category,fee,initial_reviewer,requires_executive,requires_payment,
                                   requires_dues_clearance,purpose_required,release_role,form_template,description)
                                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                               (name, category, fee, reviewer, requires_executive, requires_payment,
                                requires_dues_clearance, purpose_required, release_role, form_template, description))
                if form_template != 'vehicle-sticker':
                    cursor.execute("INSERT INTO fees(item_name,amount,status) VALUES(%s,%s,'Active') ON DUPLICATE KEY UPDATE amount=VALUES(amount)", (name, fee))
                log_audit_action(session.get('username'), 'Admin', 'Create Service Type', name, None,
                                 f"{category} | {fee:.2f} | {reviewer} | release={release_role}")
                flash('Service type added successfully.', 'success')
    except pymysql.IntegrityError:
        flash('Unable to save the service because the name conflicts with an existing record.', 'error')
    finally:
        conn.close()
    return redirect(url_for('admin') + '#services')


@app.route('/admin/service-types/<int:service_id>/toggle', methods=['POST'])
def toggle_service_type(service_id):
    if 'user_id' not in session or session.get('role')!='Admin': return redirect(url_for('index'))
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT service_name,status FROM service_types WHERE id=%s",(service_id,)); row=cursor.fetchone()
            if row:
                new='Inactive' if row['status']=='Active' else 'Active'; cursor.execute("UPDATE service_types SET status=%s WHERE id=%s",(new,service_id)); log_audit_action(session.get('username'),'Admin','Toggle Service Type',row['service_name'],row['status'],new)
    finally: conn.close()
    return redirect(url_for('admin')+'#services')


@app.route('/admin/document-templates/<template_key>/save', methods=['POST'])
def save_document_template(template_key):
    if 'user_id' not in session or session.get('role') != 'Admin':
        return redirect(url_for('index'))
    title=(request.form.get('title') or '').strip()
    body=template_text_to_internal((request.form.get('body_text') or '').strip())
    footer=template_text_to_internal((request.form.get('footer_text') or '').strip())
    allowed_sig_roles={'Secretary','Treasurer','President'}
    required_signatures=','.join([r for r in request.form.getlist('required_signatures') if r in allowed_sig_roles])
    if not title or not body:
        flash('Template title and body are required.','error')
        return redirect(url_for('admin')+'#templates')
    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""SELECT * FROM document_templates WHERE template_key=%s AND status='Active'
                              ORDER BY version_no DESC LIMIT 1""",(template_key,))
            current=cursor.fetchone()
            if not current:
                conn.close(); flash('Document template not found.','error'); return redirect(url_for('admin')+'#templates')
            next_version=int(current.get('version_no') or 0)+1
            cursor.execute("UPDATE document_templates SET status='Archived' WHERE template_key=%s AND status='Active'",(template_key,))
            cursor.execute("""INSERT INTO document_templates
                (template_key,service_name,version_no,title,body_text,footer_text,required_signatures,status,updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'Active',%s)""",
                (template_key,current.get('service_name'),next_version,title,body,footer,required_signatures,
                 session.get('username') or 'Admin'))
        conn.close()
        log_audit_action(session.get('username'),'Admin','Update Document Template',template_key,
                         f"Version {current.get('version_no')}",f"Version {next_version}")
        flash(f'Document template updated to version {next_version}. Existing requests keep their original version.','success')
    except Exception:
        app.logger.exception('Document template update failed for %s',template_key)
        flash('Unable to update the document template right now.','error')
    return redirect(url_for('admin')+'#templates')


@app.route('/officer/issue_request/<req_id>', methods=['POST'])
def issue_request(req_id):
    if 'user_id' not in session or session.get('role') not in ['Secretary','Treasurer','Admin']:
        return redirect(url_for('index'))
    remarks=(request.form.get('release_remarks') or '').strip()
    sticker_from=(request.form.get('sticker_valid_from') or '').strip()
    sticker_until=(request.form.get('sticker_valid_until') or '').strip()
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""SELECT status,user_id,request_type,assigned_officer
                              FROM requests WHERE id=%s""",(req_id,))
            req=cursor.fetchone()
            if not req or req['status']!='Ready for Release':
                flash('Only Ready for Release requests can be issued.','error')
                return redirect(url_for('officer'))
            service=get_service_config(cursor,req['request_type'],active_only=False)
            allowed=release_roles_for_request(service,req.get('assigned_officer'))
            if session.get('role')!='Admin' and session.get('role') not in allowed:
                flash('Your role is not authorized to issue this service.','error')
                return redirect(url_for('officer'))
            is_vehicle = bool(service and service.get('form_template')=='vehicle-sticker') or req['request_type']=='Vehicle Sticker Application'
            valid_from=None; valid_until=None
            if is_vehicle:
                if not sticker_from or not sticker_until:
                    flash('Vehicle sticker validity dates are required before issuance.','error')
                    return redirect(url_for('officer'))
                try:
                    valid_from=datetime.strptime(sticker_from,'%Y-%m-%d').date()
                    valid_until=datetime.strptime(sticker_until,'%Y-%m-%d').date()
                except ValueError:
                    flash('Enter valid sticker validity dates.','error')
                    return redirect(url_for('officer'))
                if valid_until < valid_from:
                    flash('Sticker expiration date cannot be earlier than the start date.','error')
                    return redirect(url_for('officer'))
            cursor.execute("""UPDATE requests SET status='Issued / Completed',issued_at=NOW(),issued_by=%s,
                              sticker_valid_from=%s,sticker_valid_until=%s WHERE id=%s""",
                           (session.get('full_name') or session.get('username'),valid_from,valid_until,req_id))
            template=get_active_document_template(cursor,request_type=req['request_type'])
            snapshot_request_signatures(cursor,req_id,template)
        record_note=remarks
        if valid_from and valid_until:
            record_note=(record_note + ' | ' if record_note else '') + f'Sticker valid {valid_from.isoformat()} to {valid_until.isoformat()}'
        record_request_history(req_id,'Issued / Completed','Ready for Release','Issued / Completed',record_note)
        log_audit_action(session.get('username'),session.get('role'),'Issue Document',req_id,'Ready for Release','Issued / Completed')
        body=f"Your {req['request_type']} request {req_id} has been issued/completed."
        if valid_from and valid_until:
            body += f" Sticker validity: {valid_from.strftime('%B %d, %Y')} to {valid_until.strftime('%B %d, %Y')}."
        send_alert_and_email(req['user_id'],'Request Completed',body,related_type='request',related_id=req_id,dedupe_key=f'issued-{req_id}')
        flash('Request marked as Issued / Completed.','success')
    finally:
        conn.close()
    return redirect(url_for('officer'))


# ===========================================================================
# ADMIN ACCOUNT MANAGEMENT
# ===========================================================================
@app.route('/create-officer', methods=['POST'])
def create_officer():
    if 'user_id' not in session or session.get('role') != 'Admin':
        return redirect(url_for('index'))
    full_name = request.form.get('full_name', '').strip()
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'Secretary')

    if not all([full_name, username, email, phone, password, role]):
        flash('Full Name, Username, Email, Phone Number, Password, and Role are all required.', 'error')
        return redirect(url_for('admin'))
    if len(password) < 8:
        flash('Password must be at least 8 characters.', 'error')
        return redirect(url_for('admin'))
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        flash('Please enter a valid email address.', 'error')
        return redirect(url_for('admin'))
    if not re.match(r'^09\d{9}$', phone):
        flash('Phone number must contain 11 digits and start with 09.', 'error')
        return redirect(url_for('admin'))
    if role not in ['Secretary', 'Treasurer', 'President', 'Vice President', 'Board Director']:
        flash('Invalid role selected.', 'error')
        return redirect(url_for('admin'))

    name_parts = full_name.split(' ', 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else 'Officer'
    pwd_hash = generate_password_hash(password, method='pbkdf2:sha256')
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE username=%s OR email=%s", (username, email))
            if cursor.fetchone():
                conn.close()
                flash('Username or email already exists.', 'error')
                return redirect(url_for('admin'))

            cursor.execute("""INSERT INTO users
                (first_name, last_name, username, email, mobile, password_hash, role, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'Active')""",
                           (first_name, last_name, username, email, phone, pwd_hash, role))
        conn.close()
        log_audit_action(session.get('username'), session.get('role'),
                         'Create Officer Account', username, None, role)
        flash(f'Officer account {username} created successfully.', 'success')
    except pymysql.IntegrityError:
        flash('Username or email already exists.', 'error')
    except Exception:
        app.logger.exception('Officer account creation failed')
        flash('Unable to create the officer account right now. Please try again.', 'error')
    return redirect(url_for('admin'))


@app.route('/edit-officer/<int:officer_id>', methods=['POST'])
def edit_officer(officer_id):
    if 'user_id' not in session or session.get('role') != 'Admin':
        return redirect(url_for('index'))
    full_name = request.form.get('full_name', '').strip()
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = request.form.get('phone', '').strip()
    role = request.form.get('role', '').strip()
    if not all([full_name, username, email, phone, role]) or role not in ['Secretary','Treasurer','President','Vice President','Board Director']:
        flash('Provide valid officer information and an allowed officer role.', 'error')
        return redirect(url_for('admin') + '#officers')
    if not re.match(r'^09\d{9}$', phone):
        flash('Phone number must contain 11 digits and start with 09.', 'error')
        return redirect(url_for('admin') + '#officers')
    parts = full_name.split(' ', 1); first = parts[0]; last = parts[1] if len(parts)>1 else 'Officer'
    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id=%s AND role IN ('Secretary','Treasurer','President','Vice President','Board Director')", (officer_id,))
            old=cursor.fetchone()
            if not old:
                conn.close(); flash('Officer account not found.', 'error'); return redirect(url_for('admin')+'#officers')
            cursor.execute("SELECT id FROM users WHERE id<>%s AND (LOWER(username)=LOWER(%s) OR LOWER(email)=LOWER(%s))", (officer_id,username,email))
            if cursor.fetchone():
                conn.close(); flash('Username or email already belongs to another account.', 'error'); return redirect(url_for('admin')+'#officers')
            cursor.execute("UPDATE users SET first_name=%s,last_name=%s,username=%s,email=%s,mobile=%s,role=%s WHERE id=%s",
                           (first,last,username,email,phone,role,officer_id))
        conn.close()
        log_audit_action(session.get('username'), 'Admin', 'Edit Officer Account', username,
                         f"{old['username']} | {old['email']} | {old['mobile']} | {old['role']}",
                         f"{username} | {email} | {phone} | {role}")
        flash(f'Officer account {username} updated successfully.', 'success')
    except pymysql.IntegrityError:
        flash('Username or email already exists.', 'error')
    except Exception:
        app.logger.exception('Officer account update failed for %s', officer_id)
        flash('Unable to update the officer account right now. Please try again.', 'error')
    return redirect(url_for('admin') + '#officers')


@app.route('/toggle-officer-status/<int:officer_id>', methods=['POST'])
def toggle_officer_status(officer_id):
    if 'user_id' not in session or session.get('role') != 'Admin':
        return redirect(url_for('index'))
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT username, status FROM users WHERE id=%s", (officer_id,))
            off = cursor.fetchone()
            if off:
                new_status = 'Inactive' if off['status'] == 'Active' else 'Active'
                cursor.execute("UPDATE users SET status=%s WHERE id=%s", (new_status, officer_id))
                log_audit_action(session.get('username'), session.get('role'),
                                 'Toggle Officer Status', off['username'], off['status'], new_status)
                flash(f'Status for {off["username"]} updated to {new_status}.', 'success')
        conn.close()
    except Exception:
        app.logger.exception('Officer status update failed for %s', officer_id)
        flash('Unable to update the officer status right now. Please try again.', 'error')
    return redirect(url_for('admin'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ===========================================================================
# AUTHENTICATION APIS
# ===========================================================================
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'status': 'error', 'message': 'Username and password required.'}), 400
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(%s) OR LOWER(email)=LOWER(%s)",
                           (username, username))
            user = cursor.fetchone()
        conn.close()
        if not user or not check_password_hash(user['password_hash'], password):
            return jsonify({'status': 'error', 'message': 'Invalid username or password.'}), 401
        if user.get('role')=='Homeowner' and user.get('verification_status')=='Pending Verification':
            return jsonify({'status':'error','message':'Your homeowner registration is still pending HOA verification.'}),403
        if user.get('role')=='Homeowner' and user.get('verification_status')=='Rejected':
            return jsonify({'status':'error','message':'Your homeowner registration was not verified. Please contact the HOA office.'}),403
        if user['status'] != 'Active':
            return jsonify({'status':'error','message':'Your account is inactive. Please contact the HOA office.'}),403
        _set_login_session(user)
        redirect_url = _role_home_url(user['role'])
        log_audit_action(user['username'], user['role'], 'User Login', user['username'])
        return jsonify({'status': 'success', 'redirect': redirect_url})
    except Exception as e:
        app.logger.exception('Login failed due to an internal error')
        return jsonify({'status': 'error', 'message': 'Unable to sign in right now. Please try again.'}), 500


@app.route('/api/send-registration-code', methods=['POST'])
def send_reg_code():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({'status': 'error', 'message': 'Enter a valid email address.'}), 400
    code, wait_seconds = _issue_one_time_code(VERIFICATION_CODES, email)
    if code is None:
        return jsonify({'status': 'error', 'message': f'Please wait {wait_seconds} second(s) before requesting another code.'}), 429
    if not app.config.get('MAIL_USERNAME'):
        if DEV_SHOW_OTP:
            app.logger.warning('[DEV OTP] Registration code for %s: %s', email, code)
            return jsonify({'status': 'success', 'message': f'Development verification code: {code}'})
        return jsonify({'status': 'error', 'message': 'Email verification is unavailable because the server email account is not configured.'}), 503
    try:
        msg = Message('Northfairway HOA - Email Verification Code', recipients=[email])
        msg.body = f"Your verification code is: {code}\n\nThis code expires in {OTP_TTL_SECONDS // 60} minutes."
        mail.send(msg)
        return jsonify({'status': 'success', 'message': 'Verification code sent successfully.'})
    except Exception as e:
        app.logger.warning('Registration code delivery failed for %s: %s', email, e)
        return jsonify({'status': 'error', 'message': 'Email delivery failed. Please verify the server email configuration and try again.'}), 503

@app.route('/api/verify-registration-code', methods=['POST'])
def verify_reg_code():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    code = (data.get('code') or '').strip()
    ok, message = _verify_one_time_code(VERIFICATION_CODES, email, code, mark_verified=True)
    if ok:
        return jsonify({'status': 'success', 'message': 'Email verified.'})
    return jsonify({'status': 'error', 'message': message}), 400

@app.route('/api/register', methods=['POST'])
def api_register():
    data=request.get_json() or {}
    first=(data.get('first_name') or '').strip(); middle=(data.get('middle_name') or '').strip(); last=(data.get('last_name') or '').strip(); suffix=(data.get('suffix') or '').strip()
    email=(data.get('email') or '').strip().lower(); username=(data.get('username') or '').strip(); mobile=(data.get('phone') or '').strip(); password=data.get('password') or ''
    block=(data.get('block') or '').strip(); lot=(data.get('lot') or '').strip(); address_line=(data.get('address_line') or '').strip()
    gender=(data.get('gender') or 'NA').strip(); dob=(data.get('dob') or '').strip() or None; civil_status=(data.get('civil_status') or 'Single').strip()
    try: household=max(1, min(30, int(data.get('household') or 1)))
    except (TypeError, ValueError): household=1
    emergency_name=(data.get('emergency_name') or '').strip(); emergency_number=(data.get('emergency_number') or '').strip(); emergency_relationship=(data.get('emergency_relationship') or '').strip()
    if not all([first,last,email,username,mobile,password,block,lot,address_line]):
        return jsonify({'status':'error','message':'Complete all required identity, contact, Block/Lot, and address fields.'}),400
    if not _registration_email_verified(email):
        return jsonify({'status':'error','message':'Verify your email address before creating the account.'}),400
    if len(password)<8: return jsonify({'status':'error','message':'Password must be at least 8 characters.'}),400
    if not re.match(r'^09\d{9}$',mobile): return jsonify({'status':'error','message':'Phone number must contain 11 digits and start with 09.'}),400
    if emergency_number and not re.match(r'^09\d{9}$', emergency_number):
        return jsonify({'status':'error','message':'Emergency contact number must contain 11 digits and start with 09, or be left blank.'}),400
    try:
        conn=get_db_connection()
        with conn.cursor() as cursor:
            public_id=generate_homeowner_public_id(cursor,block,lot)
            cursor.execute("""INSERT INTO users
                              (first_name,middle_name,last_name,suffix,gender,dob,civil_status,block,lot,address_line,household,
                               email,username,mobile,password_hash,role,status,homeowner_public_id,verification_status,
                               emergency_contact_name,emergency_contact_number,emergency_contact_relationship)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Homeowner','Inactive',%s,'Pending Verification',%s,%s,%s)""",
                           (first,middle or None,last,suffix or None,gender,dob,civil_status,block,lot,address_line,household,
                            email,username,mobile,generate_password_hash(password,method='pbkdf2:sha256'),public_id,
                            emergency_name or None,emergency_number or None,emergency_relationship or None))
        conn.close(); VERIFICATION_CODES.pop(email,None)
        return jsonify({'status':'success','message':'Registration submitted. Your account is pending HOA homeowner verification before sign-in.'})
    except pymysql.IntegrityError:
        return jsonify({'status':'error','message':'Username, email, or Homeowner ID already exists.'}),400
    except Exception:
        app.logger.exception('Homeowner registration failed for %s', email)
        return jsonify({'status':'error','message':'Unable to complete registration right now. Please try again.'}),500


@app.route('/api/forgot-password/send-code', methods=['POST'])
def forgot_send_code():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    if not email:
        return jsonify({'status': 'success', 'message': 'If that email is registered, a reset code will be sent.'})
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
            user = cursor.fetchone()
        conn.close()
    except Exception:
        app.logger.exception('Password reset lookup failed')
        return jsonify({'status': 'error', 'message': 'Unable to process the reset request right now.'}), 500
    # Do not reveal whether an account exists.
    if not user:
        time.sleep(0.15)
        return jsonify({'status': 'success', 'message': 'If that email is registered, a reset code will be sent.'})
    code, wait_seconds = _issue_one_time_code(RESET_CODES, email)
    if code is None:
        return jsonify({'status': 'error', 'message': f'Please wait {wait_seconds} second(s) before requesting another reset code.'}), 429
    if not app.config.get('MAIL_USERNAME'):
        if DEV_SHOW_OTP:
            app.logger.warning('[DEV OTP] Password reset code for %s: %s', email, code)
            return jsonify({'status': 'success', 'message': f'Development reset code: {code}'})
        return jsonify({'status': 'error', 'message': 'Password reset email is unavailable because the server email account is not configured.'}), 503
    try:
        msg = Message('Northfairway HOA - Password Reset Code', recipients=[email])
        msg.body = f"Your password reset code is: {code}\n\nThis code expires in {OTP_TTL_SECONDS // 60} minutes."
        mail.send(msg)
        return jsonify({'status': 'success', 'message': 'If that email is registered, a reset code will be sent.'})
    except Exception as e:
        app.logger.warning('Password reset email failed for %s: %s', email, e)
        return jsonify({'status': 'error', 'message': 'Email delivery failed. Please verify the server email configuration and try again.'}), 503

@app.route('/api/forgot-password/reset', methods=['POST'])
def forgot_reset_password():
    data = request.get_json() or {}
    email = (data.get('email') or '').strip().lower()
    code = (data.get('code') or '').strip()
    new_password = data.get('new_password', '')
    if len(new_password) < 8:
        return jsonify({'status': 'error', 'message': 'New password must be at least 8 characters.'}), 400
    ok, message = _verify_one_time_code(RESET_CODES, email, code, mark_verified=False)
    if not ok:
        return jsonify({'status': 'error', 'message': message}), 400
    pwd_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET password_hash=%s WHERE LOWER(email)=LOWER(%s)", (pwd_hash, email))
            if cursor.rowcount != 1:
                conn.close()
                RESET_CODES.pop(email, None)
                return jsonify({'status': 'error', 'message': 'Unable to reset this account.'}), 400
        conn.close()
        RESET_CODES.pop(email, None)
        return jsonify({'status': 'success', 'message': 'Password reset successful. You can now log in.'})
    except Exception as e:
        app.logger.exception('Password reset database error')
        return jsonify({'status': 'error', 'message': 'Unable to reset the password right now.'}), 500


# ===========================================================================
# APPROVAL EMAIL

# ===========================================================================
# APPROVAL EMAIL
# ===========================================================================
def send_approval_email(req_id, req_type, user_id, executive_name=None, executive_role=None):
    """Notify the request owner after a committed Executive approval using the actual next workflow stage."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT email, CONCAT(first_name,' ',last_name) AS name FROM users WHERE id=%s", (user_id,))
            u = cursor.fetchone()
            cursor.execute("SELECT status,payment_status,fee FROM requests WHERE id=%s", (req_id,))
            req = cursor.fetchone()
        conn.close()
        if not u or not req:
            app.logger.warning('[EMAIL FAILURE] Approval %s: homeowner/request record not found', req_id)
            return {'created': False, 'email_sent': False, 'reason': 'Homeowner or request not found.'}

        subject = f"NFH-HOA Request Update — {req_id}"
        actor = executive_name or 'Authorized Executive'
        position = executive_role or 'Executive'
        status = req.get('status') or 'Approved'
        if status == 'Ready for Payment & Release':
            next_step = ("The request is approved and prepared for face-to-face payment and release. "
                         "Please visit the Treasurer; payment and physical document release may be completed in the same visit.")
        elif status == 'Paid - Awaiting Release':
            next_step = "Your payment has been recorded. The document is still awaiting physical release confirmation."
        elif status == 'Awaiting Payment Verification':
            next_step = ("This is a legacy payment stage. Please coordinate with the Treasurer so it can be processed under the current face-to-face payment and release workflow.")
        elif status == 'Ready for Release':
            next_step = "The request is approved and cleared. It is now ready for release/issuance by the authorized HOA officer."
        elif status == 'Awaiting Dues Clearance':
            next_step = "The request is approved but cannot proceed until outstanding monthly dues are cleared with the Treasurer."
        else:
            next_step = "Open the NFH-HOA Homeowner Portal to view the current request stage and any required next action."
        body = (
            f"Dear {u['name']},\n\n"
            f"Your {req_type} request ({req_id}) has been approved.\n\n"
            f"Request ID: {req_id}\nRequest Type: {req_type}\nCurrent Status: {status}\n\n"
            f"Approved by: {actor}\nPosition: {position}\n\n{next_step}\n\n"
            "Thank you,\nNorth Fairway Homes Homeowners' Association"
        )
        return send_alert_and_email(user_id, subject, body, related_type='request', related_id=req_id,
                                    dedupe_key=f'approval-{req_id}', important=True)
    except Exception as e:
        app.logger.warning('[EMAIL FAILURE] Approval email helper for %s: %s: %s', req_id, type(e).__name__, e)
        return {'created': False, 'email_sent': False, 'reason': 'Notification delivery failed.'}



# ===========================================================================
# PDF GENERATION (individual requests + admin reports)
# ===========================================================================
def _pdf_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='NFHTitle', fontSize=13, leading=16, alignment=TA_CENTER, fontName='Helvetica-Bold'))
    styles.add(ParagraphStyle(name='NFHSub', fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=colors.HexColor('#444444')))
    styles.add(ParagraphStyle(name='NFHHeading', fontSize=11, leading=14, fontName='Helvetica-Bold',
                              textColor=colors.HexColor('#1B4332'), spaceBefore=10, spaceAfter=4))
    styles.add(ParagraphStyle(name='NFHBody', fontSize=9.5, leading=13))
    styles.add(ParagraphStyle(name='NFHRight', fontSize=8.5, leading=11, alignment=TA_RIGHT))
    return styles


def _pdf_letterhead(elements, styles, doc_title):
    elements.append(Paragraph(HOA_NAME, styles['NFHTitle']))
    elements.append(Paragraph(HOA_ADDRESS, styles['NFHSub']))
    elements.append(Paragraph(HOA_REG, styles['NFHSub']))
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#1B4332')))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(doc_title, styles['NFHHeading']))


def _kv_table(pairs, col_widths=(1.8 * inch, 4.4 * inch)):
    rows = [[Paragraph(f"<b>{k}</b>", getSampleStyleSheet()['Normal']), Paragraph(str(v), getSampleStyleSheet()['Normal'])]
            for k, v in pairs]
    t = Table(rows, colWidths=list(col_widths))
    t.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LINEBELOW', (0, 0), (-1, -1), 0.3, colors.HexColor('#DDDDDD')),
    ]))
    return t


def _response_pdf(buffer, filename):
    """Return PDFs as normal downloads, or inline when the UI asks for an in-system preview."""
    buffer.seek(0)
    preview_mode = str(request.args.get('preview', '')).strip().lower() in {'1', 'true', 'yes', 'inline'}
    response = send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=not preview_mode,
        download_name=filename,
        max_age=0,
    )
    if preview_mode:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response


REQUEST_TYPE_FIELD_LABELS = {
    'fullName': 'Full Name', 'blockNum': 'Block Number', 'lotNum': 'Lot Number',
    'materialsList': 'List of Materials',
    'verifiedOwner': 'Verified Owner', 'issuanceDate': 'Date of Issuance Request',
    'requestDate': 'Date', 'applicationDate': 'Application Date',
    'moveInDate': 'Move-in Date', 'moveOutDate': 'Move-out Date',
    'applicantName': "Applicant's Name", 'blockLot': 'Block and Lot',
    'phoneNum': 'Cellphone No.', 'emailAddr': 'Email Address',
}


def generate_request_pdf(req):
    """Builds a PDF document tailored to the request's type."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch)
    styles = _pdf_styles()
    elements = []

    req_type = req['request_type']
    _pdf_letterhead(elements, styles, f"{req_type} — Request Form")

    try:
        details = json.loads(req.get('details') or '{}')
    except Exception:
        details = {}

    elements.append(_kv_table([
        ('Request ID', req['id']),
        ('Homeowner', req['homeowner']),
        ('Category', req.get('category') or 'Document Request'),
        ('Date Submitted', format_pdf_datetime(req.get('date_submitted'))),
        ('Status', req['status']),
        ('Payment Status', req['payment_status']),
        ('Fee', f"₱{float(req['fee']):.2f}"),
        *([('Sticker Valid From', str(req.get('sticker_valid_from') or '—')), ('Sticker Valid Until', str(req.get('sticker_valid_until') or '—'))]
          if req_type == 'Vehicle Sticker Application' and (req.get('sticker_valid_from') or req.get('sticker_valid_until')) else []),
    ]))
    elements.append(Spacer(1, 12))
    elements.append(Paragraph('Request Details', styles['NFHHeading']))

    if req_type == 'Vehicle Sticker Application' and 'vehicles' in details:
        top_pairs = [(REQUEST_TYPE_FIELD_LABELS.get(k, k), v) for k, v in details.items() if k != 'vehicles']
        if top_pairs:
            elements.append(_kv_table(top_pairs))
            elements.append(Spacer(1, 8))
        veh_rows = [['#', 'Type', 'Plate No.', 'Model / Year', 'Color']]
        for i, v in enumerate(details.get('vehicles', []), start=1):
            veh_rows.append([str(i), v.get('type', ''), v.get('plate', ''), v.get('model', ''), v.get('color', '')])
        vt = Table(veh_rows, colWidths=[0.3 * inch, 1.2 * inch, 1.4 * inch, 1.7 * inch, 1.4 * inch])
        vt.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E2EBE2')),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#CCCCCC')),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
        ]))
        elements.append(vt)
    elif details:
        pairs = [(REQUEST_TYPE_FIELD_LABELS.get(k, k.replace('_', ' ').title()), v)
                 for k, v in details.items() if k not in ('oathAgreed',)]
        elements.append(_kv_table(pairs))
    else:
        elements.append(Paragraph('No additional details recorded.', styles['NFHBody']))

    if req.get('remarks'):
        elements.append(Spacer(1, 10))
        elements.append(Paragraph('Officer Remarks', styles['NFHHeading']))
        elements.append(Paragraph(req['remarks'], styles['NFHBody']))

    elements.append(Spacer(1, 24))
    elements.append(Paragraph(f"Generated on {now_str()} — For official HOA processing use only.", styles['NFHSub']))

    doc.build(elements)
    safe_id = re.sub(r'[^A-Za-z0-9_\-]', '_', req['id'])
    return buffer, f"NFH_{safe_id}.pdf"



def _safe_details(raw):
    try:
        return json.loads(raw or '{}') if not isinstance(raw, dict) else raw
    except Exception:
        return {}


def _pdf_logo(width=0.78*inch):
    logo_path=os.path.join(app.static_folder,'logo.png')
    if os.path.exists(logo_path):
        return Image(logo_path,width=width,height=width)
    return Spacer(1,width)


def _official_header(elements, styles):
    name=Paragraph('<b>NORTH FAIRWAY HOMES HOMEOWNERS ASSOCIATION INC.</b>', styles['NFHTitle'])
    addr=Paragraph('Sitio Pastol, Brgy. Muzon West, City of San Jose del Monte, Bulacan 3023', styles['NFHSub'])
    reg=Paragraph('HLURB Registration Number: NTR-20986-R', styles['NFHSub'])
    tin=Paragraph('TIN NO: 486-778-923-000', styles['NFHSub'])
    head=Table([[_pdf_logo(), [name,addr,reg,tin]]], colWidths=[0.95*inch,5.75*inch])
    head.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    elements.append(head)
    elements.append(Spacer(1,12))


def _officer_display_names(cursor):
    result={'Secretary':'NFH-HOA Secretary','President':'NFH-HOA President','Treasurer':'HOA Treasurer'}
    cursor.execute("""SELECT role, CONCAT(first_name,' ',last_name) AS full_name
                      FROM users WHERE role IN ('Secretary','President','Treasurer') AND status='Active'
                      ORDER BY id ASC""")
    for row in cursor.fetchall():
        if row['role'] not in result or result[row['role']].startswith('NFH') or result[row['role']].startswith('HOA'):
            result[row['role']]=row['full_name']
    return result


def _official_doc_context(req):
    details=_safe_details(req.get('details'))
    # The official issuance date belongs to the released document. Before actual
    # issuance, previews intentionally leave the issuance date blank instead of
    # pretending that the preview date is the legal/official issue date.
    issue_dt=req.get('issued_at')
    if isinstance(issue_dt,str):
        issue_text=issue_dt
    else:
        issue_text=issue_dt.strftime('%B %d, %Y') if issue_dt else ''
    vehicles=details.get('vehicles') or []
    first_vehicle=vehicles[0] if vehicles else {}
    vehicle_summary='; '.join(f"{v.get('type','')} — {v.get('plate','')} — {v.get('model','')} — {v.get('color','')}" for v in vehicles) or '—'
    submitted=req.get('date_submitted')
    if isinstance(submitted,str): request_date=submitted
    else: request_date=submitted.strftime('%B %d, %Y') if submitted else ''
    return {
        'homeowner': req.get('homeowner') or '', 'block': req.get('block') or details.get('blockNum') or '',
        'lot': req.get('lot') or details.get('lotNum') or '', 'address': req.get('address_line') or details.get('addressLine') or '',
        'issue_date': issue_text, 'request_date': request_date,
        'permit_no': details.get('permitNo') or details.get('constructionPermit') or '—',
        'materials': details.get('materialsList') or '—', 'mobile': req.get('mobile') or details.get('phoneNum') or '',
        'email': req.get('email') or details.get('emailAddr') or '', 'vehicle_summary': vehicle_summary,
        'vehicle_plate': first_vehicle.get('plate') or '', 'vehicle_brand': first_vehicle.get('brand') or '',
        'vehicle_model': first_vehicle.get('model') or '', 'vehicle_color': first_vehicle.get('color') or '',
        'vehicle_type': first_vehicle.get('type') or '',
        'tenant_name': details.get('fullName') or req.get('homeowner') or '',
        'previous_address': details.get('previousAddress') or '—', 'rented_address': details.get('rentedAddress') or '—',
        'owner_name': details.get('ownerName') or '—',
        'emergency_contact': f"{details.get('emergencyName') or '—'} / {details.get('emergencyPhone') or '—'}",
        'request_id': req.get('id') or '', 'request_type': req.get('request_type') or '', 'purpose': req.get('purpose') or '',
    }


def _fill_template_text(text, context):
    out=str(text or '')
    for k,v in context.items():
        out=out.replace('{'+k+'}', str(v if v is not None else ''))
    return out


def generate_official_document_pdf(req, template, officer_names=None):
    """Generate the actual NFH-HOA document requested by the homeowner.

    This is intentionally different from generate_request_pdf(), which is only an
    internal request record. The official document preview must look like the HOA
    document that will be printed/released and must not expose request-report
    metadata such as status, payment state, Request ID, or template version.
    """
    buffer = BytesIO()
    styles = _pdf_styles()
    elements = []
    doc = SimpleDocTemplate(
        buffer, pagesize=LETTER,
        topMargin=0.40 * inch, bottomMargin=0.48 * inch,
        leftMargin=0.62 * inch, rightMargin=0.62 * inch,
    )
    _official_header(elements, styles)

    ctx = _official_doc_context(req)
    details = _safe_details(req.get('details'))
    key = (template or {}).get('template_key') or 'generic'
    names = officer_names or {
        'Secretary': 'NFH-HOA Secretary',
        'President': 'NFH-HOA President',
        'Treasurer': 'HOA Treasurer',
    }
    ctx.update({
        'secretary_name': names.get('Secretary', 'NFH-HOA Secretary'),
        'treasurer_name': names.get('Treasurer', 'HOA Treasurer'),
        'president_name': names.get('President', 'NFH-HOA President'),
    })
    body = _fill_template_text((template or {}).get('body_text') or '', ctx)
    footer = _fill_template_text((template or {}).get('footer_text') or '', ctx)
    title = ((template or {}).get('title') or req.get('request_type') or 'NFH-HOA DOCUMENT').strip()

    title_style = ParagraphStyle(
        'OfficialDocTitle', parent=styles['NFHTitle'], fontSize=17, leading=21,
        alignment=TA_CENTER, fontName='Helvetica-Bold', spaceAfter=20,
        textColor=colors.black,
    )
    body_style = ParagraphStyle(
        'OfficialDocBody', parent=styles['NFHBody'], fontSize=10.5, leading=16,
        textColor=colors.black, spaceAfter=10,
    )
    body_center = ParagraphStyle(
        'OfficialDocBodyCenter', parent=body_style, alignment=TA_CENTER,
    )
    small_style = ParagraphStyle(
        'OfficialSmall', parent=styles['NFHBody'], fontSize=9, leading=12,
        textColor=colors.black,
    )

    def official_title(text=None):
        elements.append(Paragraph(html.escape(text or title), title_style))

    def line_value(value, width=2.0*inch):
        text = html.escape(str(value or ''))
        return Paragraph(f'<u>{text if text else "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"}</u>', body_style)

    def role_signature_flow(role, caption=None):
        asset = ((req or {}).get('_signature_assets') or {}).get(role) or {}
        officer_name = asset.get('name') or names.get(role) or f'NFH-HOA {role}'
        flows = []
        if caption:
            flows.append(Paragraph(f'<b>{html.escape(caption)}</b>', body_center))
            flows.append(Spacer(1, 7))
        if asset.get('path'):
            try:
                im = Image(asset['path'], width=1.35*inch, height=0.52*inch)
                im.hAlign = 'CENTER'
                flows.append(im)
            except Exception:
                flows.append(Spacer(1, 0.52*inch))
        else:
            flows.append(Spacer(1, 0.52*inch))
        flows.append(Paragraph('____________________________', styles['NFHSub']))
        flows.append(Paragraph(
            f'<b>{html.escape(officer_name)}</b><br/>NFH-HOA {html.escape(role)}',
            styles['NFHSub'],
        ))
        return flows

    def add_signature_row(role_specs):
        # role_specs: [(role, caption), ...]
        cells = [role_signature_flow(role, caption) for role, caption in role_specs]
        if not cells:
            return
        table = Table([cells], colWidths=[6.25*inch/len(cells)]*len(cells))
        table.setStyle(TableStyle([
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'BOTTOM'),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ]))
        elements.append(table)

    # ------------------------------------------------------------------
    # Official NFH-HOA forms based on the supplied physical documents.
    # ------------------------------------------------------------------
    if key == 'cert-improvement':
        official_title('CERTIFICATE OF IMPROVEMENT')
        elements.append(Spacer(1, 18))
        elements.append(Paragraph(
            f'This is to certify that <b>{html.escape(ctx["homeowner"])}</b>, the owner of the unit in '
            f'Blk <b>{html.escape(str(ctx["block"]))}</b> Lot <b>{html.escape(str(ctx["lot"]))}</b> of North '
            f'Fairway Homes, Brgy. Muzon West, City of San Jose del Monte, Bulacan, is going to '
            f'<b>IMPROVE</b> their unit.', body_style))
        elements.append(Spacer(1, 24))
        elements.append(Paragraph(
            f'<b>ISSUED</b> this {html.escape(ctx["issue_date"] or "____________")} at North Fairway Homes, '
            'Brgy. Muzon West, City of San Jose del Monte, Bulacan.', body_style))
        elements.append(Spacer(1, 38))
        add_signature_row([('Secretary', None), ('President', None)])

    elif key == 'proof-residency':
        official_title('Proof of Residency')
        elements.append(Spacer(1, 20))
        elements.append(Paragraph(
            f'This is to certify that <b>{html.escape(ctx["homeowner"])}</b> of Block '
            f'<b>{html.escape(str(ctx["block"]))}</b> Lot <b>{html.escape(str(ctx["lot"]))}</b> of North '
            'Fairway Homes, Brgy. Muzon West, City of San Jose del Monte, Bulacan is a bona fide resident '
            'of North Fairway Homes Homeowners Association Inc.', body_style))
        elements.append(Spacer(1, 15))
        elements.append(Paragraph(f'<b>Owner/Member:</b> {html.escape(ctx["homeowner"])}', body_style))
        elements.append(Spacer(1, 12))
        elements.append(Paragraph(
            f'Issued this {html.escape(ctx["issue_date"] or "____________")} at North Fairway Homes, '
            'Brgy. Muzon West, City of San Jose del Monte, Bulacan.', body_style))
        elements.append(Spacer(1, 36))
        add_signature_row([('Secretary', None), ('President', None)])

    elif key in {'move-in', 'move-out'}:
        official_title('NFH/HOA GATE PASS FOR MOVE-IN' if key == 'move-in' else 'NFH/HOA GATE PASS FOR MOVE-OUT')
        elements.append(Paragraph('<b>TO WHOM IT MAY CONCERN:</b>', body_style))
        elements.append(Spacer(1, 12))
        if key == 'move-in':
            sentence = (
                f'This is to certify that <b>{html.escape(ctx["homeowner"])}</b>, the owner of the unit in '
                f'Blk <b>{html.escape(str(ctx["block"]))}</b> Lot <b>{html.escape(str(ctx["lot"]))}</b>, is going '
                'to move in to their unit. They are allowed to bring the needed things inside the subdivision '
                'for their transfer to North Fairway Homes.'
            )
        else:
            sentence = (
                f'This is to certify that <b>{html.escape(ctx["homeowner"])}</b>, the owner of the unit in '
                f'Blk <b>{html.escape(str(ctx["block"]))}</b> Lot <b>{html.escape(str(ctx["lot"]))}</b>, is going '
                'to move out to another place. They are allowed to bring the needed things outside the '
                'subdivision for their transfer.'
            )
        elements.append(Paragraph(sentence, body_style))
        elements.append(Spacer(1, 15))
        elements.append(Paragraph(
            f'<b>ISSUED</b> this {html.escape(ctx["issue_date"] or "____________")} at North Fairway Homes, '
            'Brgy. Muzon West, City of San Jose del Monte, Bulacan. This is upon the request of the interested '
            'party for whatever legal purpose it may serve.', body_style))
        elements.append(Spacer(1, 34))
        add_signature_row([('Secretary', None), ('President', None)])

    elif key == 'cert-membership':
        official_title('Certificate of Membership')
        elements.append(Spacer(1, 20))
        elements.append(Paragraph(
            f'This is to certify that <b>{html.escape(ctx["homeowner"])}</b> of Block '
            f'<b>{html.escape(str(ctx["block"]))}</b> Lot <b>{html.escape(str(ctx["lot"]))}</b> of North '
            'Fairway Homes Subdivision, Brgy. Muzon West, City of San Jose del Monte, Bulacan is now a bona fide '
            'member of the North Fairway Homes Homeowners Association Inc.', body_style))
        elements.append(Spacer(1, 18))
        elements.append(Paragraph(
            f'Issued this {html.escape(ctx["issue_date"] or "____________")} at North Fairway Homes, '
            'Brgy. Muzon West, City of San Jose del Monte, Bulacan.', body_style))
        elements.append(Spacer(1, 36))
        add_signature_row([('Secretary', None), ('President', None)])

    elif key == 'gate-pass':
        official_title('GATE PASS')
        # The supplied NFH-HOA Gate Pass is a printable permit, not a request report.
        top = Table([
            [Paragraph('<b>NAME:</b>', body_style), Paragraph(html.escape(ctx['homeowner']), body_style)],
            [Paragraph('<b>ADDRESS:</b>', body_style), Paragraph(
                f'BLK {html.escape(str(ctx["block"]))} &nbsp;&nbsp;&nbsp; LOT {html.escape(str(ctx["lot"]))}', body_style)],
        ], colWidths=[1.05*inch, 5.05*inch])
        top.setStyle(TableStyle([
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('LINEBELOW',(1,0),(1,-1),0.65,colors.black),
            ('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),4),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
        ]))
        elements.append(top)
        elements.append(Spacer(1, 22))
        permit = html.escape(str(ctx['permit_no'] or ''))
        materials = [x.strip() for x in str(ctx['materials'] or '').split('\n') if x.strip()]
        left_lines = [Paragraph('<b>List of Materials</b>', body_style)]
        for i in range(6):
            value = materials[i] if i < len(materials) else ''
            left_lines.append(Paragraph(html.escape(value) + ('_'*1 if value else '&nbsp;'), small_style))
            left_lines.append(HRFlowable(width='100%', thickness=0.55, color=colors.black, spaceBefore=1, spaceAfter=3))
        right_lines = [Paragraph('<b>Major/Minor Construction Permit number:</b>', body_style),
                       Paragraph(permit or '&nbsp;', small_style),
                       HRFlowable(width='100%', thickness=0.55, color=colors.black, spaceBefore=1, spaceAfter=3)]
        for _ in range(5):
            right_lines.extend([Paragraph('&nbsp;', small_style), HRFlowable(width='100%', thickness=0.55, color=colors.black, spaceBefore=1, spaceAfter=3)])
        grid = Table([[left_lines, right_lines]], colWidths=[3.0*inch, 3.0*inch], hAlign='CENTER')
        grid.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8)]))
        elements.append(grid)
        elements.append(Spacer(1, 15))
        elements.append(Paragraph('<b>Remarks:</b> _________________________________________________', body_style))
        elements.append(Spacer(1, 26))
        add_signature_row([('Treasurer', 'Checked By:'), ('President', 'Approved By:')])

    elif key == 'vehicle-sticker':
        official_title('VEHICLE STICKER APPLICATION FORM')
        if body.strip():
            elements.append(Paragraph(html.escape(body.strip()), ParagraphStyle(
                'VehicleIntroOfficial', parent=small_style, fontSize=8.9, leading=13, spaceAfter=14)))
        app_type = details.get('applicationType') or 'New'
        resident_type = details.get('residentType') or 'HOA Member'
        app_info = Table([
            [Paragraph(f'☐ New &nbsp;&nbsp; ☐ Renewal &nbsp;&nbsp; <b>Selected:</b> {html.escape(str(app_type))}', small_style),
             Paragraph(f'<b>Date:</b> {html.escape(str(details.get("applicationDate") or ctx["request_date"] or ""))}', small_style)],
            [Paragraph(f'☐ HOA MEMBER &nbsp;&nbsp; ☐ Renter &nbsp;&nbsp; <b>Selected:</b> {html.escape(str(resident_type))}', small_style),
             Paragraph(f'<b>Term of Lease:</b> {html.escape(str(details.get("termOfLease") or "—"))}', small_style)],
        ], colWidths=[3.7*inch,2.35*inch])
        app_info.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        elements.append(app_info)
        elements.append(Spacer(1,10))
        elements.append(_kv_table([
            ("Applicant's Name", ctx['homeowner']), ('Cellphone No.', ctx['mobile']),
            ('Block and Lot', f"{ctx['block']} / {ctx['lot']}"), ('Email Address', ctx['email']),
        ], col_widths=(1.45*inch,4.65*inch)))
        elements.append(Spacer(1,12))
        elements.append(Paragraph('<b>VEHICLE INFORMATION</b>', body_center))
        rows=[['PLATE NO.','BRAND','YEAR / MODEL','BODY COLOR','VEHICLE TYPE','STICKER NO.']]
        for v in details.get('vehicles',[]):
            rows.append([v.get('plate',''),v.get('brand',''),v.get('model',''),v.get('color',''),v.get('type',''),''])
        while len(rows)<5: rows.append(['','','','','',''])
        t=Table(rows,colWidths=[1.0*inch,0.9*inch,1.2*inch,0.95*inch,1.05*inch,0.95*inch])
        t.setStyle(TableStyle([
            ('GRID',(0,0),(-1,-1),0.55,colors.black),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('FONTSIZE',(0,0),(-1,-1),7.7),('ALIGN',(0,0),(-1,0),'CENTER'),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),
        ]))
        elements.append(t)
        elements.append(Spacer(1,12))
        elements.append(Paragraph('I hereby certify that all information provided herein is true and correct.', small_style))
        if req.get('sticker_valid_from') or req.get('sticker_valid_until'):
            elements.append(Spacer(1,8))
            elements.append(Paragraph(
                f'<b>Sticker Validity:</b> {html.escape(str(req.get("sticker_valid_from") or "—"))} to '
                f'{html.escape(str(req.get("sticker_valid_until") or "—"))}', small_style))
        elements.append(Spacer(1,30))
        elements.append(Paragraph('Applicant Signature over Printed Name: _______________________________', body_center))
        elements.append(Spacer(1,22))
        required = _signature_roles(template)
        if required:
            add_signature_row([(r, 'Verified/Issued by:' if i == 0 else None) for i, r in enumerate(required)])
        else:
            elements.append(Paragraph('Verified/Issued by: _______________________________<br/>HOA Representative', body_center))

    elif key == 'tenant-form':
        official_title('RENTER’S / TENANT’S INFORMATION FORM')
        pairs = [
            ('Full Name Head of Renter / Tenant', details.get('fullName') or ctx['homeowner']),
            ('Gender', details.get('gender') or '—'), ('Age', details.get('age') or '—'),
            ('Cellphone Number / Tel. #', details.get('phoneNum') or '—'),
            ('Occupation', details.get('occupation') or '—'), ('Birthdate', details.get('dob') or '—'),
            ('Civil Status', details.get('civilStatus') or '—'), ('Spouse Name', details.get('spouseName') or '—'),
            ('Spouse Age', details.get('spouseAge') or '—'), ('Spouse Contact', details.get('spousePhone') or '—'),
            ('Children / House Companions', details.get('houseCompanions') or '—'),
            ('Previous Address', details.get('previousAddress') or '—'),
            ('Address of Unit to be Rented', details.get('rentedAddress') or '—'),
            ('Name of Unit Owner', details.get('ownerName') or '—'),
            ('Cellphone / Tel. # of Unit Owner', details.get('ownerPhone') or '—'),
            ('Emergency Contact', ctx['emergency_contact']),
        ]
        elements.append(_kv_table(pairs, col_widths=(2.15*inch,3.95*inch)))
        elements.extend([
            Spacer(1,10), Paragraph('<b>Requirements to be submitted to HOA:</b>', body_style),
            Paragraph('• Barangay Clearance from Previous Address<br/>• Authorization Letter to Occupy from the Owner of the Unit<br/>• Move-in Permit from NFH-HOA<br/>• Xerox Copy of Valid ID of Renter and companion(s)', small_style),
            Spacer(1,12), Paragraph('<b>OATH OF RENTER / TENANT</b>', body_center),
        ])
        oath = _fill_template_text((template or {}).get('body_text') or '', ctx)
        elements.append(Paragraph(html.escape(oath).replace('\n','<br/>'), small_style))
        elements.append(Spacer(1,20))
        elements.append(Paragraph('Signature of Renter: _______________________________', small_style))
        elements.append(Spacer(1,20))
        elements.append(Paragraph('Witnessed by: _______________________________', small_style))

    else:
        # Generic administrator-created service: still render as an official HOA
        # document, not as a request-status report.
        official_title(title)
        for para in body.split('\n'):
            if para.strip():
                elements.append(Paragraph(html.escape(para.strip()), body_style))
        if footer.strip():
            elements.extend([Spacer(1,14), Paragraph(html.escape(footer).replace('\n','<br/>'), body_style)])
        required = _signature_roles(template)
        if required:
            elements.append(Spacer(1,28))
            add_signature_row([(role, None) for role in required])

    # NOTE: Deliberately no Request ID, workflow status, payment status, template
    # version, audit metadata, or "generated on" footer here. Those belong to the
    # internal request record, not to the official HOA document being previewed.
    doc.build(elements)
    return buffer, f"NFH_{re.sub(r'[^A-Za-z0-9_-]','_', req.get('id') or 'document')}_{key}.pdf"



@app.route('/requests/<req_id>/document.pdf')
def request_document_pdf(req_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""SELECT r.*, CONCAT(u.first_name,' ',u.last_name) AS homeowner,u.block,u.lot,u.address_line,u.email,u.mobile
                              FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=%s""",(req_id,)); req=cursor.fetchone()
            if not req: flash('Request not found.','error'); return redirect(url_for('index'))
            if session.get('role')=='Homeowner' and req['user_id']!=session.get('user_id'): return redirect(url_for('homeowner'))
            if session.get('role')=='Homeowner' and req['status'] != 'Issued / Completed':
                flash('The official document becomes available in your portal after it has been physically released to you.','warning'); return redirect(url_for('homeowner'))
            template=None
            if req.get('document_template_id'):
                cursor.execute("SELECT * FROM document_templates WHERE id=%s",(req['document_template_id'],)); template=cursor.fetchone()
            if not template: template=get_active_document_template(cursor,request_type=req['request_type'])
            names=_officer_display_names(cursor)
            req['_signature_assets']=request_signature_assets(cursor, req_id, template) if template else {}

            # Treasurer/Admin may prepare the official print copy during the
            # face-to-face payment/release transaction. The selected transaction
            # date is used only for this generated PDF; the database is not marked
            # Issued until release is actually confirmed.
            requested_issue_date=(request.args.get('issue_date') or '').strip()
            if requested_issue_date and session.get('role') in ['Treasurer','Admin'] and req.get('status') in ('Ready for Payment & Release','Paid - Awaiting Release','Awaiting Payment Verification','Approved'):
                try:
                    req['issued_at']=datetime.strptime(requested_issue_date,'%Y-%m-%d')
                except ValueError:
                    pass
        if not template:
            buffer,filename=generate_request_pdf(req)
        else:
            buffer,filename=generate_official_document_pdf(req,template,names)
        if session.get('role') != 'Homeowner':
            audit_action='Download Document for Printing' if str(request.args.get('download','')).lower() in {'1','true','yes'} else 'View Generated Document'
            log_audit_action(session.get('username'),session.get('role'),audit_action,req_id)
        return _response_pdf(buffer,filename)
    finally: conn.close()


@app.route('/document-templates/<template_key>/preview')
def document_template_preview(template_key):
    if 'user_id' not in session or session.get('role') not in OFFICER_ROLES: return redirect(url_for('index'))
    conn=get_db_connection()
    try:
        with conn.cursor() as cursor:
            template=get_active_document_template(cursor,template_key=template_key)
            names=_officer_display_names(cursor)
            sample_signatures={}
            for role in _signature_roles(template):
                officer=_current_role_signature(cursor,role)
                sample_signatures[role]={'name':(officer or {}).get('full_name') or f'NFH-HOA {role}','path':_signature_abs((officer or {}).get('e_signature_path'))}
        if not template: flash('Template not found.','error'); return redirect(url_for('officer'))
        sample={'id':'SAMPLE-PREVIEW','request_type':template.get('service_name') or template.get('title'),'homeowner':'Juan Dela Cruz','block':'1','lot':'12','email':'homeowner@example.com','mobile':'09170000000','details':json.dumps({'materialsList':'Cement\nSand\nSteel Bars','vehicles':[{'type':'4 Wheels','plate':'ABC 1234','brand':'Toyota','model':'Vios 2026','color':'Black'}],'fullName':'Sample Tenant','previousAddress':'Previous Address','rentedAddress':'Block 1 Lot 12','ownerName':'Juan Dela Cruz','emergencyName':'Maria Dela Cruz','emergencyPhone':'09170000000'}),'status':'Preview','payment_status':'Not Required','fee':0,'date_submitted':datetime.now(),'document_prepared_at':datetime.now()}
        sample['_signature_assets']=sample_signatures
        buffer,filename=generate_official_document_pdf(sample,template,names); return _response_pdf(buffer,'PREVIEW_'+filename)
    finally: conn.close()




@app.route('/requests/<req_id>/pdf')
def request_pdf(req_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("""SELECT r.*, CONCAT(u.first_name,' ',u.last_name) AS homeowner
                              FROM requests r JOIN users u ON r.user_id=u.id WHERE r.id=%s""", (req_id,))
            req = cursor.fetchone()
        conn.close()
        if not req:
            flash('Request not found.', 'error')
            return redirect(url_for('index'))
        role = session.get('role')
        if role == 'Homeowner' and req['user_id'] != session.get('user_id'):
            flash('You are not authorized to view this document.', 'error')
            return redirect(url_for('homeowner'))
        if role not in ['Homeowner'] + OFFICER_ROLES:
            return redirect(url_for('index'))

        buffer, filename = generate_request_pdf(req)
        if session.get('role') != 'Homeowner':
            preview_mode = str(request.args.get('preview', '')).strip().lower() in {'1', 'true', 'yes', 'inline'}
            log_audit_action(session.get('username'), session.get('role'),
                             'Preview Request Record' if preview_mode else 'Download Request PDF', req_id)
        return _response_pdf(buffer, filename)
    except Exception:
        app.logger.exception('Request PDF generation failed for %s', req_id)
        flash('Unable to generate the request PDF right now. Please try again.', 'error')
        return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Admin PDF reports (with filters)
# ---------------------------------------------------------------------------
def _require_admin():
    return 'user_id' in session and session.get('role') == 'Admin'


def _filters_paragraph(styles, filters):
    active = [f"{k}: {v}" for k, v in filters.items() if v]
    text = 'Filters applied: ' + (', '.join(active) if active else 'None (showing all records)')
    return Paragraph(text, styles['NFHSub'])


@app.route('/admin/reports/audit-trail/pdf')
def report_audit_trail_pdf():
    if not _require_admin():
        return redirect(url_for('index'))
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    username = request.args.get('username', '')
    role = request.args.get('role', '')
    action = request.args.get('action', '')

    query = "SELECT * FROM audit_logs WHERE role IN ('Admin','Secretary','Treasurer','President','Vice President','Board Director')"
    params = []
    if date_from:
        query += " AND timestamp >= %s"; params.append(date_from + ' 00:00:00')
    if date_to:
        query += " AND timestamp <= %s"; params.append(date_to + ' 23:59:59')
    if username:
        query += " AND username LIKE %s"; params.append(f"%{username}%")
    if role:
        query += " AND role = %s"; params.append(role)
    if action:
        query += " AND action LIKE %s"; params.append(f"%{action}%")
    query += " ORDER BY timestamp ASC"

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.5 * inch, rightMargin=0.5 * inch)
    styles = _pdf_styles()
    elements = []
    _pdf_letterhead(elements, styles, 'Audit Trail Report')
    elements.append(_filters_paragraph(styles, {
        'Date From': date_from, 'Date To': date_to, 'User': username, 'Role': role, 'Action': action}))
    elements.append(Spacer(1, 10))

    table_rows = [['Timestamp', 'User', 'Role', 'Action', 'Item', 'Previous', 'New']]
    for r in rows:
        table_rows.append([format_pdf_datetime(r['timestamp']), r['username'] or '-', r['role'] or '-', r['action'],
                           r['item'] or '-', (r['prev_val'] or '-')[:20], (r['new_val'] or '-')[:20]])
    if len(table_rows) == 1:
        table_rows.append(['No matching records', '', '', '', '', '', ''])
    t = Table(table_rows, repeatRows=1, colWidths=[1.1 * inch, 0.9 * inch, 0.7 * inch, 1.2 * inch, 1.0 * inch, 0.8 * inch, 0.8 * inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B4332')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 16))
    elements.append(Paragraph(f"Generated on {now_str()} by {session.get('username')}.", styles['NFHSub']))
    doc.build(elements)
    log_audit_action(session.get('username'), session.get('role'), 'Generate Audit Trail PDF', 'audit_logs')
    return _response_pdf(buffer, 'NFH_Audit_Trail_Report.pdf')


@app.route('/admin/reports/requests/pdf')
def report_requests_pdf():
    if not _require_admin():
        return redirect(url_for('index'))
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    status = request.args.get('status', '')
    req_type = request.args.get('request_type', '')
    payment_status = request.args.get('payment_status', '')

    query = """SELECT r.*, CONCAT(u.first_name,' ',u.last_name) AS homeowner FROM requests r
               JOIN users u ON r.user_id = u.id WHERE 1=1"""
    params = []
    if date_from:
        query += " AND r.date_submitted >= %s"; params.append(date_from + ' 00:00:00')
    if date_to:
        query += " AND r.date_submitted <= %s"; params.append(date_to + ' 23:59:59')
    if status:
        query += " AND r.status = %s"; params.append(status)
    if req_type:
        query += " AND r.request_type = %s"; params.append(req_type)
    if payment_status:
        query += " AND r.payment_status = %s"; params.append(payment_status)
    query += " ORDER BY r.date_submitted DESC"

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.5 * inch, rightMargin=0.5 * inch)
    styles = _pdf_styles()
    elements = []
    _pdf_letterhead(elements, styles, 'Requests Report')
    elements.append(_filters_paragraph(styles, {
        'Date From': date_from, 'Date To': date_to, 'Status': status,
        'Type': req_type, 'Payment': payment_status}))
    elements.append(Spacer(1, 10))

    table_rows = [['ID', 'Homeowner', 'Type', 'Status', 'Payment', 'Submitted']]
    for r in rows:
        table_rows.append([r['id'], r['homeowner'], r['request_type'], r['status'], r['payment_status'],
                           format_pdf_datetime(r['date_submitted'])])
    if len(table_rows) == 1:
        table_rows.append(['No matching records', '', '', '', '', ''])
    t = Table(table_rows, repeatRows=1, colWidths=[1.3 * inch, 1.3 * inch, 1.5 * inch, 1.1 * inch, 0.8 * inch, 1.1 * inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B4332')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
    ]))
    elements.append(t)
    doc.build(elements)
    log_audit_action(session.get('username'), session.get('role'), 'Generate Requests PDF', 'requests')
    return _response_pdf(buffer, 'NFH_Requests_Report.pdf')


@app.route('/admin/reports/vehicle-stickers/pdf')
def report_vehicle_stickers_pdf():
    if not _require_admin():
        return redirect(url_for('index'))
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    status = request.args.get('status', '')

    query = """SELECT r.*, CONCAT(u.first_name,' ',u.last_name) AS homeowner FROM requests r
               JOIN users u ON r.user_id = u.id WHERE r.request_type = 'Vehicle Sticker Application'"""
    params = []
    if date_from:
        query += " AND r.date_submitted >= %s"; params.append(date_from + ' 00:00:00')
    if date_to:
        query += " AND r.date_submitted <= %s"; params.append(date_to + ' 23:59:59')
    if status:
        query += " AND r.status = %s"; params.append(status)
    query += " ORDER BY r.date_submitted DESC"

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.5 * inch, rightMargin=0.5 * inch)
    styles = _pdf_styles()
    elements = []
    _pdf_letterhead(elements, styles, 'Vehicle Stickers Report')
    elements.append(_filters_paragraph(styles, {'Date From': date_from, 'Date To': date_to, 'Status': status}))
    elements.append(Spacer(1, 10))

    table_rows = [['Req ID', 'Homeowner', 'Plate No.', 'Type', 'Model/Year', 'Color', 'Status']]
    for r in rows:
        try:
            details = json.loads(r.get('details') or '{}')
        except Exception:
            details = {}
        vehicles = details.get('vehicles', [])
        if not vehicles:
            table_rows.append([r['id'], r['homeowner'], '-', '-', '-', '-', r['status']])
        for v in vehicles:
            table_rows.append([r['id'], r['homeowner'], v.get('plate', ''), v.get('type', ''),
                               v.get('model', ''), v.get('color', ''), r['status']])
    if len(table_rows) == 1:
        table_rows.append(['No matching records', '', '', '', '', '', ''])
    t = Table(table_rows, repeatRows=1, colWidths=[1.1 * inch, 1.2 * inch, 0.9 * inch, 0.9 * inch, 1.1 * inch, 0.8 * inch, 0.9 * inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B4332')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
    ]))
    elements.append(t)
    doc.build(elements)
    log_audit_action(session.get('username'), session.get('role'), 'Generate Vehicle Stickers PDF', 'requests')
    return _response_pdf(buffer, 'NFH_Vehicle_Stickers_Report.pdf')


@app.route('/admin/reports/adjustments/pdf')
def report_adjustments_pdf():
    if not _require_admin():
        return redirect(url_for('index'))
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    query = "SELECT * FROM financial_adjustments WHERE 1=1"
    params = []
    if date_from:
        query += " AND created_at >= %s"; params.append(date_from + ' 00:00:00')
    if date_to:
        query += " AND created_at <= %s"; params.append(date_to + ' 23:59:59')
    query += " ORDER BY created_at DESC"

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.5 * inch, rightMargin=0.5 * inch)
    styles = _pdf_styles()
    elements = []
    _pdf_letterhead(elements, styles, 'Missing Monthly Dues Report')
    elements.append(_filters_paragraph(styles, {'Date From': date_from, 'Date To': date_to}))
    elements.append(Spacer(1, 10))

    table_rows = [['ID', 'Homeowner', 'Monthly Due', 'Missing Months', 'Total', 'Submitted By', 'Date']]
    for r in rows:
        table_rows.append([r['id'], r['homeowner_name'], f"₱{float(r['monthly_due']):.2f}",
                           r['missing_months'], f"₱{float(r['proposed_adjustment']):.2f}",
                           r['submitted_by'], str(r['created_at'])])
    if len(table_rows) == 1:
        table_rows.append(['No matching records', '', '', '', '', '', ''])
    t = Table(table_rows, repeatRows=1, colWidths=[0.5 * inch, 1.3 * inch, 0.9 * inch, 0.9 * inch, 0.8 * inch, 1.0 * inch, 1.1 * inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B4332')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
    ]))
    elements.append(t)
    doc.build(elements)
    log_audit_action(session.get('username'), session.get('role'), 'Generate Missing Dues PDF', 'financial_adjustments')
    return _response_pdf(buffer, 'NFH_Missing_Dues_Report.pdf')



@app.route('/admin/reports/officers/pdf')
def report_officers_pdf():
    if not _require_admin():
        return redirect(url_for('index'))
    role = request.args.get('role', '')
    status = request.args.get('status', '')

    query = "SELECT * FROM users WHERE role IN ('Secretary','Treasurer','President','Vice President','Board Director')"
    params = []
    if role:
        query += " AND role = %s"; params.append(role)
    if status:
        query += " AND status = %s"; params.append(status)
    query += " ORDER BY created_at DESC"

    conn = get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    conn.close()

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.5 * inch, rightMargin=0.5 * inch)
    styles = _pdf_styles()
    elements = []
    _pdf_letterhead(elements, styles, 'Officer / User Records Report')
    elements.append(_filters_paragraph(styles, {'Role': role, 'Status': status}))
    elements.append(Spacer(1, 10))

    table_rows = [['ID', 'Full Name', 'Username', 'Email', 'Phone', 'Role', 'Status']]
    for r in rows:
        table_rows.append([r['id'], f"{r['first_name']} {r['last_name']}", r['username'],
                           r['email'], r['mobile'], r['role'], r['status']])
    if len(table_rows) == 1:
        table_rows.append(['No matching records', '', '', '', '', '', ''])
    t = Table(table_rows, repeatRows=1, colWidths=[0.4 * inch, 1.3 * inch, 1.0 * inch, 1.5 * inch, 0.9 * inch, 0.8 * inch, 0.7 * inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1B4332')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#CCCCCC')),
    ]))
    elements.append(t)
    doc.build(elements)
    log_audit_action(session.get('username'), session.get('role'), 'Generate Officer Records PDF', 'users')
    return _response_pdf(buffer, 'NFH_Officer_Records_Report.pdf')


@app.route('/admin/reports/processing-time/pdf')
def report_processing_time_pdf():
    if not _require_admin():
        return redirect(url_for('index'))
    request_type = (request.args.get('request_type') or '').strip()
    query = """SELECT r.id, r.request_type,
                      CONCAT(u.first_name,' ',u.last_name) AS homeowner,
                      r.date_submitted, h.reviewed_at, r.executive_decided_at,
                      r.ready_for_release_at, r.issued_at,
                      ROUND(TIMESTAMPDIFF(MINUTE,r.date_submitted,r.issued_at)/60,2) AS total_hours
               FROM requests r
               JOIN users u ON u.id=r.user_id
               LEFT JOIN (
                   SELECT request_id, MIN(created_at) AS reviewed_at
                   FROM request_history WHERE action='Initial Review' GROUP BY request_id
               ) h ON h.request_id=r.id
               WHERE r.status='Issued / Completed' AND r.issued_at IS NOT NULL"""
    params=[]
    if request_type:
        query += " AND r.request_type=%s"
        params.append(request_type)
    query += " ORDER BY r.issued_at DESC"
    conn=get_db_connection()
    with conn.cursor() as cursor:
        cursor.execute(query, tuple(params))
        rows=cursor.fetchall()
    conn.close()
    buffer=BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=LETTER,topMargin=0.6*inch,bottomMargin=0.6*inch,leftMargin=0.45*inch,rightMargin=0.45*inch)
    styles=_pdf_styles(); elements=[]
    _pdf_letterhead(elements,styles,'Request Processing Time Report')
    elements.append(_filters_paragraph(styles,{'Request Type':request_type}))
    elements.append(Spacer(1,10))
    table_rows=[['Request ID','Homeowner','Type','Submitted','Reviewed','Ready','Issued','Total Hrs']]
    for r in rows:
        table_rows.append([r['id'],r['homeowner'],r['request_type'],format_pdf_datetime(r['date_submitted']),
                           format_pdf_datetime(r.get('reviewed_at')),format_pdf_datetime(r.get('ready_for_release_at')),
                           format_pdf_datetime(r.get('issued_at')),r.get('total_hours') if r.get('total_hours') is not None else '—'])
    if len(table_rows)==1:
        table_rows.append(['No completed requests','','','','','','',''])
    t=Table(table_rows,repeatRows=1,colWidths=[0.8*inch,1.0*inch,1.05*inch,1.0*inch,0.9*inch,0.9*inch,0.9*inch,0.6*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1B4332')),('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('FONTSIZE',(0,0),(-1,-1),6.8),('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#CCCCCC')),
        ('VALIGN',(0,0),(-1,-1),'TOP')]))
    elements.append(t); doc.build(elements)
    log_audit_action(session.get('username'),session.get('role'),'Generate Processing Time PDF','requests')
    return _response_pdf(buffer,'NFH_Processing_Time_Report.pdf')


# ===========================================================================
if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() in {'1','true','yes','on'}
    app.run(debug=debug_mode, host=os.getenv('FLASK_HOST','127.0.0.1'), port=int(os.getenv('FLASK_PORT','5000')))
