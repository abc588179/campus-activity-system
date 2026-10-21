"""教师提供的模拟运行数据分析脚本（实验三）。

用法：
    python tools/analyze_data.py --data-dir <项目数据/data 目录>

输出直接打印到标准输出，已保存的原始输出见 docs/v3-analysis-output.txt。
"""

import argparse
from pathlib import Path

import pandas as pd

BUYOUT_THRESHOLD_MIN = 6  # 开放后 6 分钟内报满，视为「爆款」
PEAK_WINDOW_MIN = 20      # 以开放后 20 分钟作为峰值窗口


def load(data_dir: Path) -> dict:
    read = lambda name: pd.read_csv(data_dir / name, encoding="utf-8-sig")
    frames = {
        "activity": read("activity.csv"),
        "registration": read("registration_log.csv"),
        "waitlist": read("waitlist_log.csv"),
        "feedback": read("feedback.csv"),
        "system": read("system_log.csv"),
        "behavior": read("user_behavior.csv"),
    }
    frames["activity"]["registration_open"] = pd.to_datetime(
        frames["activity"]["registration_open"]
    )
    frames["registration"]["request_ts"] = pd.to_datetime(
        frames["registration"]["request_ts"]
    )
    return frames


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    f = load(data_dir)
    act, reg, wl, fb, syslog, beh = (
        f["activity"], f["registration"], f["waitlist"], f["feedback"], f["system"], f["behavior"]
    )

    section("0. 数据规模")
    for name, frame in [
        ("activity", act), ("registration_log", reg), ("waitlist_log", wl),
        ("feedback", fb), ("system_log", syslog), ("user_behavior", beh),
    ]:
        print(f"{name:20s} rows={len(frame):6d} cols={len(frame.columns)}")
    print("数据窗口:",
          reg.request_ts.min(), "→", reg.request_ts.max())

    section("1. 报名请求结果构成")
    counts = reg.result.value_counts()
    for key, value in counts.items():
        print(f"{key:20s} {value:6d}  {value/len(reg)*100:6.2f}%")
    print(f"{'TOTAL':20s} {len(reg):6d}")

    section("2. 各活动「开放 → 满额」耗时")
    last_acc = reg[reg.result == "accepted"].groupby("activity_id").request_ts.max()
    table = act.set_index("activity_id").join(last_acc.rename("last_accepted"))
    table["fill_minutes"] = (
        (table["last_accepted"] - table["registration_open"]).dt.total_seconds() / 60
    )
    table["waitlisted"] = (
        reg[reg.result == "waitlisted"].groupby("activity_id").size().reindex(table.index).fillna(0).astype(int)
    )
    table = table.sort_values("fill_minutes")
    print(table[["registration_open", "capacity", "fill_minutes", "waitlisted"]].to_string())
    hot = table[table.fill_minutes <= BUYOUT_THRESHOLD_MIN]
    rest = table[table.fill_minutes > BUYOUT_THRESHOLD_MIN]
    print(f"\n爆款活动数={len(hot)} / {len(table)} ({len(hot)/len(table)*100:.1f}%)，"
          f"耗时 {hot.fill_minutes.min():.1f}–{hot.fill_minutes.max():.1f} 分钟")
    print(f"其余活动耗时 {rest.fill_minutes.min()/60:.1f}–{rest.fill_minutes.max()/60:.1f} 小时")
    print(f"爆款活动候补合计 {int(hot.waitlisted.sum())} / {len(wl[wl.new_status=='waiting'])} "
          f"({hot.waitlisted.sum()/len(wl[wl.new_status=='waiting'])*100:.1f}%)")

    section("3. 峰值窗口 vs 平峰期（总体）")
    merged = reg.merge(act[["activity_id", "registration_open"]], on="activity_id")
    merged["after_open_min"] = (
        merged.request_ts - merged.registration_open
    ).dt.total_seconds() / 60
    merged["win"] = [
        f"0-{PEAK_WINDOW_MIN}min" if 0 <= x <= PEAK_WINDOW_MIN else f">{PEAK_WINDOW_MIN}min"
        for x in merged.after_open_min
    ]
    summary = merged.groupby("win").latency_ms.agg(
        n="count", p50="median", p90=lambda s: s.quantile(0.9),
        slow_pct=lambda s: round((s > 1300).mean() * 100, 1),
    )
    summary["share_pct"] = (summary.n / len(merged) * 100).round(1)
    print(summary.round(1).to_string())

    section("4. 控制结果类型后的耗时对比")
    pivot = merged.pivot_table(index="result", columns="win", values="latency_ms", aggfunc="median")
    pivot["ratio"] = (pivot.iloc[:, 0] / pivot.iloc[:, -1]).round(2)
    print(pivot.round(1).to_string())

    section("5. 各类结果在峰值窗口中的富集程度")
    baseline = ((merged.after_open_min >= 0) & (merged.after_open_min <= PEAK_WINDOW_MIN)).mean() * 100
    print(f"基准：全部请求落在 [0, {PEAK_WINDOW_MIN}] 分钟的比例 = {baseline:.1f}%\n")
    rows = []
    for result, group in merged.groupby("result"):
        inside = ((group.after_open_min >= 0) & (group.after_open_min <= PEAK_WINDOW_MIN)).sum()
        share = inside / len(group) * 100
        rows.append({
            "result": result, "n": len(group), "in_window": int(inside),
            "share_pct": round(share, 1), "enrichment": round(share / baseline, 2),
        })
    print(pd.DataFrame(rows).sort_values("enrichment", ascending=False).to_string(index=False))

    section("6. 候补队列去向")
    total_waiting = int((wl.new_status == "waiting").sum())
    promoted = int((wl.new_status == "accepted").sum())
    print(f"首次进入候补 {total_waiting} 条")
    print(f"递补成功     {promoted} 条（{promoted/total_waiting*100:.1f}%）")
    print(f"仍停留在候补 {total_waiting - promoted} 条（{(total_waiting-promoted)/total_waiting*100:.1f}%）")
    print(f"涉及学生 {wl[wl.new_status=='waiting'].student_id.nunique()} 人，"
          f"活动 {wl[wl.new_status=='waiting'].activity_id.nunique()} 个")
    cancels = int((reg.result == "cancelled").sum())
    print(f"取消报名 {cancels} 次；递补数 / 取消数 = {promoted / cancels:.2f}")
    stuck = wl[wl.new_status == "waiting"].copy()
    stuck["change_ts"] = pd.to_datetime(stuck["change_ts"])
    data_end = pd.to_datetime(wl.change_ts).max()
    waited = (data_end - stuck.change_ts).dt.total_seconds() / 86400
    print(f"数据窗口结束时间 {data_end}")
    print(f"仍未递补者的中位已等待时长 {waited.median():.2f} 天")

    section("7. 匿名反馈主题分布")
    themes = {
        "候选结果/报名状态不可知": ["还在候补", "隔了两天打开页面才发现", "担心错过候补结果",
                        "自己登录后才知道", "更容易发现自己报名结果的变化", "不会专门去页面查看"],
        "检索筛选费时、标题区分度低": ["查起来有点费时间", "标题看着差不多", "一条条打开看",
                          "逐个看", "想找周末的活动", "翻了几轮"],
        "正面/中性评价": ["组织得不错", "基本都能查到", "发布活动比较方便", "用起来还可以",
                    "使用很顺畅", "信息整体比较齐全"],
        "提交结果不确定、重复点击": ["弄清楚原因", "不清楚原因", "不确定到底有没有提交成功",
                        "连续点了两次", "页面卡住", "提示内容有点难理解"],
        "活动信息质量": ["地点说明", "时间和课程冲突", "海报里的文字"],
        "热门活动等待明显更长": ["等待时间明显更长"],
    }
    feedback = fb.copy()
    feedback["theme"] = None
    for name, keywords in themes.items():
        mask = feedback.feedback_text.apply(
            lambda t: any(k in t for k in keywords)
        ) & feedback.theme.isna()
        feedback.loc[mask, "theme"] = name
    distribution = feedback.theme.fillna("未归类").value_counts()
    for name, value in distribution.items():
        print(f"{name:26s} {value:4d} 条  {value/len(feedback)*100:5.1f}%")
    print(f"唯一原文种类 {feedback.feedback_text.nunique()} / {len(feedback)}")

    section("8. 数据质量提示（分析时必须说明的限制）")
    print("user_behavior 重复 event_id 行数:", len(beh) - beh.event_id.nunique())
    print("user_behavior 空 student_id:", int(beh.student_id.isna().sum()),
          "空 device:", int(beh.device.isna().sum()))
    print("system_log 中 500 错误:", int((syslog.event_code == "E500").sum()),
          "409 冲突:", int((syslog.event_code == "E409").sum()),
          "SLOW_REQUEST:", int((syslog.event_code == "SLOW_REQUEST").sum()),
          "静态资源重试:", int((syslog.event_code == "ASSET_RETRY").sum()))
    later = pd.to_datetime(fb.submit_ts) > reg.request_ts.max()
    print(f"feedback 中晚于报名日志窗口的记录: {int(later.sum())} 条")


if __name__ == "__main__":
    main()
