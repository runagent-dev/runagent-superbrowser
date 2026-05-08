"""Image-matching utilities shared by the jigsaw and rotation solvers.

Optional dependencies: `opencv-python`, `numpy`. When absent, callers see
a clear ImportError — the registry loads this lazily so the rest of the
solver framework keeps working.
"""

from __future__ import annotations

from typing import Optional


def find_piece_x_offset(container_jpeg: bytes, piece_jpeg_b64: str) -> int:
    """Return the x-pixel offset (inside the container crop) where the
    piece's sharp-edge mask best matches. Used by the jigsaw solver."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "OpenCV not installed (pip install opencv-python)."
        ) from e
    if not container_jpeg or not piece_jpeg_b64:
        raise RuntimeError("missing container or piece crop")
    import base64
    piece_bytes = base64.b64decode(piece_jpeg_b64)
    container = cv2.imdecode(
        np.frombuffer(container_jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE,
    )
    piece = cv2.imdecode(
        np.frombuffer(piece_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE,
    )
    if container is None or piece is None:
        raise RuntimeError("failed to decode one of the crops")
    # Edge-map both, then template match. Canny thresholds tuned for
    # typical geetest/arkose jigsaw artwork; consumers can override by
    # pre-processing their own crops if needed.
    container_edges = cv2.Canny(container, 50, 150)
    piece_edges = cv2.Canny(piece, 50, 150)
    res = cv2.matchTemplate(container_edges, piece_edges, cv2.TM_CCOEFF_NORMED)
    _minv, _maxv, _minloc, maxloc = cv2.minMaxLoc(res)
    return int(maxloc[0] + piece.shape[1] / 2)


def estimate_upright_angle_delta(image_jpeg: bytes) -> float:
    """Return the angle (degrees) by which the image needs to be rotated
    clockwise to appear upright. A rough estimator based on dominant
    gradient direction — good enough for 15°-granularity rotation
    captchas. Production systems can swap this for a CNN."""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "OpenCV not installed (pip install opencv-python)."
        ) from e
    arr = np.frombuffer(image_jpeg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise RuntimeError("failed to decode image")
    # Sobel gradients → histogram of orientations. Peak indicates the
    # dominant structural direction; most photos have their dominant
    # structure horizontal (or vertical) when upright.
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1)
    angles = (np.degrees(np.arctan2(gy, gx)) + 180) % 180  # [0, 180)
    hist, _ = np.histogram(angles.flatten(), bins=12, range=(0, 180))
    peak = int(hist.argmax() * 15) + 7  # centre of the winning 15° bucket
    # Wrap to the shortest rotation toward 0° (horizontal) or 90° (vertical).
    # We bias toward 0° because most captcha images are horizontally composed.
    delta_h = -peak if peak <= 90 else 180 - peak
    return float(delta_h)
