import os
import zipfile
import io
from flask import Blueprint, send_file, abort, jsonify, render_template
from flask_login import login_required
from database.schema import get_db, UPLOAD_FOLDER
from sqlalchemy import text

files_bp = Blueprint('files', __name__)


@files_bp.route('/files')
@login_required
def browser():
    with get_db() as conn:
        batches = conn.execute(text("""
            SELECT b.id, b.name, b.invoice_type, b.created_at,
                   COUNT(l.id) as file_count
            FROM batches b
            LEFT JOIN logs l ON l.batch_id = b.id AND l.file_path IS NOT NULL
            GROUP BY b.id
            ORDER BY b.created_at DESC
        """)).mappings().fetchall()
    return render_template('files/browser.html', batches=[dict(r) for r in batches])


@files_bp.route('/files/batch/<int:batch_id>')
@login_required
def batch_files(batch_id):
    with get_db() as conn:
        batch = conn.execute(text("""
            SELECT b.*, u.full_name AS processed_by
            FROM batches b LEFT JOIN users u ON u.id = b.user_id
            WHERE b.id = :id
        """), {'id': batch_id}).mappings().fetchone()

        if not batch:
            abort(404)

        logs = conn.execute(text("""
            SELECT id, original_name, renamed_to, store_name, location,
                   invoice_number, invoice_date, brand_code, status, file_path
            FROM logs
            WHERE batch_id = :id AND file_path IS NOT NULL
            ORDER BY created_at ASC
        """), {'id': batch_id}).mappings().fetchall()

    # Group files by folder
    folders = {}
    for log in logs:
        d = dict(log)
        path = d.get('file_path', '')
        folder = 'DALA'
        if batch['invoice_type'] == 'brand' and path:
            parts = path.split(os.sep)
            if len(parts) >= 2:
                folder = parts[-2]
        folders.setdefault(folder, []).append(d)

    return render_template('files/batch_files.html',
        batch=dict(batch),
        folders=folders
    )


@files_bp.route('/files/download/<int:log_id>')
@login_required
def download_file(log_id):
    with get_db() as conn:
        row = conn.execute(text("""
            SELECT file_path, renamed_to, file_data FROM logs WHERE id = :id
        """), {'id': log_id}).mappings().fetchone()

    if not row:
        abort(404)

    download_name = row['renamed_to'] or os.path.basename(row['file_path'] or 'pod.pdf')
    if row['file_path'] and os.path.exists(row['file_path']):
        return send_file(row['file_path'], as_attachment=True, download_name=download_name)

    if row['file_data']:
        return send_file(io.BytesIO(bytes(row['file_data'])),
                         mimetype='application/pdf' if download_name.lower().endswith('.pdf') else 'application/octet-stream',
                         as_attachment=True,
                         download_name=download_name)

    abort(404)


@files_bp.route('/files/batch/<int:batch_id>/zip')
@login_required
def download_batch_zip(batch_id):
    with get_db() as conn:
        batch = conn.execute(text("SELECT name FROM batches WHERE id = :id"), {'id': batch_id}).mappings().fetchone()
        logs = conn.execute(text("""
            SELECT file_path, renamed_to, status, file_data FROM logs
            WHERE batch_id = :id AND file_path IS NOT NULL
            ORDER BY created_at ASC
        """), {'id': batch_id}).mappings().fetchall()

    if not batch:
        abort(404)

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        seen = {}
        for log in logs:
            path = log['file_path']
            name = log['renamed_to'] or os.path.basename(path or 'pod.pdf')
            if log['status'] == 'review':
                name = f'Review/{name}'
            if (not path or not os.path.exists(path)) and not log['file_data']:
                continue
            # Handle duplicates in zip
            zip_name = name
            if zip_name in seen:
                seen[zip_name] += 1
                base, ext = os.path.splitext(zip_name)
                zip_name = f'{base} ({seen[zip_name]}){ext}'
            else:
                seen[zip_name] = 0
            if path and os.path.exists(path):
                zf.write(path, zip_name)
            else:
                zf.writestr(zip_name, bytes(log['file_data']))

    memory_file.seek(0)
    safe_name = batch['name'].replace(' ', '_').replace('/', '_')
    return send_file(memory_file,
                     mimetype='application/zip',
                     as_attachment=True,
                     download_name=f'{safe_name}.zip')
