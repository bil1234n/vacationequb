from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsStaffOrReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or bool(request.user and request.user.is_staff)


class IsOwner(BasePermission):
    owner_field = "user"

    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or getattr(obj, self.owner_field, None) == request.user
