"""Pluggable push delivery (Expo push service by default for React Native)."""
import logging

import requests
from django.conf import settings
from django.utils.module_loading import import_string

log = logging.getLogger(__name__)


class LogPushBackend:
    def send(self, token: str, title: str, body: str, data: dict) -> bool:
        log.info("PUSH -> %s | %s | %s", token or "<no token>", title, body)
        return True


class ExpoPushBackend:
    URL = "https://exp.host/--/api/v2/push/send"

    def send(self, token, title, body, data):
        if not token:
            return False
        headers = {"Content-Type": "application/json"}
        if settings.PUSH.get("EXPO_ACCESS_TOKEN"):
            headers["Authorization"] = f"Bearer {settings.PUSH['EXPO_ACCESS_TOKEN']}"
        try:
            r = requests.post(self.URL, json={"to": token, "title": title, "body": body, "data": data,
                                              "sound": "default", "priority": "high"}, headers=headers, timeout=10)
            return r.ok
        except requests.RequestException:
            log.exception("Expo push failed")
            return False


def get_backend():
    return import_string(settings.PUSH["BACKEND"])()
