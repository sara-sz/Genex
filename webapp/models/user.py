"""
User Model
==========
Database model for user authentication and management.
"""

from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from webapp import db


class User(UserMixin, db.Model):
    """
    User model for authentication and profile management.
    
    Attributes:
        id: Primary key
        email: Unique email address
        password_hash: Hashed password
        created_at: Account creation timestamp
        is_admin: Admin flag
        is_active: Account active status
        children: Relationship to child profiles
    """
    
    __tablename__ = 'users'
    
    # Primary key
    id = db.Column(db.Integer, primary_key=True)
    
    # Authentication
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    
    # User info
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    
    # Metadata
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    
    # Status flags
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    
    # Relationships
    children = db.relationship('ChildProfile', backref='parent', lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<User {self.email}>'
    
    @property
    def password(self):
        """Prevent password from being accessed."""
        raise AttributeError('password is not a readable attribute')
    
    @password.setter
    def password(self, password):
        """Hash password when setting."""
        self.password_hash = generate_password_hash(password)
    
    def verify_password(self, password):
        """
        Verify password against hash.
        
        Args:
            password: Plain text password to verify
            
        Returns:
            bool: True if password matches, False otherwise
        """
        return check_password_hash(self.password_hash, password)
    
    def get_id(self):
        """Override Flask-Login's get_id to return string."""
        return str(self.id)
    
    def update_last_login(self):
        """Update last login timestamp."""
        self.last_login = datetime.utcnow()
        db.session.commit()
    
    @property
    def full_name(self):
        """Get user's full name."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.email
    
    def to_dict(self):
        """
        Convert user to dictionary (for API responses).
        
        Returns:
            dict: User data (excluding sensitive fields)
        """
        return {
            'id': self.id,
            'email': self.email,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'full_name': self.full_name,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'is_admin': self.is_admin,
            'children_count': self.children.count()
        }
    
    @staticmethod
    def find_by_email(email):
        """
        Find user by email address.
        
        Args:
            email: Email address to search for
            
        Returns:
            User object or None
        """
        return User.query.filter_by(email=email.lower()).first()
    
    @staticmethod
    def create_user(email, password, **kwargs):
        """
        Create a new user.
        
        Args:
            email: User email
            password: Plain text password
            **kwargs: Additional user attributes
            
        Returns:
            User: Created user object
        """
        user = User(
            email=email.lower(),
            **kwargs
        )
        user.password = password
        
        db.session.add(user)
        db.session.commit()
        
        return user
