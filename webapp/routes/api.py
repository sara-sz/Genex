"""
API Routes
==========
RESTful API endpoints for AJAX calls and external integrations.
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from webapp.models.child_profile import ChildProfile
from webapp.models.therapy import TherapySession, TherapyPlan, MilestoneTracking

api_bp = Blueprint('api', __name__)


@api_bp.before_request
def set_api_flag():
    """Mark requests as API requests for proper error handling."""
    from flask import current_app
    current_app.config['API_REQUEST'] = True


@api_bp.route('/health')
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'service': 'genex-api',
        'version': '1.0.0'
    })


@api_bp.route('/children', methods=['GET'])
@login_required
def get_children():
    """Get all child profiles for current user."""
    children = current_user.children.all()
    return jsonify({
        'children': [child.to_dict() for child in children]
    })


@api_bp.route('/children/<int:child_id>', methods=['GET'])
@login_required
def get_child(child_id):
    """Get specific child profile."""
    child = ChildProfile.query.get_or_404(child_id)
    
    if child.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    return jsonify(child.to_dict())


@api_bp.route('/children/<int:child_id>/plans', methods=['GET'])
@login_required
def get_child_plans(child_id):
    """Get all therapy plans for a child."""
    child = ChildProfile.query.get_or_404(child_id)
    
    if child.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    plans = child.therapy_plans.order_by(TherapyPlan.created_at.desc()).all()
    
    return jsonify({
        'plans': [plan.to_dict() for plan in plans]
    })


@api_bp.route('/plans/<int:plan_id>', methods=['GET'])
@login_required
def get_plan(plan_id):
    """Get specific therapy plan."""
    plan = TherapyPlan.query.get_or_404(plan_id)
    
    if plan.child.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    return jsonify(plan.to_dict())


@api_bp.route('/therapy/question', methods=['POST'])
@login_required
def submit_answer():
    """
    Submit answer to a therapy question.
    
    Expects JSON:
    {
        "session_id": 123,
        "question_id": "q1",
        "answer": "yes"
    }
    """
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    session_id = data.get('session_id')
    question_id = data.get('question_id')
    answer = data.get('answer')
    
    if not all([session_id, question_id, answer]):
        return jsonify({'error': 'Missing required fields'}), 400
    
    # Get session
    session = TherapySession.query.get_or_404(session_id)
    
    if session.child.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    # TODO: Process answer with TherapyAgent
    # Update session state
    # Return next question or completion status
    
    session.add_answer(question_id, answer)
    session.current_question_index += 1
    
    return jsonify({
        'success': True,
        'message': 'Answer recorded',
        'next_question_index': session.current_question_index
    })


@api_bp.route('/research/query', methods=['POST'])
@login_required
def research_query():
    """
    Submit a research query to the multi-model research agent.
    
    Expects JSON:
    {
        "child_id": 123,
        "query": "What therapies help with fine motor skills?"
    }
    """
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    child_id = data.get('child_id')
    query = data.get('query')
    
    if not child_id or not query:
        return jsonify({'error': 'Missing required fields'}), 400
    
    # Get child
    child = ChildProfile.query.get_or_404(child_id)
    
    if child.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    # TODO: Call ResearchAgent with query and child profile
    # This will be implemented in Phase 2
    
    return jsonify({
        'success': True,
        'message': 'Research query submitted',
        'query_id': 'temp-query-id',
        'status': 'processing'
    })


@api_bp.route('/milestones/<int:child_id>', methods=['GET'])
@login_required
def get_milestones(child_id):
    """Get milestone tracking for a child."""
    child = ChildProfile.query.get_or_404(child_id)
    
    if child.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    milestones = child.milestones.all()
    
    return jsonify({
        'milestones': [m.to_dict() for m in milestones]
    })


@api_bp.route('/milestones/<int:milestone_id>/achieve', methods=['POST'])
@login_required
def mark_milestone_achieved(milestone_id):
    """Mark a milestone as achieved."""
    milestone = MilestoneTracking.query.get_or_404(milestone_id)
    
    if milestone.child.user_id != current_user.id:
        return jsonify({'error': 'Access denied'}), 403
    
    child_age = milestone.child.age_months
    milestone.mark_achieved(child_age)
    
    return jsonify({
        'success': True,
        'milestone': milestone.to_dict()
    })


@api_bp.errorhandler(404)
def api_not_found(error):
    """API 404 handler."""
    return jsonify({'error': 'Resource not found'}), 404


@api_bp.errorhandler(500)
def api_server_error(error):
    """API 500 handler."""
    return jsonify({'error': 'Internal server error'}), 500
