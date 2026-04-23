from flask_login import UserMixin
from flask_bcrypt import Bcrypt
from database.schema import get_db
from sqlalchemy import text
from werkzeug.security import check_password_hash

bcrypt = Bcrypt()

class User(UserMixin):
    def __init__(self, id, username, full_name, role):
        self.id = id
        self.username = username
        self.full_name = full_name
        self.role = role

    def is_admin(self):
        return self.role == 'admin'

    @staticmethod
    def get_by_id(user_id):
        with get_db() as conn:
            row = conn.execute(
                text("SELECT id, username, full_name, role FROM users WHERE id = :id"),
                {'id': user_id}
            ).mappings().fetchone()
        if row:
            return User(row['id'], row['username'], row['full_name'], row['role'])
        return None

    @staticmethod
    def get_by_username(username):
        with get_db() as conn:
            row = conn.execute(
                text("SELECT id, username, full_name, password_hash, role FROM users WHERE username = :u"),
                {'u': username}
            ).mappings().fetchone()
        return dict(row) if row else None

    @staticmethod
    def verify_password(password_hash, password):
        if password_hash.startswith(('pbkdf2:', 'scrypt:')):
            return check_password_hash(password_hash, password)
        return bcrypt.check_password_hash(password_hash, password)
