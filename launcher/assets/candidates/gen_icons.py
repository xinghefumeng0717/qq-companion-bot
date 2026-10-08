# -*- coding: utf-8 -*-
"""青梓桌面图标候选方案：纯 Pillow 程序化绘制。

约束：不使用任何 AI 绘图、不使用现成图库或网络素材，所有图形均由 ImageDraw
的 rectangle / polygon / line / arc / ellipse 组合而成。

运行：
    D:\\qq-companion\\venv\\Scripts\\python.exe launcher/assets/candidates/gen_icons.py

一次运行会输出：
  direction_1..5.png           五个方向的 512×512 RGBA 母版
  direction_2_alt.png          方向二的备选配色（宣纸底 + 青瓷气泡）
  candidates_sheet.png         方向一~三的 256/64/32 三档缩放对照表
  candidates_sheet_v2.png      全部五个方向的 256/64/32 三档缩放对照表

输出是确定性的（固定随机种子），重跑的字节完全一致。
"""
from __future__ import annotations

import math
import os
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))

MASTER = 512          # 输出母版边长
SS = 4                # 超采样倍数（先画 2048 再 LANCZOS 缩到 512，得到干净的抗锯齿边缘）
S = MASTER * SS

# ---- 配色：严格取自 companion/admin_render.py 的看板 CSS 变量 ----
PAPER = (246, 241, 231, 255)      # --paper      #f6f1e7 宣纸底
CARD = (251, 248, 241, 255)       # --card       #fbf8f1 纸页卡片
INK = (43, 43, 38, 255)           # --ink        #2b2b26 墨色
CELADON = (79, 138, 109, 255)     # --celadon    #4f8a6d 青瓷绿
CELADON_BG = (227, 238, 231, 255)  # --celadon-bg #e3eee7 青瓷浅底
CINNABAR = (192, 72, 81, 255)     # --cinnabar   #c04851 朱砂
GOLD = (184, 134, 47, 255)        # --gold       #b8862f 暖金

KAI = "C:/Windows/Fonts/simkai.ttf"    # 楷体
SUN = "C:/Windows/Fonts/simsun.ttc"    # 宋体（兜底）
YAHEI = "C:/Windows/Fonts/msyh.ttc"    # 微软雅黑（仅用于对照表文字）

PLATE_RADIUS = 0.215   # 圆角方块圆角比例


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def new_layer(size=S):
    """全透明画布。半透明元素必须画在独立图层上再 alpha_composite：
    ImageDraw 是直接覆写像素而非混合，把半透明色直接画到底图上会抠出窟窿。"""
    return Image.new("RGBA", (size, size), (0, 0, 0, 0))


def plate(img, radius_frac=PLATE_RADIUS):
    """把整幅图裁成圆角方块（外部透明），作为图标底板剪影。"""
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=int(w * radius_frac), fill=255
    )
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask))


def paper_specks(img, seed=20261008, count=1500, alpha_max=24):
    """极淡的纸点纹理（模拟宣纸纤维/颗粒）。

    必须画在独立图层上再 alpha_composite：ImageDraw 是直接覆写像素而不是混合，
    直接画在底板上会把底色抠成一堆半透明的"白点"。
    """
    n = img.size[0]
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rnd = random.Random(seed)
    for _ in range(count):
        x, y = rnd.random() * n, rnd.random() * n
        r = n * rnd.uniform(0.0008, 0.0045)
        a = rnd.randint(6, alpha_max)
        d.ellipse([x - r, y - r, x + r, y + r], fill=INK[:3] + (a,))
    img.alpha_composite(layer)


def qbez(p0, p1, p2, n=48):
    """二次贝塞尔采样，用于气泡尾巴 / 琴码上沿这类顺滑曲线。"""
    pts = []
    for i in range(n):
        u = i / (n - 1)
        v = 1 - u
        pts.append((
            v * v * p0[0] + 2 * v * u * p1[0] + u * u * p2[0],
            v * v * p0[1] + 2 * v * u * p1[1] + u * u * p2[1],
        ))
    return pts


