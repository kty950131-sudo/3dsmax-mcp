"""Biped 복사 컬렉션(.cpy) 파일에서 포즈 카드 목록을 뽑는다.

Max 없이 돈다 — 파일 구조만 읽는다. .cpy 는 문서화되지 않은 바이너리지만
포즈마다 Max 가 찍은 TGA 썸네일이 통째로 들어 있어, 그 썸네일을 카드 이미지로
쓴다. 실제 포즈 적용은 Max 안에서 `biped.loadCopyPasteFile` 이 하므로 여기서
포즈 데이터 자체는 해석하지 않는다.

파일 안에서 포즈 하나는 이렇게 생겼다 (실측: cgjoy-hand_pose.cpy, Max 2026 로드
확인):

    <u32 이름길이><이름> 03 05 01 00 00 00 <u32 TGA길이><TGA 바이트>

TGA 는 32bpp RLE(타입 10) 이고 끝에 TRUEVISION-XFILE 푸터가 붙는다. Max
파이썬에는 PIL 이 없어서 디코더와 PNG 기록을 직접 한다 — 139×113 짜리라
순수 파이썬으로도 순간이다.
"""

import base64
import hashlib
import os
import re
import struct
import zlib
from typing import NamedTuple

_POSE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "assets",
    "poses",
)

#: 포즈 파일은 저장소 assets/ 에 둔다 — 스튜디오가 어디서 실행되든 같은 파일을
#: 찾아야 Max 안의 컬렉션 이름과 카드가 어긋나지 않는다.
HAND_CPY = os.path.join(_POSE_DIR, "hand_pose.cpy")
BODY_CPY = os.path.join(_POSE_DIR, "body_pose.cpy")
DEFAULT_CPY = HAND_CPY

#: 선반 두 개. 손 포즈는 **손가락 노드만** 담는 포스처라 좌우 미러가 있고,
#: 전신 포즈는 바이패드 전체를 담는 포즈라 미러 개념이 없다(맥스의 pasteBipPose
#: 는 opposite 인자를 받지만 전신에서는 좌우를 통째로 뒤집는 뜻이 된다).
SHELVES = {
    "hand": {
        "label": "손 포즈",
        "collection": "hand_pose",
        "path": HAND_CPY,
        "kind": "posture",
        "sides": True,
    },
    "body": {
        "label": "포즈",
        "collection": "body_pose",
        "path": BODY_CPY,
        "kind": "pose",
        "sides": False,
    },
}


def shelf_info(shelf: str) -> dict:
    try:
        return SHELVES[shelf]
    except KeyError:
        raise ValueError(f"모르는 선반입니다: {shelf}") from None

_FOOTER = b"TRUEVISION-XFILE.\x00"
# idlen=0, cmap=0, type 2(raw)|10(RLE), cmap spec 5×0, origin 4×0
_TGA_HEADER = re.compile(rb"\x00\x00[\x02\x0a]\x00\x00\x00\x00\x00\x00\x00\x00\x00")
_NAME_GAP = 10  # 이름 끝 ~ TGA 시작 사이의 고정 바이트 (03 05 01 00 00 00 + u32 길이)
_MAX_NAME = 64


class Pose(NamedTuple):
    index: int  # Max 의 getCopy 인덱스 (1부터). 파일 순서와 같다.
    name: str
    tga: bytes


def parse_cpy(data: bytes) -> list[Pose]:
    """포즈 이름과 TGA 썸네일을 파일 순서대로 돌려준다."""
    poses: list[Pose] = []
    search_from = 0
    for footer in re.finditer(re.escape(_FOOTER), data):
        end = footer.end()
        start = _find_tga_start(data, search_from, end)
        if start is None:
            search_from = end
            continue
        name = _name_before(data, start)
        poses.append(Pose(len(poses) + 1, name, data[start:end]))
        search_from = end
    return poses


def _find_tga_start(data: bytes, lo: int, end: int):
    """푸터 앞에서 헤더 후보를 찾아, 바로 앞 u32 길이가 맞는 것을 고른다."""
    for m in _TGA_HEADER.finditer(data, lo, end):
        start = m.start()
        if start < 4:
            continue
        (length,) = struct.unpack_from("<I", data, start - 4)
        if length == end - start:
            return start
    return None


def _name_before(data: bytes, tga_start: int) -> str:
    name_end = tga_start - _NAME_GAP
    for length in range(1, _MAX_NAME + 1):
        at = name_end - length - 4
        if at < 0:
            break
        (stored,) = struct.unpack_from("<I", data, at)
        if stored == length:
            return data[at + 4 : name_end].decode("latin-1")
    return f"pose_{tga_start}"


