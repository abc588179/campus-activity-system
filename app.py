from pathlib import Path
import sqlite3
from datetime import datetime

from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_connection, init_db


app = Flask(__name__)
app.config["DATABASE"] = Path(app.instance_path) / "campus_activity.db"
app.config["SECRET_KEY"] = "campus-activity-system-v1-development-key"
init_db(app.config["DATABASE"])


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
                CASE WHEN Registration.status = 'active' THEN Registration.id END
            ) AS registration_count
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
                CASE WHEN Registration.status = 'active' THEN Registration.id END
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
                CASE WHEN Registration.status = 'active' THEN Registration.id END
            ) AS registration_count
            FROM Activity
            JOIN User ON User.id = Activity.organizer_id
            LEFT JOIN Registration ON Registration.activity_id = Activity.id
            WHERE Activity.id = ?
            GROUP BY Activity.id
            """,
            (activity_id,),
        ).fetchone()
        current_count = activity["registration_count"] if activity is not None else 0
        is_registered = False
        if activity is not None:
            is_registered = connection.execute(
                """
                SELECT 1
                FROM Registration
                WHERE student_id = ? AND activity_id = ? AND status = 'active'
                LIMIT 1
                """,
                (session["user_id"], activity_id),
            ).fetchone() is not None
    if activity is None:
        flash("活动不存在。", "error")
        return redirect(url_for("activities"))
    return render_template(
        "activity_detail.html",
        activity=activity_with_display_status(activity),
        can_register=session.get("role") == "student",
        current_count=current_count,
        is_registered=is_registered,
    )


@app.post("/activities/<int:activity_id>/register")
def register_activity(activity_id):
    response = student_required()
    if response is not None:
        return response

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
            "SELECT id FROM Registration WHERE student_id = ? AND activity_id = ?",
            (session["user_id"], activity_id),
        ).fetchone()
        if existing_registration is not None:
            flash("你已经报名过这个活动，不能重复报名。", "error")
            return redirect(url_for("activity_detail", activity_id=activity_id))

        registration_count = connection.execute(
            """
            SELECT COUNT(*) AS registration_count
            FROM Registration
            WHERE activity_id = ? AND status = 'active'
            """,
            (activity_id,),
        ).fetchone()["registration_count"]
        if registration_count >= activity["max_participants"]:
            flash("活动报名人数已达到上限，不能继续报名。", "error")
            return redirect(url_for("activity_detail", activity_id=activity_id))

        connection.execute(
            """
            INSERT INTO Registration (student_id, activity_id, status, registered_at)
            VALUES (?, ?, 'active', ?)
            """,
            (session["user_id"], activity_id, datetime.now().isoformat(timespec="seconds")),
        )

    flash("报名成功！", "success")
    return redirect(url_for("activity_detail", activity_id=activity_id))


if __name__ == "__main__":
    app.run(debug=True)
