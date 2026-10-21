"""V3.0 新增能力测试：计数一致性、幂等提交、并发不超卖。

运行：python -m unittest tests.test_v3 -v
"""

import os
import sys
import threading
import unittest
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import app as app_module  # noqa: E402
from database import get_connection, init_db  # noqa: E402
from tests.test_functional import BaseCase  # noqa: E402


class CounterConsistencyMixin:
    def assert_counters_consistent(self, activity_id):
        """Activity 上的冗余计数必须与 Registration 实际行数一致。"""
        with get_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT confirmed_count, waiting_count FROM Activity WHERE id = ?",
                (activity_id,),
            ).fetchone()
            actual_confirmed = connection.execute(
                "SELECT COUNT(*) AS c FROM Registration WHERE activity_id = ? AND status = 'confirmed'",
                (activity_id,),
            ).fetchone()["c"]
            actual_waiting = connection.execute(
                "SELECT COUNT(*) AS c FROM Registration WHERE activity_id = ? AND status = 'waiting'",
                (activity_id,),
            ).fetchone()["c"]
        self.assertEqual(row["confirmed_count"], actual_confirmed)
        self.assertEqual(row["waiting_count"], actual_waiting)
        return row["confirmed_count"], row["waiting_count"]


class CountersStayConsistentTests(BaseCase, CounterConsistencyMixin):
    def test_counters_after_register_and_cancel(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        self.assertEqual(self.assert_counters_consistent(activity_id), (1, 0))

        self.login_student("stu_beta")
        self.register_activity(activity_id)
        self.assertEqual(self.assert_counters_consistent(activity_id), (1, 1))

        self.login_student("stu_alpha")
        self.client.post(f"/activities/{activity_id}/cancel", follow_redirects=True)
        self.assertEqual(self.assert_counters_consistent(activity_id), (1, 0))

    def test_counters_after_leaving_waitlist(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        self.login_student("stu_beta")
        self.register_activity(activity_id)
        self.client.post(f"/activities/{activity_id}/waitlist/leave", follow_redirects=True)
        self.assertEqual(self.assert_counters_consistent(activity_id), (1, 0))

    def test_confirmed_never_exceeds_capacity(self):
        activity_id = self.create_activity(capacity=2)
        for name in ("stu_alpha", "stu_beta"):
            self.login_student(name)
            self.register_activity(activity_id)
        confirmed, waiting = self.assert_counters_consistent(activity_id)
        self.assertEqual(confirmed, 2)
        self.assertEqual(waiting, 0)


class IdempotentSubmitTests(BaseCase, CounterConsistencyMixin):
    def test_same_token_twice_returns_first_result(self):
        activity_id = self.create_activity(capacity=5)
        self.login_student("stu_alpha")

        token = "token-abc-123"
        first = self.client.post(
            f"/activities/{activity_id}/register",
            data={"request_token": token},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("报名成功", first)

        second = self.client.post(
            f"/activities/{activity_id}/register",
            data={"request_token": token},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("本次提交此前已处理", second)
        self.assertIn("已确认参加", second)

        self.assertEqual(self.assert_counters_consistent(activity_id), (1, 0))
        with get_connection(self.db_path) as connection:
            rows = connection.execute(
                "SELECT COUNT(*) AS c FROM Registration WHERE activity_id = ?",
                (activity_id,),
            ).fetchone()["c"]
        self.assertEqual(rows, 1)

    def test_different_token_is_treated_as_new_submit(self):
        activity_id = self.create_activity(capacity=5)
        self.login_student("stu_alpha")
        self.client.post(
            f"/activities/{activity_id}/register",
            data={"request_token": "token-1"},
            follow_redirects=True,
        )
        body = self.client.post(
            f"/activities/{activity_id}/register",
            data={"request_token": "token-2"},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("不能重复报名", body)

    def test_waitlist_replay_reports_position(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)

        self.login_student("stu_beta")
        token = "token-wait-1"
        self.client.post(
            f"/activities/{activity_id}/register",
            data={"request_token": token},
            follow_redirects=True,
        )
        replay = self.client.post(
            f"/activities/{activity_id}/register",
            data={"request_token": token},
            follow_redirects=True,
        ).get_data(as_text=True)
        self.assertIn("本次提交此前已处理", replay)
        self.assertIn("候补队列第 1 位", replay)


class ConcurrentRegisterTests(BaseCase, CounterConsistencyMixin):
    """并发抢名额：确认人数不得超过容量，计数与实际行数必须一致。"""

    def test_parallel_registration_does_not_oversell(self):
        capacity = 5
        activity_id = self.create_activity(capacity=capacity)

        usernames = [f"bulk_{i:02d}" for i in range(20)]
        for name in usernames:
            self._create_student(name)

        barrier = threading.Barrier(len(usernames))
        errors = []

        def worker(username):
            client = app_module.app.test_client()
            client.post("/login", data={"username": username, "password": "pass1234"})
            try:
                barrier.wait(timeout=10)
                client.post(
                    f"/activities/{activity_id}/register",
                    data={"request_token": f"tok-{username}"},
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(name,)) for name in usernames]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        confirmed, waiting = self.assert_counters_consistent(activity_id)
        self.assertEqual(confirmed, capacity)
        self.assertEqual(waiting, len(usernames) - capacity)
        # 候补序号必须是 1..n 且不重复
        with get_connection(self.db_path) as connection:
            orders = [
                row["queue_order"]
                for row in connection.execute(
                    "SELECT queue_order FROM Registration WHERE activity_id = ? AND status = 'waiting' ORDER BY queue_order",
                    (activity_id,),
                )
            ]
        self.assertEqual(orders, list(range(1, len(usernames) - capacity + 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
