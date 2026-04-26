import os
from datetime import timedelta

from flask import Flask, jsonify, redirect, url_for
from flask_login import LoginManager
from dotenv import load_dotenv

load_dotenv()

login_manager = LoginManager()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')
    app.config['COMPANY_NAME'] = 'DALA Technologies'
    app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 25 * 1024 * 1024))
    app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=365)
    app.config['REMEMBER_COOKIE_SECURE'] = os.environ.get('FLASK_DEBUG', 'false').lower() != 'true'
    app.config['REMEMBER_COOKIE_HTTPONLY'] = True
    app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

    from database.schema import init_db
    init_db()

    from models.user import bcrypt
    bcrypt.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'

    @login_manager.user_loader
    def load_user(user_id):
        from models.user import User
        return User.get_by_id(int(user_id))

    from routes.auth import auth_bp
    from routes.dashboard import dashboard_bp
    from routes.pod import pod_bp
    from routes.history import history_bp
    from routes.files import files_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(pod_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(files_bp)

    @app.route('/')
    def index():
        return redirect(url_for('dashboard.index'))

    @app.route('/healthz')
    def healthz():
        return jsonify({'ok': True, 'service': 'pod-renamer'})

    @app.errorhandler(413)
    def file_too_large(_error):
        return jsonify({'success': False, 'error': 'Uploaded file is too large.'}), 413

    return app

app = create_app()

if __name__ == '__main__':
    app.run(
        debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true',
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000))
    )
