"""V2.0 功能回归测试（unittest + Flask test client）。

覆盖：V1.0 原有规则（注册登录、角色权限、发布活动、防重、满员、过期、越权）
以及 V2.0 新增规则（候补队列、FIFO 递补、取消报名、退出候补、管理员边界）。

运行：python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import app as app_module  # noqa: E402
from database import get_connection, init_db  # noqa: E402


def future_time(days=7):
    return (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")


def past_time(days=1):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


class BaseCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        init_db(self.db_path)

        app_module.app.config["DATABASE"] = self.db_path
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

        self.student_id = self._create_student("stu_alpha")
        self.student2_id = self._create_student("stu_beta")

    def tearDown(self):
        for _ in range(3):
            try:
                os.unlink(self.db_path)
                break
            except PermissionError:
                pass

    # ---------- helpers ----------
    def _create_student(self, username, password="pass1234"):
        self.client.post("/register", data={"username": username, "password": password})
        with get_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT id FROM User WHERE username = ?", (username,)
            ).fetchone()
        return row["id"]

    def login(self, username, password):
        return self.client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=True
        )

    def logout(self):
        self.client.get("/logout")

    def login_student(self, username="stu_alpha", password="pass1234"):
        self.logout()
        self.login(username, password)

    def login_teacher(self):
        self.logout()
        self.login("teacher", "teacher123")

    def login_admin(self):
        self.logout()
        self.login("admin", "admin123")

    def create_activity(self, capacity=1, end_time=None, title="测试活动"):
        self.login_teacher()
        self.client.post(
            "/teacher/activities",
            data={
                "title": title,
                "description": "用于测试",
                "max_participants": str(capacity),
                "end_time": end_time or future_time(),
            },
            follow_redirects=True,
        )
        with get_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT id FROM Activity ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["id"]

    def create_activity_directly(self, capacity=1, end_time=None, title="测试活动"):
        """绕过表单校验直接写入活动，用于构造已过期等表单不允许的状态。"""
        with get_connection(self.db_path) as connection:
            teacher_id = connection.execute(
                "SELECT id FROM User WHERE username = 'teacher'"
            ).fetchone()["id"]
            cursor = connection.execute(
                """
                INSERT INTO Activity
                    (title, description, organizer_id, max_participants, end_time, status)
                VALUES (?, '用于测试', ?, ?, ?, 'open')
                """,
                (title, teacher_id, capacity, end_time or future_time()),
            )
            return cursor.lastrowid

    def register_activity(self, activity_id):
        return self.client.post(
            f"/activities/{activity_id}/register", follow_redirects=True
        )

    def registration_rows(self, activity_id):
        with get_connection(self.db_path) as connection:
            return connection.execute(
                """
                SELECT r.student_id, r.status, r.queue_order, u.username
                FROM Registration r JOIN User u ON u.id = r.student_id
                WHERE r.activity_id = ?
                ORDER BY r.queue_order, r.id
                """,
                (activity_id,),
            ).fetchall()

    def status_of(self, activity_id, student_id):
        with get_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT status, queue_order FROM Registration WHERE activity_id = ? AND student_id = ?",
                (activity_id, student_id),
            ).fetchone()
        return (row["status"], row["queue_order"]) if row else (None, None)


class V1RegressionTests(BaseCase):
    """V1.0 原有业务规则必须保持不变。"""

    def test_teacher_can_publish_activity(self):
        activity_id = self.create_activity(capacity=5, title="迎新导览")
        with get_connection(self.db_path) as connection:
            row = connection.execute(
                "SELECT title, max_participants, status FROM Activity WHERE id = ?",
                (activity_id,),
            ).fetchone()
        self.assertEqual(row["title"], "迎新导览")
        self.assertEqual(row["max_participants"], 5)
        self.assertEqual(row["status"], "open")

    def test_student_can_register(self):
        activity_id = self.create_activity(capacity=5)
        self.login_student()
        response = self.register_activity(activity_id)
        self.assertIn("报名成功", response.get_data(as_text=True))
        self.assertEqual(self.status_of(activity_id, self.student_id), ("confirmed", 0))

    def test_duplicate_registration_rejected(self):
        activity_id = self.create_activity(capacity=5)
        self.login_student()
        self.register_activity(activity_id)
        response = self.register_activity(activity_id)
        self.assertIn("不能重复报名", response.get_data(as_text=True))
        self.assertEqual(len(self.registration_rows(activity_id)), 1)

    def test_activity_ended_cannot_register(self):
        activity_id = self.create_activity_directly(capacity=5, end_time=past_time())
        self.login_student()
        response = self.register_activity(activity_id)
        self.assertIn("活动已经结束", response.get_data(as_text=True))
        self.assertEqual(self.status_of(activity_id, self.student_id), (None, None))

    def test_activity_closed_cannot_register(self):
        activity_id = self.create_activity(capacity=5)
        self.login_teacher()
        self.client.post(f"/teacher/activities/{activity_id}/close", follow_redirects=True)
        self.login_student()
        response = self.register_activity(activity_id)
        self.assertIn("活动已经关闭", response.get_data(as_text=True))
        self.assertEqual(self.status_of(activity_id, self.student_id), (None, None))

    def test_student_cannot_access_teacher_page(self):
        activity_id = self.create_activity(capacity=5)
        self.login_student()
        response = self.client.get(
            f"/teacher/activities/{activity_id}/registrations", follow_redirects=True
        )
        self.assertIn("只有活动组织教师可以执行此操作", response.get_data(as_text=True))

    def test_teacher_cannot_register(self):
        activity_id = self.create_activity(capacity=5)
        self.login_teacher()
        response = self.register_activity(activity_id)
        self.assertIn("只有学生可以报名活动", response.get_data(as_text=True))

    def test_teacher_sees_only_own_activities(self):
        activity_id = self.create_activity(capacity=5)
        self.login_teacher()
        with get_connection(self.db_path) as connection:
            connection.execute(
                "INSERT INTO User (username, password, role) VALUES ('teacher2', 'x', 'teacher')"
            )
            other_id = connection.execute(
                "SELECT id FROM User WHERE username = 'teacher2'"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO Activity (title, description, organizer_id, max_participants, end_time, status)
                VALUES ('别人的活动', 'x', ?, 3, ?, 'open')
                """,
                (other_id, future_time()),
            )
        response = self.client.get("/teacher/activities")
        body = response.get_data(as_text=True)
        self.assertIn("测试活动", body)
        self.assertNotIn("别人的活动", body)


