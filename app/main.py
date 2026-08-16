"""FastAPI app: static page + evaluation endpoints."""
import asyncio
import json
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ACCESS_KEY, BASE_DIR, SESSION_TOKEN, config
from .gateway import evaluate_one, family_for, fetch_live_models, fill_prompt, parse_json, probe_model, provider_for
from .history import add_evaluation, add_followup, delete_evaluation, get_evaluation, init_db, list_evaluations

app = FastAPI(title="multi-ai-quiz")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
init_db()


@app.middleware("http")
async def auth_gate(request, call_next):
    path = request.url.path
    if path in ("/login", "/api/login") or path.startswith("/static/"):
        return await call_next(request)
    if request.cookies.get("quiz_session") == SESSION_TOKEN:
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "未登录"})
    return RedirectResponse("/login", status_code=302)


class LoginRequest(BaseModel):
    key: str


@app.post("/api/login")
async def login(body: LoginRequest):
    if body.key.strip() != ACCESS_KEY:
        raise HTTPException(status_code=401, detail="密钥错误")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("quiz_session", SESSION_TOKEN, httponly=True, samesite="lax", max_age=2592000)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("quiz_session")
    return resp


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "login.html")

_live_cache: dict = {"ts": 0.0, "models": None}
CACHE_TTL = 300.0


class EvaluateRequest(BaseModel):
    question: str
    answer_a: str
    answer_b: str
    models: list[str] | None = None
    prompt: str | None = None
    timeout: float | None = None  # 秒；None 时用默认 180s


async def all_models() -> list[dict]:
    """Merged model list: live discovery when available, else yaml. Cached 5 min."""
    now = time.monotonic()
    if _live_cache["models"] is not None and now - _live_cache["ts"] < CACHE_TTL:
        return _live_cache["models"]

    def from_yaml() -> list[dict]:
        return [
            {"id": m["id"], "name": m.get("name", m["id"]), "enabled": m.get("enabled", True), "api": m.get("api", family_for(m["id"])), "temperature": m.get("temperature"), "provider": m.get("provider", provider_for(m["id"])), "price": m.get("price")}
            for m in config.models
        ]

    live = await fetch_live_models()
    if live is None:
        models = from_yaml()
    else:
        by_id = {m["id"]: m for m in config.models}
        models = [
            {
                "id": item["id"],
                "name": by_id.get(item["id"], {}).get("name", item["name"]),
                "enabled": bool(by_id.get(item["id"], {}).get("enabled", False)),
                "api": by_id.get(item["id"], {}).get("api", family_for(item["id"])),
                "temperature": by_id.get(item["id"], {}).get("temperature"),
                "provider": by_id.get(item["id"], {}).get("provider", provider_for(item["id"])),
                "price": by_id.get(item["id"], {}).get("price"),
            }
            for item in live
        ]
    _live_cache["models"] = models
    _live_cache["ts"] = now
    return models


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/models")
async def models() -> list[dict]:
    return await all_models()


@app.get("/api/prompt")
async def prompt() -> dict:
    return {"template": config.default_prompt}


async def _validate(body: EvaluateRequest) -> tuple[str, list[str], dict[str, dict]]:
    question = body.question.strip()
    answer_a, answer_b = body.answer_a.strip(), body.answer_b.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")
    if not answer_a or not answer_b:
        raise HTTPException(status_code=400, detail="answer_a 和 answer_b 不能为空")
    all_ = await all_models()
    valid = {m["id"]: {"api": m["api"], "temperature": m.get("temperature")} for m in all_}
    model_ids = body.models or [m["id"] for m in all_ if m["enabled"]]
    unknown = set(model_ids) - set(valid)
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知模型: {sorted(unknown)}")
    if not model_ids:
        raise HTTPException(status_code=400, detail="未选择任何模型")
    if body.timeout is not None and not (1 <= body.timeout <= 3600):
        raise HTTPException(status_code=400, detail="timeout 需在 1-3600 秒之间")
    filled = fill_prompt(body.prompt or config.default_prompt, question, answer_a, answer_b)
    return filled, model_ids, valid


