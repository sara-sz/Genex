#!/usr/bin/env python3
"""
GENEX Flask Application Entry Point
====================================
Run this file to start the Flask development server.

Usage:
    python run.py
    
Or with environment:
    FLASK_ENV=development python run.py
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from webapp import create_app, db
from webapp.models import User, ChildProfile, TherapySession, TherapyPlan, MilestoneTracking

# Create Flask app instance
app = create_app(os.getenv('FLASK_ENV', 'development'))


@app.shell_context_processor
def make_shell_context():
    """
    Make database models available in Flask shell.
    
    Usage:
        flask shell
        >>> User.query.all()
    """
    return {
        'db': db,
        'User': User,
        'ChildProfile': ChildProfile,
        'TherapySession': TherapySession,
        'TherapyPlan': TherapyPlan,
        'MilestoneTracking': MilestoneTracking
    }


@app.cli.command()
def test():
    """Run the unit tests."""
    import unittest
    tests = unittest.TestLoader().discover('tests')
    unittest.TextTestRunner(verbosity=2).run(tests)


if __name__ == '__main__':
    # Get configuration
    host = os.getenv('FLASK_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_PORT', 5000))
    debug = os.getenv('FLASK_ENV') == 'development'
    
    
    print(f"""
    ============================================================
    
                  GENEX Flask Application
         Genetics Care Management & Therapy Planning
    
    ============================================================
    
    Starting server...
    
    Environment: {os.getenv('FLASK_ENV', 'development')}
    Host: {host}
    Port: {port}
    Debug: {debug}
    
    Access the application at: http://localhost:{port}
    
    Press CTRL+C to stop the server.
    """)
    
    app.run(host=host, port=port, debug=debug)
