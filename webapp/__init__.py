"""
GENEX Flask Application Factory
================================
This module creates and configures the Flask application instance.
"""

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

# Initialize extensions (but don't bind to app yet)
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()


def create_app(config_name='development'):
    """
    Application factory pattern for creating Flask app instances.

    Args:
        config_name: Configuration to use ('development', 'production', 'testing')

    Returns:
        Configured Flask application instance
    """
    # Create Flask app
    app = Flask(__name__)

    # Load configuration
    app.config.from_object(f'webapp.config.{config_name.capitalize()}Config')

    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Configure login manager
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'

    # User loader callback for Flask-Login
    @login_manager.user_loader
    def load_user(user_id):
        from webapp.models.user import User
        return User.query.get(int(user_id))

    # Register blueprints
    register_blueprints(app)

    # Register error handlers
    register_error_handlers(app)

    # Register CLI commands
    register_cli_commands(app)

    # Create database tables (only in development)
#    with app.app_context():
#       if app.config.get('DEBUG', False):
#          db.create_all()

    return app


def register_blueprints(app):
    """Register all Flask blueprints for modular routing."""
    from webapp.routes.auth import auth_bp
    from webapp.routes.main import main_bp
    from webapp.routes.profile import profile_bp
    from webapp.routes.therapy import therapy_bp
    from webapp.routes.api import api_bp

    # Register blueprints with URL prefixes
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(profile_bp, url_prefix='/profile')
    app.register_blueprint(therapy_bp, url_prefix='/therapy')
    app.register_blueprint(api_bp, url_prefix='/api/v1')


def register_error_handlers(app):
    """Register custom error handlers."""
    from flask import render_template, jsonify

    @app.errorhandler(404)
    def not_found_error(error):
        if app.config.get('API_REQUEST', False):
            return jsonify({'error': 'Resource not found'}), 404
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        if app.config.get('API_REQUEST', False):
            return jsonify({'error': 'Internal server error'}), 500
        return render_template('errors/500.html'), 500

    @app.errorhandler(403)
    def forbidden_error(error):
        if app.config.get('API_REQUEST', False):
            return jsonify({'error': 'Forbidden'}), 403
        return render_template('errors/403.html'), 403


def register_cli_commands(app):
    """Register custom CLI commands."""
    import click

    @app.cli.command()
    def init_db():
        """Initialize the database."""
        db.create_all()
        click.echo('Database initialized.')

    @app.cli.command()
    def seed_db():
        """Seed database with sample data."""
        from webapp.utils.seed_data import seed_sample_data
        seed_sample_data()
        click.echo('Database seeded with sample data.')

    @app.cli.command()
    def create_admin():
        """Create an admin user."""
        from webapp.models.user import User
        from werkzeug.security import generate_password_hash

        email = click.prompt('Admin email')
        password = click.prompt('Admin password', hide_input=True)

        admin = User(
            email=email,
            password_hash=generate_password_hash(password),
            is_admin=True
        )
        db.session.add(admin)
        db.session.commit()
        click.echo(f'Admin user created: {email}')
