"""
Routes Package
==============
Exposes all route blueprints.
"""

from webapp.routes.auth import auth_bp
from webapp.routes.main import main_bp
from webapp.routes.profile import profile_bp
from webapp.routes.therapy import therapy_bp
from webapp.routes.api import api_bp

__all__ = [
    'auth_bp',
    'main_bp',
    'profile_bp',
    'therapy_bp',
    'api_bp'
]
