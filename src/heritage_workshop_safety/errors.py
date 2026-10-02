"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域规则错误的基类。"""

    code = "domain_error"
    http_status = 400


class ValidationError(DomainError):
    """输入不满足领域约束。"""

    code = "validation_error"
    http_status = 422


class NotFoundError(DomainError):
    """引用的实体不存在。"""

    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    """状态冲突或幂等键重复。"""

    code = "conflict"
    http_status = 409


class PermissionDenied(DomainError):
    """角色无权执行该操作或查看该字段。"""

    code = "permission_denied"
    http_status = 403
