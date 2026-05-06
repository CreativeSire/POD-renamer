import csv
import io
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, render_template, request, jsonify, Response
from flask_login import login_required, current_user
from database.schema import get_db, UPLOAD_FOLDER, BACKUP_FOLDER
from sqlalchemy import text

history_bp = Blueprint('history', __name__)


def super_admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_super_admin():
            return jsonify({'success': False, 'error': 'Super admin required.'}), 403
        return f(*args, **kwargs)
    return decorated_function


def split_folder_filename(original_name):
    """Split 'Folder/file.jpg' into ('Folder', 'file.jpg')."""
    if not original_name:
        return '', ''
    if '/' in original_name:
        folder, filename = original_name.rsplit('/', 1)
        return folder, filename
    return '', original_name


@history_bp.route('/history')
@login_required
def index():
    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    search = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '')
    type_filter = request.args.get('type', '')

    where = ['1=1']
    params = {'limit': per_page, 'offset': offset}

    if search:
        where.append("(l.store_name ILIKE :q OR l.renamed_to ILIKE :q OR l.invoice_number ILIKE :q OR l.original_name ILIKE :q)")
        params['q'] = f'%{search}%'
    if status_filter:
        where.append("l.status = :status")
        params['status'] = status_filter
    if type_filter:
        where.append("l.invoice_type = :type")
        params['type'] = type_filter

    where_sql = ' AND '.join(where)

    with get_db() as conn:
        total = conn.execute(text(f"""
            SELECT COUNT(*) FROM logs l WHERE {where_sql}
        """), params).scalar()

        logs = conn.execute(text(f"""
            SELECT l.id, l.original_name, l.renamed_to, l.store_name, l.location,
                   l.invoice_number, l.invoice_date, l.brand_code, l.invoice_type,
                   l.status, l.error_message, l.file_path, l.created_at,
                   b.name AS batch_name, u.full_name AS processed_by
            FROM logs l
            LEFT JOIN batches b ON b.id = l.batch_id
            LEFT JOIN users u ON u.id = b.user_id
            WHERE {where_sql}
            ORDER BY l.created_at DESC
            LIMIT :limit OFFSET :offset
        """), params).mappings().fetchall()

        batches = conn.execute(text("""
            SELECT b.id, b.name, b.invoice_type, b.total_files, b.passed, b.review,
                   b.created_at, u.username AS processed_by
            FROM batches b
            LEFT JOIN users u ON u.id = b.user_id
            ORDER BY b.created_at DESC
            LIMIT 50
        """)).mappings().fetchall()

    # Enrich logs with folder/filename split
    enriched_logs = []
    for log in logs:
        d = dict(log)
        d['folder'], d['filename'] = split_folder_filename(d.get('original_name', ''))
        enriched_logs.append(d)

    total_pages = (total + per_page - 1) // per_page

    return render_template('history/index.html',
        logs=enriched_logs,
        batches=[dict(r) for r in batches],
        total=total,
        page=page,
        total_pages=total_pages,
        filters={'q': search, 'status': status_filter, 'type': type_filter}
    )


@history_bp.route('/history/export')
@login_required
def export_csv():
    with get_db() as conn:
        rows = conn.execute(text("""
            SELECT l.id, l.original_name, l.renamed_to, l.store_name, l.location,
                   l.invoice_number, l.invoice_date, l.brand_code, l.invoice_type,
                   l.status, l.error_message, l.created_at,
                   b.name AS batch_name
            FROM logs l
            LEFT JOIN batches b ON b.id = l.batch_id
            ORDER BY l.created_at DESC
        """)).mappings().fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'ID', 'Batch Name', 'Original Name', 'Renamed To', 'Store',
        'Location', 'Invoice', 'Date', 'Brand Code', 'Type',
        'Status', 'Error', 'Created At'
    ])
    for row in rows:
        writer.writerow([
            row['id'], row['batch_name'], row['original_name'], row['renamed_to'],
            row['store_name'], row['location'], row['invoice_number'],
            row['invoice_date'], row['brand_code'], row['invoice_type'],
            row['status'], row['error_message'],
            row['created_at'].strftime('%Y-%m-%d %H:%M') if row['created_at'] else ''
        ])

    output.seek(0)
    return Response(
        output,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=history_export.csv'}
    )


@history_bp.route('/history/batch/<int:batch_id>')
@login_required
def batch_detail(batch_id):
    status_filter = request.args.get('status', '')

    with get_db() as conn:
        batch = conn.execute(text("""
            SELECT b.*, u.username AS processed_by
            FROM batches b LEFT JOIN users u ON u.id = b.user_id
            WHERE b.id = :id
        """), {'id': batch_id}).mappings().fetchone()

        if not batch:
            return 'Batch not found', 404

        # Build status counts for clickable filters
        status_counts = conn.execute(text("""
            SELECT status, COUNT(*) as cnt
            FROM logs WHERE batch_id = :id
            GROUP BY status
        """), {'id': batch_id}).mappings().fetchall()

        counts = {'passed': 0, 'review': 0}
        for row in status_counts:
            counts[row['status']] = row['cnt']

        # Fetch logs with optional status filter
        if status_filter:
            logs = conn.execute(text("""
                SELECT id, original_name, renamed_to, store_name, location,
                   invoice_number, invoice_date, brand_code, status, error_message, file_path, created_at
            FROM logs
            WHERE batch_id = :id AND status = :status
            ORDER BY created_at ASC
            """), {'id': batch_id, 'status': status_filter}).mappings().fetchall()
        else:
            logs = conn.execute(text("""
                SELECT id, original_name, renamed_to, store_name, location,
                   invoice_number, invoice_date, brand_code, status, error_message, file_path, created_at
            FROM logs WHERE batch_id = :id ORDER BY created_at ASC
            """), {'id': batch_id}).mappings().fetchall()

    # Enrich logs with folder/filename split
    enriched_logs = []
    for log in logs:
        d = dict(log)
        d['folder'], d['filename'] = split_folder_filename(d.get('original_name', ''))
        enriched_logs.append(d)

    return render_template('history/batch.html',
        batch=dict(batch),
        logs=enriched_logs,
        counts=counts,
        active_status=status_filter
    )


