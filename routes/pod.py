import base64
import json
import os
import re
import time

import requests
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import text

from database.schema import get_db

pod_bp = Blueprint('pod', __name__)

GEMINI_API_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'
DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'
DEFAULT_GEMINI_FALLBACK_MODELS = 'gemini-2.5-flash,gemini-flash-latest'
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

DALA_PROMPT = """You are reading a DALA Technologies delivery invoice or handwritten Proof of Delivery note.

Extract exactly these 4 fields:
1. supermarket: Store/customer name. On printed invoices follows "Party :". Examples: "Supersaver", "Jendol Supermarket"
2. location: Delivery area - 1-2 words only, NOT full address. Examples: "Osapa", "Ikeja", "Egbeda", "Alakuko"
3. invoice: Invoice number digits only - strip N0-, NO-, DT- prefixes. From "N0-035263" return "035263"
4. date: Delivery date as DD-MM-YYYY. Look for "Dated". e.g. "26-02-2026"

Return ONLY raw JSON, never leave a field empty, location 1-2 words max:
{"supermarket":"...","location":"...","invoice":"...","date":"..."}"""

BRAND_PROMPT = """You are reading a brand partner/supplier delivery invoice for a Nigerian retail distribution company.

Extract exactly these 5 fields:
1. brand: The issuing brand/company name at the top. e.g. "FlozzyD", "Prothrive", "Whole Eat", "Medi Tea", "Etifarm", "August Secret"
2. supermarket: Store/customer that received delivery. Look for "NAME:" or handwritten store name.
3. location: Delivery area - 1-2 words only. Look at ADDRESS or near store name.
4. invoice: Invoice/receipt number - digits only, strip any prefixes. e.g. "06081"
5. date: Date as DDMMYY (6 digits). e.g. 25 Feb 2026 = "250226"

Return ONLY raw JSON, never leave a field empty, location 1-2 words max:
{"brand":"...","supermarket":"...","location":"...","invoice":"...","date":"..."}"""


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


def call_gemini(file_bytes, mime_type, prompt):
    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise ValueError('GEMINI_API_KEY is not configured.')
    configured_models = os.environ.get(
        'GEMINI_MODEL_FALLBACKS',
        os.environ.get('GEMINI_MODEL', DEFAULT_GEMINI_FALLBACK_MODELS),
    )
    models = [m.strip() for m in configured_models.split(',') if m.strip()]
    if not models:
        models = [DEFAULT_GEMINI_MODEL]

    payload = {
        'contents': [{'parts': [
            {'inline_data': {
                'mime_type': mime_type,
                'data': base64.b64encode(file_bytes).decode('utf-8'),
            }},
            {'text': prompt},
        ]}],
        'generationConfig': {
            'temperature': 0,
            'maxOutputTokens': 300,
            'responseMimeType': 'application/json',
        },
    }

    last_error = None
    for model in models:
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

            time.sleep(1.5 * (attempt + 1))

    if last_error:
        return_error = clean(str(last_error))[:160]
        raise ValueError(f'Gemini is temporarily unavailable. Please retry this file. {return_error}')
    raise ValueError('Gemini is temporarily unavailable. Please retry this file.')


def parse_gemini_response(resp):
    text_out = resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    text_out = re.sub(r'```json|```', '', text_out).strip()

    try:
        return json.loads(text_out)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', text_out)
        if match:
            return json.loads(match.group())
        raise ValueError('AI returned an unreadable response. Please review this file.')


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

        store = clean(parsed.get('supermarket', ''))
        loc = clean(parsed.get('location', ''))
        inv = re.sub(
            r'^(N0|NO|DT|AG|PH|WH|MT|ET)-?',
            '',
            clean(parsed.get('invoice', '')),
            flags=re.IGNORECASE,
        ).strip()
        date = clean(parsed.get('date', ''))

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