def tga_to_png(tga: bytes) -> bytes:
    """32/24bpp, raw 또는 RLE TGA 를 RGBA PNG 로 바꾼다."""
    idlen, cmap, kind = tga[0], tga[1], tga[2]
    if cmap != 0 or kind not in (2, 10):
        raise ValueError(f"지원하지 않는 TGA: type={kind} cmap={cmap}")
    width, height, bpp, desc = struct.unpack_from("<HHBB", tga, 12)
    if bpp not in (24, 32):
        raise ValueError(f"지원하지 않는 TGA 색 깊이: {bpp}")
    bytes_pp = bpp // 8
    pos = 18 + idlen
    count = width * height
    raw = bytearray()
    if kind == 2:
        raw += tga[pos : pos + count * bytes_pp]
    else:
        while len(raw) < count * bytes_pp:
            packet = tga[pos]
            pos += 1
            run = (packet & 0x7F) + 1
            if packet & 0x80:
                raw += tga[pos : pos + bytes_pp] * run
                pos += bytes_pp
            else:
                raw += tga[pos : pos + run * bytes_pp]
                pos += run * bytes_pp
    rows = []
    stride = width * bytes_pp
    for y in range(height):
        row = raw[y * stride : (y + 1) * stride]
        # TGA 는 BGR(A) 이고, desc 비트5 가 꺼져 있으면 아래줄부터 저장된다.
        if bytes_pp == 4:
            px = bytes(b for i in range(0, stride, 4) for b in (row[i + 2], row[i + 1], row[i], row[i + 3]))
        else:
            px = bytes(b for i in range(0, stride, 3) for b in (row[i + 2], row[i + 1], row[i], 255))
        rows.append(b"\x00" + px)
    if not desc & 0x20:
        rows.reverse()
    return _png(width, height, b"".join(rows))


def _png(width: int, height: int, scanlines: bytes) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def pose_cards(cpy_path: str = DEFAULT_CPY, cache_dir: str = "") -> list[dict]:
    """카드에 바로 쓸 목록: ``{index, name, image}`` (image 는 data URL).

    PNG 는 cache_dir/hand_poses/<파일해시>/ 에 남겨 두 번째부터는 디코딩을 건너뛴다.
    파일이 바뀌면 해시가 바뀌어 저절로 새로 굽는다.

    파일이 아직 없으면 빈 목록이다 — 전신 포즈 선반은 사용자가 맥스에서 처음
    저장하는 순간 파일이 생기므로, 그때까지 오류로 막으면 선반을 열 수조차 없다.
    """
    if not os.path.isfile(cpy_path):
        return []
    with open(cpy_path, "rb") as handle:
        data = handle.read()
    digest = hashlib.sha1(data).hexdigest()[:12]
    png_dir = os.path.join(cache_dir, "hand_poses", digest) if cache_dir else ""
    if png_dir:
        os.makedirs(png_dir, exist_ok=True)
    cards = []
    for pose in parse_cpy(data):
        png_path = os.path.join(png_dir, f"{pose.index:02d}.png") if png_dir else ""
        if png_path and os.path.isfile(png_path):
            with open(png_path, "rb") as handle:
                png = handle.read()
        else:
            png = tga_to_png(pose.tga)
            if png_path:
                with open(png_path, "wb") as handle:
                    handle.write(png)
        cards.append(
            {
                "index": pose.index,
                "name": pose.name,
                "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            }
        )
    return cards


def source_side(copy_name: str) -> str:
    """포스처를 **어느 쪽에서 복사했는지** 이름으로 읽는다. 모르면 빈 문자열.

    Biped 의 복사 포스처는 원본 노드를 기억한다. 그대로 붙이면 복사한 쪽으로,
    미러로 붙이면 반대쪽으로 간다. 즉 "왼쪽에 넣기" 가 미러인지 아닌지는
    **원본이 어느 쪽이냐에 따라 뒤집힌다.**

    맥스에서 손·팔·다리를 복사하면 이름이 R/L 로 시작한다(RFing01, LArm01).
    그 글자가 원본 쪽을 말해 주는 유일한 단서다.
    """
    head = (copy_name or "")[:1].upper()
    if head == "R":
        return "right"
    if head == "L":
        return "left"
    return ""


def opposites_for(source: str, target: str) -> list:
    """원본 쪽과 넣을 쪽을 보고 미러 여부를 정한다.

    같은 쪽이면 그대로, 반대쪽이면 미러다. 이름으로 원본을 모르면 예전처럼
    "원본은 오른쪽" 으로 가정한다 — 기본 컬렉션이 그렇게 만들어져 있다.
    """
    if target == "both":
        return [False, True]
    if not source:
        return [False] if target == "right" else [True]
    return [target != source]
