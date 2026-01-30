"""
Child Profile Model
===================
Database model for storing child information and genetic condition details.
"""

from datetime import datetime, date
from webapp import db


class ChildProfile(db.Model):
    """
    Child profile model for storing child-specific information.
    
    This model stores the child's basic information, diagnosis, and 
    developmental details needed for therapy planning and research.
    """
    
    __tablename__ = 'child_profiles'
    
    # Primary key
    id = db.Column(db.Integer, primary_key=True)
    
    # Foreign key to user
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Basic information
    name = db.Column(db.String(100), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    gender = db.Column(db.String(20))  # 'male', 'female', 'other', 'prefer_not_to_say'
    
    # Medical information
    diagnosis = db.Column(db.String(200), nullable=False)
    diagnosis_date = db.Column(db.Date)
    additional_conditions = db.Column(db.Text)  # JSON or comma-separated list
    
    # Developmental information
    chronological_age_months = db.Column(db.Integer)  # Calculated from DOB
    
    # Notes
    notes = db.Column(db.Text)
    
    # Metadata
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    therapy_sessions = db.relationship('TherapySession', backref='child', lazy='dynamic', cascade='all, delete-orphan')
    therapy_plans = db.relationship('TherapyPlan', backref='child', lazy='dynamic', cascade='all, delete-orphan')
    milestones = db.relationship('MilestoneTracking', backref='child', lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<ChildProfile {self.name}>'
    
    @property
    def age_years(self):
        """Calculate current age in years."""
        if not self.date_of_birth:
            return None
        today = date.today()
        return today.year - self.date_of_birth.year - (
            (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day)
        )
    
    @property
    def age_months(self):
        """Calculate current age in months."""
        if not self.date_of_birth:
            return None
        today = date.today()
        months = (today.year - self.date_of_birth.year) * 12
        months += today.month - self.date_of_birth.month
        if today.day < self.date_of_birth.day:
            months -= 1
        return months
    
    @property
    def age_display(self):
        """Get human-readable age string."""
        years = self.age_years
        months = self.age_months
        
        if years is None:
            return "Unknown"
        
        if years == 0:
            return f"{months} month{'s' if months != 1 else ''}"
        elif months % 12 == 0:
            return f"{years} year{'s' if years != 1 else ''}"
        else:
            remaining_months = months % 12
            return f"{years} year{'s' if years != 1 else ''}, {remaining_months} month{'s' if remaining_months != 1 else ''}"
    
    def update_chronological_age(self):
        """Update the stored chronological age in months."""
        self.chronological_age_months = self.age_months
        db.session.commit()
    
    def to_dict(self):
        """
        Convert child profile to dictionary.
        
        Returns:
            dict: Child profile data
        """
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'date_of_birth': self.date_of_birth.isoformat() if self.date_of_birth else None,
            'age_years': self.age_years,
            'age_months': self.age_months,
            'age_display': self.age_display,
            'gender': self.gender,
            'diagnosis': self.diagnosis,
            'diagnosis_date': self.diagnosis_date.isoformat() if self.diagnosis_date else None,
            'additional_conditions': self.additional_conditions,
            'notes': self.notes,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat()
        }
    
    def to_agent_profile(self):
        """
        Convert to profile format expected by AI agents.
        
        Returns:
            dict: Profile in agent-compatible format
        """
        return {
            'name': self.name,
            'age_years': self.age_years,
            'age_months': self.age_months,
            'diagnosis': self.diagnosis,
            'additional_conditions': self.additional_conditions,
            'notes': self.notes
        }
    
    @staticmethod
    def create_profile(user_id, name, date_of_birth, diagnosis, **kwargs):
        """
        Create a new child profile.
        
        Args:
            user_id: Parent user ID
            name: Child's name
            date_of_birth: Child's date of birth
            diagnosis: Primary diagnosis
            **kwargs: Additional profile attributes
            
        Returns:
            ChildProfile: Created profile object
        """
        profile = ChildProfile(
            user_id=user_id,
            name=name,
            date_of_birth=date_of_birth,
            diagnosis=diagnosis,
            **kwargs
        )
        profile.update_chronological_age()
        
        db.session.add(profile)
        db.session.commit()
        
        return profile
