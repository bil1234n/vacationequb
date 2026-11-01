from django.db import transaction

from .models import Notification
from .push import get_backend


def notify(user, kind: str, title: str, body: str, data: dict | None = None) -> Notification:
    note = Notification.objects.create(user=user, kind=kind, title=title, body=body, data=data or {})

    def _push():
        sent = get_backend().send(user.push_token, title, body, {"kind": kind, **(data or {})})
        if sent:
            Notification.objects.filter(pk=note.pk).update(pushed=True)

    transaction.on_commit(_push)
    return note
