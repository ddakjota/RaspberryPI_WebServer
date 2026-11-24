import json
import os
from urllib.parse import urlsplit

MESSAGES = {
    "completed": ("세탁 완료", "작업이 끝났어요. 세탁물을 확인하세요."),
    "offline": ("센서 연결 오류", "30초 동안 센서 데이터가 들어오지 않았어요."),
    "recovered": ("센서 연결 복구", "자동 감지를 다시 시작했어요."),
    "test": ("테스트 알림", "이 기기에서 세탁 알림을 받을 수 있어요."),
}
PUSH_HOSTS = {
    "fcm.googleapis.com", "web.push.apple.com", "updates.push.services.mozilla.com",
    "push.services.mozilla.com", "wns.windows.com",
}


def trusted_endpoint(endpoint):
    if not endpoint.isascii() or any(ord(character) < 33 or character in "\\\x7f" for character in endpoint):
        return False
    try:
        url = urlsplit(endpoint)
        return (
            url.scheme == "https" and url.port in (None, 443)
            and url.username is None and url.password is None
            and url.hostname in PUSH_HOSTS and bool(url.path)
            and not url.fragment
        )
    except ValueError:
        return False


def configured():
    return all(os.environ.get(name) for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"))


def send(subscription, kind, notification_id, ttl, installation_id):
    if not trusted_endpoint(subscription["endpoint"]):
        return 400

    from pywebpush import WebPushException, webpush
    import requests

    class DirectSession(requests.Session):
        def post(self, url, **kwargs):
            return super().post(url, allow_redirects=False, **kwargs)

    title, body = MESSAGES[kind]
    with DirectSession() as session:
        session.trust_env = False
        try:
            response = webpush(
                subscription_info=subscription,
                data=json.dumps({"title": title, "body": body, "id": notification_id,
                                 "installation_id": installation_id}),
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                vapid_claims={"sub": os.environ["VAPID_SUBJECT"]},
                ttl=ttl,
                headers={"Topic": notification_id[:32]},
                requests_session=session,
                timeout=8,
            )
            return response.status_code
        except WebPushException as error:
            return error.response.status_code if error.response is not None else 0
