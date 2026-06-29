# Copyright 2026 Autouse AI — https://github.com/auto-use/Auto-Use
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# If you build on this project, please keep this header and credit
# Autouse AI (https://github.com/auto-use/Auto-Use) in forks and derivative works.
# A small attribution goes a long way toward a healthy open-source
# community — thank you for contributing.

"""
OCR Detection — macOS Apple Vision OCR scanner (ocrmac).

Mirrors the Windows OCRScanner (windows_use/tree/ocr_detection.py): captures
nothing itself — it OCRs a PRE-CAPTURED screenshot so it shares the exact frame
the AX scan + annotation use — and returns a raw line list. Filtering/merging
against the AX element tree happens in the caller (element.py).

Designed to run as a parallel thread alongside the AX walk. The returned line
boxes are already converted to CG/AX POINTS (top-left origin) so they live in
the same coordinate space as AX element rects.
"""

from ocrmac import ocrmac

# Drop low-confidence detections (passed straight to ocrmac) and stray
# single-character / blank lines that add noise without being actionable.
MIN_CONFIDENCE = 0.4
MIN_TEXT_LEN = 2


class OCRScanner:
    """Run Apple Vision OCR over a pre-captured PIL screenshot.

    Stores a raw line list in CG/AX POINTS:
        [{text, left, top, right, bottom, confidence}, ...]
    Thread-safe for parallel execution — each ocrmac call creates its own
    Vision request and autorelease pool, with no shared global state.
    """

    def __init__(self, pil_image, scale, screen_origin, recognition_level="accurate"):
        self._image = pil_image                 # PIL image in PIXELS (already captured)
        self._scale = scale                     # backing scale factor (retina)
        self._ox, self._oy = screen_origin      # screen["x"], screen["y"] in CG points
        self._level = recognition_level
        self.lines = []

    def scan(self):
        """OCR the image and store results in self.lines. Zero-arg so it can be
        used directly as a threading.Thread target."""
        if self._image is None:
            self.lines = []
            return
        try:
            # px=True returns pixel coords already flipped to top-left origin,
            # so we only need the pixel -> CG-point conversion below.
            annotations = ocrmac.OCR(
                self._image,
                recognition_level=self._level,
                confidence_threshold=MIN_CONFIDENCE,
            ).recognize(px=True)
        except Exception as e:
            print(f"OCR error: {e}")
            self.lines = []
            return

        s, ox, oy = self._scale, self._ox, self._oy
        out = []
        for text, conf, (x1, y1, x2, y2) in annotations:
            t = (text or "").strip()
            if len(t) < MIN_TEXT_LEN:
                continue
            # pixel -> CG points: inverse of element.py's (rect.left - ox) * scale
            left = int(round(ox + x1 / s))
            top = int(round(oy + y1 / s))
            right = int(round(ox + x2 / s))
            bottom = int(round(oy + y2 / s))
            if right <= left or bottom <= top:
                continue
            out.append({
                "text": t,
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "confidence": float(conf),
            })
        self.lines = out

    def get_lines(self):
        """Return raw line list: [{text, left, top, right, bottom, confidence}, ...]
        in CG/AX points."""
        return self.lines
