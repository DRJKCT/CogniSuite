import zlib, struct, math

W = H = 256
INK = (16, 40, 28, 255)
COPPER = (194, 112, 61, 255)

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

raw = bytearray()
px = [[(0, 0, 0, 0) for _ in range(W)] for _ in range(H)]
ink = [[False] * W for _ in range(H)]
cop = [[False] * W for _ in range(H)]

cx, cy, r = 128.0, 128.0, 80.0
half = 18.0
steps = 256
pts = []
th0 = math.atan2(44.86 - 32, 47.3 - 32)
th1 = math.atan2(19.14 - 32, 47.3 - 32)
for i in range(steps + 1):
    t = th0 + (th1 - th0) * i / steps
    t = t % (2 * math.pi)
    pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))

def dist_seg(p, a, b):
    ax, ay = a
    bx, by = b
    pxp, pyp = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(pxp - ax, pyp - ay)
    t = ((pxp - ax) * dx + (pyp - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    qx = ax + t * dx
    qy = ay + t * dy
    return math.hypot(pxp - qx, pyp - qy)

for i in range(steps):
    a = pts[i]
    b = pts[i + 1]
    for y in range(H):
        for x in range(W):
            p = (float(x), float(y))
            if dist_seg(p, a, b) <= half or math.hypot(p[0] - a[0], p[1] - a[1]) <= half or math.hypot(p[0] - b[0], p[1] - b[1]) <= half:
                ink[y][x] = True

def in_poly(x, y, poly):
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside

diamond = [(184.0, 102.0), (210.0, 128.0), (184.0, 154.0), (158.0, 128.0)]
for y in range(H):
    for x in range(W):
        if in_poly(float(x), float(y), diamond):
            cop[y][x] = True

for y in range(H):
    row = bytearray(b"\x00")
    for x in range(W):
        if cop[y][x]:
            c = COPPER
        elif ink[y][x]:
            c = INK
        else:
            c = (0, 0, 0, 0)
        row += bytes(c)
    raw += row

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
png += chunk(b"IEND", b"")
with open("C:\\Users\\Jeremy\\Project\\CogniFinance_working\\_logo_tmp.png", "wb") as f:
    f.write(png)
