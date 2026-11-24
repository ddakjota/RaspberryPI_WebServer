import argparse
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date
from pathlib import Path

import store


def provision(path, action, sensor_id, now=None):
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    code = secrets.token_urlsafe(18)
    with store.transaction(path) as db:
        existing = db.execute("SELECT 1 FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone()
        if action == "add":
            if existing:
                raise ValueError("이미 등록된 센서입니다.")
            owner_code = secrets.token_urlsafe(18)
            db.execute("""INSERT INTO sensors(sensor_id,token,code,code_expires,owner_code,owner_code_expires)
                VALUES (?,?,?,?,?,?)""", (sensor_id, store.digest(token), store.digest(code), now + store.SHARE_TTL,
                                          store.digest(owner_code), now + store.OWNER_CODE_TTL))
            return {"센서 ID": sensor_id, "센서 토큰": token, "등록 코드": code, "관리자 등록 코드": owner_code}
        if not existing:
            raise ValueError("센서를 찾을 수 없습니다.")
        if action == "rotate-code":
            db.execute("""UPDATE sensors SET code=?, code_expires=?, owner_code=NULL, owner_code_expires=NULL
                WHERE sensor_id=?""", (store.digest(code), now + store.SHARE_TTL, sensor_id))
            db.execute("DELETE FROM phones WHERE sensor_id=?", (sensor_id,))
            return {"등록 코드": code}
        if action == "share-code":
            db.execute("UPDATE sensors SET code=?, code_expires=? WHERE sensor_id=?",
                       (store.digest(code), now + store.SHARE_TTL, sensor_id))
            return {"등록 코드": code}
        if action == "owner-code":
            db.execute("UPDATE sensors SET owner_code=?, owner_code_expires=? WHERE sensor_id=?",
                       (store.digest(code), now + store.OWNER_CODE_TTL, sensor_id))
            return {"관리자 등록 코드": code}
        if action != "rotate-token":
            raise ValueError("지원되지 않는 센서 명령입니다.")
        db.execute("UPDATE sensors SET token=? WHERE sensor_id=?", (store.digest(token), sensor_id))
        return {"센서 토큰": token}


def revoke_device(path, sensor_id, installation_id):
    with store.transaction(path) as db:
        if not db.execute("DELETE FROM phones WHERE sensor_id=? AND installation_id=?",
                          (sensor_id, installation_id)).rowcount:
            raise ValueError("등록된 기기를 찾을 수 없습니다.")


def backup(source, destination):
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = destination / f"laundry-{date.today().isoformat()}.db"
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}")
    try:
        temporary.touch(mode=0o600, exist_ok=False)
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as db:
            with closing(sqlite3.connect(temporary)) as copy:
                db.backup(copy)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    snapshots = sorted(destination.glob("laundry-????-??-??.db"), reverse=True)
    for old in snapshots[7:]:
        old.unlink()
    return target


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="세탁 알리미 센서 및 SQLite 백업 관리")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("add", "rotate-code", "rotate-token", "share-code", "owner-code"):
        command = sub.add_parser(action)
        command.add_argument("sensor_id")
        command.add_argument("--output", type=Path)
    sub.add_parser("list-devices").add_argument("sensor_id")
    revoke = sub.add_parser("revoke-device")
    revoke.add_argument("sensor_id")
    revoke.add_argument("installation_id")
    sub.add_parser("backup").add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.action == "backup":
        print(backup(store.DB, args.directory))
        return
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.sensor_id):
        parser.error("센서 ID는 영문, 숫자, 하이픈, 밑줄만 사용할 수 있습니다.")
    store.initialize(store.DB)
    try:
        if args.action == "list-devices":
            with store.transaction(store.DB, write=False) as db:
                for row in db.execute("""SELECT installation_id, controller, expires_at FROM phones
                        WHERE sensor_id=? AND expires_at>?""", (args.sensor_id, time.time())):
                    role = "관리자" if row["controller"] else "일반"
                    print(row["installation_id"], role, store.timestamp(row["expires_at"]))
        elif args.action == "revoke-device":
            revoke_device(store.DB, args.sensor_id, args.installation_id)
            print("기기 연결을 해제했습니다.")
        elif args.output is not None:
            with args.output.open("x", encoding="utf-8") as output:
                for key, value in provision(store.DB, args.action, args.sensor_id).items():
                    print(f"{key}: {value}", file=output)
        elif sys.stdout.isatty():
            for key, value in provision(store.DB, args.action, args.sensor_id).items():
                print(f"{key}: {value}")
        else:
            parser.error("비대화형 실행에서는 --output으로 새 비밀 파일의 경로를 지정하세요.")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
