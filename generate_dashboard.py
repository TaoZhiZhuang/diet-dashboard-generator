#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
饮食运动看板生成器（通用版 · 公共开源）

读数据目录下的 diet_log.csv、body_measurements.csv、workout_log.csv
（+ 可选 看板备注.md），自动生成一份纯静态的「饮食运动记录看板」HTML。
不依赖任何服务端，改数据 → 跑脚本 → 看板刷新。

日常使用：
    1. 准备数据目录（含 3 个 CSV，格式见 README / example/）
    2. 跑：python3 generate_dashboard.py --data-dir <你的数据目录>
    3. 用浏览器打开生成的 饮食运动看板.html（需先把 vendor/ 放好，见 README）

演示示例：
    python3 generate_dashboard.py --data-dir example --today 2026-08-27

内置规则（改 CONFIG 可适配你的目标）：
- 餐段顺序：早餐→午餐→晚餐→加餐；加餐多行合并成一条
- 每日明细只显示最近 7 天（滚动）
- 热量条形色：>= 上限 蓝 / 区间内 绿 / 1000-下限 琥珀 / <1000 红
- 体重/体脂/BMI：升红降绿持平灰；肌肉/基础代谢箭头恒蓝
- 运动新鲜度：0/1天灰、2天绿、3天黄(#d8c232)、4天红、5天+紫
- 图表目标区间用 box 型 annotation，不依赖日期锚点
- 供能比 = 碳4+蛋4+脂9 能量分母，各自四舍五入
"""

import argparse
import csv
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

# ============================================================
# 用户配置：改这里即可适配你自己的目标
# ============================================================
CONFIG = {
    'title': '饮食运动记录看板',          # 页面标题
    'cal_lo': 1500,                       # 每日热量目标下限 kcal
    'cal_hi': 1600,                       # 每日热量目标上限 kcal
    'protein_target': 110,                # 每日蛋白质目标 g
    'fiber_text': '25-30g',               # 膳食纤维目标（文案）
    'ratio_targets': {                    # 供能比目标区间（%）
        '碳水': (45, 50),
        '蛋白质': (25, 28),
        '脂肪': (20, 25),
    },
}
# ============================================================

# 配色（与看板 CSS 一致；一般不用改）
GREEN, GREEN2, RED, AMBER, BLUE = '#4caf7d', '#2e7d5b', '#e0564f', '#e0a24f', '#5b8def'
YELLOW, PURPLE = '#d8c232', '#b47def'
GRID_COLOR = '#23262a'

MEAL_ORDER = ['早餐', '午餐', '晚餐', '加餐']
MAIN_MEALS = ['早餐', '午餐', '晚餐']
WINDOW_DAYS = 10           # 常规数据窗口（每日明细表 + 热量/供能比/蛋白图表）：7-10 天
BODY_WINDOW_DAYS = 30      # 身体数据窗口（身体数据表 + 体重体脂BMI趋势图）：一个月
WORKOUT_WINDOW_DAYS = 14   # 运动记录窗口：两周
FEEL_WINDOW_DAYS = 14      # 感受板块统计窗口（天）
CAL_LO = CONFIG['cal_lo']
CAL_HI = CONFIG['cal_hi']


# ---------------- 基础工具 ----------------
def read_csv(path):
    """读取 CSV 为 list[dict]。对最后一行（备注）若混入 ASCII 逗号导致列错位，自动把多余列合并回备注列，防止静默丢列。"""
    with open(path, encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = [h.strip() for h in next(reader)]
        rows = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            row = list(row)
            if len(row) > len(header):
                # 备注里的 ASCII 逗号把列拆开了，合并回最后一列
                row[len(header) - 1] = '，'.join(row[len(header) - 1:])
                row = row[:len(header)]
            elif len(row) < len(header):
                row += [''] * (len(header) - len(row))
            rows.append(dict(zip(header, row)))
        return rows


def pdate(s):
    return datetime.strptime(s.strip(), '%Y-%m-%d').date()


def md(d):
    return f"{d.month}/{d.day}"


def md0(d):
    """身体数据专用：日补零，如 8/02"""
    return f"{d.month}/{d.day:02d}"


def fnum(x, digits=1):
    """57.0 -> '57'；14.6 -> '14.6'"""
    if x is None:
        return ''
    s = f"{float(x):.{digits}f}"
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s


def fnum1(x):
    """固定 1 位小数：93.0 / 45.0（身体数据表用）"""
    if x is None:
        return ''
    return f"{float(x):.1f}"


def esc(s):
    return html.escape(str(s), quote=False)


def ratio_pct(c, p, f):
    """供能比 %：碳4+蛋4+脂9 能量分母，四舍五入"""
    te = c * 4 + p * 4 + f * 9
    if te <= 0:
        return 0, 0, 0
    return round(c * 4 / te * 100), round(p * 4 / te * 100), round(f * 9 / te * 100)


def cal_color(kcal):
    if kcal >= CAL_HI:
        return BLUE
    if kcal >= CAL_LO:
        return GREEN
    if kcal >= 1000:
        return AMBER
    return RED


def ratio_tag(pct, lo, hi, tol=5):
    """供能比标签：目标区间±5 内 ok(绿)，超 hi(红)，低 low(琥珀)"""
    if pct > hi + tol:
        return 'hi'
    if pct < lo - tol:
        return 'low'
    return 'ok'


def short_meal(m):
    return {'早餐': '早', '午餐': '午', '晚餐': '晚'}[m]


def delta_text(cur, prev, unit, digits=1):
    d = round(cur - prev, digits)
    if d > 0:
        return f"↑ {fnum(d, digits)} {unit}".strip(), 'bad'
    if d < 0:
        return f"↓ {fnum(-d, digits)} {unit}".strip(), 'good'
    return '持平', 'flat'


def auto_range(vals):
    """bodyChart 轴范围：按数据自动留 padding；无数据返回空"""
    vals = [v for v in vals if v is not None]
    if len(vals) < 1:
        return ''
    lo, hi = min(vals), max(vals)
    if hi == lo:
        pad = max(abs(lo) * 0.05, 0.5)
        return f"min:{lo - pad:.1f}, max:{hi + pad:.1f}"
    pad = (hi - lo) * 0.15
    return f"min:{lo - pad:.1f}, max:{hi + pad:.1f}"


# ---------------- 数据加载 ----------------
def load_diet(diet_csv):
    by_day = defaultdict(lambda: defaultdict(list))
    for r in read_csv(diet_csv):
        try:
            d = pdate(r['日期'])
        except (ValueError, KeyError):
            continue
        meal = (r.get('餐次') or '').strip()
        if not meal:
            continue
        by_day[d][meal].append({
            'food': (r.get('食物') or '').strip(),
            'amt': (r.get('份量') or '').strip(),
            'kcal': float(r.get('热量kcal') or 0),
            'c': float(r.get('碳水g') or 0),
            'p': float(r.get('蛋白质g') or 0),
            'f': float(r.get('脂肪g') or 0),
            'fib': float(r.get('膳食纤维g') or 0),
        })
    days = {}
    for d, meals in by_day.items():
        all_rows = [x for rows in meals.values() for x in rows]
        days[d] = {
            'meals': {k: list(v) for k, v in meals.items()},
            'kcal': sum(x['kcal'] for x in all_rows),
            'c': sum(x['c'] for x in all_rows),
            'p': sum(x['p'] for x in all_rows),
            'f': sum(x['f'] for x in all_rows),
            'fib': sum(x['fib'] for x in all_rows),
        }
    return days


def load_body(body_csv):
    """容错读取：备注里若混入 ASCII 逗号，多余列合并回备注（防止列错位丢数据）"""
    rows = []
    with open(body_csv, encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader)
        for row in reader:
            if not row or not row[0].strip():
                continue
            row = list(row)
            if len(row) > len(header):
                row[3] = '，'.join(row[3:])
                row = row[:len(header)]
            try:
                d = pdate(row[0])
            except ValueError:
                continue
            note = row[3] if len(row) > 3 else ''
            m_bmi = re.search(r'BMI\s*([\d.]+)', note)
            m_mus = re.search(r'肌肉量?\s*([\d.]+)', note)
            m_bmr = re.search(r'基础代谢\s*([\d.]+)', note)
            rows.append({
                'date': d,
                'weight': float(row[1]),
                'fat': float(row[2]),
                'bmi': float(m_bmi.group(1)) if m_bmi else None,
                'muscle': float(m_mus.group(1)) if m_mus else None,
                'bmr': float(m_bmr.group(1)) if m_bmr else None,
            })
    rows.sort(key=lambda x: x['date'])
    return rows


def load_workout(workout_csv):
    rows = []
    for r in read_csv(workout_csv):
        try:
            d = pdate(r['日期'])
        except (ValueError, KeyError):
            continue
        note = r.get('备注', '') or ''
        m1 = re.search(r'(\d+(?:\.\d+)?)\s*大卡', note)
        m2 = re.search(r'按\s*(\d+)\s*记', note)
        if m1:
            kcal = float(m1.group(1))
        elif m2:
            kcal = float(m2.group(1))
        else:
            kcal = round(float(r['时长分钟']) * 10)
        rows.append({
            'date': d,
            'type': (r.get('运动类型') or '').strip(),
            'mins': int(float(r['时长分钟'])),
            'kcal': kcal,
            'note': note,
        })
    rows.sort(key=lambda x: x['date'])
    return rows


def load_notes(notes_md):
    out = {'unfinished': {}, 'today_extra': {}, 'bottom': '',
           'body_note': '', 'workout_note': ''}
    if not os.path.exists(notes_md):
        return out
    section = None
    with open(notes_md, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('## '):
                section = line[3:].strip()
                continue
            if line.startswith('#'):
                continue
            if section in ('未完日期', '今日额外说明'):
                m = re.match(r'(\d{1,2})/(\d{1,2}):\s*(.*)', line)
                if m:
                    try:
                        key = date(TODAY.year, int(m.group(1)), int(m.group(2)))
                    except ValueError:
                        continue
                    (out['unfinished'] if section == '未完日期'
                     else out['today_extra'])[key] = m.group(3)
            elif section == '明细底部说明':
                out['bottom'] = line
            elif section == '身体数据脚注':
                out['body_note'] = line
            elif section == '运动脚注':
                out['workout_note'] = line
    return out


def load_feelings(feel_csv):
    """读取结构化感受记录 feeling_log.csv（可选数据源，缺失不报错）。
    列：日期,时间,类型,强度,原话
    类型：饿 / 嘴馋 / 运动感受（即时体感）/ 运动恢复（次日）/ 情绪 / 生理 / 其他
    强度：饿→饿/有点饿/无；运动感受→累/有点累/正常；运动恢复→好/一般/差；嘴馋→中/强
    返回按日期+时间倒序的 list[dict]。
    """
    if not os.path.exists(feel_csv):
        return []
    rows = []
    for r in read_csv(feel_csv):
        try:
            d = pdate(r['日期'])
        except (ValueError, KeyError):
            continue
        t = (r.get('时间') or '').strip()
        rows.append({
            'date': d,
            'time': t,
            'type': (r.get('类型') or '').strip(),
            'level': (r.get('强度') or '').strip(),
            'note': (r.get('原话') or '').strip(),
        })
    rows.sort(key=lambda x: (x['date'], x['time']), reverse=True)
    return rows


# ---------------- 派生计算 ----------------
def build_unfinished(days, notes):
    """未完判定：备注文件指定 ∪ 今天且主餐不足"""
    unfin = {}
    for d, reason in notes['unfinished'].items():
        if d in days:
            unfin[d] = reason
    if TODAY in days:
        present = [m for m in MAIN_MEALS if m in days[TODAY]['meals']]
        if len(present) < 3:
            unfin[TODAY] = '仅' + '、'.join(present)
    return unfin


def verdict_tag(d, kcal, meals, unfinished):
    if d in unfinished:
        return '未完†', 'low'
    missing = [m for m in MAIN_MEALS if m not in meals]
    if d != TODAY and missing:
        base = ('超量*' if kcal >= CAL_HI
                else ('接近*' if kcal >= CAL_LO else '偏低*'))
        return base, ('ok' if '接近' in base else 'low')
    if kcal >= CAL_HI:
        return '超量', 'low'
    if kcal >= CAL_LO:
        return '接近', 'ok'
    return '偏低', 'low'


# ---------------- HTML 片段 ----------------
def meal_lines_html(rows):
    return '、'.join(
        f"{r['food']} {r['amt']}" if r['amt'] else r['food'] for r in rows)


def target_note_text():
    rt = CONFIG['ratio_targets']
    c, p, f = rt['碳水'], rt['蛋白质'], rt['脂肪']
    return (f"目标：碳{c[0]}-{c[1]} / 蛋白{p[0]}-{p[1]} / 脂{f[0]}-{f[1]}"
            f" / 纤维{CONFIG['fiber_text']}")


def render_today_panel(days, unfinished, notes):
    d = TODAY
    if d not in days:
        return '<div class="note">今日暂无记录</div>'
    day = days[d]
    left = []
    for meal in MEAL_ORDER:
        if meal not in day['meals']:
            continue
        rows = day['meals'][meal]
        kcal = sum(r['kcal'] for r in rows)
        left.append(
            f'<div class="mealhead"><span>{meal}（{int(kcal)} kcal）</span></div>')
        left.append(f'<div class="meal"><span>{esc(meal_lines_html(rows))}</span></div>')
    left_html = '\n'.join(left)

    cp, pp, fp = ratio_pct(day['c'], day['p'], day['f'])
    rt = CONFIG['ratio_targets']
    rows = []
    for name, gram, kcal, pct, (lo, hi) in [
        ('碳水', day['c'], day['c'] * 4, cp, rt['碳水']),
        ('蛋白质', day['p'], day['p'] * 4, pp, rt['蛋白质']),
        ('脂肪', day['f'], day['f'] * 9, fp, rt['脂肪']),
    ]:
        cls = ratio_tag(pct, lo, hi)
        rows.append(
            f'<tr><td>{name}</td><td>{fnum(gram)}g</td><td>{int(round(kcal))} kcal</td>'
            f'<td><span class="tag {cls}">{pct}%</span></td></tr>')
    table_html = '\n'.join(rows)

    note_parts = [target_note_text()]
    if d in unfinished:
        present = [m for m in MAIN_MEALS if m in day['meals']]
        missing = [m for m in MAIN_MEALS if m not in present]
        if missing:
            note_parts.append('待' + ''.join(short_meal(m) for m in missing) + '餐')
    if d in notes['today_extra']:
        note_parts.append(notes['today_extra'][d])

    return f'''
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start">
      <div>
        {left_html}
      </div>
      <div>
        <div class="sumrow" style="border-top:none;padding-top:0;margin-top:0">
          <div><div class="k">总热量</div><b>{int(day["kcal"])} kcal</b></div>
          <div><div class="k">膳食纤维</div><b>{fnum(day["fib"])}g</b></div>
        </div>
        <table style="margin-top:8px">
          <thead><tr><th></th><th>克数</th><th>供能</th><th>比例</th></tr></thead>
          <tbody>
            {table_html}
          </tbody>
        </table>
        <div class="note" style="margin-top:8px">{esc(" · ".join(note_parts))}</div>
      </div>
    </div>'''


def render_cards(days, body, workouts):
    # 运动消耗卡
    if workouts:
        w = workouts[-1]
        diff = (TODAY - w['date']).days
        if diff == 0:
            txt, cls = '今天', 'flat'
        elif diff == 1:
            txt, cls = f"{md(w['date'])}（昨天）", 'flat'
        else:
            txt = f"{md(w['date'])}（{diff}天前）"
            cls = {0: 'flat', 1: 'flat', 2: 'good', 3: 'warn', 4: 'bad'}.get(diff, 'purple')
        if cls == 'warn':
            bt_style = ' style="color:var(--bright-yellow)"'
        elif cls == 'purple':
            bt_style = ' class="bt purple"'
        else:
            bt_style = f' class="bt {cls}"'
        workout_html = (f'<div class="bcard"><div class="bk">运动消耗</div>'
                        f'<div><span class="bv">{int(w["kcal"])}</span>'
                        f'<span class="bu">kcal</span></div>'
                        f'<div{bt_style}>{esc(txt)}</div></div>')
    else:
        workout_html = ('<div class="bcard"><div class="bk">运动消耗</div>'
                        '<div><span class="bv">—</span></div>'
                        '<div class="bt flat">暂无记录</div></div>')

    def body_card(label, key, unit, blue=False, digits=1):
        latest = body[-1]
        prev = body[-2] if len(body) >= 2 else None
        val = latest[key]
        if prev is None or prev[key] is None or val is None:
            bt = '<div class="bt flat">—</div>'
        else:
            txt, cls = delta_text(val, prev[key], unit, digits)
            if blue:
                bt = f'<div class="bt" style="color:var(--blue)">{txt}</div>'
            else:
                bt = f'<div class="bt {cls}">{txt}</div>'
        unit_html = f'<span class="bu">{unit}</span>' if unit else ''
        return (f'<div class="bcard"><div class="bk">{label}</div>'
                f'<div><span class="bv">{fnum(val, digits)}</span>{unit_html}</div>{bt}</div>')

    cards = [workout_html]
    if body:
        cards.append(body_card('体重', 'weight', 'kg'))
        cards.append(body_card('体脂率', 'fat', ''))
        cards.append(body_card('BMI', 'bmi', ''))
        cards.append(body_card('肌肉量', 'muscle', 'kg', blue=True))
        cards.append(body_card('基础代谢', 'bmr', 'kcal', blue=True))
    else:
        cards += ['<div class="bcard"><div class="bk">体重</div><div><span class="bv">—</span></div></div>'] * 5

    return '\n'.join(f'    {c}' for c in cards)


def render_daily_table(days, window, unfinished):
    rows = []
    for d in window:
        day = days.get(d)
        if day is None:
            continue
        kcal, c, p, f = day['kcal'], day['c'], day['p'], day['f']
        cp, pp, fp = ratio_pct(c, p, f)
        vt, vcls = verdict_tag(d, kcal, day['meals'], unfinished)
        rows.append(
            f'<tr><td>{md(d)}</td><td>{int(kcal)}</td><td>{fnum(c)}</td>'
            f'<td>{fnum(p)}</td><td>{fnum(f)}</td><td>{cp}:{pp}:{fp}</td>'
            f'<td><span class="tag {vcls}">{vt}</span></td></tr>')
    return '\n'.join(rows)


def render_daily_note(days, window, unfinished, notes):
    parts = []
    for d in window:
        if d == TODAY or d not in days:
            continue
        day = days[d]
        missing = [m for m in MAIN_MEALS if m not in day['meals']]
        if missing:
            parts.append(f"* {md(d)} 缺{'、'.join(missing)}")
    for d in window:
        if d in unfinished:
            reason = notes['unfinished'].get(d) or f"仅{'、'.join(m for m in MAIN_MEALS if m in days[d]['meals'])}"
            prefix = '† ' if d != TODAY else ''
            parts.append(f"{prefix}{md(d)} {reason}")
    if notes['bottom']:
        parts.append(notes['bottom'])
    parts.append(f"{WINDOW_DAYS}天明细滚动到 {md(window[0])}-{md(window[-1])}。")
    return esc('；'.join(parts))


def render_body_table(body, notes):
    """身体数据表：只展示最近 BODY_WINDOW_DAYS 天（完整历史在 CSV，不丢）"""
    if not body:
        return '', ''
    cutoff = body[-1]['date'] - timedelta(days=BODY_WINDOW_DAYS)
    win = [r for r in body if r['date'] >= cutoff]
    rows = []
    for r in win:
        rows.append(
            f'<tr><td>{md0(r["date"])}</td><td>{fnum1(r["weight"])}</td>'
            f'<td>{fnum1(r["fat"])}</td><td>{fnum1(r["muscle"])}</td>'
            f'<td>{fnum(r["bmr"], 0)}</td></tr>')
    table_html = '\n'.join(rows)

    ws = [r['weight'] for r in win]
    fs = [r['fat'] for r in win]
    bs = [r['bmr'] for r in win if r['bmr'] is not None]
    note = (f"{md0(win[0]['date'])}-{md0(win[-1]['date'])} 近{BODY_WINDOW_DAYS}天 {len(win)}次实测；"
            f"区间内体重 {min(ws)}-{max(ws)}kg、体脂 {min(fs)}-{max(fs)}%、"
            f"基础代谢稳定 {min(bs):.0f}-{max(bs):.0f}。")
    have = {r['date'] for r in win}
    missing = []
    d = win[0]['date'] + timedelta(days=1)
    while d <= win[-1]['date']:
        if d not in have:
            missing.append(md(d))
        d += timedelta(days=1)
    if missing:
        note += f"{'、'.join(missing)} 无称重记录。"
    if notes['body_note']:
        note += notes['body_note']
    note += '（完整历史在 body_measurements.csv，看板只展示最近一个月）'
    return table_html, esc(note)


def render_workout_table(workouts, notes):
    """运动记录板块：只展示最近 WORKOUT_WINDOW_DAYS 天（完整历史在 CSV）"""
    if not workouts:
        return '暂无记录', '', esc('在 workout_log.csv 记录运动。')
    cutoff = TODAY - timedelta(days=WORKOUT_WINDOW_DAYS)
    win = [w for w in workouts if w['date'] >= cutoff]
    if not win:
        return (f'近{WORKOUT_WINDOW_DAYS}天暂无记录',
                '<tr><td colspan="5" style="color:var(--dim)">近两周暂无运动记录</td></tr>',
                esc(f'完整历史在 workout_log.csv（最近一次：{md(workouts[-1]["date"])}）。'))
    rows = []
    for w in win:
        rows.append(
            f'<tr><td>{md(w["date"])}</td><td>{esc(w["type"])}</td>'
            f'<td>{w["mins"]}</td><td>{int(w["kcal"])}</td><td>{esc(w["note"])}</td></tr>')
    table_html = '\n'.join(rows)
    total_mins = sum(w['mins'] for w in win)
    total_kcal = int(sum(w['kcal'] for w in win))
    head = (f'近{WORKOUT_WINDOW_DAYS}天 {len(win)} 次 · {md(win[0]["date"])}-{md(win[-1]["date"])}'
            f' · 总时长 {total_mins} 分钟 · 总消耗 {total_kcal} kcal')
    note = notes['workout_note'] or '频次见记录间隔。'
    note += f'（完整历史在 workout_log.csv）'
    return head, table_html, esc(note)


def _count_lv(items):
    """按档位计数的固定顺序输出，如 {'累':1,'有点累':2,'正常':0} → '累 1 · 有点累 2 · 正常 0'"""
    order = ['饿', '有点饿', '无', '累', '有点累', '正常', '好', '一般', '差', '强', '中', '弱']
    cnt = defaultdict(int)
    for it in items:
        lv = it['level'] or '-'
        cnt[lv] += 1
    parts = [f"{lv} {cnt[lv]}" for lv in order if lv in cnt]
    return ' · '.join(parts) if parts else '暂无'


def render_feelings_panel(feelings, workouts):
    """感受板块：近 FEEL_WINDOW_DAYS 天 饿感频次 + 运动体感（即时）+ 次日恢复 + 最近明细。
    统计口径（用户规则 2026-08-27）：运动体感/次日恢复以"近窗口内运动次数"为分母，
    没提的就是正常/不累（累才会说）；饿/馋只统计有记录的部分。
    """
    cutoff = TODAY - timedelta(days=FEEL_WINDOW_DAYS)
    recent = [r for r in feelings if r['date'] >= cutoff]
    recent_w = [w for w in workouts if w['date'] >= cutoff]
    w_dates = sorted({w['date'] for w in recent_w})

    if not feelings and not workouts:
        return ('    <div class="panel"><h2>感受记录<span>暂无记录</span></h2>'
                '<div class="note">在 feeling_log.csv 记录饿感/嘴馋/运动体感/次日恢复等，'
                '攒批落盘后自动上板。</div></div>')

    # 饿感：只统计明确记录的（饿/有点饿/无）
    hungry = [r for r in recent if r['type'] == '饿']
    hungry_days = len({r['date'] for r in hungry})

    # 运动体感（即时）：按运动当天匹配；没记录的默认"正常"
    feel_map = defaultdict(list)
    for r in recent:
        if r['type'] == '运动感受':
            feel_map[r['date']].append(r['level'])
    # 次日恢复：按运动次日（date+1）匹配；没记录的默认"正常"
    rec_map = defaultdict(list)
    for r in recent:
        if r['type'] == '运动恢复':
            rec_map[r['date']].append(r['level'])
    # 分母：workout_log 运动日 ∪ 有体感记录的日期 ∪ 有恢复记录的日期-1（防止只报感受没报运动行）
    feel_days = set(w_dates)
    feel_days |= set(feel_map.keys())
    feel_days |= {d - timedelta(days=1) for d in rec_map}
    feel_days = sorted(feel_days)
    n_moves = len(feel_days)

    feel_cnt = Counter()
    for d in feel_days:
        feel_cnt[feel_map[d][0] if feel_map[d] else '正常'] += 1
    rec_cnt = Counter()
    for d in feel_days:
        rec_cnt[rec_map[d + timedelta(days=1)][0] if rec_map[d + timedelta(days=1)] else '正常'] += 1

    blocks = []
    blocks.append(
        f'<div class="feel-block"><div class="k">饿感频次</div>'
        f'<div class="feel-val">{esc(_count_lv(hungry))}</div>'
        f'<div class="note">近{FEEL_WINDOW_DAYS}天有饿感 {hungry_days}/{FEEL_WINDOW_DAYS} 天</div></div>')
    feel_items = [{'level': k} for k in feel_cnt for _ in range(feel_cnt[k])]
    rec_items = [{'level': k} for k in rec_cnt for _ in range(rec_cnt[k])]
    blocks.append(
        f'<div class="feel-block"><div class="k">运动体感（即时）</div>'
        f'<div class="feel-val">{esc(_count_lv(feel_items))}</div>'
        f'<div class="note">近{FEEL_WINDOW_DAYS}天运动 {n_moves} 次 · 没提的按正常计</div></div>')
    blocks.append(
        f'<div class="feel-block"><div class="k">次日恢复</div>'
        f'<div class="feel-val">{esc(_count_lv(rec_items))}</div>'
        f'<div class="note">近{FEEL_WINDOW_DAYS}天运动 {n_moves} 次 · 次日没说的默认正常</div></div>')

    detail = feelings[:8]
    d_rows = []
    for r in detail:
        note = r['note'] if len(r['note']) <= 36 else r['note'][:36] + '…'
        d_rows.append(
            f'<tr><td>{md0(r["date"])}</td><td>{esc(r["time"])}</td>'
            f'<td>{esc(r["type"])}</td><td>{esc(r["level"])}</td>'
            f'<td>{esc(note)}</td></tr>')
    d_html = '\n'.join(d_rows)

    return f'''    <div class="panel">
      <h2>感受记录<span>近{FEEL_WINDOW_DAYS}天 · 饿感 / 运动体感 / 次日恢复</span></h2>
      <div class="feel-grid">
        {''.join(blocks)}
      </div>
      <table>
        <thead><tr><th>日期</th><th>时间</th><th>类型</th><th>强度</th><th>原话</th></tr></thead>
        <tbody>
        {d_html}
        </tbody>
      </table>
      <div class="note">感受类攒批落盘（feeling_log.csv 结构化档位 + feeling_notes.csv 详细原话），按近{FEEL_WINDOW_DAYS}天统计。</div>
    </div>'''


# ---------------- 图表 JS ----------------
def build_chart_data(days, window, body):
    labels = [md(d) for d in window]
    cal = [int(days[d]['kcal']) for d in window]
    cal_colors = [cal_color(v) for v in cal]
    ratios = [ratio_pct(days[d]['c'], days[d]['p'], days[d]['f']) for d in window]
    pro = [round(days[d]['p'], 1) for d in window]
    # 身体趋势图：与身体数据表一致，只展示最近 BODY_WINDOW_DAYS 天
    if body:
        cutoff = body[-1]['date'] - timedelta(days=BODY_WINDOW_DAYS)
        body_win = [r for r in body if r['date'] >= cutoff]
    else:
        body_win = []
    body_labels = [md0(r['date']) for r in body_win]
    return {
        'labels': labels, 'cal': cal, 'cal_colors': cal_colors,
        'rc': [r[0] for r in ratios], 'rp': [r[1] for r in ratios],
        'rf': [r[2] for r in ratios], 'pro': pro,
        'body_labels': body_labels,
        'bw': [r['weight'] for r in body_win],
        'bf': [r['fat'] for r in body_win],
        'bb': [r['bmi'] if r['bmi'] is not None else None for r in body_win],
        'y0': auto_range([r['weight'] for r in body_win]),
        'y1': auto_range([r['fat'] for r in body_win]),
        'y2': auto_range([r['bmi'] if r['bmi'] is not None else None for r in body_win]),
    }


JS_TEMPLATE = """const C = {green:'#4caf7d', red:'#e0564f', amber:'#e0a24f', blue:'#5b8def', gray:'#3a3d42', text:'#9aa0a6'};
Chart.defaults.color = C.text;
Chart.defaults.borderColor = '#2e3136';

const days = __LABELS__;

new Chart(document.getElementById('calChart'), {
  type:'bar',
  data:{labels:days, datasets:[{
    label:'热量 kcal', data:__CAL__, backgroundColor:__CAL_COLORS__,
    borderRadius:6, barThickness:26
  }]},
  options:{plugins:{legend:{display:false},
    tooltip:{callbacks:{label:(c)=>' '+c.parsed.y+' kcal'}},
    annotation:{annotations:{
      targetBox:{type:'box', yMin:__CAL_LO__, yMax:__CAL_HI__, backgroundColor:'rgba(76,175,125,0.10)', borderColor:'rgba(76,175,125,0.45)', borderWidth:1, drawTime:'beforeDatasetsDraw'}
    }}},
    scales:{y:{beginAtZero:true, grid:{color:'#23262a'}}}}
});

new Chart(document.getElementById('proChart'), {
  type:'line',
  data:{labels:days, datasets:[{
    label:'蛋白 g', data:__PRO__,
    borderColor:C.green, backgroundColor:'rgba(76,175,125,.12)', fill:true, tension:.3,
    pointRadius:4, pointBackgroundColor:C.green
  }]},
  options:{plugins:{legend:{display:false},
    tooltip:{callbacks:{label:(c)=>' '+c.parsed.y+' g'}}},
    scales:{y:{beginAtZero:true, grid:{color:'#23262a'}}}}
});

new Chart(document.getElementById('ratioChart'), {
  type:'bar',
  data:{labels:days, datasets:[
    {label:'碳水 %', data:__RC__, backgroundColor:'#5b8def', stack:'s', barThickness:26},
    {label:'蛋白 %', data:__RP__, backgroundColor:'#4caf7d', stack:'s', barThickness:26},
    {label:'脂肪 %', data:__RF__, backgroundColor:'#e0a24f', stack:'s', barThickness:26}
  ]},
  options:{scales:{x:{stacked:true}, y:{stacked:true, beginAtZero:true, max:100, grid:{color:'#23262a'}}},
    plugins:{legend:{position:'bottom', labels:{boxWidth:12}}}}
});

new Chart(document.getElementById('bodyChart'), {
  type:'line',
  data:{labels:__BODY_LABELS__, datasets:[
    {label:'体重 kg', data:__BW__, borderColor:C.green, yAxisID:'y', tension:.3, pointRadius:4},
    {label:'体脂率 %', data:__BF__, borderColor:C.amber, yAxisID:'y1', tension:.3, pointRadius:4, borderDash:[5,4]},
    {label:'BMI', data:__BB__, borderColor:C.blue, yAxisID:'y2', tension:.3, pointRadius:4, borderDash:[2,3]}
  ]},
  options:{plugins:{legend:{position:'bottom', labels:{boxWidth:12}}},
    scales:{
      y:{position:'left', grid:{color:'#23262a'}, __Y0__},
      y1:{position:'right', grid:{display:false}, __Y1__},
      y2:{position:'right', grid:{display:false}, __Y2__}
    }}
});
"""


def build_js(chart):
    return (JS_TEMPLATE
            .replace('__LABELS__', json.dumps(chart['labels']))
            .replace('__CAL__', json.dumps(chart['cal']))
            .replace('__CAL_COLORS__', json.dumps(chart['cal_colors']))
            .replace('__PRO__', json.dumps(chart['pro']))
            .replace('__RC__', json.dumps(chart['rc']))
            .replace('__RP__', json.dumps(chart['rp']))
            .replace('__RF__', json.dumps(chart['rf']))
            .replace('__CAL_LO__', str(CAL_LO))
            .replace('__CAL_HI__', str(CAL_HI))
            .replace('__BODY_LABELS__', json.dumps(chart['body_labels']))
            .replace('__BW__', json.dumps(chart['bw']))
            .replace('__BF__', json.dumps(chart['bf']))
            .replace('__BB__', json.dumps(chart['bb']))
            .replace('__Y0__', chart['y0'])
            .replace('__Y1__', chart['y1'])
            .replace('__Y2__', chart['y2']))


# ---------------- 页面骨架 ----------------
CSS = """:root{
  --bg:#111214; --card:#1a1c1f; --card2:#222529; --line:#2e3136;
  --text:#e8eaed; --muted:#9aa0a6; --dim:#6b7075;
  --green:#4caf7d; --green2:#2e7d5b; --red:#e0564f; --amber:#e0a24f; --blue:#5b8def;
  --bright-yellow:#d8c232; --purple:#b47def;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding:24px;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:22px;font-weight:600;letter-spacing:.5px}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.panel h2{font-size:15px;font-weight:600;margin-bottom:12px}
.panel h2 span{font-weight:400;color:var(--muted);font-size:12px;margin-left:8px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.chartbox{background:var(--card2);border-radius:10px;padding:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500;font-size:12px}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;margin-right:4px}
.tag.ok{background:rgba(76,175,125,.15);color:var(--green)}
.tag.low{background:rgba(224,162,79,.15);color:var(--amber)}
.tag.hi{background:rgba(224,86,79,.15);color:var(--red)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid3{display:grid;grid-template-columns:3fr 2fr;gap:16px;margin-bottom:16px}
.grid4{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px}
@media(max-width:760px){.charts,.grid2,.grid3{grid-template-columns:1fr}.grid4{grid-template-columns:repeat(2,1fr)}}
.purple{color:#b47def}
.note{color:var(--dim);font-size:12px;margin-top:10px}
.meal{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px dashed var(--line);font-size:13px}
.meal:last-child{border-bottom:none}
.meal .n{color:var(--muted)}
.meal .c{color:var(--dim);font-size:12px}
.mealhead{display:flex;justify-content:space-between;align-items:center;margin:10px 0 4px;font-size:12px;color:var(--muted)}
.mealhead:first-child{margin-top:0}
.sumrow{display:flex;gap:24px;padding-top:10px;margin-top:8px;border-top:1px solid var(--line);font-size:13px}
.sumrow b{font-size:18px}
.big{font-size:26px;font-weight:600;margin-top:2px}
.k{color:var(--muted);font-size:12px}
.up{color:var(--green)} .down{color:var(--red)} .warn{color:var(--amber)}
.bcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;text-align:center}
.bcard .bk{color:var(--muted);font-size:12px;margin-bottom:6px}
.bcard .bv{font-size:32px;font-weight:600;line-height:1.2}
.bcard .bu{font-size:13px;color:var(--muted);margin-left:2px}
.bcard .bt{font-size:12px;margin-top:6px;display:flex;align-items:center;justify-content:center;gap:4px}
.bt.good{color:var(--green)} .bt.bad{color:var(--red)} .bt.flat{color:var(--dim)}
.feel-grid{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px}
.feel-block{background:var(--card2);border-radius:10px;padding:12px 16px;min-width:200px;flex:1}
.feel-block .feel-val{font-size:15px;font-weight:600;margin-top:4px;color:var(--text)}"""


def build_html(days, body, workouts, notes, window, unfinished, data_dir, feelings):
    chart = build_chart_data(days, window, body)
    today_panel = render_today_panel(days, unfinished, notes)
    cards_html = render_cards(days, body, workouts)
    daily_table = render_daily_table(days, window, unfinished)
    daily_note = render_daily_note(days, window, unfinished, notes)
    body_table, body_note = render_body_table(body, notes)
    whead, wtable, wnote = render_workout_table(workouts, notes)
    feel_panel = render_feelings_panel(feelings, workouts)

    all_dates = sorted(set(list(days.keys()) + [r['date'] for r in body] +
                           [r['date'] for r in workouts]))
    dirname = os.path.basename(os.path.normpath(data_dir))
    subtitle = (f"{min(days).isoformat()} 至 {max(days).isoformat()}"
                f" · 数据源 {dirname}/*.csv · 自动生成")

    today_span = f"{md(TODAY)} · {len([m for m in MEAL_ORDER if m in days.get(TODAY, {}).get('meals', {})])} 餐" if TODAY in days else f"{md(TODAY)} · 暂无记录"
    cal_title = f"每日热量 vs 目标区间（{CAL_LO}-{CAL_HI}）"
    pro_title = f"每日蛋白质 vs 目标（{CONFIG['protein_target']}g）"

    head = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(CONFIG['title'])}</title>
<script src="vendor/chart.umd.min.js"></script>
<script src="vendor/chartjs-plugin-annotation.min.js"></script>
<style>
{CSS}
</style>
</head>
<body>
<div class="wrap">
  <h1>{esc(CONFIG['title'])}</h1>
  <div class="sub">{esc(subtitle)}</div>

  <div style="height:16px"></div>
  <div class="panel" style="margin-bottom:16px">
    <h2>今日饮食<span>{esc(today_span)}</span></h2>
    {today_panel}
  </div>

  <div class="grid4">
{cards_html}
  </div>

  <div class="charts">
    <div class="chartbox">
      <h2 style="font-size:13px;font-weight:500;color:var(--muted)">{esc(cal_title)}</h2>
      <canvas id="calChart" height="220"></canvas>
    </div>
    <div class="chartbox">
      <h2 style="font-size:13px;font-weight:500;color:var(--muted)">碳蛋脂供能比（%·按日）</h2>
      <canvas id="ratioChart" height="220"></canvas>
    </div>
  </div>

  <div style="height:16px"></div>
  <div class="charts">
    <div class="chartbox">
      <h2 style="font-size:13px;font-weight:500;color:var(--muted)">{esc(pro_title)}</h2>
      <canvas id="proChart" height="220"></canvas>
    </div>
    <div class="chartbox">
      <h2 style="font-size:13px;font-weight:500;color:var(--muted)">体重 / 体脂 / BMI 趋势</h2>
      <canvas id="bodyChart" height="220"></canvas>
    </div>
  </div>

  <div style="height:16px"></div>
  <div class="grid2">
    <div class="panel">
      <h2>每日明细<span>热量 / 碳 / 蛋白 / 脂</span></h2>
      <table>
        <thead><tr><th>日期</th><th>热量<br><span style="font-weight:400;color:var(--dim);font-size:11px">kcal</span></th><th>碳水<br><span style="font-weight:400;color:var(--dim);font-size:11px">g</span></th><th>蛋白<br><span style="font-weight:400;color:var(--dim);font-size:11px">g</span></th><th>脂肪<br><span style="font-weight:400;color:var(--dim);font-size:11px">g</span></th><th>供能比 C:P:F</th><th>达标</th></tr></thead>
        <tbody>
{daily_table}
        </tbody>
      </table>
      <div class="note">{daily_note}</div>
    </div>
    <div class="panel">
      <h2>身体数据<span>体脂秤实测</span></h2>
      <table>
        <thead><tr><th>日期</th><th>体重<br><span style="font-weight:400;color:var(--dim);font-size:11px">kg</span></th><th>体脂率<br><span style="font-weight:400;color:var(--dim);font-size:11px">%</span></th><th>肌肉<br><span style="font-weight:400;color:var(--dim);font-size:11px">kg</span></th><th>代谢<br><span style="font-weight:400;color:var(--dim);font-size:11px">kcal</span></th></tr></thead>
        <tbody>
{body_table}
        </tbody>
      </table>
      <div class="note">{body_note}</div>
    </div>
  </div>

  <div class="panel">
    <h2>运动记录<span>{esc(whead)}</span></h2>
    <table>
      <thead><tr><th>日期</th><th>类型</th><th>时长<br><span style="font-weight:400;color:var(--dim);font-size:11px">min</span></th><th>消耗<br><span style="font-weight:400;color:var(--dim);font-size:11px">kcal</span></th><th>备注</th></tr></thead>
      <tbody>
{wtable}
      </tbody>
    </table>
    <div class="note">{wnote}</div>
  </div>

  {feel_panel}

  <div class="note" style="text-align:center;padding-bottom:10px">数据实时来自 {esc(dirname)}/diet_log.csv · workout_log.csv · body_measurements.csv · 看板备注.md</div>
</div>

<script>
"""

    tail = f"""{build_js(chart)}
</script>
</body>
</html>
"""
    return head + tail


# ---------------- 主流程 ----------------
def main():
    global BASE, DIET_CSV, BODY_CSV, WORKOUT_CSV, NOTES_MD, OUT_HTML, TODAY

    BASE = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description='饮食运动看板生成器（读 CSV → 生成静态 HTML）')
    ap.add_argument('--data-dir', default=BASE,
                    help='数据目录（含 diet_log.csv / body_measurements.csv / workout_log.csv 与 vendor/），默认脚本所在目录')
    ap.add_argument('--out', default=None, help='输出 HTML 路径（默认 <data-dir>/饮食运动看板.html）')
    ap.add_argument('--today', default=None, help='演示用：指定"今天"日期 YYYY-MM-DD（默认系统今天）')
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    DIET_CSV = os.path.join(data_dir, 'diet_log.csv')
    BODY_CSV = os.path.join(data_dir, 'body_measurements.csv')
    WORKOUT_CSV = os.path.join(data_dir, 'workout_log.csv')
    NOTES_MD = os.path.join(data_dir, '看板备注.md')
    FEEL_CSV = os.path.join(data_dir, 'feeling_log.csv')
    OUT_HTML = args.out or os.path.join(data_dir, '饮食运动看板.html')
    TODAY = date.fromisoformat(args.today) if args.today else date.today()

    for f in (DIET_CSV, BODY_CSV, WORKOUT_CSV):
        if not os.path.exists(f):
            print(f'错误：缺少 {f}（参考 example/ 目录的数据格式）')
            return 1

    days = load_diet(DIET_CSV)
    body = load_body(BODY_CSV)
    workouts = load_workout(WORKOUT_CSV)
    notes = load_notes(NOTES_MD)
    feelings = load_feelings(FEEL_CSV)

    if not days:
        print('错误：diet_log.csv 无数据')
        return 1

    # vendor 检查（Chart.js 本地引用，缺失只提示不报错）
    vendor_dir = os.path.join(data_dir, 'vendor')
    missing_vendor = [f for f in ('chart.umd.min.js', 'chartjs-plugin-annotation.min.js')
                      if not os.path.exists(os.path.join(vendor_dir, f))]
    if missing_vendor:
        print('提示：HTML 引用 vendor/ 下的 Chart.js，当前缺失：')
        for f in missing_vendor:
            print(f'  - vendor/{f}')
        print('  下载后放在 <data-dir>/vendor/ 下即可（命令见 README），否则图表区域空白。')

    window = sorted(days.keys())[-WINDOW_DAYS:]
    unfinished = build_unfinished(days, notes)
    html_out = build_html(days, body, workouts, notes, window, unfinished, data_dir, feelings)

    os.makedirs(os.path.dirname(OUT_HTML) or '.', exist_ok=True)
    with open(OUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f'已生成 {OUT_HTML}')
    print(f'数据范围：饮食 {md(min(days))}-{md(max(days))} · 身体 {len(body)} 次 · 运动 {len(workouts)} 次')
    print(f'今日窗口：{md(window[0])} 至 {md(window[-1])}（{len(window)} 天）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