def catmull(keys, samples=26):
    """Catmull-Rom 通过型样条，让大提琴琴身宽度曲线平滑无折角。"""
    pts = [keys[0]] + list(keys) + [keys[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for j in range(samples):
            u = j / samples
            u2, u3 = u * u, u * u * u
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * u
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * u2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * u3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * u
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * u2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * u3)
            out.append((x, y))
    out.append(keys[-1])
    return out


def rline(d, p1, p2, width, fill):
    """带圆头的线段（PIL 的 line 只圆化折点，不圆化端点）。"""
    d.line([p1, p2], fill=fill, width=width)
    r = width / 2
    for (x, y) in (p1, p2):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def chaikin(pts, iterations=2):
    """闭合折线的切角平滑：把琴身上端/下端的钝角磨圆，避免描边时炸出尖刺。"""
    for _ in range(iterations):
        out = []
        n = len(pts)
        for i in range(n):
            p, q = pts[i], pts[(i + 1) % n]
            out.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            out.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        pts = out
    return pts


def stroke_path(d, pts, width, fill, closed=True):
    """沿法线双向偏移后填充成一条闭合色带。

    比 ImageDraw.line(joint=...) 更干净：PIL 的 joint 走多边形填充，在点很密的
    曲线上会留下梳齿状毛边，而色带填充是纯多边形，边缘天然光滑。
    """
    n = len(pts)
    r = width / 2
    out, inn = [], []
    for i in range(n):
        p = pts[i - 1] if (i or closed) else pts[0]
        q = pts[(i + 1) % n] if closed else pts[min(i + 1, n - 1)]
        tx, ty = q[0] - p[0], q[1] - p[1]
        ln = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / ln, tx / ln
        out.append((pts[i][0] + nx * r, pts[i][1] + ny * r))
        inn.append((pts[i][0] - nx * r, pts[i][1] - ny * r))
    d.polygon(out + inn[::-1], fill=fill)
    if not closed:
        for (x, y) in (pts[0], pts[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def fit_font(path, ch, box_w, box_h):
    """二分出能让字形墨迹盒恰好塞进 box 的最大字号，并返回墨迹盒偏移。"""
    lo, hi = 8, 3000
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = ImageFont.truetype(path, mid)
        l, t, r, b = f.getbbox(ch)
        if (r - l) <= box_w and (b - t) <= box_h:
            best = (f, l, t, r - l, b - t)
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        raise RuntimeError("找不到合适字号")
    return best


def thick_text(d, xy, ch, font, fill, thick, rings=((1.0, 12), (0.62, 10))):
    """多层偏移描边加粗：让楷体这类偏瘦的笔画在小尺寸下也立得住。"""
    d.text(xy, ch, font=font, fill=fill)
    for ratio, n in rings:
        r = thick * ratio
        for i in range(n):
            a = 2 * math.pi * i / n
            d.text((xy[0] + r * math.cos(a), xy[1] + r * math.sin(a)),
                   ch, font=font, fill=fill)


def finish(img):
    return img.resize((MASTER, MASTER), Image.Resampling.LANCZOS)


def drop_shadow(layer, offset, blur, alpha):
    """按图层 alpha 生成一层柔和投影（墨色），返回 (阴影图, 偏移)。"""
    a = layer.getchannel("A").filter(ImageFilter.GaussianBlur(blur))
    a = a.point(lambda v: int(v * alpha))
    sh = Image.new("RGBA", layer.size, INK[:3] + (0,))
    sh.putalpha(a)
    return sh, offset


def tape_polygon(x0, y0, w, h, teeth=3, depth_frac=0.055):
    """和纸胶带外形：矩形两端各切出几道锯齿。

    造型参照 admin_render.py 的 .card-hero::before（clip-path 多边形切端 + 半透明），
    齿数加密以便缩小后仍有"撕口"感。
    """
    d = w * depth_frac
    right, left = [], []
    for i in range(teeth):
        t0, t1 = i / teeth, (i + 1) / teeth
        dep = d * (0.80 + 0.30 * (i % 2))
        right.append((x0 + w, y0 + h * t0))
        right.append((x0 + w - dep, y0 + h * (t0 + t1) / 2))
        left.append((x0, y0 + h * (1 - t0)))
        left.append((x0 + dep * 1.15, y0 + h * (1 - (t0 + t1) / 2)))
    return [(x0, y0), (x0 + w, y0)] + right + [(x0 + w, y0 + h), (x0, y0 + h)] + left


# --------------------------------------------------------------------------
# 方向一：朱砂印章「梓」
# --------------------------------------------------------------------------

def build_seal(char="梓", seal_frac=0.665, shape="square", angle=-1.6,
               seed=1024, glyph_frac=0.78, notch_count=95, bleed_count=26):
    """盖歪一点点、边缘轻微做旧的朱砂印章（RGBA，居中）。

    shape="square" 方章（方向一）/ shape="circle" 圆形闲章（方向五）。
    圆章的字要按内接正方形缩小留白，否则字形四角会捅出圆外。
    """
    seal_px = int(S * seal_frac)
    pad = int(seal_px * 0.075)
    size = seal_px + pad * 2
    layer = new_layer(size)
    d = ImageDraw.Draw(layer)

    box = [pad, pad, pad + seal_px - 1, pad + seal_px - 1]
    if shape == "circle":
        d.ellipse(box, fill=CINNABAR)
        ccx = ccy = pad + seal_px / 2
        rad = seal_px / 2
    else:
        d.rounded_rectangle(box, radius=int(seal_px * 0.075), fill=CINNABAR)

    rnd = random.Random(seed)
    # 用透明小圆沿边啃掉一点，做出"手压盖章"的边缘不齐
    # 注意：方章分支里 randrange/uniform 的抽取顺序必须保持 side→t→depth，
    # 否则随机序列一变，方向一那张母版就不再是原来的画了。
    for _ in range(notch_count):
        if shape == "circle":
            a = rnd.uniform(0, 2 * math.pi)
            depth = rnd.uniform(seal_px * 0.003, seal_px * 0.013)
            rr = rad - depth
            x, y = ccx + rr * math.cos(a), ccy + rr * math.sin(a)
        else:
            side = rnd.randrange(4)
            t = rnd.uniform(0.10, 0.90)
            depth = rnd.uniform(seal_px * 0.003, seal_px * 0.013)
            if side == 0:
                x, y = pad + t * seal_px, pad + depth
            elif side == 1:
                x, y = pad + t * seal_px, pad + seal_px - depth
            elif side == 2:
                x, y = pad + depth, pad + t * seal_px
            else:
                x, y = pad + seal_px - depth, pad + t * seal_px
        r = rnd.uniform(seal_px * 0.004, seal_px * 0.012)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(0, 0, 0, 0))

    # 朱砂微微洇进纸里：边缘外少量淡红飞点（克制，否则小尺寸下会糊掉轮廓）
    for _ in range(bleed_count):
        if shape == "circle":
            a = rnd.uniform(0, 2 * math.pi)
            off = rnd.uniform(seal_px * 0.005, seal_px * 0.022)
            rr = rad + off
            x, y = ccx + rr * math.cos(a), ccy + rr * math.sin(a)
        else:
            side = rnd.randrange(4)
            t = rnd.uniform(0.08, 0.92)
            off = rnd.uniform(seal_px * 0.005, seal_px * 0.022)
            if side == 0:
                x, y = pad + t * seal_px, pad - off
            elif side == 1:
                x, y = pad + t * seal_px, pad + seal_px + off
            elif side == 2:
                x, y = pad - off, pad + t * seal_px
            else:
                x, y = pad + seal_px + off, pad + t * seal_px
        r = rnd.uniform(seal_px * 0.003, seal_px * 0.008)
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=CINNABAR[:3] + (rnd.randint(26, 52),))

    # 反白「梓」：字体优先楷体，找不到退宋体
    path = KAI if os.path.exists(KAI) else SUN
    target = seal_px * glyph_frac
    font, l, t, w, h = fit_font(path, char, target, target)
    ox = pad + (seal_px - w) / 2 - l
    oy = pad + (seal_px - h) / 2 - t
    thick_text(d, (ox, oy), char, font, PAPER, seal_px * 0.016)

    return layer.rotate(angle, resample=Image.Resampling.BICUBIC,
                        expand=False, fillcolor=(0, 0, 0, 0))


def build_direction1():
    img = Image.new("RGBA", (S, S), PAPER)
    paper_specks(img, count=1600, alpha_max=26)

    seal = build_seal(shape="square", angle=-1.6, seed=1024)
    img.alpha_composite(seal, ((S - seal.size[0]) // 2, (S - seal.size[1]) // 2))

    plate(img)
    return finish(img)


def build_direction5():
    """方向一的变体：只把方章换成圆形闲章，其余（底板、纸点、反白字、做旧手法）保持一致。"""
    img = Image.new("RGBA", (S, S), PAPER)
    paper_specks(img, count=1600, alpha_max=26)

    seal = build_seal(shape="circle", seal_frac=0.720, angle=1.8,
                      seed=5150, glyph_frac=0.660, notch_count=110, bleed_count=30)
    img.alpha_composite(seal, ((S - seal.size[0]) // 2, (S - seal.size[1]) // 2))

    plate(img)
    return finish(img)


# --------------------------------------------------------------------------
# 方向四：手账便签 + 和纸胶带
# --------------------------------------------------------------------------

def build_direction4():
    tilt = -4.0
    note_w, note_h = 0.700 * S, 0.775 * S
    nx0, ny0 = (S - note_w) / 2, (S - note_h) / 2
    nx1, ny1 = nx0 + note_w, ny0 + note_h

    group = new_layer(S)
    d = ImageDraw.Draw(group)

    # 便签本体（纸页色）+ 圆角
    radius = int(note_w * 0.022)
    d.rounded_rectangle([nx0, ny0, nx1, ny1], radius=radius, fill=CARD)

    # 纸点纹理：只在便签范围内，靠蒙版裁掉溢出部分
    specks = new_layer(S)
    paper_specks(specks, count=1400, alpha_max=22)
    note_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(note_mask).rounded_rectangle(
        [nx0, ny0, nx1, ny1], radius=radius, fill=255)
    specks.putalpha(ImageChops.multiply(specks.getchannel("A"), note_mask))
    group.alpha_composite(specks)

    # 极淡墨色边框（半透明，必须走独立图层）
    border = new_layer(S)
    ImageDraw.Draw(border).rounded_rectangle(
        [nx0, ny0, nx1, ny1], radius=radius,
        outline=INK[:3] + (52,), width=int(S * 0.0039))
    group.alpha_composite(border)

    # 和纸胶带：贴在便签顶边正中，自身再歪一点点，半透明青瓷绿
    tape_w = note_w * 0.52
    tape_h = tape_w / 3.8
    tx0 = (S - tape_w) / 2
    ty0 = ny0 - tape_h / 2
    tape = new_layer(S)
    ImageDraw.Draw(tape).polygon(
        tape_polygon(tx0, ty0, tape_w, tape_h, teeth=3, depth_frac=0.022),
        fill=CELADON)
    tape.putalpha(tape.getchannel("A").point(lambda v: int(v * 0.72)))
    tape = tape.rotate(-0.8, resample=Image.Resampling.BICUBIC,
                       center=(S / 2, ny0))
    group.alpha_composite(tape)

    # 墨色手写「梓」：略微歪斜 + 略微偏移，模拟手写不端正
    path = KAI if os.path.exists(KAI) else SUN
    target = note_w * 0.55
    font, l, t, w, h = fit_font(path, "梓", target, target)
    glyph = new_layer(S)
    gd = ImageDraw.Draw(glyph)
    ox = (S - w) / 2 - l + note_w * 0.018
    oy = (S - h) / 2 - t + note_h * 0.020
    thick_text(gd, (ox, oy), "梓", font, INK, S * 0.010)
    glyph = glyph.rotate(3.5, resample=Image.Resampling.BICUBIC,
                         center=(S / 2, S / 2))
    group.alpha_composite(glyph)

    # 整体一起歪 -4°，再补投影；投影偏移让视觉重心回到画面正中
    group = group.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=False)
    out = new_layer(S)
    sh, off = drop_shadow(group, (int(S * 0.016), int(S * 0.028)),
                          blur=S * 0.035, alpha=0.30)
    out.alpha_composite(sh, (-off[0] // 2, -off[1] // 2))
    out.alpha_composite(group, (-off[0] // 2, -off[1] // 2))
    return finish(out)


# --------------------------------------------------------------------------
# 方向二：青瓷气泡 + 朱砂点
# --------------------------------------------------------------------------

def build_direction2(bg, bubble, dot, speck_alpha=14, speck_count=1400):
    img = Image.new("RGBA", (S, S), bg)
    paper_specks(img, count=speck_count, alpha_max=speck_alpha)
    d = ImageDraw.Draw(img)

    # 气泡主体
    bx0, by0, bx1, by1 = 0.145 * S, 0.205 * S, 0.855 * S, 0.645 * S
    r = (by1 - by0) * 0.245
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=r, fill=bubble)

    # 小尾巴：两条二次贝塞尔拼出的弯钩，起点/终点都落在气泡内部，接缝自然消失
    a = (0.315 * S, 0.510 * S)
    c1 = (0.252 * S, 0.690 * S)
    tip = (0.228 * S, 0.815 * S)
    c2 = (0.345 * S, 0.700 * S)
    b = (0.485 * S, 0.570 * S)
    d.polygon(qbez(a, c1, tip) + qbez(tip, c2, b), fill=bubble)

    # 一颗朱砂圆点（"正在输入"）
    cx, cy, dr = 0.5 * S, 0.425 * S, 0.098 * S
    d.ellipse([cx - dr, cy - dr, cx + dr, cy + dr], fill=dot)

    plate(img)
    return finish(img)


# --------------------------------------------------------------------------
# 方向三：大提琴极简线稿
# --------------------------------------------------------------------------

BODY_TOP = 0.300      # 琴身顶端
BODY_H = 0.550        # 琴身高（高:宽 ≈ 1.5，接近真实大提琴，避免看着像吉他）
# (沿琴身的位置 t, 半宽) —— 上弧 / 束腰 / 下弧；t≈0.34、0.62 处的小外凸是琴角
BODY_KEYS = [
    (0.000, 0.028), (0.045, 0.085), (0.100, 0.130), (0.160, 0.150),
    (0.230, 0.153), (0.300, 0.143), (0.335, 0.151), (0.375, 0.129),
    (0.460, 0.118), (0.545, 0.133), (0.585, 0.151), (0.620, 0.146),
    (0.690, 0.170), (0.760, 0.181), (0.820, 0.184), (0.875, 0.176),
    (0.925, 0.155), (0.970, 0.112), (1.000, 0.052),
]


def build_direction3():
    img = Image.new("RGBA", (S, S), PAPER)
    paper_specks(img, count=1200, alpha_max=14)
    d = ImageDraw.Draw(img)
    cx = 0.5 * S

    w_body = int(S * 0.030)    # 琴身轮廓线宽（512 母版下 ≈15px）
    w_neck = int(S * 0.025)    # 琴颈 / 琴头
    w_str = int(S * 0.014)     # 琴弦
    w_pin = int(S * 0.026)     # 尾柱

    # 琴身：由宽度样条生成左右轮廓，切角平滑后用色带填充描边
    prof = catmull(BODY_KEYS, samples=24)
    center = [(cx + hw * S, (BODY_TOP + t * BODY_H) * S) for (t, hw) in prof]
    center += [(cx - hw * S, (BODY_TOP + t * BODY_H) * S) for (t, hw) in reversed(prof)]
    stroke_path(d, chaikin(center, 2), w_body, INK)

    # 琴颈（两根平行线，与琴身上端同宽顺接）
    neck_top, neck_bot = 0.128 * S, 0.330 * S
    rline(d, (cx - 0.0230 * S, neck_top), (cx - 0.0215 * S, neck_bot), w_neck, INK)
    rline(d, (cx + 0.0230 * S, neck_top), (cx + 0.0215 * S, neck_bot), w_neck, INK)

    # 琴头：一枚圆环 + 两对弦轴
    sc_r = 0.034 * S
    sc_cy = 0.098 * S
    d.ellipse([cx - sc_r, sc_cy - sc_r, cx + sc_r, sc_cy + sc_r],
              outline=INK, width=int(S * 0.025))
    for py in (0.152 * S, 0.178 * S):
        rline(d, (cx - 0.054 * S, py), (cx + 0.054 * S, py), int(S * 0.015), INK)

    # 琴弦 2 根：从弦枕微微发散到琴码
    for sgn in (-1, 1):
        rline(d, (cx + sgn * 0.013 * S, 0.202 * S),
              (cx + sgn * 0.030 * S, 0.600 * S), w_str, INK)

    # 青瓷绿点缀：琴码（上沿微拱，两侧带脚）
    top = qbez((cx - 0.056 * S, 0.618 * S), (cx, 0.588 * S), (cx + 0.056 * S, 0.618 * S))
    brg = top + [(cx + 0.064 * S, 0.660 * S), (cx - 0.064 * S, 0.660 * S)]
    d.polygon(brg, fill=CELADON)

    # 尾柱
    rline(d, (cx, 0.820 * S), (cx, 0.928 * S), w_pin, INK)
    rline(d, (cx - 0.024 * S, 0.930 * S), (cx + 0.024 * S, 0.930 * S), w_pin, INK)

    plate(img)
    return finish(img)


# --------------------------------------------------------------------------
# 对照表
# --------------------------------------------------------------------------

def build_sheet(rows, path, title="青梓图标候选 · 小尺寸辨识度对照"):
    sizes = (256, 64, 32)
    pad, header, label_w, gap, row_gap = 40, 78, 210, 74, 54
    row_h = sizes[0] + 62
    W = pad * 2 + label_w + sum(sizes) + gap * 2 + 120
    H = pad * 2 + header + len(rows) * row_h + (len(rows) - 1) * row_gap

    sheet = Image.new("RGBA", (W, H), PAPER)
    d = ImageDraw.Draw(sheet)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=26, fill=PAPER)

    f_title = ImageFont.truetype(YAHEI, 34)
    f_label = ImageFont.truetype(YAHEI, 25)
    f_tag = ImageFont.truetype(YAHEI, 19)

    d.text((pad, pad - 6), title, font=f_title, fill=INK)

    sizes_x = []
    x = pad + label_w
    for s in sizes:
        sizes_x.append((x, s))
        x += s + gap
    for col, (x, s) in enumerate(sizes_x):
        d.text((x, pad + header - 34), f"{s}px", font=f_tag, fill=INK)

    y = pad + header
    for name, im in rows:
        d.rounded_rectangle([pad - 14, y - 14, W - pad + 14, y + row_h - 6],
                            radius=18, fill=CARD)
        d.text((pad, y + row_h // 2 - 22), name, font=f_label, fill=INK)
        for x, s in sizes_x:
            thumb = im.resize((s, s), Image.Resampling.LANCZOS)
            # 棋盘格衬底，方便看出圆角底板的透明区
            cell = Image.new("RGBA", (s, s), CARD)
            dc = ImageDraw.Draw(cell)
            step = max(6, s // 12)
            for gy in range(0, s, step):
                for gx in range(0, s, step):
                    if (gx // step + gy // step) % 2:
                        dc.rectangle([gx, gy, gx + step - 1, gy + step - 1],
                                     fill=(238, 233, 221, 255))
            cell.alpha_composite(thumb)
            sheet.alpha_composite(cell, (x, y + (sizes[0] - s) // 2))
        y += row_h + row_gap

    sheet.convert("RGB").save(path)


# --------------------------------------------------------------------------

def main():
    d1 = build_direction1()
    d1.save(os.path.join(HERE, "direction_1.png"))

    # 方向二主选：青瓷底 + 宣纸气泡（大色块在桌面/缩略图上辨识度更高）
    d2 = build_direction2(CELADON, PAPER, CINNABAR, speck_alpha=16, speck_count=1100)
    d2.save(os.path.join(HERE, "direction_2.png"))

    # 备选：宣纸底 + 青瓷气泡（更"手账纸"，但小尺寸下底色弱）
    alt = build_direction2(PAPER, CELADON, CINNABAR, speck_alpha=24, speck_count=1500)
    alt.save(os.path.join(HERE, "direction_2_alt.png"))

    d3 = build_direction3()
    d3.save(os.path.join(HERE, "direction_3.png"))

    d4 = build_direction4()
    d4.save(os.path.join(HERE, "direction_4.png"))

    d5 = build_direction5()
    d5.save(os.path.join(HERE, "direction_5.png"))

    build_sheet(
        [("方向一 朱砂印章", d1), ("方向二 青瓷气泡", d2), ("方向三 大提琴线稿", d3)],
        os.path.join(HERE, "candidates_sheet.png"),
    )
    build_sheet(
        [("方向一 朱砂印章", d1), ("方向二 青瓷气泡", d2), ("方向三 大提琴线稿", d3),
         ("方向四 手账便签", d4), ("方向五 圆形朱印", d5)],
        os.path.join(HERE, "candidates_sheet_v2.png"),
        title="青梓图标候选 · 五方向小尺寸辨识度对照",
    )
    print("ok:", ", ".join(sorted(f for f in os.listdir(HERE) if f.endswith(".png"))))


if __name__ == "__main__":
    main()
