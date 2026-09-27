# babycare · 宝宝「会吃」用餐分析演示

> 摄像头拍到做菜 → 多模态模型识别出菜品 → 查《中国食物成分表》折算营养 →
> 对照月龄标准表和今日累计算出缺口 → **正向**给备餐建议，**负向**拦住不该吃的东西。

这是一个**可以直接跑起来的**前后端演示包。前端是一页自包含的 HTML，后端是纯 Python
标准库 + 两份结构化数据表。**不装任何第三方依赖也能跑通全部功能。**

![首页：左为无后端时的静态设计稿，右为接入后端后的实时计算结果](screenshot.png)

---

## 一、30 秒跑起来

### 方式 A：零依赖（推荐，什么都不用装）

需要 Python 3.9+（`python --version` 看一下）。在**本目录**下执行：

```bash
python serve.py
```

浏览器会自动打开 <http://127.0.0.1:8115/>。

> **Windows 上如果 `python` 打不开**，试试 `py serve.py`；
> 若提示找不到命令，说明没装 Python，去 <https://www.python.org/downloads/> 装一个，
> 安装时记得勾上 *Add Python to PATH*。

### 方式 B：带接口文档（可选）

想让 `/docs` 出现可交互的 Swagger 文档，就多装两个包：

```bash
pip install -r requirements.txt
python serve.py
```

装完再运行，启动日志会显示 `运行模式：FastAPI + uvicorn`，此时
<http://127.0.0.1:8115/docs> 可以看到全部接口并直接点着试。

**两种方式的接口返回值完全一致** —— 因为业务逻辑写在 `server/api.py` 里，
FastAPI 和标准库只是两层不同的壳。

### 常用参数

```bash
python serve.py --port 9000        # 换端口（默认 8115，被占用会自动往后找）
python serve.py --no-browser       # 不自动开浏览器
python serve.py --host 0.0.0.0     # 让同局域网的其他设备也能访问
```

### 方式 C：只想看前端

直接双击 `eat.html` 就能看。这时页面用的是设计稿里的静态示例数据，
不会显示「已连接本地后端」的绿色标识 —— 这是**故意设计的**，见下文第四节。

---

## 二、跑起来之后看什么

页面是一个手机原型，底部 5 个 Tab。**首页的数据是后端实时算出来的**：

| 位置 | 静态打开 | 接了后端 |
|---|---|---|
| 圆环达标率 | 75% | 66%（后端按今日摄入 / 月龄推荐量算出） |
| 四项指标 | 82 / 68 / 78 / 54% | 79 / 70 / 83 / 31% |
| 三餐卡片标签 | 固定文案 | 按该餐是否触发安全网 / 是否首次尝试高致敏食材动态变化 |
| 备餐建议摘要 | 固定文案 | 「今天富铁食物只到 30%，建议下一餐补猪肝泥。」 |
| 页面底部 | 无 | 🟢 已连接本地后端 · 11 月龄 · 家常三餐 · 7-12 月龄 |

### 三个内置场景，覆盖三条分支

后端准备了三个场景，改一下 URL 参数就能切换（页面上是默认的第一个）：

```bash
# ① 正向分支：营养都够，只缺铁 → 出备餐建议卡片
curl "http://127.0.0.1:8115/api/meal/day?scenario=demo_day"

# ② 负向分支（安全网）：跟大人一起吃饭 → 拦下加盐、蜂蜜、钠超标
curl "http://127.0.0.1:8115/api/meal/day?scenario=salted_day"

# ③ 月龄标准表切换：27 月龄走 2-6 岁档，推荐量整体上移
curl "http://127.0.0.1:8115/api/meal/day?scenario=toddler_day"
```

场景 ② 是这条链路真正的价值所在。它返回的 `safety` 数组里是**带出处的拦截**：

- ⛔ `salt_under_1y` —— 1 岁内不应添加食盐（`block` 级，指出是哪一餐检出的）
- ⚠️ `sodium_high` —— 今日钠 1409.98 mg，已达 11 月龄适宜摄入量 350 mg 的 403%
- ⚠️ `age_unsuitable` —— 蜂蜜月龄不宜（肉毒杆菌芽孢风险）

