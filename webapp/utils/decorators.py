"""
Decorators
==========
Custom decorators for route protection and functionality.
"""

from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user


def admin_required(f):
    """
    Decorator to require admin access for a route.
    
    Usage:
        @app.route('/admin')
        @login_required
        @admin_required
        def admin_page():
            return "Admin only"
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('auth.login'))
        
        if not current_user.is_admin:
            flash('Access denied. Admin privileges required.', 'error')
            return redirect(url_for('main.dashboard'))
        
        return f(*args, **kwargs)
    
    return decorated_function


def child_owner_required(f):
    """
    Decorator to ensure user owns the child profile.
    
    Expects child_id in route parameters.
    """
    @wraps(f)
    def decorated_function(child_id, *args, **kwargs):
        from webapp.models.child_profile import ChildProfile
        
        child = ChildProfile.query.get_or_404(child_id)
        
        if child.user_id != current_user.id:
            flash('Access denied.', 'error')
            return redirect(url_for('main.dashboard'))
        
        return f(child_id, *args, **kwargs)
    
    return decorated_function
