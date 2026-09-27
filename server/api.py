"""API 业务逻辑（与 Web 框架无关）。

这里只做「入参 → 出参」的纯函数，不依赖 FastAPI。
server/app.py（FastAPI 模式）和根目录 serve.py（标准库模式）都调用同一份逻辑，
保证两种启动方式返回完全一致的结果。
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from . import pipeline, scenarios

VERSION = "1.0.0"


def _scenario(sid: str | None) -> dict:
    return scenarios.SCENARIOS.get(sid or scenarios.DEFAULT_SCENARIO,
                                   scenarios.SCENARIOS[scenarios.DEFAULT_SCENARIO])


def _analyze(sid: str | None, month_age: int | None) -> dict:
    sc = _scenario(sid)
    month = int(month_age) if month_age else sc["baby"]["monthAge"]
    result = pipeline.analyze_day(sc["meals"], month_age=month,
                                 tried_before=sc.get("triedBefore", ()))
    result["scenario"] = {"id": sc["id"], "title": sc["title"],
                          "expect": sc["expect"], "baby": sc["baby"]}
    result["baby"] = {**sc["baby"], "monthAge": month}
    return result


# ------------------------------------------------------------------ 各接口

def health() -> dict:
    return {
        "ok": True, "version": VERSION,
        "foodTable": {"source": pipeline.FOOD_TABLE["_source"],
                      "items": len(pipeline.FOODS),
                      "note": pipeline.FOOD_TABLE["_note"]},
        "ageStandard": {"source": pipeline.AGE_STANDARD["_source"],
                        "bands": [b["label"] for b in pipeline.AGE_STANDARD["bands"]]},
        "vision": {"provider": "qwen-vl (可选)",
                   "configured": bool(__import__("os").getenv("QWEN_VL_API_KEY"))},
    }


def list_scenarios() -> dict:
    return {"default": scenarios.DEFAULT_SCENARIO,
            "items": [{"id": s["id"], "title": s["title"], "expect": s["expect"],
                       "monthAge": s["baby"]["monthAge"], "meals": len(s["meals"])}
                      for s in scenarios.SCENARIOS.values()]}


def day(sid: str | None = None, month_age: int | None = None) -> dict:
    """今日完整分析：五项达标率 + 备餐建议（正向）+ 安全网（负向）+ 新食物观察期。"""
    return _analyze(sid, month_age)


def safety(sid: str | None = None, month_age: int | None = None) -> dict:
    result = _analyze(sid, month_age)
    return {"scenario": result["scenario"], "alerts": result["safety"],
            "intake": result["report"]["intake"],
            "sodiumLimit": result["report"]["sodiumLimit"]}


def advice(sid: str | None = None, month_age: int | None = None) -> dict:
    result = _analyze(sid, month_age)
    return {"scenario": result["scenario"], "advice": result["advice"]}


def food_table(q: str | None = None) -> dict:
    """查成分表。带 q 时按别名归一后返回单条，附带该食材的溯源信息。"""
    if not q:
        return {"source": pipeline.FOOD_TABLE["_source"], "items": pipeline.FOODS}
    hit = pipeline.lookup(q)
    if not hit:
        return {"query": q, "found": False,
                "hint": "成分表中没有该食材；Pipeline 不会估算，会把它计入 unresolved。"}
    return {"query": q, "found": True, "canonical": hit["name"], "row": hit}


def recognize(payload: dict) -> dict:
    """③ 之前的那一步：把菜品/食材/烹饪方式转成结构化标签（可选走 Qwen-VL）。"""
    month = int(payload.get("monthAge") or 11)
    image = payload.get("imageUrl")
    if image and payload.get("useVision"):
        return pipeline.recognize_via_qwen_vl(image, month_age=month)
    return pipeline.recognize(payload.get("dish", "未命名餐次"),
                              payload.get("ingredients") or [],
                              payload.get("cooking") or [],
                              month_age=month, amounts=payload.get("amounts"),
                              source=payload.get("source", "manual"))


def analyze(payload: dict) -> dict:
    """自定义一组餐次跑完整链路（POST 用）。"""
    month = int(payload.get("monthAge") or 11)
    meals = payload.get("meals") or []
    if not meals:
        raise ValueError("meals 不能为空")
    return pipeline.analyze_day(meals, month_age=month,
                                tried_before=payload.get("triedBefore") or ())


# ------------------------------------------- 标准库模式用的统一分发器

GET_ROUTES = {
    "/api/health": lambda q: health(),
    "/api/meal/scenarios": lambda q: list_scenarios(),
    "/api/meal/day": lambda q: day(_one(q, "scenario"), _one(q, "monthAge")),
    "/api/meal/safety": lambda q: safety(_one(q, "scenario"), _one(q, "monthAge")),
    "/api/meal/advice": lambda q: advice(_one(q, "scenario"), _one(q, "monthAge")),
    "/api/meal/food": lambda q: food_table(_one(q, "q")),
}

POST_ROUTES = {
    "/api/meal/recognize": recognize,
    "/api/meal/analyze": analyze,
}


def _one(query: dict, key: str) -> Any:
    value = query.get(key)
    return value[0] if isinstance(value, list) else value


def dispatch(method: str, path: str, query: dict, body: dict | None = None):
    """返回 (status, payload)。给 serve.py 的标准库模式复用。"""
    try:
        if method == "GET":
            handler = GET_ROUTES.get(path)
            if not handler:
                return 404, {"error": "not found", "path": path,
                             "available": sorted(GET_ROUTES) + sorted(POST_ROUTES)}
            return 200, handler(query)
        if method == "POST":
            handler = POST_ROUTES.get(path)
            if not handler:
                return 404, {"error": "not found", "path": path}
            return 200, handler(body or {})
        return 405, {"error": "method not allowed"}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except RuntimeError as exc:
        return 503, {"error": str(exc)}


def parse_query(qs: str) -> dict:
    return urllib.parse.parse_qs(qs or "")
