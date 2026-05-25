"""ORM model package — import all models so Base.metadata is fully populated.

Importing this package (or any sub-module that imports from it) registers every
table with SQLAlchemy's metadata before create_all() / drop_all() is called.
"""

from src.persistence.models.base import Base
from src.persistence.models.conversation import Conversation
from src.persistence.models.hitl import HITLApproval, HITLAuditLog
from src.persistence.models.message import Message
from src.persistence.models.user import RefreshToken, User

__all__ = [
    "Base",
    "Conversation",
    "HITLApproval",
    "HITLAuditLog",
    "Message",
    "RefreshToken",
    "User",
]
