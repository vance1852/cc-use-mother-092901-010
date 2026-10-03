"""技能赛训协作基础服务的服务端基础包。"""

from .accessibility import AccessibilityService
from .service import DomainService

__all__ = ["DomainService", "AccessibilityService"]
