import base64
from io import BytesIO
import json
import os
import re
import threading
import time

import requests
from flask import Blueprint, jsonify, make_response, render_template, request
from flask_login import current_user, login_required
from PIL import Image, ImageOps
from sqlalchemy import text

from database.schema import get_db, UPLOAD_FOLDER

pod_bp = Blueprint('pod', __name__)
_thread_local = threading.local()

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

BRAND_FOLDER_MAP = {
    'AG': 'August Secret',
    'PH': 'Prothrive',
    'WH': 'Whole Eat',
    'ET': 'Etifarm',
    'XX': 'Other',
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


def clean(value):
    return re.sub(r'\s+', ' ', re.sub(r'[\/\\:*?"<>|]', '', str(value or ''))).strip()


def allowed_file(filename):
    return os.path.splitext(filename.lower())[1] in ALLOWED_EXTENSIONS


def save_uploaded_file(batch_id, invoice_type, brand_code, file_bytes, filename):
    """Save file to server uploads dir organized by batch and brand."""
    if not batch_id:
        return None
    batch_dir = os.path.join(UPLOAD_FOLDER, f'batch_{batch_id}')
    if invoice_type == 'dala':
        target_dir = os.path.join(batch_dir, 'DALA')
    else:
        brand_folder = BRAND_FOLDER_MAP.get(brand_code, brand_code or 'Other')
        target_dir = os.path.join(batch_dir, brand_folder)
    os.makedirs(target_dir, exist_ok=True)
    safe_name = clean(filename)
    file_path = os.path.join(target_dir, safe_name)
    # Handle duplicates
    base, ext = os.path.splitext(file_path)
    counter = 1
    while os.path.exists(file_path):
        file_path = f'{base} ({counter}){ext}'
        counter += 1
    with open(file_path, 'wb') as f:
        f.write(file_bytes)
    return file_path


def make_storage_pdf(file_bytes, mime_type):
    """Store successful image PODs as real PDFs for History/Files downloads."""
    if mime_type == 'application/pdf':
        return file_bytes
    with Image.open(BytesIO(file_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        out = BytesIO()
        img.save(out, format='PDF', resolution=100.0)
        return out.getvalue()


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


def get_http_session():
    session = getattr(_thread_local, 'http_session', None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        session.mount('https://', adapter)
        _thread_local.http_session = session
    return session


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
            'maxOutputTokens': 800,
        },
    }

    last_error = None
    for model in models:
        for attempt in range(2):
            try:
                resp = get_http_session().post(
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


@pod_bp.route('/pod')
@login_required
def index():
    resp = make_response(render_template('pod/index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


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
    saved_path = None

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
        date = normalize_date(parsed.get('date', ''), invoice_type)

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
        new_name = f'REVIEW_{clean(file.filename)}'

    # Always save the file to server storage (passed or review)
    storage_bytes = make_storage_pdf(file_bytes, mime_type) if status == 'passed' else file_bytes
    saved_path = save_uploaded_file(batch_id, invoice_type, brand_code, storage_bytes, new_name)

    if batch_id:
        with get_db() as conn:
            conn.execute(text("""
                INSERT INTO logs (batch_id, original_name, renamed_to, store_name, location,
                                  invoice_number, invoice_date, brand_code, invoice_type, status, error_message, file_path)
                VALUES (:bid, :orig, :renamed, :store, :loc, :inv, :date, :brand, :type, :status, :err, :fpath)
            """), {
                'bid': batch_id, 'orig': file.filename, 'renamed': new_name,
                'store': store, 'loc': loc, 'inv': inv, 'date': date,
                'brand': brand_code, 'type': invoice_type,
                'status': status, 'err': error_msg, 'fpath': saved_path,
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
        return jsonify({'success': False, 'error': error_msg, 'status': 'review', 'filename': new_name, 'file_path': saved_path}), 422

    return jsonify({
        'success': True,
        'filename': new_name,
        'original': file.filename,
        'store': store,
        'location': loc,
        'invoice': inv,
        'date': date,
        'file_path': saved_path,
    })


@pod_bp.route('/pod/reprocess/<int:log_id>', methods=['POST'])
@login_required
def reprocess_log(log_id):
    with get_db() as conn:
        log = conn.execute(text("""
            SELECT id, batch_id, original_name, invoice_type, file_path
            FROM logs WHERE id = :id
        """), {'id': log_id}).mappings().fetchone()

    if not log:
        return jsonify({'success': False, 'error': 'Log not found.'}), 404

    batch_id = log['batch_id']
    invoice_type = log['invoice_type']

    # Read stored file
    if not log['file_path'] or not os.path.exists(log['file_path']):
        return jsonify({'success': False, 'error': 'Original file not found on server.'}), 404

    with open(log['file_path'], 'rb') as f:
        file_bytes = f.read()

    # Successful stored outputs are PDFs even when the original upload was an image.
    ext = os.path.splitext((log['file_path'] or log['original_name']).lower())[1]
    mime_type = MIME_BY_EXTENSION.get(ext, 'image/jpeg')

    store = loc = inv = date = brand_code = new_name = None
    status = 'passed'
    error_msg = None
    saved_path = log['file_path']

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
        date = normalize_date(parsed.get('date', ''), invoice_type)

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

        storage_bytes = make_storage_pdf(file_bytes, mime_type)
        saved_path = save_uploaded_file(batch_id, invoice_type, brand_code, storage_bytes, new_name)

    except Exception as exc:
        status = 'review'
        error_msg = str(exc)
        new_name = f'REVIEW_{clean(log["original_name"])}'

    # Update log record
    with get_db() as conn:
        conn.execute(text("""
            UPDATE logs SET
                renamed_to = :renamed,
                store_name = :store,
                location = :loc,
                invoice_number = :inv,
                invoice_date = :date,
                brand_code = :brand,
                status = :status,
                error_message = :err,
                file_path = :fpath
            WHERE id = :id
        """), {
            'id': log_id, 'renamed': new_name, 'store': store, 'loc': loc,
            'inv': inv, 'date': date, 'brand': brand_code,
            'status': status, 'err': error_msg, 'fpath': saved_path,
        })
        conn.commit()

    if status == 'review':
        return jsonify({'success': False, 'error': error_msg, 'status': 'review', 'filename': new_name}), 422

    return jsonify({
        'success': True,
        'filename': new_name,
        'store': store,
        'location': loc,
        'invoice': inv,
        'date': date,
        'file_path': saved_path,
    })


@pod_bp.route('/pod/process-batch', methods=['POST'])
@login_required
def process_batch():
    """Process multiple files in a single request."""
    invoice_type = request.form.get('invoice_type', 'dala')
    batch_id = request.form.get('batch_id', type=int)
    files = request.files.getlist('files')

    if invoice_type not in {'dala', 'brand'}:
        return jsonify({'success': False, 'error': 'Invalid invoice type.'}), 400
    if not files:
        return jsonify({'success': False, 'error': 'No files received'}), 400

    results = []
    for file in files:
        if not allowed_file(file.filename):
            results.append({'success': False, 'error': 'Unsupported file type.', 'original': file.filename})
            continue

        mime_type = infer_mime_type(file)
        file_bytes = file.read()

        store = loc = inv = date = brand_code = new_name = None
        status = 'passed'
        error_msg = None
        saved_path = None

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
            date = normalize_date(parsed.get('date', ''), invoice_type)

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
            new_name = f'REVIEW_{clean(file.filename)}'

        storage_bytes = make_storage_pdf(file_bytes, mime_type) if status == 'passed' else file_bytes
        saved_path = save_uploaded_file(batch_id, invoice_type, brand_code, storage_bytes, new_name)

        if batch_id:
            with get_db() as conn:
                conn.execute(text("""
                    INSERT INTO logs (batch_id, original_name, renamed_to, store_name, location,
                                      invoice_number, invoice_date, brand_code, invoice_type, status, error_message, file_path)
                    VALUES (:bid, :orig, :renamed, :store, :loc, :inv, :date, :brand, :type, :status, :err, :fpath)
                """), {
                    'bid': batch_id, 'orig': file.filename, 'renamed': new_name,
                    'store': store, 'loc': loc, 'inv': inv, 'date': date,
                    'brand': brand_code, 'type': invoice_type,
                    'status': status, 'err': error_msg, 'fpath': saved_path,
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

        results.append({
            'success': status == 'passed',
            'filename': new_name,
            'original': file.filename,
            'status': status,
            'error': error_msg,
            'file_path': saved_path,
        })

    return jsonify({'success': True, 'results': results})
