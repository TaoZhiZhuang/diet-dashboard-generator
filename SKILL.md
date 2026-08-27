---
name: diet-dashboard-generator
description: 从饮食/运动/体重 CSV 自动生成一份静态「饮食运动记录看板」HTML。当用户有 diet_log.csv / body_measurements.csv / workout_log.csv 数据文件，想要"生成看板 / 刷新看板 / 记录饮食运动后更新看板"时使用。读 3 个 CSV（+ 可选 看板备注.md）→ 跑 generate_dashboard.py → 输出完整看板（今日饮食面板、6 小卡、4 图表、7 天滚动明细、身体数据表、运动记录板块）。纯本地运行，不依赖服务端。
---

# 饮食运动看板生成器（Diet Dashboard Generator）

把 CSV 记录变成一张深色风格的静态数据看板。改数据 → 跑脚本 → 看板刷新，全程本地，无需任何服务端。

## 项目结构
- `generate_dashboard.py`：生成脚本（Python 3，无第三方依赖）
- `example/`：脱敏示例数据（复制它来建你自己的数据目录）
- 数据目录需包含：`diet_log.csv`、`body_measurements.csv`、`workout_log.csv`、`vendor/`（Chart.js，见 README），可选 `feeling_log.csv`、`看板备注.md`

## 快速开始（3 步）
```bash
# 1. 建数据目录（复制示例后替换成自己的记录）
cp -r example mydata

# 2. 下载 Chart.js 到数据目录（一条命令，见 README 完整说明）
mkdir -p mydata/vendor && curl -L -o mydata/vendor/chart.umd.min.js https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js
curl -L -o mydata/vendor/chartjs-plugin-annotation.min.js https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js

# 3. 生成看板
python3 generate_dashboard.py --data-dir mydata
# 用浏览器打开 mydata/饮食运动看板.html
```

演示（不建数据目录也能看效果）：
```bash
python3 generate_dashboard.py --data-dir example --today 2026-08-27
```

## 数据格式（列顺序固定，别改表头）

**diet_log.csv**（每行一条食物）：
`日期,餐次,食物,份量,热量kcal,碳水g,蛋白质g,脂肪g,膳食纤维g,备注`
- 餐次取值：早餐 / 午餐 / 晚餐 / 加餐（顺序固定；加餐多行自动合并为一条）
- 份量可留空

**body_measurements.csv**（每行一次实测）：
`日期,体重kg,体脂率%,备注`
- 备注必须包含 `BMI xx.x`、`肌肉量 xx.xkg`、`基础代谢 xxxx`（脚本从备注正则解析；"肌肉"或"肌肉量"均可）

**workout_log.csv**：
`日期,运动类型,时长分钟,强度,备注`
- 消耗 kcal 解析顺序：备注里 `X大卡` → `按X记` → 否则 `时长×10`
- 强度列当前未使用，可填中/低/高

**feeling_log.csv**（可选，感受板块数据源）：
`日期,时间,类型,强度,原话`
- 类型：`饿` / `嘴馋` / `运动感受`（运动后即时体感）/ `运动恢复`（次日恢复）/ `情绪` / `生理` / `其他`
- 强度：饿→`饿/有点饿/无`；运动感受→`累/有点累/正常`；运动恢复→`好/一般/差`；嘴馋→`强/中/弱`（3 档）
- 看板「感受记录」板块统计**近 14 天**：
  - 饿感频次：只统计明确记录的（饿/有点饿/无），并给出"有饿感 X/14 天"
  - 运动体感 / 次日恢复：**以"近 14 天运动次数"为分母**，运动日没写感受记录的**默认按"正常"计**（累/恢复差才会说），口径更接近真实
- 明细列出最近记录；文件缺失或为空时板块显示"暂无记录"不报错

**看板备注.md**（可选）：`## 未完日期` / `## 今日额外说明`（`M/D: 内容` 格式，仅展示窗口内生效，滚出自动忽略）/ `## 明细底部说明` / `## 身体数据脚注` / `## 运动脚注`

## 配置（改 generate_dashboard.py 顶部 CONFIG）
```python
CONFIG = {
    'title': '饮食运动记录看板',   # 页面标题
    'cal_lo': 1500, 'cal_hi': 1600,  # 每日热量目标区间
    'protein_target': 110,        # 每日蛋白质目标 g
    'fiber_text': '25-30g',       # 纤维目标（文案）
    'ratio_targets': {            # 供能比目标区间 %
        '碳水': (45, 50), '蛋白质': (25, 28), '脂肪': (20, 25),
    },
}
```

## 内置规则
- **展示窗口（CSV 全量留存，看板只是窗口投影）**：每日明细表/热量/供能比/蛋白图表 = 最近 **10 天**；身体数据表/体重体脂BMI趋势图 = 最近 **30 天**（一个月）；运动记录 = 近 **14 天**（两周）；感受板块 = 近 **14 天**（两周）。**所有历史数据始终保留在 CSV 里，不会因窗口滚动丢失**
- 热量条形色：>=上限 蓝 / 区间内 绿 / 1000-下限 琥珀 / <1000 红
- 体重/体脂/BMI：升红降绿持平灰；肌肉/基础代谢箭头恒蓝
- 运动新鲜度：0/1天灰、2天绿、3天黄、4天红、5天+紫
- 图表目标区间用 box 型 annotation（不依赖日期，天然防滚动错位）
- 供能比 = 碳4+蛋4+脂9 能量分母
- 感受板块统计窗口 = 近 14 天（`FEEL_WINDOW_DAYS` 可改）

## 日常更新节奏（省积分版）
- **硬数据（饮食/体重/运动）每次更新后跑一次脚本**刷新看板
- **感受记录攒着不单独刷**：写入 feeling_log.csv 后不立即跑脚本，等下次硬数据更新时顺带刷新（建议每天固定一次，如早上第一餐后）
- 这样一天通常只跑 1-2 次脚本，看板永远是最新的，积分消耗最小

## 常见问题
- **今日面板显示"今日暂无记录"**：数据里没有"今天"的日期。示例数据是固定历史日期，跑示例请加 `--today 2026-08-27`；你自己的数据里包含今天日期即可
- **图表区域空白**：数据目录缺 `vendor/`（Chart.js），按快速开始下载
- **身体表肌肉/代谢为空**：body_measurements.csv 备注里没写 BMI/肌肉量/基础代谢，或备注里用了 ASCII 逗号 `,`（会列错位，一律用中文逗号 `，`）
- **运动消耗异常**：workout_log 备注里写 `X大卡` 或 `按X记` 才会被解析，否则按时长×10 估
