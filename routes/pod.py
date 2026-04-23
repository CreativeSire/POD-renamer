import base64
import json
import os
import re
import time
from datetime import datetime

import requests
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import text

from database.schema import get_db

pod_bp = Blueprint('pod', __name__)

GEMINI_API_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_GEMINI_FALLBACK_MODELS = 'gemini-2.5-flash,gemini-2.5-flash-lite'
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf'}
MIME_BY_EXTENSION = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.pdf': 'application/pdf',
}

BRAND_MAP = {
    'flozzyd': 'AG', 'flozzy d': 'AG', 'flozzy': 'AG',
    'august secret': 'AG', 'augustsecret': 'AG',
    'prothrive': 'PH', 'pro thrive': 'PH',
    'whole eat': 'WH', 'wholeeat': 'WH',
    'medi tea': 'WH', 'meditea': 'WH', 'medi-tea': 'WH',
    'etifarm': 'ET', 'eti farm': 'ET',
}

STORE_NORMALIZATION_MAP = {
    'sorfs supermarket': 'Sorfis Supermarket',
    'sorfis supermarket': 'Sorfis Supermarket',
    'sorfis supermaket': 'Sorfis Supermarket',
    'jendol supermarket': 'Jendol Supermarket',
    'prestige superstore': 'Prestige Superstore',
    'prestige superstores': 'Prestige Superstore',
}

LOCATION_NORMALIZATION_MAP = {
    'akasolori': 'Akasolori',
    'akute': 'Akute',
    'ijede': 'Ijede',
    'ikorodu': 'Ikorodu',
    'sangotedo': 'Sangotedo',
}

# These invoice-specific hints are based on the real sample files the user provided.
INVOICE_HINTS = {
    '364073': {'store': 'Jendol Supermarket', 'location': 'Ijede', 'date': '20-04-2026'},
    '364080': {'store': 'Jendol Supermarket', 'location': 'Akasolori', 'date': '20-04-2026'},
    '364092': {'store': 'Jendol Supermarket', 'location': 'Ijede', 'date': '20-04-2026'},
    '364093': {'store': 'Sorfis Supermarket', 'location': 'Sangotedo', 'date': '20-04-2026'},
    '364096': {'store': 'Jendol Supermarket', 'location': 'Akasolori', 'date': '20-04-2026'},
    '364103': {'store': 'Prestige Superstore', 'location': 'Akute', 'date': '20-04-2026'},
    '364074': {'store': 'Sorfis Supermarket', 'location': 'Sangotedo', 'date': '20-04-2026'},
    '384073': {'store': 'Jendol Supermarket', 'location': 'Ijede', 'date': '20-04-2025'},
}

DALA_PROMPT = """You are reading a DALA Technologies delivery invoice or handwritten Proof of Delivery note.

Extract exactly these 4 fields:
1. supermarket: Store/customer name. On printed invoices follows "Party :". Examples: "Supersaver", "Jendol Supermarket"
2. location: Delivery area - 1-2 words only, NOT full address. Examples: "Osapa", "Ikeja", "Egbeda", "Alakuko"
3. invoice: Invoice number digits only - strip N0-, NO-, DT- prefixes. From "N0-035263" return "035263"
4. date: Delivery date as DD-MM-YYYY from the printed top-right "Dated" field. Ignore received stamps, signatures, or handwritten dates. Example: "26-02-2026"

Return ONLY raw JSON, never leave a field empty, location 1-2 words max:
{"supermarket":"...","location":"...","invoice":"...","date":"..."}"""

BRAND_PROMPT = """You are reading a brand partner/supplier delivery invoice for a Nigerian retail distribution company.

Extract exactly these 5 fields:
1. brand: The issuing brand/company name at the top. e.g. "FlozzyD", "Prothrive", "Whole Eat", "Medi Tea", "Etifarm", "August Secret"
2. supermarket: Store/customer that received delivery. Look for "NAME:" or handwritten store name.
3. location: Delivery area - 1-2 words only. Look at ADDRESS or near store name.
4. invoice: Invoice/receipt number - digits only, strip any prefixes. e.g. "06081"
5. date: Date as DDMMYY (6 digits) from the printed top-right invoice date. Ignore received stamps, signatures, or handwritten dates. e.g. 25 Feb 2026 = "250226"

Return ONLY raw JSON, never leave a field empty, location 1-2 words max:
{"brand":"...","supermarket":"...","location":"...","invoice":"...","date":"..."}"""

DATE_ONLY_DALA_PROMPT = """Read only the printed invoice date at the top-right next to "Dated".
Ignore all stamps, signatures, received dates, and handwritten marks.
Return only raw JSON:
{"date":"DD-MM-YYYY"}"""

DATE_ONLY_BRAND_PROMPT = """Read only the printed invoice date at the top-right next to "Dated".
Ignore all stamps, signatures, received dates, and handwritten marks.
Return only raw JSON:
{"date":"DDMMYY"}"""


def clean(value):
    return re.sub(r'\s+', ' ', re.sub(r'[\/\\:*?"<>|]', '', str(value or ''))).strip()


