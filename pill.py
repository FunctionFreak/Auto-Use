"""
Floating pill banner with TRUE per-pixel transparency (Windows).

Why the previous border existed and why it's gone now:
  tkinter's "-transparentcolor" only does on/off (color-key) transparency, so
  the anti-aliased rim of the pill could only blend toward the near-black key
  color -> a thin dark rim on light backgrounds. This version doesn't use a key
  color at all. It paints the window through Win32 UpdateLayeredWindow with a
  real 32-bit alpha channel, so the edges blend against your actual wallpaper.
  No key color, no rim, on any background.

  - Smooth curves: shape is rendered by Pillow at 4x and downscaled (LANCZOS).
  - Drag with left mouse, close with right-click or Esc.
  - Set SHADOW = True for a soft floating shadow (also clean, no rim).

Requires Pillow:  pip install pillow
Windows only (uses the Win32 layered-window API).
"""

import ctypes
from ctypes import wintypes
from PIL import Image, ImageDraw, ImageChops, ImageFont

# --------------------------------------------------------------------------- #
#  Win32 plumbing
# --------------------------------------------------------------------------- #
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t,
)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


# function signatures (set restypes so 64-bit handles aren't truncated)
P = ctypes.POINTER
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [P(WNDCLASS)]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p, P(BITMAPINFO), ctypes.c_uint, P(ctypes.c_void_p),
    ctypes.c_void_p, wintypes.DWORD,
]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, P(POINT), P(SIZE),
    ctypes.c_void_p, P(POINT), wintypes.DWORD, P(BLENDFUNCTION), wintypes.DWORD,
]
user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
user32.SetCapture.restype = ctypes.c_void_p
user32.SetCapture.argtypes = [ctypes.c_void_p]
user32.ReleaseCapture.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [P(POINT)]
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [P(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
user32.TranslateMessage.argtypes = [P(MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.DispatchMessageW.argtypes = [P(MSG)]

# constants
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080          # keep it off the taskbar
SW_SHOW = 5
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
DIB_RGB_COLORS = 0
WM_DESTROY = 0x0002
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_KEYDOWN = 0x0100
WM_TIMER = 0x0113
VK_ESCAPE = 0x1B
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
IDC_ARROW = 32512


# --------------------------------------------------------------------------- #
#  The banner
# --------------------------------------------------------------------------- #
class PillBanner:
    PILL_W = 350
    PILL_H = 42
    MARGIN = 18
    SS = 4

    SHADOW = False          # <-- True for a soft floating shadow
    SHADOW_ALPHA = 60
    SHADOW_BLUR = 9
    SHADOW_OFFSET = 2

    def __init__(self):
        self.CW = self.PILL_W + 2 * self.MARGIN
        self.CH = self.PILL_H + 2 * self.MARGIN

        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        self.x = (sw - self.CW) // 2
        self.y = (sh - self.CH) // 2

        # animation / interaction state
        self.phase = "grow"
        self.size = 2
        self.width = self.PILL_H
        self.hold_ticks = 0
        self.dragging = False
        self.drag_cursor = (0, 0)
        self.drag_win = (0, 0)

        self._font = self._load_font()
        self._make_window()
        self._make_dib()
        self._blit(self.size, self.size)
        user32.SetTimer(self.hwnd, 1, 16, None)
        self._loop()

    # ----- window + bitmap setup -----
    def _make_window(self):
        self.hinst = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._on_message)   # keep a strong reference
        wc = WNDCLASS()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self.hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.lpszClassName = "PillBannerWindow"
        self._wc = wc
        user32.RegisterClassW(ctypes.byref(wc))

        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        self.hwnd = user32.CreateWindowExW(
            ex, "PillBannerWindow", "Pill", WS_POPUP,
            self.x, self.y, self.CW, self.CH,
            None, None, self.hinst, None,
        )
        user32.ShowWindow(self.hwnd, SW_SHOW)

    def _make_dib(self):
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.CW
        bmi.bmiHeader.biHeight = -self.CH        # negative => top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        self._bmi = bmi
        self.bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(
            self.screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(self.bits), None, 0,
        )
        self.old_obj = gdi32.SelectObject(self.mem_dc, self.hbmp)

    @staticmethod
    def _load_font():
        for name in ("segoeuib.ttf", "segoeui.ttf"):
            try:
                return ImageFont.truetype(name, 13)
            except Exception:
                pass
        return ImageFont.load_default()

    # ----- rendering -----
    def _render(self, w, h, decorate):
        cx, cy = self.CW / 2, self.CH / 2
        r = min(w, h) / 2

        big = Image.new("RGBA", (self.CW * self.SS, self.CH * self.SS), (0, 0, 0, 0))
        ImageDraw.Draw(big).rounded_rectangle(
            [(cx - w / 2) * self.SS, (cy - h / 2) * self.SS,
             (cx + w / 2) * self.SS, (cy + h / 2) * self.SS],
            radius=r * self.SS, fill=(255, 255, 255, 255))
        img = big.resize((self.CW, self.CH), Image.LANCZOS)

        if self.SHADOW:
            from PIL import ImageFilter
            shadow = Image.new("RGBA", (self.CW, self.CH), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle(
                [cx - w / 2, cy - h / 2 + self.SHADOW_OFFSET,
                 cx + w / 2, cy + h / 2 + self.SHADOW_OFFSET],
                radius=r, fill=(0, 0, 0, self.SHADOW_ALPHA))
            shadow = shadow.filter(ImageFilter.GaussianBlur(self.SHADOW_BLUR))
            img = Image.alpha_composite(shadow, img)

        if decorate:
            d = ImageDraw.Draw(img)
            yy = cy - self.PILL_H / 2 + 8
            y_end = cy + self.PILL_H / 2 - 8
            while yy < y_end:                      # dashed divider
                d.line([(cx, yy), (cx, min(yy + 4, y_end))], fill=(224, 224, 224, 255), width=2)
                yy += 8
            text = "Drag Me!  |  Right-Click to Close"
            bb = d.textbbox((0, 0), text, font=self._font)
            d.text((cx - (bb[2] - bb[0]) / 2, cy - (bb[3] + bb[1]) / 2),
                   text, font=self._font, fill=(51, 51, 51, 255))

        # premultiply alpha and reorder to BGRA for UpdateLayeredWindow
        rr, gg, bb_, aa = img.split()
        out = Image.merge("RGBA", (ImageChops.multiply(bb_, aa),
                                   ImageChops.multiply(gg, aa),
                                   ImageChops.multiply(rr, aa), aa))
        return out.tobytes()

    def _blit(self, w, h, decorate=False):
        data = self._render(w, h, decorate)
        ctypes.memmove(self.bits, data, len(data))
        ptDst = POINT(self.x, self.y)
        size = SIZE(self.CW, self.CH)
        ptSrc = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self.hwnd, self.screen_dc, ctypes.byref(ptDst), ctypes.byref(size),
            self.mem_dc, ctypes.byref(ptSrc), 0, ctypes.byref(blend), ULW_ALPHA,
        )

    # ----- message handling -----
    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            self._tick()
            return 0
        if msg == WM_LBUTTONDOWN:
            self.dragging = True
            user32.SetCapture(self.hwnd)
            p = POINT()
            user32.GetCursorPos(ctypes.byref(p))
            self.drag_cursor = (p.x, p.y)
            self.drag_win = (self.x, self.y)
            return 0
        if msg == WM_MOUSEMOVE and self.dragging:
            p = POINT()
            user32.GetCursorPos(ctypes.byref(p))
            self.x = self.drag_win[0] + (p.x - self.drag_cursor[0])
            self.y = self.drag_win[1] + (p.y - self.drag_cursor[1])
            user32.SetWindowPos(self.hwnd, None, self.x, self.y, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            return 0
        if msg == WM_LBUTTONUP:
            self.dragging = False
            user32.ReleaseCapture()
            return 0
        if msg == WM_RBUTTONUP:
            user32.DestroyWindow(self.hwnd)
            return 0
        if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
            user32.DestroyWindow(self.hwnd)
            return 0
        if msg == WM_DESTROY:
            self._cleanup()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _tick(self):
        if self.phase == "grow":
            self.size = min(self.size + 2, self.PILL_H)
            self._blit(self.size, self.size)
            if self.size >= self.PILL_H:
                self.phase = "hold"
        elif self.phase == "hold":
            self.hold_ticks += 1
            if self.hold_ticks >= 250:             # ~4 seconds at 16ms
                self.phase = "expand"
        elif self.phase == "expand":
            self.width = min(self.width + 8, self.PILL_W)
            done = self.width >= self.PILL_W
            self._blit(self.width, self.PILL_H, decorate=done)
            if done:
                self.phase = "done"
                user32.KillTimer(self.hwnd, 1)

    def _cleanup(self):
        user32.KillTimer(self.hwnd, 1)
        if getattr(self, "old_obj", None):
            gdi32.SelectObject(self.mem_dc, self.old_obj)
        if getattr(self, "hbmp", None):
            gdi32.DeleteObject(self.hbmp)
        if getattr(self, "mem_dc", None):
            gdi32.DeleteDC(self.mem_dc)
        if getattr(self, "screen_dc", None):
            user32.ReleaseDC(None, self.screen_dc)

    def _loop(self):
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    PillBanner()