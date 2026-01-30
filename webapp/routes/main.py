"""
Main Routes
===========
Handles main application pages: home, dashboard, about, etc.
"""

from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    """
    Landing page / home page.
    
    If user is logged in, redirect to dashboard.
    Otherwise, show marketing/welcome page.
    """
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    return render_template('main/index.html')


@main_bp.route('/dashboard')
@login_required
def dashboard():
    """
    Main dashboard - shows overview of child profiles and recent activity.
    
    This is the user's home screen after login.
    """
    # Get user's children
    children = current_user.children.all()
    
    # If no children, redirect to create profile
    if not children:
        return redirect(url_for('profile.create_child'))
    
    # Get active therapy plans
    active_plans = []
    for child in children:
        plans = child.therapy_plans.filter_by(status='active').all()
        active_plans.extend(plans)
    
    return render_template(
        'main/dashboard.html',
        children=children,
        active_plans=active_plans
    )


@main_bp.route('/about')
def about():
    """About page - information about GeneX."""
    return render_template('main/about.html')


@main_bp.route('/features')
def features():
    """Features page - showcase application capabilities."""
    return render_template('main/features.html')


@main_bp.route('/privacy')
def privacy():
    """Privacy policy page."""
    return render_template('main/privacy.html')


@main_bp.route('/terms')
def terms():
    """Terms of service page."""
    return render_template('main/terms.html')


@main_bp.route('/help')
def help():
    """Help and FAQ page."""
    return render_template('main/help.html')
