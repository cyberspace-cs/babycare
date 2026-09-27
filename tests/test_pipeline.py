#!/usr/bin/env python3
"""自检：python -m unittest discover -s tests -v

覆盖链路的关键不变量。这些用例不是走形式——每一条都对应开发时真实踩到的坑：
别名匹配、区间口径、告警去重、以及「查不到就不估算」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import api, pipeline, scenarios  # noqa: E402


class TestFoodTable(unittest.TestCase):
    def test_alias_resolves(self):
        self.assertEqual(pipeline.resolve("西红柿"), "番茄")
        self.assertEqual(pipeline.resolve("西蓝花"), "西兰花")
        self.assertEqual(pipeline.resolve("米糊"), "婴儿米粉")

    def test_longest_alias_wins(self):
        """「花生油」必须命中植物油，不能被「花生」吃掉（花生 minAge=36）。"""
        self.assertEqual(pipeline.resolve("花生油"), "植物油")
        self.assertEqual(pipeline.lookup("花生油")["minAge"], 6)
        self.assertEqual(pipeline.resolve("橄榄油"), "植物油")

    def test_unknown_food_is_not_guessed(self):
        """查不到就返回 None，绝不估算——这是合规底线。"""
        self.assertIsNone(pipeline.resolve("火龙果"))
        self.assertFalse(api.food_table("火龙果")["found"])

    def test_unknown_ingredient_goes_to_unresolved(self):
        structured = pipeline.recognize("神秘料理", ["火龙果", "鸡蛋"], month_age=11)
        self.assertEqual([i["name"] for i in structured["ingredients"]], ["鸡蛋"])
        self.assertEqual(structured["unresolved"], ["火龙果"])


class TestRatios(unittest.TestCase):
    def setUp(self):
        self.report = api.day("demo_day")["report"]

    def test_ratio_keys(self):
        self.assertEqual(set(self.report["ratios"]),
                         {"protein", "iron", "vegetable", "staple"})

    def test_ratio_never_exceeds_one(self):
        for key, row in self.report["ratios"].items():
            self.assertLessEqual(row["ratio"], 1.0, key)

    def test_group_metrics_have_range_and_three_states(self):
        for key in ("vegetable", "staple"):
            row = self.report["ratios"][key]
            self.assertIsNotNone(row["range"], key)
            self.assertIn(row["status"], ("low", "ok", "over"), key)

    def test_nutrient_metrics_are_two_state(self):
        for key in ("protein", "iron"):
            self.assertIsNone(self.report["ratios"][key]["range"], key)
            self.assertIn(self.report["ratios"][key]["status"], ("low", "ok"), key)

    def test_in_range_is_not_reported_as_gap(self):
        """蔬菜 70g 落在推荐区间 25-100g 内，即使 ratio 只有 70% 也不算缺口。"""
        row = self.report["ratios"]["vegetable"]
        self.assertEqual(row["status"], "ok")
        self.assertTrue(row["inRange"])

    def test_over_is_not_reported_as_gap(self):
        """谷薯超量不等于不足，不能反过来推荐「再补碳水」。"""
        over = api.day("salted_day")["report"]["ratios"]["staple"]
        self.assertEqual(over["status"], "over")
        self.assertGreater(over["value"], over["range"][1])


class TestBranches(unittest.TestCase):
    def test_forward_branch_picks_worst_gap(self):
        """正向分支：demo_day 缺富铁 → 推荐富铁类食材。"""
        advice = api.day("demo_day")["advice"]
        self.assertTrue(advice["hasGap"])
        self.assertEqual(advice["metric"], "iron")
        self.assertEqual(advice["dish"], "猪肝泥")
        self.assertIn("膳食指南", advice["source"])
        self.assertTrue(advice["citation"])

    def test_no_gap_when_all_ok(self):
        """27 月龄那组全部达标/在区间内，不该硬凑一条建议。"""
        advice = api.day("toddler_day")["advice"]
        self.assertFalse(advice["hasGap"])

    def test_salt_block(self):
        alerts = api.day("salted_day")["safety"]
        codes = [a["code"] for a in alerts]
        self.assertIn("salt_under_1y", codes)
        block = [a for a in alerts if a["level"] == "block"]
        self.assertEqual(len(block), 1)
        self.assertEqual(block[0]["title"], "1 岁内不应添加盐")

    def test_sodium_high(self):
        result = api.day("salted_day")
        self.assertGreater(result["report"]["intake"]["sodium"],
                           result["report"]["sodiumLimit"])
        self.assertIn("sodium_high", [a["code"] for a in result["safety"]])

    def test_age_unsuitable_deduped_and_not_double_reported_for_salt(self):
        """食盐已由 salt_under_1y 硬拦截，不该再以「月龄不宜」重复报一次。"""
        alerts = api.day("salted_day")["safety"]
        age_titles = [a["title"] for a in alerts if a["code"] == "age_unsuitable"]
        self.assertEqual(len(age_titles), len(set(age_titles)), "月龄不宜告警出现重复")
        self.assertFalse(any("食盐" in t or "酱油" in t for t in age_titles),
                         "盐类调味品不应重复触发月龄不宜")
        self.assertTrue(any("蜂蜜" in t for t in age_titles))

    def test_block_sorted_before_warn(self):
        levels = [a["level"] for a in api.day("salted_day")["safety"]]
        self.assertEqual(levels, sorted(levels, key=lambda x: {"block": 0, "warn": 1}[x]))

    def test_clean_day_has_no_alerts(self):
        self.assertEqual(api.day("demo_day")["safety"], [])


class TestNewFoods(unittest.TestCase):
    def test_allergen_first_and_deduped(self):
        flags = api.day("demo_day")["newFoods"]
        names = [f["food"] for f in flags]
        self.assertEqual(len(names), len(set(names)), "新食物出现重复")
        self.assertIn("虾仁", names)
        self.assertTrue(flags[0]["allergen"], "致敏食材应排在前面")

    def test_oil_and_seasoning_excluded(self):
        """油脂/调味品不参与过敏观察期，标出来只是噪音。"""
        names = [f["food"] for f in api.day("demo_day")["newFoods"]]
        for noisy in ("植物油", "食盐", "酱油"):
            self.assertNotIn(noisy, names)


class TestAgeBands(unittest.TestCase):
    def test_band_switch(self):
        self.assertEqual(api.day("demo_day")["report"]["band"]["id"], "6-12m")
        self.assertEqual(api.day("toddler_day")["report"]["band"]["id"], "25-72m")

    def test_month_age_override(self):
        """11 月龄的餐次按 24 月龄复核时，应切到 13-24m 档。"""
        result = api.day("demo_day", month_age=20)
        self.assertEqual(result["report"]["band"]["id"], "13-24m")
        self.assertEqual(result["report"]["monthAge"], 20)

    def test_salt_forbidden_only_under_one_year(self):
        self.assertTrue(pipeline.band_for(11)["saltForbidden"])
        self.assertFalse(pipeline.band_for(20)["saltForbidden"])


class TestSources(unittest.TestCase):
    def test_every_advice_has_citation(self):
        for sid in scenarios.SCENARIOS:
            advice = api.day(sid)["advice"]
            if advice["hasGap"]:
                self.assertTrue(advice["citation"], sid)
                self.assertTrue(advice["source"], sid)

    def test_every_alert_has_citation(self):
        for alert in api.day("salted_day")["safety"]:
            self.assertTrue(alert["citation"], alert["code"])
            self.assertTrue(alert["source"], alert["code"])

    def test_simulated_data_is_labelled(self):
        """模拟识别结果必须带 source 标记，前端才能显著标注。"""
        report = api.day("demo_day")["report"]
        for meal in report["meals"]:
            self.assertTrue(meal["source"], meal["dish"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
