from pathlib import Path
import sqlite3

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


if __name__ == "__main__":
    app.run(debug=True)
