from flask import Blueprint, render_template
from flask_login import login_required
from database.schema import get_db
from sqlalchemy import text

dashboard_bp = Blueprint('dashboard', __name__)

@dashboard_bp.route('/')
@dashboard_bp.route('/dashboard')
@login_required
def index():
    with get_db() as conn:
        stats = conn.execute(text("""
            SELECT
                COUNT(DISTINCT b.id)                          AS total_batches,
                COALESCE(SUM(b.total_files), 0)               AS total_files,
                COALESCE(SUM(b.passed), 0)                    AS total_passed,
                COALESCE(SUM(b.review), 0)                    AS total_review,
                COUNT(DISTINCT CASE WHEN b.created_at >= NOW() - INTERVAL '7 days'
                      THEN b.id END)                          AS batches_this_week,
                COALESCE(SUM(CASE WHEN b.created_at >= NOW() - INTERVAL '7 days'
                      THEN b.total_files END), 0)             AS files_this_week
            FROM batches b
        """)).mappings().fetchone()

        recent = conn.execute(text("""
            SELECT b.id, b.name, b.invoice_type, b.total_files, b.passed, b.review,
                   b.created_at, u.full_name AS processed_by
            FROM batches b
            LEFT JOIN users u ON u.id = b.user_id
            ORDER BY b.created_at DESC
            LIMIT 10
        """)).mappings().fetchall()

        top_stores = conn.execute(text("""
            SELECT store_name, COUNT(*) AS cnt
            FROM logs
            WHERE store_name IS NOT NULL AND store_name != ''
            GROUP BY store_name
            ORDER BY cnt DESC
            LIMIT 8
        """)).mappings().fetchall()

    return render_template('dashboard/index.html',
        stats=dict(stats),
        recent=[dict(r) for r in recent],
        top_stores=[dict(r) for r in top_stores]
    )