---

## 三、它对应架构图的哪一步

```
摄像头检测到烹饪活动（非 24h 连续上传）
        ↓
多模态视觉模型（首选 Qwen-VL 系列）→ 结构化标签 {菜品, 食材, 烹饪方式}
        ↓
查《中国食物成分表（第6版）》结构化 JSON（含钠 mg/100g）
        ↓
对照「月龄标准表」+ 今日累计摄入 → 算出缺口
        ↓
   ┌─ 正向分支 ──► 备餐建议卡片（附标准原文出处，一键转发家庭群）
   └─ 负向分支 ──► ⛔ 1岁内不应添加盐 / ⚠️ 钠偏高 / ⚠️ 月龄不宜
```

| 架构图里的环节 | 代码位置 |
|---|---|
| ① 结构化标签 | `server/pipeline.py` → `recognize()`；接了 Qwen-VL 走 `recognize_via_qwen_vl()` |
| ② 查成分表 | `server/data/food_table.json` + `pipeline.lookup()` / `nutrition_of()` |
| ③ 对照月龄标准表 | `server/data/age_standard.json` + `pipeline.band_for()` |
| ④ 算缺口 | `pipeline.aggregate()` → `report.ratios` / `report.overallRatio` |
| ⑤ 正向分支 | `pipeline.prep_advice()` → `advice` |
| ⑤ 负向分支 | `pipeline.safety_checks()` → `safety` |

`server/pipeline.py` 顶部有一段注释说明每一步的输入输出，函数名与架构图一一对应。

---

## 四、两个设计取舍（评委可能会问）

### 1. 为什么前端要「有没有后端都能用」？

因为演示现场的网络和 Python 环境都不受我们控制。所以做成**渐进增强**：
页面本身是完整可用的静态原型，只在 `GET /api/meal/day` 真的返回数据时，
才用后端数值覆盖静态值、并亮出绿色标识。

这段逻辑在 `eat.html` 末尾，只有 100 行左右，三条硬约束写在注释里：

```js
fetch('api/meal/day', { cache: 'no-store' })
  .then(r => r.ok ? r.json() : Promise.reject(r.status))
  .then(d => { if (d && d.report) applyDay(d); })
  .catch(() => { /* 没有后端：保持静态，什么都不做 */ });
```

- `file://` 双击打开、后端没启动、接口 404 —— 三种情况都走 `catch`，**页面零变化**
- 覆盖范围只改文本和内联样式，不动 DOM 结构、不加第二次请求
- 「已连接本地后端」这个绿色标识**本身就是后端在跑的证据**，不是装饰

### 2. 为什么营养报告页是静态的？

首页是「**今天**」，后端有单日数据，所以实时算。
营养报告页是「**本周**整体达标 78%」「近 7 天趋势」「本月 Top 4 食材」这类**聚合视图**，
单日接口喂不了它。我们选择保持静态示例，**而不是拿一天的数据去凑一周的图** ——
演示里出现假数据，比演示不完整更糟。

同理，首页右上角原本是「比昨天 ↑12%」：没有「昨天」的数据源，我们就不编，
改成讲今天的真实缺口「还差 7.0mg 富铁食物」。

### 3. 营养素两态，食物类别三态

蛋白质、铁这类**营养素**只有「够 / 不够」两种状态；
蔬菜、谷薯这类**食物类别**要三态 —— 因为膳食指南给的是**区间**（如 7-12 月龄
蔬菜 25-100 g），吃 110 g 是**超了**，不能报成「未达标」然后推荐「再多吃点蔬菜」。

这条规则在 `pipeline.aggregate()` 里，`status` 取 `low` / `ok` / `over`，
`inRange` 只在 `ok` 时为真。有单测锁住（`test_over_is_not_reported_as_gap`）。

---

