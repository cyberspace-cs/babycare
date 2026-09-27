"""FastAPI 应用（可选模式）。

若环境里装了 fastapi + uvicorn，serve.py 会优先用这个应用启动；
否则 serve.py 退回标准库实现，接口完全一致。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api

ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="babycare · 用餐分析 API",
    description="摄像头/照片 → 结构化标签 → 中国食物成分表 → 月龄标准表 → 缺口 → 备餐建议 / 安全网",
    version=api.VERSION,
)

# 允许 file:// 直接打开前端时也能取数（file:// 的 Origin 是 null）
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.get("/api/health")
def health(): return api.health()


@app.get("/api/meal/scenarios")
def list_scenarios(): return api.list_scenarios()


@app.get("/api/meal/day")
def day(scenario: str | None = Query(None), monthAge: int | None = Query(None)):
    return api.day(scenario, monthAge)


@app.get("/api/meal/safety")
def safety(scenario: str | None = Query(None), monthAge: int | None = Query(None)):
    return api.safety(scenario, monthAge)


@app.get("/api/meal/advice")
def advice(scenario: str | None = Query(None), monthAge: int | None = Query(None)):
    return api.advice(scenario, monthAge)


@app.get("/api/meal/food")
def food(q: str | None = Query(None)):
    return api.food_table(q)


@app.post("/api/meal/recognize")
def recognize(payload: dict = Body(...)):
    return api.recognize(payload)


@app.post("/api/meal/analyze")
def analyze(payload: dict = Body(...)):
    return api.analyze(payload)


# ---- 静态前端 ----

@app.get("/")
@app.get("/eat.html")
def eat_html():
    return FileResponse(ROOT / "eat.html")


app.mount("/assets", StaticFiles(directory=str(ROOT / "assets")), name="assets")
