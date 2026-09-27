"""用餐分析 Pipeline —— 严格对应架构图的五步链路。

    ① 摄像头检测到烹饪活动（非 24h 连续上传）
        ↓
    ② 多模态视觉模型 → 结构化标签 {菜品, 食材, 烹饪方式}
        ↓
    ③ 查《中国食物成分表（第 6 版）》结构化表（含钠 mg/100g）
        ↓
    ④ 对照「月龄标准表」+ 今日累计摄入 → 算出缺口
        ↓
    ⑤ ┌─ 正向分支（主路径）──► 备餐建议卡片（附标准原文出处，可一键转发家庭群）
       └─ 负向分支（安全网）──► ⛔ 1 岁内不应添加盐 / ⚠️ 钠偏高 / ⚠️ 月龄不宜

本模块零数据库、零外部 API 依赖：没有 API Key 时视觉识别走内置规则，
有 `QWEN_VL_API_KEY` 时可切到真实多模态模型（见 recognize()）。

合规红线（与项目 PRD 一致）：
- 只做「摄入量 vs 推荐量」的对照，不做营养估算之外的医学结论；
- 不诊断、不评分排名、不识别身份；
- 模拟识别结果一律带 `source` 字段，前端必须显著标注。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

DATA_DIR = Path(__file__).resolve().parent / "data"

# 每份食物的默认克重（月龄分档）。视觉模型只给「是什么」，克重由这里按月龄补。
# 真实部署时克重应来自餐盘秤或家长手填，这里给的是演示用经验值。
DEFAULT_PORTION = {
    "6-12m": {"staple": 20, "vegetable": 30, "fruit": 30, "meat": 20, "poultry": 20,
              "seafood": 20, "egg": 30, "dairy": 100, "bean": 25, "fungus": 15,
              "oil": 3, "seasoning": 2},
    "13-24m": {"staple": 40, "vegetable": 50, "fruit": 50, "meat": 35, "poultry": 35,
               "seafood": 35, "egg": 50, "dairy": 150, "bean": 40, "fungus": 25,
               "oil": 5, "seasoning": 3},
    "25-72m": {"staple": 60, "vegetable": 80, "fruit": 80, "meat": 50, "poultry": 50,
               "seafood": 50, "egg": 50, "dairy": 200, "bean": 60, "fungus": 35,
               "oil": 8, "seasoning": 4},
}

# 食物类别 → 计入哪一个达标率口径
GROUP_TO_METRIC = {
    "staple": "staple", "vegetable": "vegetable",
    "fruit": "fruit", "meat": "protein", "poultry": "protein",
    "seafood": "protein", "egg": "protein", "dairy": "protein",
    "bean": "protein", "fungus": "vegetable",
}


def _load(filename: str) -> dict:
    return json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))


FOOD_TABLE = _load("food_table.json")
AGE_STANDARD = _load("age_standard.json")
FOODS: dict[str, dict] = FOOD_TABLE["items"]

# 建立「别名 → 标准名」索引，让「西红柿」「西蓝花」这类口语叫法也能命中成分表
ALIAS_INDEX: dict[str, str] = {}
for _canonical, _item in FOODS.items():
    ALIAS_INDEX[_canonical] = _canonical
    for _alias in _item.get("alias", []):
        ALIAS_INDEX[_alias] = _canonical


# ---------------------------------------------------------------- 基础查询

def resolve(name: str) -> str | None:
    """把任意叫法归一化到成分表里的标准名；查不到返回 None（不猜、不估算）。

    匹配顺序：精确命中 → 最长子串命中。
    「最长」很关键：「花生油」同时包含「花生」和（若按短匹配）会命中花生，
    而花生 minAge=36、植物油 minAge=6，取最短会把食用油误报成「月龄不宜」。
    """
    if not name:
        return None
    key = name.strip()
    if key in ALIAS_INDEX:
        return ALIAS_INDEX[key]
    # 退一步做包含匹配，但取**最长**的别名，避免短别名吃掉长词
    hits = [alias for alias in ALIAS_INDEX if alias and alias in key]
    if hits:
        return ALIAS_INDEX[max(hits, key=len)]
    return None


def lookup(name: str) -> dict | None:
    canonical = resolve(name)
    return {**FOODS[canonical], "name": canonical} if canonical else None


def band_for(month_age: int) -> dict:
    """按宝宝月龄取「月龄标准表」里的那一档。"""
    for band in AGE_STANDARD["bands"]:
        if band["minMonth"] <= month_age <= band["maxMonth"]:
            return band
    # 超出表范围时：小于最小档取下限档，大于最大档取上限档
    bands = AGE_STANDARD["bands"]
    return bands[0] if month_age < bands[0]["minMonth"] else bands[-1]


def band_key(band: dict) -> str:
    return band["id"] if band["id"] in DEFAULT_PORTION else "6-12m"


def _range_bounds(text: str) -> tuple[float, float] | None:
    """从「25-100 g」「50 g」「< 1.5 g」这类推荐量文本里取出 (下限, 上限)。"""
    import re

    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")]
    if not numbers:
        return None
    if len(numbers) == 1:
        # 「< 1.5 g」「50 g」这种单值写法：上界即该值，下界取 0
        return (0.0, numbers[0])
    return (min(numbers), max(numbers))


# 食物类别的推荐区间，直接从月龄标准表的 dailyPortions 解析，避免两处硬编码打架
FOOD_GROUP_RANGE: dict[str, dict[str, tuple[float, float]]] = {"vegetable": {}, "staple": {}}
for _band in AGE_STANDARD["bands"]:
    for _metric, _label in (("vegetable", "蔬菜"), ("staple", "谷薯类")):
        _bounds = _range_bounds(_band["dailyPortions"].get(_label, ""))
        if _bounds:
            FOOD_GROUP_RANGE[_metric][_band["id"]] = _bounds


# ---------------------------------------------------- ② 视觉识别（结构化标签）

def recognize(dish: str, ingredients: Iterable[str] | None = None,
              cooking: Iterable[str] | None = None, *, month_age: int = 11,
              amounts: dict[str, float] | None = None,
              source: str = "rule-based") -> dict:
    """把一段视频 / 一句话，变成结构化标签 {菜品, 食材, 烹饪方式}。

    没有 API Key 时：用内置别名表把口语叫法归一到成分表标准名，克重按月龄补经验值。
    有 `QWEN_VL_API_KEY` 时：改走真实多模态模型，把画面直接转成同样的结构（见文件末）。

    `amounts` 可指定实际克重（餐盘秤 / 家长手填 / 包装标示），优先于月龄经验值。

    查不到的成分一律进 `unresolved`，**不做任何估算**——宁可少算，不可乱算。
    """
    band = band_for(month_age)
    portions = DEFAULT_PORTION[band_key(band)]
    amounts = {resolve(k) or k: v for k, v in (amounts or {}).items()}

    items, unresolved = [], []
    for raw in (ingredients or []):
        hit = lookup(raw)
        if not hit:
            unresolved.append(raw)
            continue
        grams = amounts.get(hit["name"]) or hit.get("defaultGrams") or portions.get(hit["group"], 30)
        items.append({"name": hit["name"], "raw": raw, "group": hit["group"],
                      "grams": float(grams), "minAge": hit.get("minAge", 0),
                      "richIron": bool(hit.get("richIron")),
                      "salt": bool(hit.get("salt")),
                      "risk": hit.get("risk")})

    return {
        "dish": dish,
        "ingredients": items,
        "cooking": list(cooking or []),
        "unresolved": unresolved,
        "source": source,
        "monthAge": month_age,
    }


def recognize_via_qwen_vl(image_url: str, *, month_age: int = 11) -> dict:
    """可选：走真实多模态模型（Qwen-VL）。未配置 Key 时抛 RuntimeError。

    只做「画面 → 结构化标签」这一步，后续折算/对照逻辑完全复用 recognize() 之后的链路，
    所以换模型不影响下游，也不会改变合规边界。
    """
    import httpx

    base = os.getenv("QWEN_VL_BASE_URL", "").strip()
    key = os.getenv("QWEN_VL_API_KEY", "").strip()
    model = os.getenv("QWEN_VL_MODEL", "Qwen3-VL-4B").strip()
    if not (base and key):
        raise RuntimeError("未配置 QWEN_VL_BASE_URL / QWEN_VL_API_KEY，无法调用多模态模型")

    prompt = ("识别这张儿童餐盘照片，只输出 JSON："
              '{"dish":"菜名","ingredients":["食材1","食材2"],"cooking":["烹饪方式"]}。'
              "只列看得见的食材，不要推测，不要输出解释。")
    resp = httpx.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}]},
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    start, end = text.find("{"), text.rfind("}")
    parsed = json.loads(text[start:end + 1]) if start >= 0 else {}
    return recognize(parsed.get("dish", "未命名餐次"),
                     parsed.get("ingredients", []), parsed.get("cooking", []),
                     month_age=month_age, source="qwen-vl")


# ------------------------------------------- ③ 查成分表 → 折算这一餐的营养

def nutrition_of(structured: dict) -> dict:
    """结构化标签 → 本餐营养。数值全部来自成分表，逐项可回溯。"""
    totals = {"protein": 0.0, "iron": 0.0, "sodium": 0.0}
    by_group: dict[str, float] = {}
    breakdown = []

    for ing in structured["ingredients"]:
        row = FOODS[ing["name"]]
        grams = ing["grams"]
        factor = grams / 100.0
        item = {
            "name": ing["name"], "grams": grams,
            "protein": round(row["protein"] * factor, 3),
            "iron": round(row["iron"] * factor, 3),
            "sodium": round(row["sodium"] * factor, 2),
        }
        breakdown.append(item)
        totals["protein"] += item["protein"]
        totals["iron"] += item["iron"]
        totals["sodium"] += item["sodium"]
        by_group[row["group"]] = by_group.get(row["group"], 0.0) + grams

    return {
        "protein": round(totals["protein"], 2),
        "iron": round(totals["iron"], 2),
        "sodium": round(totals["sodium"], 2),
        "gramsByGroup": {k: round(v, 1) for k, v in by_group.items()},
        "breakdown": breakdown,
    }


# ------------------------------------------------------- ④ 今日累计 + 缺口

def aggregate(observations: list[dict], month_age: int = 11) -> dict:
    """把一天里所有餐次汇总，并对照月龄标准表算出四项达标率。"""
    band = band_for(month_age)
    intake = {"protein": 0.0, "iron": 0.0, "sodium": 0.0}
    group_grams: dict[str, float] = {}
    meals = []

    for obs in observations:
        structured = obs.get("structured") or recognize(
            obs.get("dish", "未命名"), obs.get("ingredients"),
            obs.get("cooking"), month_age=month_age, amounts=obs.get("amounts"),
            source=obs.get("source", "simulated"))
        nut = nutrition_of(structured)
        intake["protein"] += nut["protein"]
        intake["iron"] += nut["iron"]
        intake["sodium"] += nut["sodium"]
        for group, grams in nut["gramsByGroup"].items():
            group_grams[group] = group_grams.get(group, 0.0) + grams
        meals.append({
            "time": obs.get("time"), "label": obs.get("label"),
            "dish": structured["dish"], "source": structured["source"],
            "ingredients": [i["name"] for i in structured["ingredients"]],
            "nutrition": nut,
        })

    # 食物类别 → 达标率口径
    metric_grams = {"protein": 0.0, "vegetable": 0.0, "staple": 0.0, "fruit": 0.0}
    for group, grams in group_grams.items():
        metric = GROUP_TO_METRIC.get(group)
        if metric:
            metric_grams[metric] += grams

    targets = band["targets"]
    # 达标率口径（详见 age_standard.json 的 _howRatioIsComputed）：
    #   营养素（蛋白质 / 铁）→ 分子是摄入量，分母是 RNI / AI，100% 即「满足推荐摄入量」；
    #   食物类别（蔬菜 / 谷薯）→ 膳食指南给的是推荐「区间」，分子是摄入量，
    #                          分母取区间上限（达到上限即视为该类别完全达标），
    #                          同时给出 inRange 标记表示摄入是否已落在推荐区间内。
    # 只看 ratio 会把「已在区间内」误判成缺口（例如 2-6 岁蔬菜 110g 落在 100-300g 区间内，
    # ratio 只有 37%，看着像严重不足）。所以缺口判断必须同时看 inRange。
    metrics = {
        "protein": {"kind": "nutrient", "numerator": intake["protein"],
                    "denominator": targets["protein"], "unit": "g"},
        "iron": {"kind": "nutrient", "numerator": intake["iron"],
                 "denominator": targets["iron"], "unit": "mg"},
        "vegetable": {"kind": "group", "numerator": metric_grams["vegetable"],
                      "denominator": FOOD_GROUP_RANGE["vegetable"][band["id"]][1],
                      "range": FOOD_GROUP_RANGE["vegetable"][band["id"]], "unit": "g"},
        "staple": {"kind": "group", "numerator": metric_grams["staple"],
                   "denominator": FOOD_GROUP_RANGE["staple"][band["id"]][1],
                   "range": FOOD_GROUP_RANGE["staple"][band["id"]], "unit": "g"},
    }
    ratios = {}
    for key, m in metrics.items():
        denom = m["denominator"] or 0
        ratio = min(1.0, m["numerator"] / denom) if denom else 0.0
        if m["kind"] == "nutrient":
            # 营养素只有「够 / 不够」两态，没有上限概念（这里不设可耐受最高摄入量）
            status = "ok" if m["numerator"] >= denom else "low"
        else:
            low, high = m["range"]
            # 食物类别是三态：低于区间下限=不足，落在区间内=合适，高于上限=超量
            status = "low" if m["numerator"] < low else ("over" if m["numerator"] > high else "ok")
        ratios[key] = {
            "value": round(m["numerator"], 2), "target": denom,
            "ratio": round(ratio, 4), "unit": m["unit"],
            "kind": m["kind"], "range": list(m["range"]) if m.get("range") else None,
            "status": status, "inRange": status == "ok",
        }
    overall = sum(r["ratio"] for r in ratios.values()) / len(ratios)

    return {
        "monthAge": month_age,
        "band": {"id": band["id"], "label": band["label"]},
        "meals": meals,
        "intake": {k: round(v, 2) for k, v in intake.items()},
        "ratios": ratios,
        "overallRatio": round(overall, 4),
        "sodiumLimit": band["sodiumLimit"],
    }


# 食物类别的推荐范围上限（来自 age_standard.json 的 dailyPortions，这里显式列出便于计算）
FOOD_GROUP_UPPER = {
    "vegetable": {"6-12m": 100.0, "13-24m": 150.0, "25-72m": 300.0},
    "staple": {"6-12m": 75.0, "13-24m": 100.0, "25-72m": 150.0},
}


# ------------------------------------------------------- ⑤ 正向分支：备餐建议

# 缺口 → 候选补法。每一项都带「为什么」和标准出处，前端可一键转发家庭群。
GAP_PLAYBOOK = {
    "iron": {
        "metric": "富铁食物",
        "candidates": [
            {"dish": "猪肝泥", "foods": ["猪肝"], "why": "猪肝富含血红素铁，吸收率高，是 6-24 月龄补铁的首选食材之一。",
             "how": "猪肝蒸熟后碾成泥，首次尝试 1 小勺（约 10g），混入米糊或粥中。"},
            {"dish": "牛肉末粥", "foods": ["牛肉", "粳米"], "why": "瘦牛肉含血红素铁与优质蛋白，可与维生素 C 丰富的蔬菜同食促进吸收。",
             "how": "牛肉剁碎煮烂后与软粥同煮，每日 20-30g。"},
            {"dish": "强化铁米粉", "foods": ["婴儿米粉"], "why": "强化铁米粉是 6 月龄后最稳定的补铁来源，铁含量明确、易计量。",
             "how": "按包装说明冲调，每日 1-2 次。"},
        ],
        "citation": "《中国居民膳食指南（2022）》7-24 月龄婴幼儿喂养指南："
                    "7-12 月龄婴儿铁的推荐摄入量为 10 mg/日，应优先添加富铁食物（如强化铁米粉、肉泥、肝泥）。",
        "source": "《中国居民膳食指南（2022）》",
    },
    "protein": {
        "metric": "蛋白质",
        "candidates": [
            {"dish": "鸡蛋羹", "foods": ["鸡蛋"], "why": "鸡蛋蛋白质氨基酸组成接近人体需要，消化吸收率高。",
             "how": "全蛋打散加温水蒸 8 分钟，每日 1 个。"},
            {"dish": "三文鱼泥", "foods": ["三文鱼"], "why": "富含优质蛋白与 DHA，质地软嫩适合咀嚼初期。",
             "how": "蒸熟后压成泥，首次尝试 15g 观察 3 天。"},
        ],
        "citation": "《中国居民膳食营养素参考摄入量（2023 版）》：7-12 月龄蛋白质 RNI 为 20 g/日。",
        "source": "《中国居民膳食营养素参考摄入量（2023 版）》",
    },
    "vegetable": {
        "metric": "蔬菜",
        "candidates": [
            {"dish": "菠菜碎", "foods": ["菠菜"], "why": "深色蔬菜富含 β-胡萝卜素与叶酸，同时提供膳食纤维。",
             "how": "焯水去草酸后切碎拌入粥或面中，每日 30-50g。"},
            {"dish": "南瓜泥", "foods": ["南瓜"], "why": "口感甜糯、纤维柔软，是接受度最高的蔬菜之一。",
             "how": "蒸熟压泥，可直接喂食或拌入米粉。"},
        ],
        "citation": "《中国居民膳食指南（2022）》：7-12 月龄每日蔬菜 25-100 g。",
        "source": "《中国居民膳食指南（2022）》",
    },
    "staple": {
        "metric": "碳水化合物",
        "candidates": [
            {"dish": "小米粥", "foods": ["小米"], "why": "小米含 B 族维生素，质地软烂易消化。",
             "how": "小米熬至开花，每日 30-50g。"},
            {"dish": "土豆泥", "foods": ["土豆"], "why": "提供易消化的淀粉与钾，可替代部分主食。",
             "how": "蒸熟压泥，可加少量配方奶调稠度。"},
        ],
        "citation": "《中国居民膳食指南（2022）》：7-12 月龄每日谷薯类 20-75 g。",
        "source": "《中国居民膳食指南（2022）》",
    },
}

# 缺口低于这个阈值才推建议；全部达标时不推
GAP_THRESHOLD = 0.85


def prep_advice(report: dict) -> dict:
    """正向分支（主路径）：算出最大缺口 → 给出带出处的备餐建议卡片。

    缺口判定必须同时满足 ratio 低于阈值 **且** status 为 low——
    只看百分比会把「已在推荐区间内」误报成缺口，也会把「超量」误判成不足。
    """
    gaps = {k: v for k, v in report["ratios"].items()
            if v["ratio"] < GAP_THRESHOLD and v["status"] == "low"}
    if not gaps:
        return {"hasGap": False,
                "summary": "今日四类营养均已达标，保持现有喂养节奏即可。",
                "citation": report["band"]["label"] + "膳食推荐量已全部满足。",
                "source": "《中国居民膳食指南（2022）》",
                "candidates": []}

    worst_key = min(gaps, key=lambda k: gaps[k]["ratio"])
    play = GAP_PLAYBOOK[worst_key]
    worst = gaps[worst_key]
    return {
        "hasGap": True,
        "metric": worst_key,
        "metricLabel": play["metric"],
        "current": worst["value"],
        "target": worst["target"],
        "ratio": worst["ratio"],
        "dish": play["candidates"][0]["dish"],
        "foods": play["candidates"][0]["foods"],
        "why": play["candidates"][0]["why"],
        "how": play["candidates"][0]["how"],
        "candidates": play["candidates"],
        "citation": play["citation"],
        "source": play["source"],
        "summary": f"今天{play['metric']}只到 {round(worst['ratio'] * 100)}%，"
                   f"建议下一餐补{play['candidates'][0]['dish']}。",
        "shareText": f"【今日喂养建议】{play['metric']}摄入 {round(worst['ratio'] * 100)}%，"
                     f"建议下一餐添加{play['candidates'][0]['dish']}。依据：{play['source']}",
    }


# ------------------------------------------------------- ⑤ 负向分支：安全网

def safety_checks(report: dict, month_age: int | None = None) -> list[dict]:
    """负向分支（安全网）：三类硬拦截。

    ⛔ salt_under_1y  —— 1 岁内不应添加盐（看烹饪方式 / 调味品）
    ⚠️ sodium_high   —— 今日钠累计超过月龄 AI
    ⚠️ age_unsuitable —— 食材最低适用月龄高于宝宝月龄
    """
    month = month_age if month_age is not None else report["monthAge"]
    band = band_for(month)
    alerts: list[dict] = []

    # ⛔ 1 岁内不应添加盐
    if band["saltForbidden"]:
        salted = []
        for meal in report["meals"]:
            for ing in meal["ingredients"]:
                row = FOODS.get(ing, {})
                if row.get("salt"):
                    salted.append({"meal": meal["label"], "time": meal["time"], "item": ing})
        if salted:
            first = salted[0]
            alerts.append({
                "level": "block", "code": "salt_under_1y",
                "title": "1 岁内不应添加盐",
                "detail": f"{first['time']} {first['meal']} 检测到「{first['item']}」。"
                          f"{band['label']}宝宝肾脏发育尚未成熟，额外加盐会增加肾脏负担，"
                          f"并让宝宝形成重口味偏好。",
                "action": "本餐单独盛出后再给成人调味；用番茄、南瓜、香菇等天然食材提鲜。",
                "citation": band["quote"], "source": "《中国居民膳食指南（2022）》",
            })

    # ⚠️ 钠偏高
    if report["intake"]["sodium"] > band["sodiumLimit"]:
        pct = round(report["intake"]["sodium"] / band["sodiumLimit"] * 100)
        alerts.append({
            "level": "warn", "code": "sodium_high",
            "title": "钠偏高",
            "detail": f"今日钠累计 {report['intake']['sodium']} mg，"
                      f"已达 {band['label']}每日适宜摄入量（{band['sodiumLimit']} mg）的 {pct}%。",
            "action": "明日减少加工食品与调味品；注意面条、面包、火腿等「隐形盐」。",
            "citation": band["quote"], "source": "《中国居民膳食指南（2022）》",
        })

    # ⚠️ 月龄不宜
    # 盐类调味品已由上面的 salt_under_1y 硬拦截覆盖，这里跳过，避免同一条问题报两遍。
    seen: set[tuple[str, str]] = set()
    for meal in report["meals"]:
        for ing in meal["ingredients"]:
            row = FOODS.get(ing, {})
            if row.get("salt"):
                continue
            min_age = row.get("minAge", 0)
            if min_age <= month or (ing, "age_unsuitable") in seen:
                continue
            seen.add((ing, "age_unsuitable"))
            alerts.append({
                "level": "warn", "code": "age_unsuitable",
                "title": f"{ing} 月龄不宜",
                "detail": row.get("risk") or
                          f"{ing}的建议起始月龄为 {min_age} 月，宝宝当前 {month} 月龄。",
                "action": "本餐先不添加，待月龄达到后再单独引入并观察 3 天。",
                "citation": "《中国居民膳食指南（2022）》7-24 月龄婴幼儿喂养指南",
                "source": "《中国居民膳食指南（2022）》",
            })

    order = {"block": 0, "warn": 1}
    alerts.sort(key=lambda a: order.get(a["level"], 9))
    return alerts


# --------------------------------------------------------- 新食物观察期标记

def new_food_flags(report: dict, tried_before: Iterable[str] = ()) -> list[dict]:
    """标出「首次尝试」的食材：24-72 小时为过敏观察期（对应语音助手的时间线对照）。

    油脂和调味品不参与过敏观察期——它们不是致敏原，标出来只会造成噪音。
    """
    known = {resolve(x) or x for x in tried_before}
    flags, seen = [], set()
    for meal in report["meals"]:
        for ing in meal["ingredients"]:
            if ing in known or ing in seen:
                continue
            if FOODS.get(ing, {}).get("group") in ("oil", "seasoning"):
                continue
            seen.add(ing)
            flags.append({"food": ing, "meal": meal["label"], "time": meal["time"],
                          "allergen": bool(FOODS.get(ing, {}).get("allergen")),
                          "note": f"首次尝试{ing}，之后 24-72 小时为过敏观察期。"})
    # 高致敏食材排前面，提醒家长优先盯
    flags.sort(key=lambda f: not f["allergen"])
    return flags


# --------------------------------------------------------------- 对外总入口

def analyze_day(observations: list[dict], month_age: int = 11,
                tried_before: Iterable[str] = ()) -> dict:
    """跑完五步链路，返回前端需要的全部数据。"""
    report = aggregate(observations, month_age=month_age)
    return {
        "report": report,
        "advice": prep_advice(report),
        "safety": safety_checks(report),
        "newFoods": new_food_flags(report, tried_before),
        "foodTableSource": FOOD_TABLE["_source"],
        "standardSource": AGE_STANDARD["_source"],
    }
