"""SQLite persistence for evaluation history."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .config import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  question TEXT NOT NULL,
  answer_a TEXT NOT NULL,
  answer_b TEXT NOT NULL,
  prompt TEXT NOT NULL,
  models TEXT NOT NULL,
  results TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_db()) as conn, conn:
        conn.execute(_SCHEMA)


def _winners_summary(results: list) -> dict:
    """Bucket per-model results into a/b/tie/fail/other counts."""
    summary = {"a": 0, "b": 0, "tie": 0, "fail": 0, "other": 0}
    for r in results:
        if not isinstance(r, dict):
            continue
        if not r.get("ok"):
            summary["fail"] += 1
            continue
        result = r.get("result")
        if isinstance(result, dict) and result.get("winner") in summary:
            summary[result["winner"]] += 1
        else:
            summary["other"] += 1
    return summary


def add_evaluation(question: str, answer_a: str, answer_b: str, prompt: str, models: list, results: list) -> int:
    with closing(get_db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO evaluations (question, answer_a, answer_b, prompt, models, results) VALUES (?, ?, ?, ?, ?, ?)",
            (
                question,
                answer_a,
                answer_b,
                prompt,
                json.dumps(models, ensure_ascii=False),
                json.dumps(results, ensure_ascii=False),
            ),
        )
        rowid = cur.lastrowid
        if rowid is None:
            raise RuntimeError("插入失败：未返回 lastrowid")
        return int(rowid)


def list_evaluations(limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    with closing(get_db()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0]
        rows = conn.execute(
            "SELECT id, created_at, question, models, results FROM evaluations ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        models = json.loads(row["models"])
        results = json.loads(row["results"])
        items.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "question": row["question"],
                "models": models,
                "winners": _winners_summary(results),
            }
        )
    return items, total


def get_evaluation(eval_id: int) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT id, created_at, question, answer_a, answer_b, prompt, models, results FROM evaluations WHERE id = ?",
            (eval_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "question": row["question"],
        "answer_a": row["answer_a"],
        "answer_b": row["answer_b"],
        "prompt": row["prompt"],
        "models": json.loads(row["models"]),
        "results": json.loads(row["results"]),
    }


def delete_evaluation(eval_id: int) -> bool:
    with closing(get_db()) as conn, conn:
        cur = conn.execute("DELETE FROM evaluations WHERE id = ?", (eval_id,))
        return cur.rowcount > 0


def add_followup(eval_id: int, model_id: str, question: str, answer: str, result: dict | None = None) -> bool:
    """Append a follow-up Q&A to one model's result. Returns False if the record/model is not found."""
    with closing(get_db()) as conn, conn:
        row = conn.execute("SELECT results FROM evaluations WHERE id = ?", (eval_id,)).fetchone()
        if row is None:
            return False
        results = json.loads(row["results"])
        for r in results:
            if isinstance(r, dict) and r.get("model") == model_id:
                followups = r.setdefault("followups", [])
                followups.append(
                    {
                        "question": question,
                        "answer": answer,
                        "result": result,
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                conn.execute(
                    "UPDATE evaluations SET results = ? WHERE id = ?",
                    (json.dumps(results, ensure_ascii=False), eval_id),
                )
                return True
        return False
