import os
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from database.schema import get_db
from sqlalchemy import text

history_bp = Blueprint('history', __name__)


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
                   l.status, l.error_message, l.created_at,
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
                   b.created_at, u.full_name AS processed_by
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


@history_bp.route('/history/batch/<int:batch_id>')
@login_required
def batch_detail(batch_id):
    status_filter = request.args.get('status', '')

    with get_db() as conn:
        batch = conn.execute(text("""
            SELECT b.*, u.full_name AS processed_by
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
                SELECT * FROM logs
                WHERE batch_id = :id AND status = :status
                ORDER BY created_at ASC
            """), {'id': batch_id, 'status': status_filter}).mappings().fetchall()
        else:
            logs = conn.execute(text("""
                SELECT * FROM logs WHERE batch_id = :id ORDER BY created_at ASC
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
