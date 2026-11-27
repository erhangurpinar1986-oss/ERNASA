"""Minimal QR Code encoder (ISO/IEC 18004), standard library only.

Used to show TOTP enrolment URIs as a QR code rendered locally as inline SVG, so the
secret never leaves ERNASA (no third-party QR service, no external script).

Scope: byte mode, error correction level M, versions 1-10 (up to 213 bytes), automatic
mask selection with the standard penalty rules. Structure follows the public-domain
reference design by Project Nayuki.
"""

# Level M tables for versions 1..10 (index 0 unused).
ECC_PER_BLOCK_M = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
BLOCKS_M = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
FORMAT_ECL_BITS_M = 0
MAX_VERSION = 10


class QrError(ValueError):
    pass


def _raw_data_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        num_align = ver // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver):
    return _raw_data_modules(ver) // 8 - ECC_PER_BLOCK_M[ver] * BLOCKS_M[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver):
    if ver == 1:
        return []
    num_align = ver // 7 + 2
    step = (ver * 8 + num_align * 3 + 5) // (num_align * 4 - 4) * 2
    result = [ver * 4 + 10 - i * step for i in range(num_align - 1)] + [6]
    return list(reversed(result))


class _Matrix:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set_function(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        dist = max(abs(dx), abs(dy))
                        self.set_function(x, y, dist not in (2, 4))
        positions = _alignment_positions(self.ver)
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format_bits(0)
        self.draw_version()

    def draw_format_bits(self, mask):
        data = FORMAT_ECL_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 != 0
        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        size = self.size
        for i in range(8):
            self.set_function(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, size - 15 + i, bit(i))
        self.set_function(8, size - 8, True)

    def draw_version(self):
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.ver << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self.set_function(a, b, dark)
            self.set_function(b, a, dark)

    def draw_codewords(self, data):
        i = 0
        size = self.size
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = size - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        for y in range(self.size):
            for x in range(self.size):
                if self.function[y][x]:
                    continue
                invert = (
                    (x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
                    (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0,
                    (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0,
                )[mask]
                self.modules[y][x] ^= invert

    def penalty(self):
        size, m = self.size, self.modules
        score = 0
        lines = [m[y] for y in range(size)] + [[m[y][x] for y in range(size)] for x in range(size)]
        for line in lines:
            run, color = 0, None
            for v in line:
                if v == color:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, color = 1, v
            if run >= 5:
                score += run - 2
            text = ''.join('1' if v else '0' for v in line)
            for pattern in ('10111010000', '00001011101'):
                start = text.find(pattern)
                while start != -1:
                    score += 40
                    start = text.find(pattern, start + 1)
        for y in range(size - 1):
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(v for row in m for v in row)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + k * 10


def encode(text):
    """Return the QR matrix (list of rows of bools) for ``text`` (UTF-8 bytes)."""
    data = text.encode('utf-8')
    for ver in range(1, MAX_VERSION + 1):
        count_bits = 8 if ver < 10 else 16
        if 4 + count_bits + len(data) * 8 <= _data_codewords(ver) * 8:
            break
    else:
        raise QrError('Metin QR kod için çok uzun.')
    capacity = _data_codewords(ver) * 8
    bits = []
    append = lambda value, length: bits.extend((value >> i) & 1 for i in reversed(range(length)))
    append(0b0100, 4)
    append(len(data), count_bits)
    for b in data:
        append(b, 8)
    append(0, min(4, capacity - len(bits)))
    append(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        append(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    num_blocks, ecc_len = BLOCKS_M[ver], ECC_PER_BLOCK_M[ver]
    raw = _raw_data_modules(ver) // 8
    short_blocks = num_blocks - raw % num_blocks
    short_len = raw // num_blocks
    divisor = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(num_blocks):
        size = short_len - ecc_len + (0 if i < short_blocks else 1)
        chunk = codewords[k:k + size]
        k += size
        ecc = _rs_remainder(chunk, divisor)
        if i < short_blocks:
            chunk = chunk + [None]
        blocks.append(chunk + ecc)
    final = [blk[i] for i in range(len(blocks[0])) for blk in blocks if blk[i] is not None]

    best, best_score = None, None
    for mask in range(8):
        matrix = _Matrix(ver)
        matrix.draw_function_patterns()
        matrix.draw_codewords(final)
        matrix.apply_mask(mask)
        matrix.draw_format_bits(mask)
        score = matrix.penalty()
        if best_score is None or score < best_score:
            best, best_score = matrix, score
    return best.modules


def svg(text, scale=6, border=4, label='QR kod'):
    """Inline SVG markup (no external resources; CSP-safe)."""
    modules = encode(text)
    size = len(modules)
    path = ''.join(f'M{x + border},{y + border}h1v1h-1z' for y, row in enumerate(modules) for x, v in enumerate(row) if v)
    dim = (size + border * 2)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{label}" width="{dim * scale}" '
            f'height="{dim * scale}" viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
            f'<rect width="{dim}" height="{dim}" fill="#ffffff"/><path d="{path}" fill="#000000"/></svg>')
