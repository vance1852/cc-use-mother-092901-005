"""跨区域旅游包车联审协同服务的服务端基础包。"""

from .charter import CharterService
from .service import DomainService

__all__ = ["CharterService", "DomainService"]
