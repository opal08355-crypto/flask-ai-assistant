import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file

from database import get_connection, init_db

load_dotenv()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
init_db()

DOMAINS = {
    "general": "Answer clearly and helpfully.",
    "informatics": "Focus on IT and programming. Explain technical terms clearly.",
    "finance": "Focus on financial concepts. Explain uncertainty and avoid personalized financial advice.",
    "data_analysis": "Focus on data analysis and statistics. Explain assumptions and limitations.",
    "other": "Adapt your answer to the user's subject.",
}


def get_client():
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("MISSING_GROQ_API_KEY")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OPENAI_SDK_MISSING") from exc
    return OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")


def friendly_api_error(error):
    if "MISSING_GROQ_API_KEY" in str(error):
        return "Groq API key is missing. Add GROQ_API_KEY to your .env file."
    if "OPENAI_SDK_MISSING" in str(error):
        return "The OpenAI client package is missing. Install project dependencies."
    status = getattr(error, "status_code", None)
    name = type(error).__name__.lower()
    if status in (401, 403) or "authentication" in name:
        return "Groq rejected the API key. Check that it is correct and active."
    if status == 429 or "ratelimit" in name:
        return "The Groq quota or rate limit was reached. Check your account limits."
    if "connection" in name or "timeout" in name:
        return "Could not connect to Groq. Check your internet connection."
    if status and status >= 500:
        return "Groq is temporarily unavailable. Please try again shortly."
    return "The assistant could not complete the request. Please try again."


def estimate_tokens(text):
    return max(1, (len(text) + 3) // 4) if text else 0


def create_pdf(title, text):
    """Create a dependency-free PDF with as many pages as the text needs."""
    text = str(text).encode("latin-1", "replace").decode("latin-1")
    lines = []
    for paragraph in text.splitlines() or [""]:
        lines.extend([paragraph[i:i + 92] for i in range(0, max(1, len(paragraph)), 92)])
    page_lines = [lines[i:i + 48] for i in range(0, len(lines), 48)] or [[]]
    def esc(value):
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    page_count = len(page_lines)
    font_id = 3 + 2 * page_count
    kids = " ".join(f"{3 + 2*i} 0 R" for i in range(page_count))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode()]
    for i, batch in enumerate(page_lines):
        page_id = 3 + 2 * i
        content_id = page_id + 1
        commands = ["BT", "/F1 16 Tf", "50 790 Td", f"({esc((title if i == 0 else title + ' (continued)')[:80])}) Tj", "/F1 10 Tf"]
        for line in batch:
            commands.extend(["0 -14 Td", f"({esc(line)}) Tj"])
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", "replace")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode())
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{idx} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(pdf)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/conversations")
def list_conversations():
    with get_connection() as db:
        rows = db.execute("SELECT id,title,domain,updated_at FROM conversations ORDER BY updated_at DESC,id DESC").fetchall()
    return jsonify(conversations=[dict(row) for row in rows])


@app.post("/api/conversations")
def create_conversation():
    data = request.get_json(silent=True) or {}
    domain = data.get("domain", "general")
    if domain not in DOMAINS:
        return jsonify(error="Choose a valid conversation domain."), 400
    with get_connection() as db:
        cur = db.execute("INSERT INTO conversations (domain) VALUES (?)", (domain,))
        db.commit()
        return jsonify(id=cur.lastrowid, title="New conversation", domain=domain), 201