@history_bp.route('/history/batch/<int:batch_id>/delete', methods=['POST'])
@login_required
def delete_batch(batch_id):
    with get_db() as conn:
        conn.execute(text("DELETE FROM batches WHERE id = :id"), {'id': batch_id})
        conn.commit()
    return jsonify({'success': True})


# ── Super Admin endpoints ──────────────────────────────────────

@history_bp.route('/admin/delete-today', methods=['POST'])
@super_admin_required
def delete_today():
    today = datetime.utcnow().date()
    tomorrow = today + timedelta(days=1)
    with get_db() as conn:
        # Get batch IDs to clean up storage
        batch_ids = conn.execute(text("""
            SELECT id FROM batches
            WHERE created_at >= :today AND created_at < :tomorrow
        """), {'today': today, 'tomorrow': tomorrow}).mappings().fetchall()

        for row in batch_ids:
            bid = row['id']
            batch_dir = os.path.join(UPLOAD_FOLDER, f'batch_{bid}')
            if os.path.exists(batch_dir):
                import shutil
                shutil.rmtree(batch_dir)

        conn.execute(text("""
            DELETE FROM logs WHERE batch_id IN (
                SELECT id FROM batches WHERE created_at >= :today AND created_at < :tomorrow
            )
        """), {'today': today, 'tomorrow': tomorrow})
        conn.execute(text("""
            DELETE FROM batches WHERE created_at >= :today AND created_at < :tomorrow
        """), {'today': today, 'tomorrow': tomorrow})
        conn.commit()
    return jsonify({'success': True, 'message': "Today's data cleared."})


@history_bp.route('/admin/backup', methods=['POST'])
@super_admin_required
def backup_database():
    from database.schema import _run_backup
    try:
        _run_backup()
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        return jsonify({'success': True, 'file': f'backup_{timestamp}.sql.gz'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@history_bp.route('/admin/audit')
@super_admin_required
def audit_batches():
    limit = request.args.get('limit', 25, type=int)
    limit = max(1, min(limit, 100))

    with get_db() as conn:
        batches = conn.execute(text("""
            SELECT b.id, b.name, b.invoice_type, b.created_at, b.total_files, b.passed, b.review,
                   u.username AS processed_by,
                   COUNT(l.id) AS log_count,
                   COUNT(*) FILTER (WHERE l.status = 'passed') AS log_passed,
                   COUNT(*) FILTER (WHERE l.status = 'review') AS log_review,
                   COUNT(*) FILTER (WHERE l.status = 'passed'
                                    AND (l.file_path IS NOT NULL OR l.file_data IS NOT NULL)) AS zip_ready,
                   COUNT(*) FILTER (WHERE l.status = 'review'
                                    AND (l.file_path IS NOT NULL OR l.file_data IS NOT NULL)) AS review_ready,
                   COUNT(*) FILTER (WHERE l.file_path IS NULL AND l.file_data IS NULL) AS no_output_copy
            FROM batches b
            LEFT JOIN logs l ON l.batch_id = b.id
            LEFT JOIN users u ON u.id = b.user_id
            GROUP BY b.id, u.username
            ORDER BY b.created_at DESC
            LIMIT :limit
        """), {'limit': limit}).mappings().fetchall()

        batch_ids = [row['id'] for row in batches]
        rows_by_batch = {}
        if batch_ids:
            logs = conn.execute(text("""
                SELECT batch_id, id, status, original_name, renamed_to, file_path,
                       file_data IS NOT NULL AS has_file_data, created_at
                FROM logs
                WHERE batch_id = ANY(:batch_ids)
                ORDER BY batch_id DESC, created_at ASC
            """), {'batch_ids': batch_ids}).mappings().fetchall()
            for log in logs:
                rows_by_batch.setdefault(log['batch_id'], []).append(dict(log))

    report = []
    for row in batches:
        batch = dict(row)
        logs = rows_by_batch.get(batch['id'], [])
        missing_disk = 0
        missing_any_copy = 0
        file_rows = []
        for log in logs:
            exists_on_disk = bool(log.get('file_path') and os.path.exists(log['file_path']))
            has_db_copy = bool(log.get('has_file_data'))
            if log.get('file_path') and not exists_on_disk:
                missing_disk += 1
            if not exists_on_disk and not has_db_copy:
                missing_any_copy += 1
            file_rows.append({
                'id': log['id'],
                'status': log['status'],
                'original': log['original_name'],
                'renamed': log['renamed_to'],
                'has_path': bool(log.get('file_path')),
                'exists_on_disk': exists_on_disk,
                'has_db_copy': has_db_copy,
            })

        expected_total = batch['log_count']
        output_ready = batch['zip_ready'] + batch['review_ready']
        batch['created_at'] = batch['created_at'].isoformat() if batch['created_at'] else None
        batch['output_ready'] = output_ready
        batch['missing_disk_copy'] = missing_disk
        batch['missing_any_copy'] = missing_any_copy
        batch['count_mismatch'] = (
            batch['total_files'] != expected_total
            or batch['passed'] != batch['log_passed']
            or batch['review'] != batch['log_review']
            or missing_any_copy > 0
        )
        batch['files'] = file_rows
        report.append(batch)

    return jsonify({'success': True, 'batches': report})
