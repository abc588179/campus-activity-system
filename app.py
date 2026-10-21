from pathlib import Path
import sqlite3
from datetime import datetime

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_connection, init_db


app = Flask(__name__)
app.config["DATABASE"] = Path(app.instance_path) / "campus_activity.db"
app.config["SECRET_KEY"] = "campus-activity-system-v2-development-key"
init_db(app.config["DATABASE"])

REGISTRATION_STATUS_LABELS = {
    "confirmed": "已确认参加",
    "waiting": "候补中",
    "cancelled": "已取消",
}


@app.get("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("用户名和密码不能为空。", "error")
            return render_template("register.html")

        try:
            with get_connection(app.config["DATABASE"]) as connection:
                connection.execute(
                    "INSERT INTO User (username, password, role) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), "student"),
                )
        except sqlite3.IntegrityError:
            flash("用户名已存在，请换一个用户名。", "error")
            return render_template("register.html")

        flash("学生注册成功，请登录。", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with get_connection(app.config["DATABASE"]) as connection:
            user = connection.execute(
                "SELECT id, username, password, role FROM User WHERE username = ?",
                (username,),
            ).fetchone()

        if user is None or not check_password_hash(user["password"], password):
            flash("用户名或密码错误。", "error")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        flash("登录成功。", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.get("/dashboard")
def dashboard():
    if "user_id" not in session:
        flash("请先登录。", "error")
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=session)


@app.get("/logout")
def logout():
    session.clear()
    flash("已退出登录。", "success")
    return redirect(url_for("index"))


def login_required():
    if "user_id" not in session:
        flash("请先登录。", "error")
        return redirect(url_for("login"))
    return None


def teacher_required():
    response = login_required()
    if response is not None:
        return response
    if session.get("role") != "teacher":
        flash("只有活动组织教师可以执行此操作。", "error")
        return redirect(url_for("activities"))
    return None


def student_required():
    response = login_required()
    if response is not None:
        return response
    if session.get("role") != "student":
        flash("只有学生可以报名活动。", "error")
        return redirect(url_for("activities"))
    return None


def admin_required():
    response = login_required()
    if response is not None:
        return response
    if session.get("role") != "admin":
        flash("只有系统管理员可以访问该页面。", "error")
        return redirect(url_for("dashboard"))
    return None


def activity_status(activity):
    if activity["status"] == "closed":
        return "已关闭"
    if datetime.fromisoformat(activity["end_time"]) <= datetime.now():
        return "已结束"
    return "进行中"


def activity_with_display_status(activity):
    activity = dict(activity)
    activity["display_status"] = activity_status(activity)
    return activity


def _renumber_waitlist(connection, activity_id):
    """把候补队列按现有先后顺序重新编号为 1..n，用于递补或退出后的位置前移。"""
    rows = connection.execute(
        """
        SELECT id FROM Registration
        WHERE activity_id = ? AND status = 'waiting'
        ORDER BY queue_order ASC, id ASC
        """,
        (activity_id,),
    ).fetchall()
    for position, row in enumerate(rows, start=1):
        connection.execute(
            "UPDATE Registration SET queue_order = ? WHERE id = ?",
            (position, row["id"]),
        )


def _promote_first_waiting(connection, activity_id):
    """把候补队列首位递补为已确认，并重排剩余候补顺序。返回递补成功的学生 id。"""
    candidate = connection.execute(
        """
        SELECT id, student_id FROM Registration
        WHERE activity_id = ? AND status = 'waiting'
        ORDER BY queue_order ASC, id ASC
        LIMIT 1
        """,
        (activity_id,),
    ).fetchone()
    if candidate is None:
        return None
    connection.execute(
        "UPDATE Registration SET status = 'confirmed', queue_order = 0 WHERE id = ?",
        (candidate["id"],),
    )
    _renumber_waitlist(connection, activity_id)
    return candidate["student_id"]


@app.route("/teacher/activities", methods=["GET", "POST"])
def teacher_activities():
    response = teacher_required()
    if response is not None:
        return response

    if request.method == "GET" and request.args.get("create") == "1":
        return render_template("teacher_activity_form.html")

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        max_participants = request.form.get("max_participants", "").strip()
        end_time = request.form.get("end_time", "").strip()

        if not title or not description or not max_participants or not end_time:
            flash("请完整填写活动信息。", "error")
            return render_template("teacher_activity_form.html")

        try:
            max_participants = int(max_participants)
            parsed_end_time = datetime.fromisoformat(end_time)
            if max_participants <= 0 or parsed_end_time <= datetime.now():
                raise ValueError
        except ValueError:
            flash("人数上限必须为正数，结束时间必须晚于当前时间。", "error")
            return render_template("teacher_activity_form.html")

        with get_connection(app.config["DATABASE"]) as connection:
            connection.execute(
                """
                INSERT INTO Activity
                    (title, description, organizer_id, max_participants, end_time, status)
                VALUES (?, ?, ?, ?, ?, 'open')
                """,
                (title, description, session["user_id"], max_participants, end_time),
            )
        flash("活动创建成功。", "success")
        return redirect(url_for("teacher_activities"))

    with get_connection(app.config["DATABASE"]) as connection:
        activities = connection.execute(
            """
            SELECT Activity.*, COUNT(
                CASE WHEN Registration.status = 'confirmed' THEN Registration.id END
            ) AS registration_count,
            COUNT(
                CASE WHEN Registration.status = 'waiting' THEN Registration.id END
            ) AS waiting_count
            FROM Activity
            LEFT JOIN Registration ON Registration.activity_id = Activity.id
            WHERE Activity.organizer_id = ?
            GROUP BY Activity.id
            ORDER BY Activity.id DESC
            """,
            (session["user_id"],),
        ).fetchall()
    activities = [activity_with_display_status(activity) for activity in activities]
    return render_template("teacher_activities.html", activities=activities)


@app.get("/teacher/activities/<int:activity_id>/registrations")
def activity_registrations(activity_id):
    response = teacher_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        activity = connection.execute(
            "SELECT id, title, max_participants FROM Activity WHERE id = ? AND organizer_id = ?",
            (activity_id, session["user_id"]),
        ).fetchone()
        if activity is None:
            flash("找不到属于当前教师的活动。", "error")
            return redirect(url_for("teacher_activities"))

        confirmed = connection.execute(
            """
            SELECT User.username AS student_username, Registration.registered_at
            FROM Registration
            JOIN User ON User.id = Registration.student_id
            WHERE Registration.activity_id = ? AND Registration.status = 'confirmed'
            ORDER BY Registration.registered_at ASC
            """,
            (activity_id,),
        ).fetchall()

        waiting = connection.execute(
            """
            SELECT User.username AS student_username, Registration.registered_at,
                   Registration.queue_order
            FROM Registration
            JOIN User ON User.id = Registration.student_id
            WHERE Registration.activity_id = ? AND Registration.status = 'waiting'
            ORDER BY Registration.queue_order ASC
            """,
            (activity_id,),
        ).fetchall()

    return render_template(
        "teacher_registrations.html",
        activity=activity,
        confirmed=confirmed,
        waiting=waiting,
    )


@app.route("/teacher/activities/<int:activity_id>/edit", methods=["GET", "POST"])
def edit_activity(activity_id):
    response = teacher_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        activity = connection.execute(
            "SELECT * FROM Activity WHERE id = ? AND organizer_id = ?",
            (activity_id, session["user_id"]),
        ).fetchone()
        if activity is None:
            flash("找不到属于当前教师的活动。", "error")
            return redirect(url_for("teacher_activities"))

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            max_participants = request.form.get("max_participants", "").strip()
            end_time = request.form.get("end_time", "").strip()
            try:
                max_participants = int(max_participants)
                parsed_end_time = datetime.fromisoformat(end_time)
                if not title or not description or max_participants <= 0 or parsed_end_time <= datetime.now():
                    raise ValueError
            except ValueError:
                flash("请填写有效信息，人数上限必须为正数且结束时间必须晚于当前时间。", "error")
                return render_template("teacher_activity_form.html", activity=activity)

            connection.execute(
                """
                UPDATE Activity
                SET title = ?, description = ?, max_participants = ?, end_time = ?
                WHERE id = ? AND organizer_id = ?
                """,
                (title, description, max_participants, end_time, activity_id, session["user_id"]),
            )
            flash("活动修改成功。", "success")
            return redirect(url_for("teacher_activities"))

    return render_template("teacher_activity_form.html", activity=activity)


@app.post("/teacher/activities/<int:activity_id>/close")
def close_activity(activity_id):
    response = teacher_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        cursor = connection.execute(
            "UPDATE Activity SET status = 'closed' WHERE id = ? AND organizer_id = ?",
            (activity_id, session["user_id"]),
        )
    if cursor.rowcount == 0:
        flash("找不到属于当前教师的活动。", "error")
    else:
        flash("活动已关闭。", "success")
    return redirect(url_for("teacher_activities"))


@app.get("/activities")
def activities():
    response = login_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        activity_rows = connection.execute(
            """
            SELECT Activity.*, User.username AS organizer_name, COUNT(
                CASE WHEN Registration.status = 'confirmed' THEN Registration.id END
            ) AS registration_count
            FROM Activity
            JOIN User ON User.id = Activity.organizer_id
            LEFT JOIN Registration ON Registration.activity_id = Activity.id
            GROUP BY Activity.id
            ORDER BY Activity.id DESC
            """
        ).fetchall()
    activity_rows = [activity_with_display_status(activity) for activity in activity_rows]
    return render_template("activities.html", activities=activity_rows)


@app.get("/activities/<int:activity_id>")
def activity_detail(activity_id):
    response = login_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        activity = connection.execute(
            """
            SELECT Activity.*, User.username AS organizer_name, COUNT(
                CASE WHEN Registration.status = 'confirmed' THEN Registration.id END
            ) AS registration_count
            FROM Activity
            JOIN User ON User.id = Activity.organizer_id
            LEFT JOIN Registration ON Registration.activity_id = Activity.id
            WHERE Activity.id = ?
            GROUP BY Activity.id
            """,
            (activity_id,),
        ).fetchone()

        if activity is None:
            flash("活动不存在。", "error")
            return redirect(url_for("activities"))

        registration_count = activity["registration_count"]
        waiting_count = connection.execute(
            """
            SELECT COUNT(*) AS waiting_count FROM Registration
            WHERE activity_id = ? AND status = 'waiting'
            """,
            (activity_id,),
        ).fetchone()["waiting_count"]

        my_registration = connection.execute(
            """
            SELECT status, queue_order FROM Registration
            WHERE student_id = ? AND activity_id = ?
            """,
            (session["user_id"], activity_id),
        ).fetchone()

    my_status = my_registration["status"] if my_registration is not None else None
    my_queue_order = my_registration["queue_order"] if my_registration is not None else 0
    return render_template(
        "activity_detail.html",
        activity=activity_with_display_status(activity),
        can_register=session.get("role") == "student",
        current_count=registration_count,
        waiting_count=waiting_count,
        my_status=my_status,
        my_queue_order=my_queue_order,
        my_status_label=REGISTRATION_STATUS_LABELS.get(my_status),
    )


@app.post("/activities/<int:activity_id>/register")
def register_activity(activity_id):
    response = student_required()
    if response is not None:
        return response

    now = datetime.now().isoformat(timespec="seconds")
    with get_connection(app.config["DATABASE"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        activity = connection.execute(
            "SELECT * FROM Activity WHERE id = ?",
            (activity_id,),
        ).fetchone()
        if activity is None:
            flash("活动不存在。", "error")
            return redirect(url_for("activities"))

        if activity["status"] == "closed":
            flash("活动已经关闭，不能报名。", "error")
            return redirect(url_for("activity_detail", activity_id=activity_id))
        if datetime.fromisoformat(activity["end_time"]) <= datetime.now():
            flash("活动已经结束，不能报名。", "error")
            return redirect(url_for("activity_detail", activity_id=activity_id))

        existing_registration = connection.execute(
            "SELECT id, status FROM Registration WHERE student_id = ? AND activity_id = ?",
            (session["user_id"], activity_id),
        ).fetchone()
        if existing_registration is not None and existing_registration["status"] != "cancelled":
            flash("你已经报名过这个活动，不能重复报名。", "error")
            return redirect(url_for("activity_detail", activity_id=activity_id))

        confirmed_count = connection.execute(
            """
            SELECT COUNT(*) AS confirmed_count FROM Registration
            WHERE activity_id = ? AND status = 'confirmed'
            """,
            (activity_id,),
        ).fetchone()["confirmed_count"]

        if confirmed_count < activity["max_participants"]:
            if existing_registration is not None:
                connection.execute(
                    """
                    UPDATE Registration
                    SET status = 'confirmed', queue_order = 0, registered_at = ?
                    WHERE id = ?
                    """,
                    (now, existing_registration["id"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO Registration (student_id, activity_id, status, queue_order, registered_at)
                    VALUES (?, ?, 'confirmed', 0, ?)
                    """,
                    (session["user_id"], activity_id, now),
                )
            flash("报名成功！", "success")
            return redirect(url_for("activity_detail", activity_id=activity_id))

        waiting_count = connection.execute(
            """
            SELECT COUNT(*) AS waiting_count FROM Registration
            WHERE activity_id = ? AND status = 'waiting'
            """,
            (activity_id,),
        ).fetchone()["waiting_count"]
        position = waiting_count + 1

        if existing_registration is not None:
            connection.execute(
                """
                UPDATE Registration
                SET status = 'waiting', queue_order = ?, registered_at = ?
                WHERE id = ?
                """,
                (position, now, existing_registration["id"]),
            )
        else:
            connection.execute(
                """
                INSERT INTO Registration (student_id, activity_id, status, queue_order, registered_at)
                VALUES (?, ?, 'waiting', ?, ?)
                """,
                (session["user_id"], activity_id, position, now),
            )
        flash(f"活动名额已满，你已进入候补队列，当前候补位置第 {position} 位。", "success")
        return redirect(url_for("activity_detail", activity_id=activity_id))


@app.post("/activities/<int:activity_id>/cancel")
def cancel_registration(activity_id):
    response = student_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        registration = connection.execute(
            "SELECT id, status FROM Registration WHERE student_id = ? AND activity_id = ?",
            (session["user_id"], activity_id),
        ).fetchone()
        if registration is None or registration["status"] != "confirmed":
            flash("只有已确认参加的报名可以取消。", "error")
            return redirect(url_for("activity_detail", activity_id=activity_id))

        connection.execute(
            "UPDATE Registration SET status = 'cancelled', queue_order = 0 WHERE id = ?",
            (registration["id"],),
        )
        promoted_student_id = _promote_first_waiting(connection, activity_id)

    if promoted_student_id is None:
        flash("已取消报名，名额已释放。", "success")
    else:
        flash("已取消报名，候补队列首位学生已自动递补为已确认。", "success")
    return redirect(url_for("activity_detail", activity_id=activity_id))


@app.post("/activities/<int:activity_id>/waitlist/leave")
def leave_waitlist(activity_id):
    response = student_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        connection.execute("BEGIN IMMEDIATE")
        registration = connection.execute(
            "SELECT id, status FROM Registration WHERE student_id = ? AND activity_id = ?",
            (session["user_id"], activity_id),
        ).fetchone()
        if registration is None or registration["status"] != "waiting":
            flash("你当前不在该活动的候补队列中。", "error")
            return redirect(url_for("activity_detail", activity_id=activity_id))

        connection.execute(
            "UPDATE Registration SET status = 'cancelled', queue_order = 0 WHERE id = ?",
            (registration["id"],),
        )
        _renumber_waitlist(connection, activity_id)

    flash("已退出候补队列，后续候补学生顺序已自动前移。", "success")
    return redirect(url_for("activity_detail", activity_id=activity_id))


@app.get("/my/registrations")
def my_registrations():
    response = login_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        rows = connection.execute(
            """
            SELECT Registration.status, Registration.queue_order, Registration.registered_at,
                   Activity.id AS activity_id, Activity.title, Activity.end_time,
                   Activity.max_participants
            FROM Registration
            JOIN Activity ON Activity.id = Registration.activity_id
            WHERE Registration.student_id = ? AND Registration.status != 'cancelled'
            ORDER BY Registration.registered_at DESC
            """,
            (session["user_id"],),
        ).fetchall()

    registrations = []
    for row in rows:
        item = dict(row)
        item["status_label"] = REGISTRATION_STATUS_LABELS.get(item["status"], item["status"])
        registrations.append(item)
    return render_template("my_registrations.html", registrations=registrations)


@app.get("/admin/activities")
def admin_activities():
    response = admin_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        rows = connection.execute(
            """
            SELECT Activity.*, User.username AS organizer_name,
                   COUNT(CASE WHEN Registration.status = 'confirmed' THEN Registration.id END)
                       AS registration_count,
                   COUNT(CASE WHEN Registration.status = 'waiting' THEN Registration.id END)
                       AS waiting_count
            FROM Activity
            JOIN User ON User.id = Activity.organizer_id
            LEFT JOIN Registration ON Registration.activity_id = Activity.id
            GROUP BY Activity.id
            ORDER BY Activity.id DESC
            """
        ).fetchall()
    activities = [activity_with_display_status(row) for row in rows]
    return render_template("admin_activities.html", activities=activities)


@app.get("/admin/users")
def admin_users():
    response = admin_required()
    if response is not None:
        return response

    with get_connection(app.config["DATABASE"]) as connection:
        rows = connection.execute(
            """
            SELECT User.id, User.username, User.role, User.account_status,
                   (SELECT COUNT(*) FROM Activity WHERE Activity.organizer_id = User.id)
                       AS activity_count,
                   (SELECT COUNT(*) FROM Registration
                    WHERE Registration.student_id = User.id
                      AND Registration.status IN ('confirmed', 'waiting'))
                       AS registration_count
            FROM User
            ORDER BY User.id ASC
            """
        ).fetchall()

    role_labels = {"student": "学生", "teacher": "活动组织教师", "admin": "系统管理员"}
    users = []
    for row in rows:
        item = dict(row)
        item["role_label"] = role_labels.get(item["role"], item["role"])
        users.append(item)
    return render_template("admin_users.html", users=users)


if __name__ == "__main__":
    app.run(debug=True)
