"""
GENEX Flask Application Configuration
======================================
Environment-based configuration classes for different deployment scenarios.
"""

import os
from datetime import timedelta
from pathlib import Path


class Config:
    """Base configuration with common settings."""
    
    # Get the base directory (genex root)
    BASE_DIR = Path(__file__).resolve().parent.parent
    
    # Flask Core
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    
    # Database
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = False
    
    # Session
    SESSION_TYPE = 'filesystem'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=1)
    SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # CSRF Protection
    WTF_CSRF_ENABLED = True
    WTF_CSRF_SECRET_KEY = os.environ.get('WTF_CSRF_SECRET_KEY') or SECRET_KEY
    
    # File Upload
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))  # 16MB
    UPLOAD_FOLDER = BASE_DIR / 'webapp' / 'static' / 'uploads'
    ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'xlsx', 'csv'}
    
    # AI Agent Configuration
    GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
    GOOGLE_GENAI_USE_VERTEXAI = os.environ.get('GOOGLE_GENAI_USE_VERTEXAI', 'FALSE')
    
    # Agent Settings
    AGENT_TIMEOUT = int(os.environ.get('AGENT_TIMEOUT', 120))  # seconds
    AGENT_MAX_RETRIES = int(os.environ.get('AGENT_MAX_RETRIES', 3))
    ENABLE_AGENT_CACHING = os.environ.get('ENABLE_AGENT_CACHING', 'True').lower() == 'true'
    
    # CDC Data
    CDC_TABLE_PATH = BASE_DIR / 'data' / 'milestone-cdc-table.xlsx'
    
    # Feature Flags
    ENABLE_RESEARCH_AGENT = os.environ.get('ENABLE_RESEARCH_AGENT', 'True').lower() == 'true'
    ENABLE_THERAPY_AGENT = os.environ.get('ENABLE_THERAPY_AGENT', 'True').lower() == 'true'
    ENABLE_MULTI_MODEL_RESEARCH = os.environ.get('ENABLE_MULTI_MODEL_RESEARCH', 'True').lower() == 'true'
    
    # Celery (for async tasks)
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
    
    # Rate Limiting
    RATELIMIT_STORAGE_URL = os.environ.get('RATELIMIT_STORAGE_URL', 'memory://')
    
    # Logging
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_FILE = BASE_DIR / 'logs' / 'genex.log'
    
    # Application
    APP_NAME = os.environ.get('APP_NAME', 'GeneX')
    
    @staticmethod
    def init_app(app):
        """Initialize application-specific configuration."""
        # Create necessary directories
        (Config.BASE_DIR / 'logs').mkdir(exist_ok=True)
        Config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


class DevelopmentConfig(Config):
    """Development environment configuration."""
    
    DEBUG = True
    TESTING = False
    
    # Database - SQLite for development
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        f'sqlite:///{Config.BASE_DIR / "data" / "genex_dev.db"}'
    
    SQLALCHEMY_ECHO = True  # Log all SQL queries
    
    # Disable CSRF for easier testing (enable in production!)
    WTF_CSRF_ENABLED = False  # Can be toggled
    
    # Session
    SESSION_COOKIE_SECURE = False
    
    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        print(f"Running in DEVELOPMENT mode")
        print(f"Database: {cls.SQLALCHEMY_DATABASE_URI}")


class ProductionConfig(Config):
    """Production environment configuration."""
    
    DEBUG = False
    TESTING = False
    
    # Database - PostgreSQL recommended for production
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        f'sqlite:///{Config.BASE_DIR / "data" / "genex_prod.db"}'
    
    # Force HTTPS
    SESSION_COOKIE_SECURE = True
    
    # Strict CSRF
    WTF_CSRF_ENABLED = True
    
    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        
        # Log to file in production
        import logging
        from logging.handlers import RotatingFileHandler
        
        if not app.debug:
            file_handler = RotatingFileHandler(
                cls.LOG_FILE,
                maxBytes=10240000,  # 10MB
                backupCount=10
            )
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
            ))
            file_handler.setLevel(logging.INFO)
            app.logger.addHandler(file_handler)
            app.logger.setLevel(logging.INFO)
            app.logger.info('GeneX startup')


class TestingConfig(Config):
    """Testing environment configuration."""
    
    DEBUG = True
    TESTING = True
    
    # Use in-memory SQLite for tests
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    
    # Disable CSRF for testing
    WTF_CSRF_ENABLED = False
    
    # Speed up password hashing in tests
    BCRYPT_LOG_ROUNDS = 4
    
    @classmethod
    def init_app(cls, app):
        Config.init_app(app)


# Configuration dictionary for easy access
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
