"""Locate each zoomed frame from `z7_live_view.py afmap` in the full frame.

    python conductor/tracks/preview-mode_20261102/z7_afmap_match.py z7-afmap

Needs opencv-python-headless. For each zoomed frame, tries magnifications
from 1.2x to 24x, shrinks the frame by each and finds it in the reference
with normalized cross-correlation. Prints the best box as fractions of the
frame, with its score; scores under about 0.5 are guesses.
"""

import argparse
import re
from pathlib import Path

import cv2
import numpy as np


def gray(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    return cv2.GaussianBlur(image, (3, 3), 0)


def locate(reference: np.ndarray, zoomed: np.ndarray):
    rh, rw = reference.shape
    best = (-1.0, 0.0, 0, 0)
    for scale in np.geomspace(1.2, 24, 100):
        w, h = round(zoomed.shape[1] / scale), round(zoomed.shape[0] / scale)
        if w < 16 or h < 12 or w > rw or h > rh:
            continue
        template = cv2.resize(zoomed, (w, h), interpolation=cv2.INTER_AREA)
        if template.std() < 2:
            continue
        result = cv2.matchTemplate(reference, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(result)
        if score > best[0]:
            best = (score, scale, x, y)
    score, scale, x, y = best
    if score < 0:
        return score, 0.0, (0.0, 0.0, 0.0, 0.0)
    w, h = zoomed.shape[1] / scale, zoomed.shape[0] / scale
    return score, scale, (x / rw, y / rh, w / rw, h / rh)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    reference = gray(args.directory / "reference-start.jpg")
    pattern = re.compile(r"zoom(\d+)-af(\d+)x(\d+)\.jpg")
    rows = []
    for path in args.directory.glob("zoom*-af*.jpg"):
        zoom, x, y = map(int, pattern.fullmatch(path.name).groups())
        rows.append((zoom, y, x, path))
    print("zoom  changeafarea   score  magnif  box (x, y, w, h) in frame fractions")
    for zoom, y, x, path in sorted(rows, key=lambda r: (-r[0], r[1], r[2])):
        score, scale, (bx, by, bw, bh) = locate(reference, gray(path))
        if not scale:
            print(f"{zoom:>4}  {x:>4}x{y:<4}    no texture to match")
            continue
        cx, cy = bx + bw / 2, by + bh / 2
        print(
            f"{zoom:>4}  {x:>4}x{y:<4}    {score:5.2f}  {scale:5.2f}x  "
            f"({bx:.3f}, {by:.3f}, {bw:.3f}, {bh:.3f})  centre ({cx:.3f}, {cy:.3f})"
        )


if __name__ == "__main__":
    main()
