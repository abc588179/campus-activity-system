"""并发压测脚本：对比同一场景下不同版本 build 的报名处理表现。

用途：实验三 V3.0 的验证环节。对指定的项目目录启动 waitress（8 线程）服务，
预置 N 个学生与 1 个热门活动，然后让所有学生在同一瞬间提交报名，
记录每个请求的耗时与结果，并在结束后核对数据库中的最终状态。

不改动被测代码，也不依赖被测项目的测试设施。

用法：
    python tools/loadtest.py --app-dir <项目目录> --label V2.0 --out result_v2.json
    python tools/loadtest.py --app-dir <项目目录> --label V3.0 --out result_v3.json

注意：会删除 --app-dir 下 instance/campus_activity.db，请勿对正式数据目录运行。
"""

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from werkzeug.security import generate_password_hash

STUDENT_PASSWORD = "pass1234"
SERVER_THREADS = 8


def remove_with_retry(path, attempts=40, delay=0.25):
    """Windows 上刚被服务进程关闭的 SQLite 文件常有短暂占用，重试释放。"""
    for _ in range(attempts):
        try:
            if path.exists():
                path.unlink()
            return True
        except PermissionError:
            time.sleep(delay)
    return False


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_up(base_url, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(base_url + "/", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def seed(sqlite_path, student_count, capacity, title):
    """直接写库预置测试数据，避免通过 HTTP 反复注册带来的密码哈希开销。"""
    password_hash = generate_password_hash(STUDENT_PASSWORD)
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        teacher = connection.execute(
            "SELECT id FROM User WHERE username = 'teacher'"
        ).fetchone()
        if teacher is None:
            connection.execute(
                "INSERT INTO User (username, password, role) VALUES ('teacher', ?, 'teacher')",
                (password_hash,),
            )
            teacher_id = connection.execute(
                "SELECT id FROM User WHERE username = 'teacher'"
            ).fetchone()["id"]
        else:
            teacher_id = teacher["id"]

        students = [f"load_{i:03d}" for i in range(student_count)]
        student_rows = []
        for name in students:
            cursor = connection.execute(
                "INSERT INTO User (username, password, role) VALUES (?, ?, 'student')",
                (name, password_hash),
            )
            student_rows.append((cursor.lastrowid, name))

        end_time = "2027-12-31T23:59:00"
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(Activity)")}
        if "confirmed_count" in columns:
            cursor = connection.execute(
                """
                INSERT INTO Activity
                    (title, description, organizer_id, max_participants, end_time, status,
                     confirmed_count, waiting_count)
                VALUES (?, '压测用活动', ?, ?, ?, 'open', 0, 0)
                """,
                (title, teacher_id, capacity, end_time),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO Activity
                    (title, description, organizer_id, max_participants, end_time, status)
                VALUES (?, '压测用活动', ?, ?, ?, 'open')
                """,
                (title, teacher_id, capacity, end_time),
            )
        activity_id = cursor.lastrowid
        connection.commit()
        return student_rows, activity_id
    finally:
        connection.close()


def read_secret_key(app_dir):
    """从被测项目的 app.py 中读取 SECRET_KEY，用于直接构造会话 cookie。"""
    source = (app_dir / "app.py").read_text(encoding="utf-8")
    match = re.search(r'app\.config\["SECRET_KEY"\]\s*=\s*"([^"]+)"', source)
    if match is None:
        raise RuntimeError("未能从 app.py 中解析出 SECRET_KEY")
    return match.group(1)


def build_sessions(secret_key, students):
    """绕过登录页直接生成 Flask 会话 cookie。

    登录本身包含密码哈希校验，耗时远大于被测量的报名请求，
    放进并发压测会污染延迟统计，因此单独构造 cookie。
    """
    from flask import Flask
    from flask.sessions import SecureCookieSessionInterface

    app = Flask(__name__)
    app.secret_key = secret_key
    serializer = SecureCookieSessionInterface().get_signing_serializer(app)

    sessions = {}
    cookie_values = {}
    for student_id, name in students:
        cookie_value = serializer.dumps(
            {"user_id": student_id, "username": name, "role": "student"}
        )
        session = requests.Session()
        session.cookies.set("session", cookie_value, domain="127.0.0.1", path="/")
        sessions[name] = session
        cookie_values[name] = cookie_value
    return sessions, cookie_values


def burst(base_url, sessions, activity_id, mode, workers):
    """所有请求在同一时刻发出，记录每个请求的耗时与结果。"""
    names = list(sessions)
    barrier = threading.Barrier(len(names), timeout=120)
    results = []
    lock = threading.Lock()

    def one(name):
        session = sessions[name]
        token = f"{mode}-{name}"
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        start = time.perf_counter()
        try:
            response = session.post(
                f"{base_url}/activities/{activity_id}/register",
                data={"request_token": token},
                timeout=120,
                allow_redirects=False,
            )
            elapsed = (time.perf_counter() - start) * 1000
            body = ""
            if response.status_code != 302:
                body = response.text[:400]
            record = {
                "student": name,
                "status": response.status_code,
                "ms": elapsed,
                "error": None if response.status_code < 500 else body,
            }
        except Exception as exc:
            record = {
                "student": name,
                "status": 0,
                "ms": (time.perf_counter() - start) * 1000,
                "error": repr(exc),
            }
        with lock:
            results.append(record)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, names))
    return results


def duplicate_probe(base_url, cookie_value, activity_id, attempts):
    """同一学生并发提交同一个令牌，统计响应语义与落库行数。

    每个并发请求使用独立的 cookie 罐，避免 flash 消息在共享会话中互相覆盖。
    """
    barrier = threading.Barrier(attempts, timeout=60)
    results = []
    lock = threading.Lock()

    def one(index):
        session = requests.Session()
        session.cookies.set("session", cookie_value, domain="127.0.0.1", path="/")
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        start = time.perf_counter()
        try:
            response = session.post(
                f"{base_url}/activities/{activity_id}/register",
                data={"request_token": "dup-token-fixed"},
                timeout=120,
                allow_redirects=True,
            )
            text = response.text
            record = {
                "attempt": index,
                "status": response.status_code,
                "ms": (time.perf_counter() - start) * 1000,
                # V3.0 幂等分支的固定提示，V2.0 不存在该分支
                "idempotent_ack": "本次提交此前已处理" in text,
                "duplicate_rejected": "不能重复报名" in text,
                "registered": "报名成功" in text,
                "server_error": response.status_code >= 500,
            }
        except Exception as exc:
            record = {"attempt": index, "status": 0, "ms": 0.0, "error": repr(exc)}
        with lock:
            results.append(record)

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        list(pool.map(one, range(attempts)))
    return results


def verify(sqlite_path, activity_id):
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(Activity)")}
        row = connection.execute(
            "SELECT max_participants FROM Activity WHERE id = ?", (activity_id,)
        ).fetchone()
        capacity = row["max_participants"]
        confirmed = connection.execute(
            "SELECT COUNT(*) AS c FROM Registration WHERE activity_id = ? AND status = 'confirmed'",
            (activity_id,),
        ).fetchone()["c"]
        waiting = connection.execute(
            "SELECT COUNT(*) AS c FROM Registration WHERE activity_id = ? AND status = 'waiting'",
            (activity_id,),
        ).fetchone()["c"]
        orders = [
            r["queue_order"]
            for r in connection.execute(
                "SELECT queue_order FROM Registration WHERE activity_id = ? AND status = 'waiting' ORDER BY queue_order",
                (activity_id,),
            )
        ]
        result = {
            "capacity": capacity,
            "confirmed_rows": confirmed,
            "waiting_rows": waiting,
            "oversold": max(0, confirmed - capacity),
            "queue_order_is_1_to_n": orders == list(range(1, len(orders) + 1)),
            "has_counter_columns": "confirmed_count" in columns,
        }
        if "confirmed_count" in columns:
            counters = connection.execute(
                "SELECT confirmed_count, waiting_count FROM Activity WHERE id = ?",
                (activity_id,),
            ).fetchone()
            result["counter_confirmed"] = counters["confirmed_count"]
            result["counter_waiting"] = counters["waiting_count"]
            result["counter_matches_rows"] = (
                counters["confirmed_count"] == confirmed
                and counters["waiting_count"] == waiting
            )
        return result
    finally:
        connection.close()


def summarize(results, label):
    latencies = sorted(r["ms"] for r in results)
    errors = [r for r in results if r["status"] >= 500 or r["status"] == 0]
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    def pct(p):
        if not latencies:
            return 0.0
        index = min(len(latencies) - 1, int(round(p / 100 * (len(latencies) - 1))))
        return round(latencies[index], 1)

    return {
        "label": label,
        "requests": len(results),
        "status_counts": counts,
        "error_count": len(errors),
        "error_rate_pct": round(len(errors) / len(results) * 100, 2) if results else 0.0,
        "mean_ms": round(statistics.fmean(latencies), 1) if latencies else 0.0,
        "p50_ms": pct(50),
        "p90_ms": pct(90),
        "p99_ms": pct(99),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "errors_sample": [e["error"] for e in errors[:3]],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--label", default="build")
    parser.add_argument("--students", type=int, default=60)
    parser.add_argument("--capacity", type=int, default=10)
    parser.add_argument("--workers", type=int, default=60)
    parser.add_argument("--rounds", type=int, default=3, help="重复轮数，取多轮以降低噪声")
    parser.add_argument("--out", required=True)
    parser.add_argument("--title", default="压测活动")
    args = parser.parse_args()

    app_dir = Path(args.app_dir).resolve()
    sqlite_path = app_dir / "instance" / "campus_activity.db"
    secret_key = read_secret_key(app_dir)

    report = {"label": args.label, "app_dir": str(app_dir), "rounds": []}

    for round_index in range(1, args.rounds + 1):
        for suffix in ("", "-wal", "-shm", "-journal"):
            if not remove_with_retry(Path(str(sqlite_path) + suffix)):
                raise RuntimeError(f"无法删除旧的数据库文件：{sqlite_path}{suffix}")

        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        bootstrap = (
            "from waitress import serve\n"
            "from app import app\n"
            f"serve(app, host='127.0.0.1', port={port}, threads={SERVER_THREADS},"
            " connection_limit=1024, backlog=512,"
            " clear_untrusted_proxy_headers=True)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", bootstrap],
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if not wait_until_up(base_url):
                raise RuntimeError(f"{args.label} 服务未能在超时内启动（端口 {port}）")

            students, activity_id = seed(
                sqlite_path, args.students, args.capacity, f"{args.title}-{round_index}"
            )
            sessions, cookie_values = build_sessions(secret_key, students)

            results = burst(base_url, sessions, activity_id, "rush", args.workers)
            summary = summarize(results, args.label)
            summary["round"] = round_index
            summary["db"] = verify(sqlite_path, activity_id)

            # 幂等探测：同一学生用同一令牌并发重复提交
            probe_cookie = cookie_values[students[0][1]]
            probe_activity = seed(
                sqlite_path, 0, 999, f"幂等探测-{round_index}"
            )[1]
            dup = duplicate_probe(base_url, probe_cookie, probe_activity, attempts=8)
            dup_counts = {}
            for item in dup:
                dup_counts[item["status"]] = dup_counts.get(item["status"], 0) + 1
            connection = sqlite3.connect(sqlite_path)
            try:
                probe_rows = connection.execute(
                    "SELECT COUNT(*) FROM Registration WHERE activity_id = ?",
                    (probe_activity,),
                ).fetchone()[0]
            finally:
                connection.close()
            summary["idempotent_probe"] = {
                "attempts": len(dup),
                "status_counts": dup_counts,
                "rows_created": probe_rows,
                "idempotent_ack_count": sum(1 for d in dup if d.get("idempotent_ack")),
                "duplicate_rejected_count": sum(1 for d in dup if d.get("duplicate_rejected")),
                "registered_count": sum(1 for d in dup if d.get("registered")),
                "server_error_count": sum(1 for d in dup if d.get("server_error")),
            }

            report["rounds"].append(summary)
            print(f"[{args.label}] round {round_index}: "
                  f"p50={summary['p50_ms']}ms p90={summary['p90_ms']}ms "
                  f"errors={summary['error_count']} status={summary['status_counts']} "
                  f"db={summary['db']}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()

    rounds = report["rounds"]
    if rounds:
        report["aggregate"] = {
            "p50_ms": round(statistics.fmean(r["p50_ms"] for r in rounds), 1),
            "p90_ms": round(statistics.fmean(r["p90_ms"] for r in rounds), 1),
            "p99_ms": round(statistics.fmean(r["p99_ms"] for r in rounds), 1),
            "mean_ms": round(statistics.fmean(r["mean_ms"] for r in rounds), 1),
            "error_count": sum(r["error_count"] for r in rounds),
            "oversold": sum(r["db"]["oversold"] for r in rounds),
        }

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {args.out}")
    print(json.dumps(report.get("aggregate", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
