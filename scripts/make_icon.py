"""生成 Pray 应用图标变体（PNG + ICO），并拼出预览图供挑选。

用法:
    python -X utf8 scripts/make_icon.py --variant a   # 单变体 → assets/icon-<v>.png + icon-<v>.ico
    python -X utf8 scripts/make_icon.py --preview    # 生成全部变体 + assets/preview.png 横向拼图
    python -X utf8 scripts/make_icon.py --variant a --ico assets/icon.ico  # 指定输出（打包用）

变体：
  a) node-network 节点网络（Agent 图编排隐喻）
  b) letter-P      深底白色 P 徽标（产品字母标）
  c) atomic-orbit  原子轨道（中心核 + 轨道节点，Agent 智能体隐喻）
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
S = 512
SS = 4
C = S * SS

TOP = (99, 102, 241)
BOT = (30, 27, 75)
NODE = (248, 250, 252)
EDGE = (199, 210, 254)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def px(p):
    return (p[0] * SS, p[1] * SS)


def make_canvas():
    img = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    radius = int(118 * SS)
    mask = Image.new("L", (C, C), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, C - 1, C - 1], radius, fill=255)
    grad = Image.new("RGB", (C, C))
    gd = ImageDraw.Draw(grad)
    for y in range(C):
        gd.line([(0, y), (C, y)], fill=lerp(TOP, BOT, y / (C - 1)))
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    for i in range(60, 0, -1):
        a = int(3 * (60 - i) / 60)
        d.ellipse([C // 2 - i * SS, -i * SS * 2, C // 2 + i * SS, i * SS * 3], fill=(255, 255, 255, a))
    return img, d


def dot(d, xy, r, fill):
    x, y = px(xy)
    rr = int(r * SS)
    d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=fill)


def glow_dot(d, xy, r, color=(129, 140, 248)):
    x, y = px(xy)
    for rr in range(int(r * SS), 0, -1):
        a = int(8 * (int(r * SS) - rr) / int(r * SS))
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=(*color, a))


def variant_a(d):
    """节点网络：三角节点 + hub。"""
    pts = [(256, 158), (150, 362), (362, 362)]
    center = (256, 300)
    coords = [px(p) for p in pts]
    for cx, cy in coords:
        glow_dot(d, (cx // SS, cy // SS), 30)
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            d.line(coords[i] + coords[j], fill=EDGE, width=int(13 * SS))
    ch = px(center)
    for cx, cy in coords:
        d.line([ch, (cx, cy)], fill=(199, 210, 254, 150), width=int(7 * SS))
    for cx, cy in coords:
        r = int(54 * SS)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=NODE)
    r = int(30 * SS)
    d.ellipse([ch[0] - r, ch[1] - r, ch[0] + r, ch[1] + r], fill=(255, 255, 255))


def variant_b(d):
    """字母 P 徽标 + 右下数据点。"""
    try:
        font = ImageFont.truetype(r"C:\Windows\Fonts\seguibl.ttf", int(300 * SS))
    except OSError:
        font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", int(300 * SS))
    # P 的主体圆头与竖干：画在 (105..407) x 范围，中心 ~y 260
    # 简化：用文字"P"，再覆盖一小节让字形统一
    d.text(px((62, 60)), "P", font=font, fill=NODE)
    # 右下数据/节点小点
    glow_dot(d, (392, 388), 34)
    dot(d, (392, 388), 20, (165, 180, 252))


def variant_c(d):
    """原子轨道：中心核 + 两圈轨道节点。"""
    center = (256, 268)
    glow_dot(d, center, 52)
    r = int(26 * SS)
    d.ellipse([px(center)[0] - r, px(center)[1] - r, px(center)[0] + r, px(center)[1] + r],
              fill=(255, 255, 255))
    # 轨道 1：横椭圆（前弧可见）
    box1 = [px((96, 208)), px((416, 328))]
    for rr in range(int(5 * SS), 0, -1):
        a = int(9 * (int(5 * SS) - rr) / int(5 * SS))
        d.arc(box1, 190, 350, fill=(199, 210, 254, a), width=rr)
    # 轨道 2：斜椭圆
    box2 = [px((128, 168)), px((384, 368))]
    d.arc(box2, 10, 170, fill=(199, 210, 254, 170), width=int(4 * SS))
    # 轨道节点
    glow_dot(d, (168, 262), 22)
    dot(d, (168, 262), 15, NODE)
    glow_dot(d, (368, 330), 22)
    dot(d, (368, 330), 15, NODE)
    glow_dot(d, (352, 214), 18)
    dot(d, (352, 214), 12, (199, 210, 254))


def _sparkle_poly(d, cx, cy, length, width, angle_deg, fill):
    """画一根绕中心旋转 angle_deg 的瘦菱形星芒（512 系坐标）。"""
    import math

    def rot(x, y):
        a = math.radians(angle_deg)
        return (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))

    cx, cy = cx * SS, cy * SS
    L, W = length * SS, width * SS
    tips = [(0, -L), (W, 0), (0, L), (-W, 0)]
    d.polygon([(cx + rot(x, y)[0], cy + rot(x, y)[1]) for x, y in tips], fill=fill)


def variant_d(d):
    """八臂星光（sparkle）：近黑深底 + 琥珀星光，呼应 Pray/祈愿。"""
    star = (247, 185, 85)          # amber #F7B955
    star_core = (255, 224, 160)    # 星核亮色
    cx, cy = 256, 268
    # 柔和星辉
    glow_dot(d, (cx, cy), 150, color=star)
    # 4 条长主臂（十字）+ 4 条短对角臂（45° 间隔），合成八臂星光
    for ang in range(0, 360, 90):
        _sparkle_poly(d, cx, cy, 132, 26, ang, star)
    for ang in range(45, 360, 90):
        _sparkle_poly(d, cx, cy, 66, 20, ang, star)
    # 中心亮核
    r = int(26 * SS)
    d.ellipse([cx * SS - r, cy * SS - r, cx * SS + r, cy * SS + r], fill=star_core)


VARIANTS = {"a": variant_a, "b": variant_b, "c": variant_c, "d": variant_d}


def render(variant: str) -> Image.Image:
    img, d = make_canvas()
    VARIANTS[variant](d)
    return img.resize((S, S), Image.LANCZOS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=sorted(VARIANTS))
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--ico", help="额外把当前变体另存为此 .ico 路径")
    args = ap.parse_args()
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)

    if args.preview:
        imgs = []
        for v in sorted(VARIANTS):
            im = render(v)
            im.save(assets / f"icon-{v}.png")
            im.save(assets / f"icon-{v}.ico",
                    sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
            imgs.append(im)
            print(f"已生成 icon-{v}.png / .ico")
        gap = 24
        n = len(imgs)
        canvas = Image.new("RGBA", (S * n + gap * (n - 1), S), (15, 15, 25, 255))
        for i, im in enumerate(imgs):
            canvas.paste(im, (i * (S + gap), 0))
        canvas.save(assets / "preview.png")
        print(f"预览图: {assets / 'preview.png'}")
        return

    if not args.variant:
        ap.error("需要 --variant a|b|c 或 --preview")
    im = render(args.variant)
    im.save(assets / f"icon-{args.variant}.png")
    im.save(assets / f"icon-{args.variant}.ico",
            sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)])
    print(f"已生成 icon-{args.variant}.png / .ico")
    if args.ico:
        im.save(args.ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        print(f"已另存 {args.ico}")


if __name__ == "__main__":
    sys.exit(main())