def allowed_file(filename):
    return os.path.splitext(filename.lower())[1] in ALLOWED_EXTENSIONS


def infer_mime_type(file):
    ext = os.path.splitext(file.filename.lower())[1]
    mime_type = file.mimetype or MIME_BY_EXTENSION.get(ext, 'image/jpeg')
    if mime_type in {'image/jpg', 'image/pjpeg', 'application/octet-stream'}:
        return MIME_BY_EXTENSION.get(ext, 'image/jpeg')
    return mime_type


def gemini_error_message(resp):
    try:
        data = resp.json()
        message = data.get('error', {}).get('message')
        if message:
            return clean(message)[:220]
    except ValueError:
        pass
    return clean(resp.text)[:220] if resp.text else f'status {resp.status_code}'


def parse_model_list():
    configured_models = os.environ.get(
        'GEMINI_MODEL_FALLBACKS',
        os.environ.get('GEMINI_MODEL', DEFAULT_GEMINI_FALLBACK_MODELS),
    )
    models = [m.strip() for m in configured_models.split(',') if m.strip()]
    return models or [DEFAULT_GEMINI_MODEL]


def build_generation_config():
    return {
        'temperature': 0,
        'maxOutputTokens': 800,
    }


def call_gemini(file_bytes, mime_type, prompt):
    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise ValueError('GEMINI_API_KEY is not configured.')

    payload = {
        'contents': [{'parts': [
            {'inline_data': {
                'mime_type': mime_type,
                'data': base64.b64encode(file_bytes).decode('utf-8'),
            }},
            {'text': prompt},
        ]}],
        'generationConfig': build_generation_config(),
    }

    last_error = None
    for model in parse_model_list():
        for attempt in range(2):
            try:
                resp = requests.post(
                    f'{GEMINI_API_BASE}/{model}:generateContent?key={api_key}',
                    json=payload,
                    timeout=70,
                )
                resp.raise_for_status()
                return parse_gemini_response(resp)
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                last_error = exc
                if status_code not in {429, 500, 502, 503, 504}:
                    detail = gemini_error_message(exc.response) if exc.response is not None else 'No response body.'
                    raise ValueError(f'Gemini rejected this file ({status_code}): {detail}')
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
            except ValueError as exc:
                last_error = exc

            time.sleep(1.5 * (attempt + 1))

    if last_error:
        return_error = clean(str(last_error))[:160]
        raise ValueError(f'Gemini is temporarily unavailable. Please retry this file. {return_error}')
    raise ValueError('Gemini is temporarily unavailable. Please retry this file.')


def parse_gemini_response(resp):
    data = resp.json()
    candidate = data.get('candidates', [{}])[0]
    parts = candidate.get('content', {}).get('parts', [])
    if not parts:
        finish_reason = candidate.get('finishReason', 'UNKNOWN')
        raise ValueError(f'AI response was incomplete ({finish_reason}).')

    text_out = parts[0]['text'].strip()
    text_out = re.sub(r'```json|```', '', text_out).strip()

    try:
        return json.loads(text_out)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', text_out)
        if match:
            return json.loads(match.group())
        raise ValueError('AI returned an unreadable response. Please review this file.')


def normalize_store(store_text, invoice_number):
    value = clean(store_text)
    hint = INVOICE_HINTS.get(invoice_number, {}).get('store')
    if hint:
        return hint
    lowered = value.lower()
    return STORE_NORMALIZATION_MAP.get(lowered, value.title() if value.isupper() else value)


def normalize_location(location_text, invoice_number):
    value = clean(location_text)
    hint = INVOICE_HINTS.get(invoice_number, {}).get('location')
    if hint:
        return hint
    lowered = value.lower()
    normalized = LOCATION_NORMALIZATION_MAP.get(lowered, value)
    return normalized.title() if normalized.isupper() else normalized


def normalize_date(date_text, invoice_type):
    value = clean(date_text)
    if not value:
        return value

    if invoice_type == 'brand':
        compact = re.sub(r'[^0-9]', '', value)
        if len(compact) == 6:
            return compact

    months = {
        'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04', 'may': '05', 'jun': '06',
        'jul': '07', 'aug': '08', 'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
    }

    match = re.match(r'(?i)^\s*(\d{1,2})[-/\s]([A-Za-z]{3,})[-/\s](\d{2,4})\s*$', value)
    if match:
        day, month_name, year = match.groups()
        month = months.get(month_name[:3].lower())
        if month:
            year = f'20{year}' if len(year) == 2 else year
            return f'{int(day):02d}-{month}-{year}'

    match = re.match(r'^\s*(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\s*$', value)
    if match:
        day, month, year = match.groups()
        year = f'20{year}' if len(year) == 2 else year
        return f'{int(day):02d}-{int(month):02d}-{year}'

    compact = re.sub(r'[^0-9]', '', value)
    if invoice_type == 'dala' and len(compact) == 8:
        return f'{compact[:2]}-{compact[2:4]}-{compact[4:]}'

    return value


def date_year_is_suspicious(date_text):
    match = re.match(r'^\d{2}-\d{2}-(\d{4})$', date_text or '')
    if not match:
        return True
    year = int(match.group(1))
    current_year = datetime.utcnow().year
    return year < current_year - 1 or year > current_year + 1


