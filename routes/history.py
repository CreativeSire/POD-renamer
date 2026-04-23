from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from database.schema import get_db
from sqlalchemy import text

history_bp = Blueprint('history', __name__)

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

    total_pages = (total + per_page - 1) // per_page

    return render_template('history/index.html',
        logs=[dict(r) for r in logs],
        batches=[dict(r) for r in batches],
        total=total,
        page=page,
        total_pages=total_pages,
        filters={'q': search, 'status': status_filter, 'type': type_filter}
    )


@history_bp.route('/history/batch/<int:batch_id>')
@login_required
def batch_detail(batch_id):
    with get_db() as conn:
        batch = conn.execute(text("""
            SELECT b.*, u.full_name AS processed_by
            FROM batches b LEFT JOIN users u ON u.id = b.user_id
            WHERE b.id = :id
        """), {'id': batch_id}).mappings().fetchone()

        logs = conn.execute(text("""
            SELECT * FROM logs WHERE batch_id = :id ORDER BY created_at ASC
        """), {'id': batch_id}).mappings().fetchall()

    if not batch:
        return 'Batch not found', 404

    return render_template('history/batch.html',
        batch=dict(batch),
        logs=[dict(r) for r in logs]
    )


@history_bp.route('/history/batch/<int:batch_id>/delete', methods=['POST'])
@login_required
def delete_batch(batch_id):
    with get_db() as conn:
        conn.execute(text("DELETE FROM batches WHERE id = :id"), {'id': batch_id})
        conn.commit()
    return jsonify({'success': True})


@history_bp.route('/admin/clear-all', methods=['POST'])
@login_required
def clear_all():
    with get_db() as conn:
        conn.execute(text("TRUNCATE TABLE logs, batches RESTART IDENTITY CASCADE"))
        conn.commit()
    return jsonify({'success': True, 'message': 'All history and logs cleared.'})