## 五、API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 探活 + 数据表来源与条数 |
| GET | `/api/meal/scenarios` | 三个演示场景列表 |
| GET | `/api/meal/day?scenario=&monthAge=` | **主接口**：五项达标率 + 建议 + 安全网 + 新食物观察期 |
| GET | `/api/meal/safety?scenario=` | 只看负向分支 |
| GET | `/api/meal/advice?scenario=` | 只看正向分支 |
| GET | `/api/meal/food?q=猪肝` | 按别名查成分表，附溯源 |
| POST | `/api/meal/recognize` | 菜品/食材/烹饪方式 → 结构化标签 |
| POST | `/api/meal/analyze` | 传自定义三餐跑完整链路 |

`POST /api/meal/analyze` 示例（可以用它现场加一组数据演示）：

```bash
curl -X POST http://127.0.0.1:8115/api/meal/analyze \
  -H 'Content-Type: application/json' \
  -d '{
    "monthAge": 11,
    "meals": [
      {"label":"午餐","time":"12:30","dish":"牛肉末粥",
       "ingredients":["牛肉","粳米","油菜"],"source":"manual"}
    ]
  }'
```

---

## 六、数据来源与口径

| 数据 | 来源 | 文件 |
|---|---|---|
| 食物成分（含钠 mg/100g） | 《中国食物成分表（第6版）》杨月欣 主编 / 北京大学医学出版社 | `server/data/food_table.json` |
| 月龄推荐量、蛋白质 RNI、铁 AI、钠 AI | 《中国居民膳食指南（2022）》7-24 月龄婴幼儿喂养指南；《中国居民膳食营养素参考摄入量（2023 版）》 | `server/data/age_standard.json` |

**关于数据规模，先说清楚**：成分表目前收录 **43 种**常见婴幼儿食材（架构图里写的是
1677 种全量版），是按演示需要挑出来的子集，字段结构与全量版一致 —— 换成全量表
只需替换这个 JSON，代码不用动。

**关于"模拟识别"**：场景里的三餐是预先写好的结构化标签，`source` 字段标记为
`simulated`，页面上会显著标注，不会伪装成模型实时识别结果。要接真实视觉模型，
设一个环境变量即可：

```bash
export QWEN_VL_API_KEY=sk-xxxx        # Windows: set QWEN_VL_API_KEY=sk-xxxx
```

然后在 `POST /api/meal/recognize` 里带上 `"useVision": true` 和 `"imageUrl"`。

---

## 七、目录结构

```
babycare/
├── eat.html                 ← 前端（单文件，无框架、无构建、无 CDN）
├── serve.py                 ← 一条命令启动（自动选 FastAPI / 标准库）
├── screenshot.png
├── requirements.txt         ← 可选依赖，不装也能跑
├── server/
│   ├── api.py               ← 业务逻辑（与 Web 框架无关，两种模式共用）
│   ├── app.py               ← FastAPI 壳（可选）
│   ├── pipeline.py          ← 架构图那 5 步链路，都在这里
│   ├── scenarios.py         ← 3 个演示场景（覆盖三条分支）
│   └── data/
│       ├── food_table.json      ← 43 种食材 + 别名索引
│       └── age_standard.json    ← 3 个月龄档（6-12m / 13-24m / 25-72m）
├── assets/eat/              ← 页面用到的插画与菜品图
└── tests/test_pipeline.py   ← 25 个单元测试
```

## 八、跑测试

```bash
python -m unittest discover -s tests -v
```

25 个用例，覆盖：成分表别名归一、缺口计算、两态/三态达标率、
三个分支的产出、新食物观察期去重、月龄档切换、每条建议都必须带出处。

---

## 九、常见问题

**Q：端口被占用？**
`serve.py` 会自动从 8115 往后试 20 个端口，启动日志里会打印实际端口。
也可以 `--port 9000` 指定。

**Q：想强制用标准库模式？**
`BABYCARE_FORCE_STDLIB=1 python serve.py`。

**Q：页面上的图裂了？**
`assets/eat/` 必须和 `eat.html` 在同级目录。如果你是单独把 `eat.html` 拷出去的，
把 `assets/` 一起拷上。

**Q：怎么接到真实数据？**
`server/data/` 换全量成分表 → 改 `server/pipeline.py` 里的识别函数接视觉模型 →
`server/scenarios.py` 换成真实三餐。三步，`eat.html` 一行都不用改。