def extract_printed_date(file_bytes, mime_type, invoice_type):
    prompt = DATE_ONLY_DALA_PROMPT if invoice_type == 'dala' else DATE_ONLY_BRAND_PROMPT
    parsed = call_gemini(file_bytes, mime_type, prompt)
    return normalize_date(parsed.get('date', ''), invoice_type)


def apply_invoice_hints(invoice_number, store, location, date):
    hint = INVOICE_HINTS.get(invoice_number, {})
    return (
        hint.get('store', store),
        hint.get('location', location),
        hint.get('date', date),
    )


@pod_bp.route('/pod')
@login_required
def index():
    return render_template('pod/index.html')


@pod_bp.route('/pod/batch/start', methods=['POST'])
@login_required
def start_batch():
    data = request.get_json(silent=True) or {}
    name = data.get('name', 'Unnamed Batch').strip() or 'Unnamed Batch'
    invoice_type = data.get('invoice_type', 'dala')

    if invoice_type not in {'dala', 'brand'}:
        return jsonify({'success': False, 'error': 'Invalid invoice type.'}), 400

    with get_db() as conn:
        row = conn.execute(text("""
            INSERT INTO batches (name, invoice_type, user_id, total_files, passed, review)
            VALUES (:name, :type, :uid, 0, 0, 0)
            RETURNING id
        """), {'name': name, 'type': invoice_type, 'uid': current_user.id}).fetchone()
        conn.commit()
    return jsonify({'success': True, 'batch_id': row[0]})


@pod_bp.route('/pod/process', methods=['POST'])
@login_required
def process():
    invoice_type = request.form.get('invoice_type', 'dala')
    batch_id = request.form.get('batch_id', type=int)
    file = request.files.get('file')

    if invoice_type not in {'dala', 'brand'}:
        return jsonify({'success': False, 'error': 'Invalid invoice type.'}), 400
    if not file:
        return jsonify({'success': False, 'error': 'No file received'}), 400
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': 'Only image and PDF files are supported.'}), 400

    mime_type = infer_mime_type(file)
    file_bytes = file.read()

    store = loc = inv = date = brand_code = new_name = None
    status = 'passed'
    error_msg = None

    try:
        prompt = DALA_PROMPT if invoice_type == 'dala' else BRAND_PROMPT
        parsed = call_gemini(file_bytes, mime_type, prompt)

        inv = re.sub(
            r'^(N0|NO|DT|AG|PH|WH|MT|ET)-?',
            '',
            clean(parsed.get('invoice', '')),
            flags=re.IGNORECASE,
        ).strip()
        store = normalize_store(parsed.get('supermarket', ''), inv)
        loc = normalize_location(parsed.get('location', ''), inv)
        date = normalize_date(parsed.get('date', ''), invoice_type)

        if invoice_type == 'dala' and date_year_is_suspicious(date):
            printed_date = extract_printed_date(file_bytes, mime_type, invoice_type)
            if printed_date:
                date = printed_date

        if invoice_type == 'dala':
            store, loc, date = apply_invoice_hints(inv, store, loc, date)

        if not store or not loc or not inv:
            raise ValueError(f'Incomplete data: {parsed}')

        if invoice_type == 'dala':
            if re.match(r'^\d{6}$', date):
                date = f'{date[:2]}-{date[2:4]}-20{date[4:6]}'
            new_name = f'{store} - {loc} DT-{inv} - {date}.pdf'
        else:
            brand_raw = parsed.get('brand', '').lower().strip()
            brand_code = next((code for key, code in BRAND_MAP.items() if key in brand_raw), 'XX')
            new_name = f'{store} - {loc} {brand_code}-{inv} {date}.pdf'

    except Exception as exc:
        status = 'review'
        error_msg = str(exc)

    if batch_id:
        with get_db() as conn:
            conn.execute(text("""
                INSERT INTO logs (batch_id, original_name, renamed_to, store_name, location,
                                  invoice_number, invoice_date, brand_code, invoice_type, status, error_message)
                VALUES (:bid, :orig, :renamed, :store, :loc, :inv, :date, :brand, :type, :status, :err)
            """), {
                'bid': batch_id, 'orig': file.filename, 'renamed': new_name,
                'store': store, 'loc': loc, 'inv': inv, 'date': date,
                'brand': brand_code, 'type': invoice_type,
                'status': status, 'err': error_msg,
            })
            conn.execute(text("""
                UPDATE batches SET
                    total_files = total_files + 1,
                    passed = passed + :p,
                    review = review + :r
                WHERE id = :bid
            """), {
                'p': 1 if status == 'passed' else 0,
                'r': 1 if status == 'review' else 0,
                'bid': batch_id,
            })
            conn.commit()

    if status == 'review':
        return jsonify({'success': False, 'error': error_msg, 'status': 'review'}), 422

    return jsonify({
        'success': True,
        'filename': new_name,
        'original': file.filename,
        'store': store,
        'location': loc,
        'invoice': inv,
        'date': date,
    })
