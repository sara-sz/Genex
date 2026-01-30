"""
Profile Routes
==============
Handles child profile creation, viewing, and editing.
"""

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import datetime
from webapp import db
from webapp.models.child_profile import ChildProfile

profile_bp = Blueprint('profile', __name__)


@profile_bp.route('/children')
@login_required
def list_children():
    """List all child profiles for the current user."""
    children = current_user.children.all()
    return render_template('profile/list.html', children=children)


@profile_bp.route('/child/<int:child_id>')
@login_required
def view_child(child_id):
    """View detailed profile for a specific child."""
    child = ChildProfile.query.get_or_404(child_id)
    
    # Ensure user owns this profile
    if child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('profile.list_children'))
    
    # Get therapy plans
    therapy_plans = child.therapy_plans.order_by(ChildProfile.created_at.desc()).all()
    
    # Get milestone tracking
    milestones = child.milestones.order_by(MilestoneTracking.category).all()
    
    return render_template(
        'profile/view.html',
        child=child,
        therapy_plans=therapy_plans,
        milestones=milestones
    )


@profile_bp.route('/child/create', methods=['GET', 'POST'])
@login_required
def create_child():
    """Create a new child profile."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        dob_str = request.form.get('date_of_birth', '')
        gender = request.form.get('gender', '')
        diagnosis = request.form.get('diagnosis', '').strip()
        diagnosis_date_str = request.form.get('diagnosis_date', '')
        additional_conditions = request.form.get('additional_conditions', '').strip()
        notes = request.form.get('notes', '').strip()
        
        # Validation
        errors = []
        
        if not name:
            errors.append('Child\'s name is required.')
        
        if not dob_str:
            errors.append('Date of birth is required.')
        else:
            try:
                dob = datetime.strptime(dob_str, '%Y-%m-%d').date()
            except ValueError:
                errors.append('Invalid date of birth format.')
                dob = None
        
        if not diagnosis:
            errors.append('Diagnosis is required.')
        
        # Parse diagnosis date if provided
        diagnosis_date = None
        if diagnosis_date_str:
            try:
                diagnosis_date = datetime.strptime(diagnosis_date_str, '%Y-%m-%d').date()
            except ValueError:
                errors.append('Invalid diagnosis date format.')
        
        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('profile/create.html')
        
        # Create profile
        try:
            child = ChildProfile.create_profile(
                user_id=current_user.id,
                name=name,
                date_of_birth=dob,
                diagnosis=diagnosis,
                gender=gender if gender else None,
                diagnosis_date=diagnosis_date,
                additional_conditions=additional_conditions if additional_conditions else None,
                notes=notes if notes else None
            )
            
            flash(f'Profile created for {child.name}!', 'success')
            return redirect(url_for('profile.view_child', child_id=child.id))
        
        except Exception as e:
            db.session.rollback()
            flash('An error occurred while creating the profile.', 'error')
            print(f"Profile creation error: {e}")
    
    return render_template('profile/create.html')


@profile_bp.route('/child/<int:child_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_child(child_id):
    """Edit an existing child profile."""
    child = ChildProfile.query.get_or_404(child_id)
    
    # Ensure user owns this profile
    if child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('profile.list_children'))
    
    if request.method == 'POST':
        child.name = request.form.get('name', '').strip()
        
        dob_str = request.form.get('date_of_birth', '')
        if dob_str:
            try:
                child.date_of_birth = datetime.strptime(dob_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid date of birth format.', 'error')
                return render_template('profile/edit.html', child=child)
        
        child.gender = request.form.get('gender', '')
        child.diagnosis = request.form.get('diagnosis', '').strip()
        
        diagnosis_date_str = request.form.get('diagnosis_date', '')
        if diagnosis_date_str:
            try:
                child.diagnosis_date = datetime.strptime(diagnosis_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass  # Keep existing or None
        
        child.additional_conditions = request.form.get('additional_conditions', '').strip()
        child.notes = request.form.get('notes', '').strip()
        
        # Update chronological age
        child.update_chronological_age()
        
        try:
            db.session.commit()
            flash(f'Profile updated for {child.name}!', 'success')
            return redirect(url_for('profile.view_child', child_id=child.id))
        except Exception as e:
            db.session.rollback()
            flash('An error occurred while updating the profile.', 'error')
            print(f"Profile update error: {e}")
    
    return render_template('profile/edit.html', child=child)


@profile_bp.route('/child/<int:child_id>/delete', methods=['POST'])
@login_required
def delete_child(child_id):
    """Delete a child profile."""
    child = ChildProfile.query.get_or_404(child_id)
    
    # Ensure user owns this profile
    if child.user_id != current_user.id:
        flash('Access denied.', 'error')
        return redirect(url_for('profile.list_children'))
    
    try:
        child_name = child.name
        db.session.delete(child)
        db.session.commit()
        flash(f'Profile for {child_name} has been deleted.', 'info')
    except Exception as e:
        db.session.rollback()
        flash('An error occurred while deleting the profile.', 'error')
        print(f"Profile deletion error: {e}")
    
    return redirect(url_for('profile.list_children'))