@app.get("/api/conversations/<int:conversation_id>")
def get_conversation(conversation_id):
    with get_connection() as db:
        conv = db.execute("SELECT id,title,domain,created_at,updated_at FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not conv:
            return jsonify(error="Conversation not found."), 404
        rows = db.execute("SELECT id,role,content,created_at,response_ms,input_tokens,output_tokens,token_estimated,rating,sources_json FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
    messages = []
    for row in rows:
        item = dict(row)
        item["sources"] = json.loads(item.pop("sources_json") or "[]")
        messages.append(item)
    return jsonify(**dict(conv), messages=messages)


@app.delete("/api/conversations/<int:conversation_id>")
def delete_conversation(conversation_id):
    with get_connection() as db:
        cur = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        db.commit()
    if not cur.rowcount:
        return jsonify(error="Conversation not found."), 404
    return jsonify(ok=True)


@app.post("/prompt")
def prompt():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Send a valid JSON request."), 400
    messages = data.get("messages")
    domain = data.get("domain", "general")
    try:
        cid = int(data.get("conversation_id"))
    except (TypeError, ValueError):
        return jsonify(error="Start a conversation first."), 400
    if domain not in DOMAINS:
        return jsonify(error="Choose a valid conversation domain."), 400
    if not isinstance(messages, list) or not messages or any(not isinstance(m, str) or not m.strip() for m in messages):
        return jsonify(error="Messages must be a non-empty list of text."), 400
    question = messages[-1].strip()
    try:
        with get_connection() as db:
            conv = db.execute("SELECT id FROM conversations WHERE id=?", (cid,)).fetchone()
            if not conv:
                return jsonify(error="Conversation not found. Start a new one."), 404
            db.execute("UPDATE conversations SET domain=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (domain, cid))
            db.execute("INSERT INTO messages (conversation_id,role,content) VALUES (?,'user',?)", (cid, question))
            count = db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
            if count == 1:
                db.execute("UPDATE conversations SET title=? WHERE id=?", (question[:70], cid))
            history = db.execute("SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
            source_ids = data.get("source_ids", [])
            if not isinstance(source_ids, list):
                source_ids = []
            selected_sources = []
            for sid in source_ids[:20]:
                try:
                    source = db.execute("SELECT id,name,content FROM source_documents WHERE id=?", (int(sid),)).fetchone()
                except (TypeError, ValueError):
                    source = None
                if source:
                    selected_sources.append(dict(source))
    except sqlite3.Error:
        app.logger.exception("Could not save user message")
        return jsonify(error="Could not save this conversation to SQLite."), 500

    max_excerpts = max(1, min(10, int(data.get("excerpt_count", 3) or 3)))
    source_context, citations = [], []
    for src in selected_sources:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", src["content"]) if p.strip()]
        scored = sorted(enumerate(paragraphs, 1), key=lambda pair: sum(w.lower() in pair[1].lower() for w in re.findall(r"\w{4,}", question)), reverse=True)
        for n, passage in scored[:max_excerpts]:
            citations.append({"source_id": src["id"], "name": src["name"], "paragraph": n, "text": passage[:700]})
            source_context.append(f"[{src['name']} — paragraph {n}] {passage[:1200]}")
    system = DOMAINS[domain]
    if source_context:
        system += " Use the provided source excerpts and cite relevant sources by their [name — paragraph number]. If the sources do not contain an answer, say so.\n\n" + "\n\n".join(source_context)
    prompt_messages = [{"role": "system", "content": system}]
    prompt_messages.extend({"role": row["role"], "content": row["content"]} for row in history)
    input_text = "\n".join(m["content"] for m in prompt_messages)
    started = time.perf_counter()
    try:
        client = get_client()
        response = client.chat.completions.create(model="openai/gpt-oss-20b", messages=prompt_messages)
        answer = response.choices[0].message.content or ""
        if not answer.strip():
            answer = "The assistant returned an empty response. Please try again."
    except Exception as error:
        app.logger.exception("Groq request failed")
        return jsonify(error=friendly_api_error(error)), 502
    latency = round((time.perf_counter() - started) * 1000, 1)
    in_tokens = estimate_tokens(input_text)
    out_tokens = estimate_tokens(answer)
    usage = getattr(response, "usage", None)
    estimated = 1
    if usage:
        in_tokens = getattr(usage, "prompt_tokens", in_tokens)
        out_tokens = getattr(usage, "completion_tokens", out_tokens)
        estimated = 0
    with get_connection() as db:
        cur = db.execute("INSERT INTO messages (conversation_id,role,content,response_ms,input_tokens,output_tokens,token_estimated,sources_json) VALUES (?,'assistant',?,?,?,?,?,?)", (cid, answer, latency, in_tokens, out_tokens, estimated, json.dumps(citations)))
        db.execute("INSERT INTO usage_events (conversation_id,action,latency_ms,input_tokens,output_tokens,token_estimated) VALUES (?,'prompt',?,?,?,?)", (cid, latency, in_tokens, out_tokens, estimated))
        db.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (cid,))
        message_id = cur.lastrowid
        db.commit()
    return jsonify(reply=answer, message_id=message_id, metrics={"response_ms": latency, "input_tokens": in_tokens, "output_tokens": out_tokens, "estimated": bool(estimated)}, sources=citations)


@app.post("/api/messages/<int:message_id>/rating")
def rate_message(message_id):
    data = request.get_json(silent=True) or {}
    try:
        rating = int(data.get("rating"))
    except (TypeError, ValueError):
        return jsonify(error="Rating must be from 1 to 5."), 400
    if rating not in range(1, 6):
        return jsonify(error="Rating must be from 1 to 5."), 400
    with get_connection() as db:
        cur = db.execute("UPDATE messages SET rating=? WHERE id=? AND role='assistant'", (rating, message_id))
        db.commit()
    if not cur.rowcount:
        return jsonify(error="Assistant message not found."), 404
    return jsonify(ok=True, rating=rating)


@app.get("/api/conversations/<int:conversation_id>/metrics")
def conversation_metrics(conversation_id):
    with get_connection() as db:
        row = db.execute("SELECT COUNT(*) AS requests,COALESCE(SUM(latency_ms),0) AS total_ms,COALESCE(AVG(latency_ms),0) AS avg_ms,COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens,MAX(token_estimated) AS estimated FROM usage_events WHERE conversation_id=?", (conversation_id,)).fetchone()
    if not row:
        return jsonify(error="Conversation not found."), 404
    return jsonify(**dict(row))


@app.get("/api/conversations/<int:conversation_id>/export/<fmt>")
def export_conversation(conversation_id, fmt):
    if fmt not in ("txt", "json", "pdf"):
        return jsonify(error="Choose txt, json, or pdf."), 400
    conv = get_conversation_data(conversation_id)
    if conv is None:
        return jsonify(error="Conversation not found."), 404
    if fmt == "json":
        payload, mimetype, ext = json.dumps(conv, ensure_ascii=False, indent=2).encode("utf-8"), "application/json", "json"
    else:
        text = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in conv["messages"])
        if fmt == "txt":
            payload, mimetype, ext = text.encode("utf-8"), "text/plain; charset=utf-8", "txt"
        else:
            payload, mimetype, ext = create_pdf(conv["title"], text), "application/pdf", "pdf"
    return send_file(io.BytesIO(payload), mimetype=mimetype, as_attachment=True, download_name=f"conversation-{conversation_id}.{ext}")


def get_conversation_data(cid):
    with get_connection() as db:
        conv = db.execute("SELECT id,title,domain,created_at,updated_at FROM conversations WHERE id=?", (cid,)).fetchone()
        if not conv:
            return None
        rows = db.execute("SELECT id,role,content,created_at,response_ms,input_tokens,output_tokens,token_estimated,rating,sources_json FROM messages WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
    data = dict(conv)
    data["messages"] = []
    for row in rows:
        item = dict(row)
        item["sources"] = json.loads(item.pop("sources_json") or "[]")
        data["messages"].append(item)
    return data


@app.post("/api/conversations/<int:conversation_id>/summary.pdf")
def summary_pdf(conversation_id):
    conv = get_conversation_data(conversation_id)
    if conv is None:
        return jsonify(error="Conversation not found."), 404
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in conv["messages"])
    try:
        response = get_client().chat.completions.create(model="openai/gpt-oss-20b", messages=[{"role":"system","content":"Write a concise, accurate summary of this conversation. Include key decisions and open questions."},{"role":"user","content":transcript}])
        summary = response.choices[0].message.content or "No summary was returned."
    except Exception as error:
        return jsonify(error=friendly_api_error(error)), 502
    return send_file(io.BytesIO(create_pdf("Conversation summary", summary)), mimetype="application/pdf", as_attachment=True, download_name=f"conversation-{conversation_id}-summary.pdf")


@app.get("/api/sources")
def list_sources():
    with get_connection() as db:
        rows = db.execute("SELECT id,name,created_at FROM source_documents ORDER BY id DESC").fetchall()
    return jsonify(sources=[dict(row) for row in rows])


@app.post("/api/sources")
def add_source():
    name = (request.form.get("name") or "").strip()
    content = (request.form.get("content") or "").strip()
    upload = request.files.get("file")
    if upload and upload.filename:
        name = Path(upload.filename).name
        try:
            content = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify(error="Upload a UTF-8 text file."), 400
    if not name or not content:
        return jsonify(error="Provide a source name and its text, or upload a text file."), 400
    with get_connection() as db:
        cur = db.execute("INSERT INTO source_documents (name,content) VALUES (?,?)", (name[:200], content[:1_000_000]))
        db.commit()
    return jsonify(id=cur.lastrowid, name=name), 201


@app.delete("/api/sources/<int:source_id>")
def delete_source(source_id):
    with get_connection() as db:
        cur = db.execute("DELETE FROM source_documents WHERE id=?", (source_id,))
        db.commit()
    return (jsonify(ok=True), 200) if cur.rowcount else (jsonify(error="Source not found."), 404)


def analyze_rows(filename, columns, rows):
    total = len(rows)
    missing = {col: sum(1 for row in rows if row.get(col) is None or str(row.get(col)).strip() == "") for col in columns}
    duplicate_count = total - len({json.dumps(row, sort_keys=True, default=str) for row in rows})
    inconsistent = {}
    anomalies = {}
    for col in columns:
        vals = [str(r[col]).strip() for r in rows if r.get(col) is not None and str(r.get(col)).strip()]
        types = {"numeric" if re.fullmatch(r"[-+]?\d+(\.\d+)?", v) else "text" for v in vals}
        if len(types) > 1:
            inconsistent[col] = sorted(types)
        nums = []
        for v in vals:
            try: nums.append(float(v))
            except ValueError: pass
        if len(nums) >= 4:
            mean = sum(nums) / len(nums)
            variance = sum((n - mean) ** 2 for n in nums) / len(nums)
            sd = variance ** .5
            outliers = [n for n in nums if sd and abs(n - mean) > 3 * sd]
            if outliers: anomalies[col] = outliers[:20]
    cells = max(1, total * max(1, len(columns)))
    missing_count = sum(missing.values())
    score = round(100 * (1 - (missing_count + duplicate_count) / cells), 1)
    return {"filename": filename, "rows": total, "columns": columns, "missing_by_column": missing, "missing_cells": missing_count, "duplicate_rows": duplicate_count, "mixed_type_columns": inconsistent, "numeric_outliers_over_3sd": anomalies, "completeness_percent": round(100 * (cells - missing_count) / cells, 1), "quality_score_estimate": max(0, min(100, score)), "notes": ["Quality score is a simple estimate; review domain rules and source data before relying on it."]}


@app.post("/api/data/analyze")
def analyze_data():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="Choose a CSV or SQLite database file."), 400
    filename = Path(upload.filename).name
    suffix = Path(filename).suffix.lower()
    try:
        raw = upload.read()
        if suffix == ".csv":
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
            rows = list(reader)
            columns = reader.fieldnames or []
            reports = [analyze_rows(filename, columns, rows)]
        elif suffix in (".sqlite", ".sqlite3", ".db"):
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(raw)
                temp_path = tmp.name
            try:
                source_db = sqlite3.connect(f"file:{temp_path}?mode=ro", uri=True)
                tables = source_db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                reports = []
                for (table,) in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    cursor = source_db.execute(f"SELECT * FROM {quoted} LIMIT 10001")
                    columns = [d[0] for d in cursor.description or []]
                    rows = [dict(zip(columns, row)) for row in cursor.fetchmany(10000)]
                    reports.append(analyze_rows(f"{filename}:{table}", columns, rows))
                source_db.close()
            finally:
                os.unlink(temp_path)
        else:
            return jsonify(error="Supported formats: CSV, .sqlite, .sqlite3, or .db."), 400
    except Exception as error:
        app.logger.exception("Data analysis failed")
        return jsonify(error=f"Could not analyze this file: {error}"), 400
    report = {"file": filename, "tables": reports}
    with get_connection() as db:
        db.execute("INSERT INTO data_reports (filename,report_json) VALUES (?,?)", (filename, json.dumps(report, default=str)))
        db.commit()
    return jsonify(report=report)


@app.get("/api/data/reports")
def data_reports():
    with get_connection() as db:
        rows = db.execute("SELECT id,filename,report_json,created_at FROM data_reports ORDER BY id DESC LIMIT 20").fetchall()
    return jsonify(reports=[{**dict(row), "report": json.loads(row["report_json"])} for row in rows])


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="File is too large. The maximum upload size is 20 MB."), 413


if __name__ == "__main__":
    app.run(debug=True)
