"""
Therapy Routes
==============
Handles therapy planning, Q&A sessions, and plan management.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from webapp import db
from webapp.models.child_profile import ChildProfile
from webapp.models.therapy import TherapySession, TherapyPlan

therapy_bp = Blueprint('therapy', __name__)


@therapy_bp.route('/select-child')
@login_required
def select_child():
    """Select which child to create a therapy plan for."""
    children = current_user.children.all()
    
    if not children:
        flash('Please create a child profile first.', 'info')
        return redirect(url_for('profile.create_child'))
    
    return render_template('therapy/select_child.html', children=children)


@therapy_bp.route('/child/<int:child_id>/start')
@login_required
def start_therapy(child_id):
    """Start therapy planning process for a child."""
    child = ChildProfile.query.get_or_404(child_id)
    
    # Ensure user owns this profile
    if child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('therapy.select_child'))
    
    return render_template('therapy/start.html', child=child)


@therapy_bp.route('/child/<int:child_id>/category/<category>')
@login_required
def therapy_category(child_id, category):
    """
    Start Q&A session for a specific developmental category.
    
    Categories: gross_motor, fine_motor, speech, social, cognitive
    """
    child = ChildProfile.query.get_or_404(child_id)
    
    # Ensure user owns this profile
    if child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('therapy.select_child'))
    
    # TODO: Initialize therapy session with agent
    # This will be connected to the TherapyAgent in the next phase
    
    return render_template(
        'therapy/qna.html',
        child=child,
        category=category
    )


@therapy_bp.route('/session/<int:session_id>/question', methods=['GET', 'POST'])
@login_required
def answer_question(session_id):
    """
    Handle answering a single milestone question.
    
    TODO: Connect to TherapyAgent for question generation
    """
    session = TherapySession.query.get_or_404(session_id)
    
    # Ensure user owns this session
    if session.child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        answer = request.form.get('answer', '')  # 'yes', 'no', 'not_sure'
        
        # TODO: Process answer with agent
        # Update session state
        # Get next question or generate plan
        
        flash('Answer recorded!', 'success')
        return redirect(url_for('therapy.answer_question', session_id=session_id))
    
    return render_template('therapy/question.html', session=session)


@therapy_bp.route('/child/<int:child_id>/plans')
@login_required
def view_plans(child_id):
    """View all therapy plans for a child."""
    child = ChildProfile.query.get_or_404(child_id)
    
    # Ensure user owns this profile
    if child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('therapy.select_child'))
    
    plans = child.therapy_plans.order_by(TherapyPlan.created_at.desc()).all()
    
    return render_template('therapy/plans.html', child=child, plans=plans)


@therapy_bp.route('/plan/<int:plan_id>')
@login_required
def view_plan(plan_id):
    """View a specific therapy plan."""
    plan = TherapyPlan.query.get_or_404(plan_id)
    
    # Ensure user owns this plan
    if plan.child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('main.dashboard'))
    
    return render_template('therapy/plan_detail.html', plan=plan)


@therapy_bp.route('/plan/<int:plan_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_plan(plan_id):
    """Edit a therapy plan."""
    plan = TherapyPlan.query.get_or_404(plan_id)
    
    # Ensure user owns this plan
    if plan.child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        # TODO: Update plan with user modifications
        flash('Plan updated!', 'success')
        return redirect(url_for('therapy.view_plan', plan_id=plan_id))
    
    return render_template('therapy/edit_plan.html', plan=plan)


@therapy_bp.route('/plan/<int:plan_id>/archive', methods=['POST'])
@login_required
def archive_plan(plan_id):
    """Archive a therapy plan."""
    plan = TherapyPlan.query.get_or_404(plan_id)
    
    # Ensure user owns this plan
    if plan.child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('main.dashboard'))
    
    plan.status = 'archived'
    db.session.commit()
    
    flash('Plan archived.', 'info')
    return redirect(url_for('therapy.view_plans', child_id=plan.child_id))