class V2WaitlistTests(BaseCase):
    """V2.0 新增的候补队列规则。"""

    def test_register_goes_to_waitlist_when_full(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)

        self.login_student("stu_beta")
        response = self.register_activity(activity_id)
        body = response.get_data(as_text=True)
        self.assertIn("进入候补队列", body)
        self.assertIn("第 1 位", body)
        self.assertEqual(self.status_of(activity_id, self.student2_id), ("waiting", 1))

    def test_waitlist_order_is_fifo(self):
        activity_id = self.create_activity(capacity=1)
        self._create_student("stu_gamma")

        self.login_student("stu_alpha")
        self.register_activity(activity_id)

        self.login_student("stu_beta")
        self.register_activity(activity_id)
        self.login_student("stu_gamma")
        self.register_activity(activity_id)

        rows = self.registration_rows(activity_id)
        self.assertEqual([r["username"] for r in rows], ["stu_alpha", "stu_beta", "stu_gamma"])
        self.assertEqual([r["status"] for r in rows], ["confirmed", "waiting", "waiting"])
        self.assertEqual([r["queue_order"] for r in rows], [0, 1, 2])

    def test_cancel_promotes_first_waiting(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        self.login_student("stu_beta")
        self.register_activity(activity_id)

        self.login_student("stu_alpha")
        response = self.client.post(
            f"/activities/{activity_id}/cancel", follow_redirects=True
        )
        body = response.get_data(as_text=True)
        self.assertIn("已取消报名", body)
        self.assertIn("候补队列首位学生已自动递补", body)

        self.assertEqual(self.status_of(activity_id, self.student_id), ("cancelled", 0))
        self.assertEqual(self.status_of(activity_id, self.student2_id), ("confirmed", 0))
        # 名额释放后确认人数回到 1，未超上限
        with get_connection(self.db_path) as connection:
            confirmed = connection.execute(
                "SELECT COUNT(*) AS c FROM Registration WHERE activity_id = ? AND status = 'confirmed'",
                (activity_id,),
            ).fetchone()["c"]
        self.assertEqual(confirmed, 1)

    def test_cancel_without_waitlist_just_frees_slot(self):
        activity_id = self.create_activity(capacity=2)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        response = self.client.post(f"/activities/{activity_id}/cancel", follow_redirects=True)
        self.assertIn("名额已释放", response.get_data(as_text=True))
        self.assertEqual(self.status_of(activity_id, self.student_id), ("cancelled", 0))

    def test_leave_waitlist_shifts_queue(self):
        activity_id = self.create_activity(capacity=1)
        self._create_student("stu_gamma")

        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        self.login_student("stu_beta")
        self.register_activity(activity_id)
        self.login_student("stu_gamma")
        self.register_activity(activity_id)

        # stu_beta 退出候补，stu_gamma 应从第 2 位前移到第 1 位
        self.login_student("stu_beta")
        response = self.client.post(
            f"/activities/{activity_id}/waitlist/leave", follow_redirects=True
        )
        self.assertIn("已退出候补队列", response.get_data(as_text=True))

        self.assertEqual(self.status_of(activity_id, self.student2_id), ("cancelled", 0))
        with get_connection(self.db_path) as connection:
            gamma = connection.execute(
                "SELECT id FROM User WHERE username = 'stu_gamma'"
            ).fetchone()["id"]
        self.assertEqual(self.status_of(activity_id, gamma), ("waiting", 1))

    def test_waiting_student_cannot_cancel_registration(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        self.login_student("stu_beta")
        self.register_activity(activity_id)

        response = self.client.post(
            f"/activities/{activity_id}/cancel", follow_redirects=True
        )
        self.assertIn("只有已确认参加的报名可以取消", response.get_data(as_text=True))

    def test_teacher_sees_confirmed_and_waiting_lists(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        self.login_student("stu_beta")
        self.register_activity(activity_id)

        self.login_teacher()
        body = self.client.get(
            f"/teacher/activities/{activity_id}/registrations"
        ).get_data(as_text=True)
        self.assertIn("已确认名单", body)
        self.assertIn("候补名单", body)
        self.assertIn("stu_alpha", body)
        self.assertIn("stu_beta", body)

    def test_my_registrations_shows_waitlist_position(self):
        activity_id = self.create_activity(capacity=1)
        self.login_student("stu_alpha")
        self.register_activity(activity_id)
        self.login_student("stu_beta")
        self.register_activity(activity_id)

        body = self.client.get("/my/registrations").get_data(as_text=True)
        self.assertIn("候补中", body)
        self.assertIn("第 1 位", body)


class V2AdminTests(BaseCase):
    """V2.0 管理员角色边界。"""

    def test_admin_can_view_platform_activities(self):
        self.create_activity(capacity=3, title="管理员可见活动")
        self.login_admin()
        body = self.client.get("/admin/activities").get_data(as_text=True)
        self.assertIn("管理员可见活动", body)

    def test_admin_can_view_users(self):
        self.login_admin()
        body = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn("stu_alpha", body)
        self.assertIn("系统管理员", body)

    def test_student_cannot_access_admin(self):
        self.login_student()
        response = self.client.get("/admin/activities", follow_redirects=True)
        self.assertIn("只有系统管理员可以访问该页面", response.get_data(as_text=True))

    def test_teacher_cannot_access_admin(self):
        self.login_teacher()
        response = self.client.get("/admin/users", follow_redirects=True)
        self.assertIn("只有系统管理员可以访问该页面", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