@app.post("/api/check")
async def check_models() -> dict:
    all_ = await all_models()
    results = await asyncio.gather(
        *(probe_model(m["id"], m["api"], m.get("temperature")) for m in all_)
    )
    return {"results": list(results)}


@app.post("/api/evaluate")
async def evaluate(body: EvaluateRequest):
    filled, model_ids, meta = await _validate(body)
    prompt_template = body.prompt or config.default_prompt
    question, answer_a, answer_b = body.question.strip(), body.answer_a.strip(), body.answer_b.strip()
    queue: asyncio.Queue = asyncio.Queue()

    async def worker(model_id: str) -> None:
        m = meta[model_id]
        await queue.put(await evaluate_one(model_id, filled, m["api"], m["temperature"], body.timeout))

    tasks = [asyncio.create_task(worker(m)) for m in model_ids]
    remaining = len(tasks)

    async def event_stream():
        nonlocal remaining
        saved: list[dict] = []
        try:
            while remaining:
                result = await queue.get()
                remaining -= 1
                saved.append(result)
                yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"
        finally:
            if saved:
                eval_id = None
                try:
                    eval_id = add_evaluation(question, answer_a, answer_b, prompt_template, model_ids, saved)
                except Exception as exc:
                    print(f"[history] 保存失败: {exc}", flush=True)
                if eval_id is not None:
                    # 客户端断开时 GeneratorExit 会使 finally 中的 yield 失败，忽略即可
                    try:
                        yield f"data: {json.dumps({'type': 'saved', 'eval_id': eval_id}, ensure_ascii=False)}\n\n"
                    except (GeneratorExit, RuntimeError):
                        pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/evaluate/one")
async def evaluate_one_model(body: EvaluateRequest):
    filled, model_ids, meta = await _validate(body)
    if len(model_ids) != 1:
        raise HTTPException(status_code=400, detail="该接口一次只接受一个模型")
    m = meta[model_ids[0]]
    return await evaluate_one(model_ids[0], filled, m["api"], m["temperature"], body.timeout)


class FollowupRequest(BaseModel):
    eval_id: int
    model_id: str
    followup: str
    timeout: float | None = None


@app.post("/api/followup")
async def followup(body: FollowupRequest):
    """追问：基于历史评审记录，对该模型的上一轮回答追加一次对话，并写回历史。"""
    question = body.followup.strip()
    if not question:
        raise HTTPException(status_code=400, detail="追问内容不能为空")
    record = get_evaluation(body.eval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="评审记录不存在")
    item = next((r for r in record["results"] if isinstance(r, dict) and r.get("model") == body.model_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="该评审中无此模型")
    filled = fill_prompt(record["prompt"], record["question"], record["answer_a"], record["answer_b"])
    prev = item.get("raw")
    prompt = f"{filled}\n\n你上一轮的回答：\n{prev}" if prev else filled
    prompt = f"{prompt}\n\n用户追问：\n{question}"
    resp = await evaluate_one(body.model_id, prompt, item.get("api"), None, body.timeout)
    if not resp.get("ok"):
        return {"ok": False, "error": resp.get("error", "调用失败")}
    result = parse_json(resp["raw"])
    add_followup(body.eval_id, body.model_id, question, resp["raw"], result)
    return {"ok": True, "answer": resp["raw"], "result": result}


@app.get("/api/history")
async def history_list(limit: int = 20, offset: int = 0, q: str = "") -> dict:
    items, total = list_evaluations(limit=limit, offset=offset, q=q)
    return {"items": items, "total": total}


@app.get("/api/history/{eval_id}")
async def history_detail(eval_id: int):
    record = get_evaluation(eval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return record


@app.delete("/api/history/{eval_id}")
async def history_delete(eval_id: int) -> dict:
    if not delete_evaluation(eval_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"ok": True}
