"""
Helper Functions
================
General utility functions used throughout the application.
"""

from flask import flash, current_app
import os


def allowed_file(filename):
    """
    Check if uploaded file has allowed extension.
    
    Args:
        filename: Name of the uploaded file
        
    Returns:
        bool: True if extension is allowed
    """
    if not filename:
        return False
    
    allowed = current_app.config.get('ALLOWED_EXTENSIONS', set())
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed


def flash_errors(form):
    """
    Flash all errors from a WTForm form.
    
    Args:
        form: WTForms form object with errors
    """
    for field, errors in form.errors.items():
        for error in errors:
            flash(f"{field}: {error}", 'error')


def secure_filename_custom(filename):
    """
    Make filename safe for storing.
    
    Args:
        filename: Original filename
        
    Returns:
        str: Sanitized filename
    """
    from werkzeug.utils import secure_filename
    import uuid
    from datetime import datetime
    
    # Get extension
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    
    # Generate unique filename
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    unique_id = str(uuid.uuid4())[:8]
    
    return f"{timestamp}_{unique_id}.{ext}" if ext else f"{timestamp}_{unique_id}"


def format_age(months):
    """
    Format age in months to human-readable string.
    
    Args:
        months: Age in months
        
    Returns:
        str: Formatted age (e.g., "2 years, 3 months")
    """
    if months is None:
        return "Unknown"
    
    years = months // 12
    remaining_months = months % 12
    
    if years == 0:
        return f"{months} month{'s' if months != 1 else ''}"
    elif remaining_months == 0:
        return f"{years} year{'s' if years != 1 else ''}"
    else:
        return f"{years} year{'s' if years != 1 else ''}, {remaining_months} month{'s' if remaining_months != 1 else ''}"


def get_category_display_name(category_key):
    """
    Convert category key to display name.
    
    Args:
        category_key: Category key (e.g., 'gross_motor')
        
    Returns:
        str: Display name (e.g., 'Gross Motor')
    """
    category_map = {
        'gross_motor': 'Gross Motor',
        'fine_motor': 'Fine Motor',
        'speech': 'Speech & Language',
        'language': 'Speech & Language',
        'social': 'Social & Emotional',
        'cognitive': 'Cognitive',
        'physical': 'Physical Development'
    }
    
    return category_map.get(category_key, category_key.replace('_', ' ').title())


def paginate_query(query, page=1, per_page=10):
    """
    Paginate a SQLAlchemy query.
    
    Args:
        query: SQLAlchemy query object
        page: Page number (1-indexed)
        per_page: Items per page
        
    Returns:
        tuple: (items, total_pages, current_page)
    """
    paginated = query.paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )
    
    return paginated.items, paginated.pages, paginated.page


def generate_session_id():
    """
    Generate a unique session ID for therapy sessions.
    
    Returns:
        str: Unique session ID
    """
    import uuid
    from datetime import datetime
    
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    unique = str(uuid.uuid4())[:8]
    
    return f"session_{timestamp}_{unique}"
