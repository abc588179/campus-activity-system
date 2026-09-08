from pathlib import Path

from flask import Flask, render_template

from database import init_db


app = Flask(__name__)
app.config["DATABASE"] = Path(app.instance_path) / "campus_activity.db"
init_db(app.config["DATABASE"])


@app.get("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True)
