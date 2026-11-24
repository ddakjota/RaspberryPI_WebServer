import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import admin
import main
import store

CSRF = {"Origin": "http://localhost:8000", "X-Laundry-Request": "1"}
API = "/api/v1"
SENSOR = "A7F29C"
DEVICE = API + "/devices/" + SENSOR
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/example", "keys": {"p256dh": "example", "auth": "example"}}


class LaundryTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {
            "LAUNDRY_ORIGIN": CSRF["Origin"], "LAUNDRY_MIN_DELTA": "80",
        }))
        self.path = Path(directory) / "test.db"
        self.now = 1800000000
        self.number = 0
        self.client = self.start()
        provision = admin.provision(self.path, "add", SENSOR, self.now)
        self.code = provision["등록 코드"]
        self.owner_code = provision["관리자 등록 코드"]
        self.auth = {"Authorization": "Bearer " + provision["센서 토큰"]}

    def clock(self):
        return self.now

    def start(self):
        app = main.create_app(self.path, self.clock, background=False)
        return self.stack.enter_context(TestClient(app, base_url=CSRF["Origin"]))

    def register(self, client=None, code=None, headers=CSRF):
        client = client or self.client
        return client.post(API + "/registrations", headers=headers, json={
            "sensor_id": SENSOR, "registration_code": self.code if code is None else code,
        })

    def sample(self, brightness, *, boot="boot-1", advance=5, sequence=None, auth=None):
        self.now += advance
        if sequence is None:
            self.number += 1
            sequence = self.number
        response = self.client.post(API + "/sensor/readings", headers=self.auth if auth is None else auth, json={
            "sensor_id": SENSOR, "boot_id": boot, "sequence": sequence, "brightness": brightness,
        })
        return response

    def status(self):
        response = self.client.get(DEVICE + "/status")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def sample_many(self, brightness, count, boot="boot-1"):
        for _ in range(count):
            response = self.sample(brightness, boot=boot)
            self.assertEqual(response.json(), {"success": True}, response.text)

    def start_cycle(self):
        self.sample_many(80, 2)
        self.sample_many(1400, 3)
        self.assertEqual(self.status()["status"], "running")

    def outbox(self):
        with store.transaction(self.path) as db:
            return [row["kind"] for row in db.execute("SELECT kind FROM outbox ORDER BY rowid")]

    def test_sensor_submission_duplicate_and_automatic_completion(self):
        self.assertEqual(self.register().status_code, 201)
        self.start_cycle()
        last = self.status()["last_reading_at"]
        self.assertEqual(self.sample(80, sequence=self.number).json(), {"success": True})
        self.assertEqual(self.status()["last_reading_at"], last)
        self.sample_many(80, 6)
        self.assertEqual(self.status()["status"], "running")
        self.sample_many(80, 1)
        self.assertEqual(self.status()["status"], "completed")
        sessions = self.client.get(DEVICE + "/sessions").json()["sessions"]
        self.assertEqual([item["end_reason"] for item in sessions], ["completed"])

    def test_ambiguous_initial_light_and_short_glitch(self):
        self.register()
        self.sample_many(1400, 9)
        self.assertEqual(self.status()["status"], "idle")
        self.sample_many(80, 2)
        self.sample_many(1400, 2)
        self.sample_many(80, 2)
        self.assertEqual(self.status()["status"], "idle")
        self.sample_many(1400, 3)
        self.sample_many(80, 3)
        self.sample_many(1400, 1)
        self.sample_many(80, 6)
        self.assertEqual(self.status()["status"], "running")

    def test_single_bright_outlier_does_not_poison_calibration(self):
        self.register()
        self.sample_many(4095, 1)
        self.sample_many(80, 2)
        self.sample_many(4095, 1)
        self.sample_many(80, 2)
        self.sample_many(1400, 2)
        self.assertEqual(self.status()["status"], "idle")
        self.sample_many(1400, 1)
        self.assertEqual(self.status()["status"], "running")
        self.sample_many(80, 7)
        self.assertEqual(self.status()["status"], "completed")

    def test_legacy_calibration_is_reset_once_without_losing_sensor(self):
        with store.transaction(self.path) as db:
            db.execute("ALTER TABLE sensors DROP COLUMN last_brightness")
            db.execute("UPDATE sensors SET low=80, high=4095")
        store.initialize(self.path)
        self.register()
        self.start_cycle()
        store.initialize(self.path)
        self.sample_many(80, 7)
        self.assertEqual(self.status()["status"], "completed")

    def test_disconnect_recovery_and_reboot_keep_same_session(self):
        self.register()
        self.client.patch(API + "/push/settings", headers=CSRF, json={"notifications_enabled": True})
        self.assertEqual(self.client.put(API + "/push/subscription", headers=CSRF, json=SUB).status_code, 200)
        self.start_cycle()
        self.now += 30
        main.maintain(self.path, self.now)
        self.assertEqual(self.status()["status"], "error")
        self.assertEqual(self.status()["status"], "error")
        self.assertEqual(self.outbox(), ["offline"])
        self.number = 0
        self.sample_many(80, 6, "boot-2")
        self.assertEqual(self.status()["status"], "running")
        last = self.status()["last_reading_at"]
        self.sample(1400, boot="boot-1", sequence=50, advance=0)
        self.assertEqual(self.status()["last_reading_at"], last)
        self.sample_many(80, 1, "boot-2")
        self.assertEqual(self.status()["status"], "completed")
        self.assertEqual(self.outbox(), ["recovered", "completed"])
        self.assertEqual(len(self.client.get(DEVICE + "/sessions").json()["sessions"]), 1)

    def test_restart_cancel_resume_and_backup(self):
        self.register(code=self.owner_code).raise_for_status()
        self.start_cycle()
        self.client = self.start_with_existing_cookie()
        self.assertEqual(self.status()["status"], "idle")
        self.assertEqual(self.client.get(DEVICE + "/sessions").json()["sessions"][0]["end_reason"], "server_restart")
        self.assertEqual(self.client.post(DEVICE + "/cancel", headers=CSRF).json(), {"status": "idle", "monitoring_enabled": False})
        self.sample_many(1400, 4)
        self.assertFalse(self.status()["monitoring_enabled"])
        self.assertEqual(self.client.post(DEVICE + "/resume", headers=CSRF).json(), {"status": "idle", "monitoring_enabled": True})
        self.sample_many(1400, 3)
        self.assertEqual(self.status()["status"], "running")
        backup = admin.backup(self.path, self.path.parent / "backup")
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.now += 31 * store.DAY
        main.maintain(self.path, self.now)
        with store.transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def start_with_existing_cookie(self):
        value = self.client.cookies.get(main.COOKIE)
        client = self.start()
        client.cookies.set(main.COOKIE, value, path=API)
        return client

    def test_phone_independence_authorization_and_code_rotation(self):
        other = TestClient(self.client.app, base_url=CSRF["Origin"])
        response = self.register()
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual(self.register(other).status_code, 201)
        self.assertEqual(other.get(API + "/devices/other/status").status_code, 403)
        self.assertEqual(self.client.delete(API + "/me").status_code, 403)
        self.assertEqual(self.client.delete(API + "/me", headers=CSRF).status_code, 204)
        self.assertEqual(other.get(API + "/me").status_code, 200)
        self.assertEqual(self.sample(80, auth={"Authorization": "Bearer wrong"}).status_code, 401)
        admin.provision(self.path, "rotate-code", SENSOR, self.now)
        self.assertEqual(other.get(API + "/me").status_code, 401)
        self.assertEqual(self.register().status_code, 401)

    def test_registration_lockout(self):
        for _ in range(5):
            self.assertEqual(self.register(code="wrong").status_code, 401)
        self.assertEqual(self.register(code="wrong").status_code, 429)
        self.assertEqual(self.register().status_code, 201)
        with TestClient(self.client.app, base_url=CSRF["Origin"], client=("192.0.2.42", 5000)) as other:
            self.assertEqual(self.register(other).status_code, 201)
        self.now += 900
        self.assertEqual(self.register(code="wrong").status_code, 401)

    def test_request_limits_reject_large_readings_before_ingest(self):
        response = self.client.post(API + "/sensor/readings", headers=self.auth, content=b"x" * (16 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "BODY_TOO_LARGE")

    def test_owner_code_is_one_use_and_shared_device_keeps_read_access(self):
        self.register().raise_for_status()
        with TestClient(self.client.app, base_url=CSRF["Origin"], client=("192.0.2.43", 5000)) as owner:
            registration = self.register(owner, self.owner_code).raise_for_status()
            self.assertEqual(self.register(code=self.owner_code).status_code, 401)
            self.assertFalse(self.client.get(API + "/me").json()["can_control"])
            self.assertEqual(self.client.get(DEVICE + "/sessions").status_code, 200)
            self.assertEqual(self.client.post(DEVICE + "/cancel", headers=CSRF).status_code, 403)
            self.assertTrue(owner.get(API + "/me").json()["can_control"])
            owner.post(DEVICE + "/cancel", headers=CSRF).raise_for_status()
            owner.post(DEVICE + "/resume", headers=CSRF).raise_for_status()
            admin.revoke_device(self.path, SENSOR, registration.json()["installation_id"])
            self.assertEqual(owner.get(API + "/me").status_code, 401)
        self.assertEqual(self.client.get(API + "/me").status_code, 200)
        self.now += store.SHARE_TTL
        self.assertEqual(self.register().status_code, 401)
        self.assertEqual(self.client.get(API + "/me").status_code, 200)

    def test_existing_sensor_keeps_one_controller_and_all_device_registrations(self):
        self.register().raise_for_status()
        other = TestClient(self.client.app, base_url=CSRF["Origin"])
        self.addCleanup(other.close)
        self.register(other).raise_for_status()
        with store.transaction(self.path) as db:
            db.execute("ALTER TABLE phones DROP COLUMN controller")
        store.initialize(self.path, self.now)
        self.assertTrue(self.client.get(API + "/me").json()["can_control"])
        self.assertFalse(other.get(API + "/me").json()["can_control"])
        self.assertEqual(other.get(DEVICE + "/sessions").status_code, 200)

    def test_noninteractive_cli_uses_private_file_for_issued_secrets(self):
        command = [sys.executable, "-B", "admin.py", "owner-code", SENSOR]
        env = {**os.environ, "LAUNDRY_DB": str(self.path)}
        rejected = subprocess.run(command, env=env, capture_output=True, check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, b"")
        with store.transaction(self.path, write=False) as db:
            current = db.execute("SELECT owner_code FROM sensors WHERE sensor_id=?", (SENSOR,)).fetchone()
            self.assertEqual(current["owner_code"], store.digest(self.owner_code))
        output = self.path.with_name("issued.secrets")
        written = subprocess.run([*command, "--output", str(output)], env=env, capture_output=True, check=True)
        self.assertEqual(written.stdout, b"")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertIn("관리자 등록 코드", output.read_text())

    def test_test_push_limit_survives_reregistration_and_prioritizes_completion(self):
        self.register().raise_for_status()
        path = API + "/push/subscription"
        self.client.put(path, headers=CSRF, json=SUB).raise_for_status()
        self.client.patch(API + "/push/settings", headers=CSRF, json={"notifications_enabled": True}).raise_for_status()
        with patch("main.push.configured", return_value=True), patch("main.push.send", return_value=201) as send:
            self.start_cycle()
            self.sample_many(80, 5)
            self.client.post(API + "/push/test", headers=CSRF).raise_for_status()
            self.sample_many(80, 2)
            main.deliver_one(self.path, self.clock)
            self.assertEqual(send.call_args.args[1], "completed")
            for number in range(4):
                with TestClient(self.client.app, base_url=CSRF["Origin"], client=(f"192.0.2.{number + 50}", 5000)) as other:
                    self.register(other).raise_for_status()
                    other.put(path, headers=CSRF, json={**SUB, "endpoint": SUB["endpoint"] + str(number)}).raise_for_status()
                    other.post(API + "/push/test", headers=CSRF).raise_for_status()
            self.register().raise_for_status()
            self.client.put(path, headers=CSRF, json=SUB).raise_for_status()
            self.assertEqual(self.client.post(API + "/push/test", headers=CSRF).status_code, 429)

    def test_push_fanout_preferences_and_outbox_expiry(self):
        self.register()
        other = TestClient(self.client.app, base_url=CSRF["Origin"])
        silent = TestClient(self.client.app, base_url=CSRF["Origin"])
        for num, client in enumerate([self.client, other, silent]):
            if client is not self.client:
                self.register(client)
            subscription = {**SUB, "endpoint": SUB["endpoint"] + str(num)}
            self.assertEqual(client.put(API + "/push/subscription", headers=CSRF, json=subscription).status_code, 200)
        self.assertFalse(silent.get(API + "/me").json()["notifications_enabled"])
        for client in (self.client, other):
            settings = client.patch(API + "/push/settings", headers=CSRF, json={"notifications_enabled": True})
            self.assertEqual(settings.json(), {"notifications_enabled": True})
        with patch("main.push.configured", return_value=True), patch("main.push.send", side_effect=[503, 201, 201]) as send:
            self.start_cycle()
            self.sample_many(80, 7)
            self.assertEqual(self.outbox(), ["completed", "completed"])
            main.deliver_one(self.path, self.clock)
            self.now += 21
            main.deliver_one(self.path, self.clock)
            main.deliver_one(self.path, self.clock)
            self.assertEqual(send.call_args_list[0].args[2], send.call_args_list[2].args[2])
            self.assertEqual(self.outbox(), [])
            self.assertEqual(self.client.post(API + "/push/test", headers=CSRF).json(), {"queued": True})
        self.now += 600
        main.maintain(self.path, self.now)
        self.assertEqual(self.outbox(), [])

    def test_push_subscription_status_belongs_to_current_installation(self):
        path = API + "/push/subscription"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.client.get(path).json(), {"endpoint": None})
        self.assertEqual(self.client.put(path, headers=CSRF, json=SUB).status_code, 200)
        self.assertEqual(self.client.get(path).json(), {"endpoint": SUB["endpoint"]})

        other = TestClient(self.client.app, base_url=CSRF["Origin"])
        self.assertEqual(self.register(other).status_code, 201)
        self.assertEqual(other.get(path).json(), {"endpoint": None})
        self.assertEqual(self.client.delete(API + "/me", headers=CSRF).status_code, 204)
        self.assertEqual(self.register().status_code, 201)
        self.assertEqual(self.client.get(path).json(), {"endpoint": None})
        self.assertEqual(self.client.put(path, headers=CSRF, json=SUB).status_code, 200)

        with patch("main.push.configured", return_value=True), patch("main.push.send", return_value=410):
            self.assertEqual(self.client.post(API + "/push/test", headers=CSRF).json(), {"queued": True})
            main.deliver_one(self.path, self.clock)
        self.assertEqual(self.client.get(path).json(), {"endpoint": None})

    def test_recovery_supersedes_failed_offline_notification(self):
        self.register()
        self.client.patch(API + "/push/settings", headers=CSRF, json={"notifications_enabled": True})
        self.client.put(API + "/push/subscription", headers=CSRF, json=SUB)
        self.start_cycle()
        self.now += 30
        main.maintain(self.path, self.now)
        with patch("main.push.configured", return_value=True), patch("main.push.send", side_effect=[503, 201]) as send:
            main.deliver_one(self.path, self.clock)
            self.sample(1400)
            main.deliver_one(self.path, self.clock)
            self.now += 20
            main.deliver_one(self.path, self.clock)
            self.assertEqual([call.args[1] for call in send.call_args_list], ["offline", "recovered"])
            self.assertEqual(self.outbox(), [])

    def test_subscription_rotation_preserves_pending_completion(self):
        self.register()
        self.client.patch(API + "/push/settings", headers=CSRF, json={"notifications_enabled": True})
        path = API + "/push/subscription"
        self.client.put(path, headers=CSRF, json=SUB)
        self.start_cycle()
        self.sample_many(80, 7)
        replacement = {**SUB, "endpoint": SUB["endpoint"] + "-new"}

        def rotate_during_send(*args):
            self.assertEqual(self.client.put(path, headers=CSRF, json=replacement).status_code, 200)
            return 410

        with patch("main.push.configured", return_value=True), patch("main.push.send", side_effect=rotate_during_send) as send:
            main.deliver_one(self.path, self.clock)
            original = send.call_args.args
            self.assertEqual(self.outbox(), ["completed"])
            self.now += 20
            send.side_effect = None
            send.return_value = 201
            main.deliver_one(self.path, self.clock)
            self.assertEqual(send.call_args.args, (replacement, "completed", original[2], original[3] - 20, original[4]))
            self.assertEqual(self.outbox(), [])

    def test_disabling_and_disconnecting_clears_subscription_and_pending_notifications(self):
        self.register().raise_for_status()
        subscription = API + "/push/subscription"
        settings = API + "/push/settings"
        self.client.put(subscription, headers=CSRF, json=SUB).raise_for_status()
        self.client.patch(settings, headers=CSRF, json={"notifications_enabled": True}).raise_for_status()
        with patch("main.push.configured", return_value=True):
            self.client.post(API + "/push/test", headers=CSRF).raise_for_status()
        self.assertEqual(self.outbox(), ["test"])

        response = self.client.patch(settings, headers=CSRF, json={
            "notifications_enabled": False, "disconnect_subscription": True,
        })
        self.assertEqual(response.json(), {"notifications_enabled": False})
        self.assertEqual(self.client.get(subscription).json(), {"endpoint": None})
        self.assertEqual(self.outbox(), [])

        self.client.put(subscription, headers=CSRF, json=SUB).raise_for_status()
        self.client.patch(settings, headers=CSRF, json={"notifications_enabled": True}).raise_for_status()
        self.assertEqual(self.client.get(subscription).json(), {"endpoint": SUB["endpoint"]})
        self.assertTrue(self.client.get(API + "/me").json()["notifications_enabled"])

    def test_late_legacy_delete_does_not_remove_new_subscription(self):
        self.register().raise_for_status()
        subscription = API + "/push/subscription"
        settings = API + "/push/settings"
        self.client.patch(settings, headers=CSRF, json={"notifications_enabled": False}).raise_for_status()
        self.client.patch(settings, headers=CSRF, json={
            "notifications_enabled": True, "subscription": SUB,
        }).raise_for_status()
        response = self.client.delete(subscription, headers=CSRF)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "PUSH_STILL_ENABLED")
        self.assertEqual(self.client.get(subscription).json(), {"endpoint": SUB["endpoint"]})
        self.assertTrue(self.client.get(API + "/me").json()["notifications_enabled"])

    def test_push_settings_checks_auth_origin_and_endpoint_before_enabling(self):
        settings = API + "/push/settings"
        data = {"notifications_enabled": True, "subscription": SUB}
        self.assertEqual(self.client.patch(settings, headers=CSRF, json=data).status_code, 401)
        self.register().raise_for_status()
        self.assertEqual(self.client.patch(settings, json=data).status_code, 403)
        invalid = {**SUB, "endpoint": "http://127.0.0.1/"}
        response = self.client.patch(settings, headers=CSRF, json={
            "notifications_enabled": True, "subscription": invalid,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_ENDPOINT")
        self.assertFalse(self.client.get(API + "/me").json()["notifications_enabled"])

    def test_expired_session_is_rejected_even_when_cookie_is_still_sent(self):
        self.register().raise_for_status()
        self.assertEqual(self.client.get(API + "/me").status_code, 200)
        self.now += store.SESSION_TTL
        self.assertEqual(self.client.get(API + "/me").status_code, 401)
        main.maintain(self.path, self.now)
        with store.transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM phones").fetchone()[0], 0)

    def test_legacy_installation_keeps_subscription_during_expiry_migration(self):
        self.register().raise_for_status()
        path = API + "/push/subscription"
        self.client.put(path, headers=CSRF, json=SUB).raise_for_status()
        with store.transaction(self.path) as db:
            db.execute("DROP INDEX phones_expiry")
            db.execute("ALTER TABLE phones DROP COLUMN expires_at")
        store.initialize(self.path, self.now)
        self.assertEqual(self.client.get(path).json(), {"endpoint": SUB["endpoint"]})
        self.now += store.SESSION_TTL
        self.assertEqual(self.client.get(path).status_code, 401)

    def test_repeated_push_failure_stops_at_ten_minutes(self):
        self.register()
        path = API + "/push/subscription"
        self.assertEqual(self.client.put(path, headers=CSRF, json=SUB).status_code, 200)
        with patch("main.push.configured", return_value=True), patch("main.push.send", return_value=503) as send:
            self.assertEqual(self.client.post(API + "/push/test", headers=CSRF).json(), {"queued": True})
            queued_at = self.now
            attempts = [*range(0, 580, 20), 599]
            for elapsed in attempts:
                self.now = queued_at + elapsed
                main.deliver_one(self.path, self.clock)
            self.assertEqual(send.call_count, len(attempts))
            self.assertEqual(len({call.args[2] for call in send.call_args_list}), 1)
            self.assertEqual(send.call_args.args[3], 1)

            self.now = queued_at + 600
            main.deliver_one(self.path, self.clock)
            self.now += 20
            main.deliver_one(self.path, self.clock)
            self.assertEqual(self.outbox(), ["test"])
            main.maintain(self.path, self.now)
            main.deliver_one(self.path, self.clock)
            self.assertEqual(send.call_count, len(attempts))
            self.assertEqual(self.outbox(), [])


if __name__ == "__main__":
    unittest.main()
