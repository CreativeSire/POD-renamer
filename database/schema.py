import os
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from werkzeug.security import generate_password_hash

_engine = None


def get_database_url():
    url = os.environ.get('DATABASE_URL', '').strip()
    if not url:
        raise RuntimeError(
            'DATABASE_URL is not configured. Add a PostgreSQL database to Railway '
            'or set DATABASE_URL locally before starting the app.'
        )
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return url


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_database_url(), poolclass=NullPool, pool_pre_ping=True)
    return _engine


@contextmanager
def get_db():
    engine = get_engine()
    with engine.connect() as conn:
        yield conn


def init_db():
    with get_db() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(80) UNIQUE NOT NULL,
                full_name VARCHAR(120) NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) DEFAULT 'user',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS batches (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                invoice_type VARCHAR(10) NOT NULL,
                user_id INTEGER REFERENCES users(id),
                total_files INTEGER DEFAULT 0,
                passed INTEGER DEFAULT 0,
                review INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                batch_id INTEGER REFERENCES batches(id) ON DELETE CASCADE,
                original_name VARCHAR(500) NOT NULL,
                renamed_to VARCHAR(500),
                store_name VARCHAR(200),
                location VARCHAR(100),
                invoice_number VARCHAR(100),
                invoice_date VARCHAR(50),
                brand_code VARCHAR(10),
                invoice_type VARCHAR(10) NOT NULL,
                status VARCHAR(20) DEFAULT 'passed',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_logs_batch ON logs(batch_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_logs_status ON logs(status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_batches_user ON batches(user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_logs_store ON logs(store_name)"))

        admin_username = os.environ.get('ADMIN_USERNAME', 'admin').strip() or 'admin'
        admin_full_name = os.environ.get('ADMIN_FULL_NAME', 'Administrator').strip() or 'Administrator'
        admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
        admin_hash = generate_password_hash(admin_password)

        conn.execute(text("""
            INSERT INTO users (username, full_name, password_hash, role)
            VALUES (:username, :full_name, :password_hash, 'admin')
            ON CONFLICT (username) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                password_hash = EXCLUDED.password_hash
        """), {
            'username': admin_username,
            'full_name': admin_full_name,
            'password_hash': admin_hash,
        })

        conn.commit()
