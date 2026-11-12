import sys
from collections import deque

import numpy as np
import pytest

from educelab.devices.app import preview
from educelab.devices.app.cli import build_parser, main
from educelab.devices.app.preview import View, eight_bit
from educelab.devices.cameras.simulated import SimulatedCamera, SimulatedFileCamera
from educelab.devices.errors import DeviceError


class FakeOpenCV:
    """The parts of ``cv2`` the viewer uses, with scripted input.

    ``script`` holds what each ``waitKey()`` does, in order: a key to press,
    a function that sends mouse events through ``mouse``, or None for no
    input. After the script, ``waitKey()`` presses ``q``.
    """

    WINDOW_AUTOSIZE = COLOR_GRAY2BGR = INTER_AREA = FONT_HERSHEY_SIMPLEX = 0
    COLOR_RGB2BGR = INTER_NEAREST = 1
    EVENT_MOUSEMOVE, EVENT_LBUTTONDOWN, EVENT_LBUTTONUP = 0, 1, 4
    EVENT_MOUSEWHEEL, EVENT_MOUSEHWHEEL = 10, 11
    EVENT_FLAG_LBUTTON = 1

    def __init__(self, *script):
        self.script = deque(script)
        self.shown = []
        self.mouse = None

    def namedWindow(self, title, flags):
        pass

    def setMouseCallback(self, title, callback):
        self.mouse = callback

    def cvtColor(self, pixels, code):
        if code == self.COLOR_GRAY2BGR:
            return np.repeat(pixels[..., None], 3, axis=2)
        return pixels[..., ::-1]

    def resize(self, image, size, interpolation):
        rows = np.arange(size[1]) * image.shape[0] // size[1]
        columns = np.arange(size[0]) * image.shape[1] // size[0]
        return image[rows][:, columns].copy()

    def putText(self, *args):
        pass

    def rectangle(self, *args):
        pass

    def imshow(self, title, image):
        self.shown.append(image)

    def waitKey(self, delay):
        step = self.script.popleft() if self.script else "q"
        if step is None:
            return -1
        if callable(step):
            step(self)
            return -1
        return ord(step)

    def destroyAllWindows(self):
        pass


@pytest.fixture
def opencv(monkeypatch):
    """Install a `FakeOpenCV` playing ``script``, and open ``camera`` in
    place of the simulated one."""
    monkeypatch.setattr(preview, "has_opencv", lambda: True)
    monkeypatch.setattr(preview, "SETTLE_S", 0.0)
    monkeypatch.setattr(preview, "TICK_MS", 1)
    # No real windows: leave macOS alone.
    for name in ("activate", "accept_first_clicks", "visible_screen_size"):
        monkeypatch.setattr(preview._macos, name, lambda: None)
    # Wait for each new region to show, so the scripts play out in step.
    ask = preview._Feed.ask
    monkeypatch.setattr(
        preview._Feed, "ask", lambda feed, r: (ask(feed, r), feed.caught_up.wait(5))
    )

    def install(*script, camera=None):
        fake = FakeOpenCV(*script)
        monkeypatch.setitem(sys.modules, "cv2", fake)
        if camera is not None:
            monkeypatch.setattr(preview, "_open", lambda args: camera)
        return fake

    return install


def spy(camera):
    """Record each preview ``camera`` is asked for."""
    asked = []
    start = camera.start_preview

    def start_preview(region=None, max_size=None):
        asked.append((region, max_size))
        return start(region, max_size)

    camera.start_preview = start_preview
    return asked


def test_the_subcommand_is_only_offered_with_opencv(monkeypatch, capsys):
    monkeypatch.setattr(preview, "has_opencv", lambda: False)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["preview", "simulated"])
    assert "invalid choice: 'preview'" in capsys.readouterr().err
    monkeypatch.setattr(preview, "has_opencv", lambda: True)
    assert build_parser().parse_args(["preview", "simulated"]).backend == "simulated"


def test_has_opencv_is_false_when_it_cannot_be_found(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    assert not preview.has_opencv()


def test_shows_the_whole_sensor_reduced_to_the_window(opencv):
    camera = SimulatedCamera()
    asked = spy(camera)
    fake = opencv(None, None, None, camera=camera)
    assert main(["preview", "simulated", "--window", "200", "--frames", "3"]) == 0
    assert asked == [(None, 200)]
    assert {image.shape for image in fake.shown} == {(150, 200, 3)}
    assert camera.preview is None


def test_keys_zoom_move_and_reset(opencv, capsys):
    camera = SimulatedCamera()
    asked = spy(camera)
    opencv("+", "-", "+", "d", "s", "r", "q", camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    regions = [region for region, _ in asked[1:]]
    assert regions[0] == pytest.approx((0.1, 0.1, 0.8, 0.8))
    assert regions[1] is None
    assert regions[2] == pytest.approx((0.1, 0.1, 0.8, 0.8))
    # Moves stop at the sensor's edge.
    assert regions[3] == pytest.approx((0.2, 0.1, 0.8, 0.8))
    assert regions[4] == pytest.approx((0.2, 0.2, 0.8, 0.8))
    assert regions[5] is None
    assert "PreviewGeometry" in capsys.readouterr().out


def test_a_region_smaller_than_the_window_is_enlarged_to_fill_it(opencv):
    camera = SimulatedCamera()
    asked = spy(camera)
    fake = opencv(*"++++++++", camera=camera)
    assert main(["preview", "simulated", "--window", "400"]) == 0
    # 0.17 of a 640-pixel sensor: about 107 pixels, shown 400 wide.
    assert asked[-1][0][2] == pytest.approx(0.8**8)
    assert max(fake.shown[-1].shape[:2]) == 400


def test_capture_stops_the_preview_and_resumes_it(opencv, capsys):
    camera = SimulatedCamera()
    asked = spy(camera)
    states = []
    capture = camera.capture_frame
    camera.capture_frame = lambda *a, **k: states.append(camera.preview) or capture()
    opencv("c", "x", "q", camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    assert states == [None]
    assert len(asked) == 2
    assert "captured 640x480 Mono8" in capsys.readouterr().out


def test_capture_names_the_files_of_a_file_camera(opencv, capsys):
    camera = SimulatedFileCamera()
    opencv("c", "x", "q", camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    assert "captured " in capsys.readouterr().out


def test_scrolling_zooms_about_the_pointer_on_windows_and_linux(opencv, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    camera = SimulatedCamera()
    asked = spy(camera)

    def scroll(cv2):
        cv2.mouse(cv2.EVENT_MOUSEWHEEL, 150, 30, 120 << 16, None)

    opencv(scroll, "q", camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    # The point under the pointer, (0.75, 0.2) of the sensor, stays there.
    assert asked[1][0] == pytest.approx((0.15, 0.04, 0.8, 0.8))


def test_scrolling_on_macos_reads_the_amount_from_y(opencv, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    camera = SimulatedCamera()
    asked = spy(camera)

    def scroll(cv2):
        cv2.mouse(cv2.EVENT_MOUSEMOVE, 150, 30, 0, None)
        cv2.mouse(cv2.EVENT_MOUSEHWHEEL, 9, 0, 0, None)
        cv2.mouse(cv2.EVENT_MOUSEWHEEL, 0, preview.POINTS_PER_NOTCH, 0, None)

    opencv(scroll, "q", camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    assert asked[1][0] == pytest.approx((0.15, 0.04, 0.8, 0.8))


def test_dragging_moves_the_region_once_the_button_is_up(opencv):
    camera = SimulatedCamera()
    asked = spy(camera)

    def press(cv2):
        cv2.mouse(cv2.EVENT_LBUTTONDOWN, 100, 75, 1, None)
        cv2.mouse(cv2.EVENT_MOUSEMOVE, 60, 75, 1, None)

    def release(cv2):
        cv2.mouse(cv2.EVENT_LBUTTONUP, 60, 75, 1, None)

    opencv("+", "+", press, "x", release, "q", camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    regions = [region for region, _ in asked[1:]]
    # Nothing is asked of the camera while the button is down.
    assert len(regions) == 3
    assert regions[2][0] > regions[1][0] and regions[2][1] == regions[1][1]


def test_a_view_stays_on_the_sensor_and_no_smaller_than_its_limit():
    view = View()
    view.zoom(100)
    assert view.region[2] == pytest.approx(0.512)
    for _ in range(40):
        view.zoom(3)
    assert view.region[2] == preview.SMALLEST
    view.move(1000, 1000)
    x, y, side = view.region
    assert x + side == pytest.approx(1.0) and y + side == pytest.approx(1.0)
    view.zoom(-100)
    view.zoom(-100)
    for _ in range(20):
        view.zoom(-3)
    assert view.region == (0.0, 0.0, 1.0)
    assert view.request() is None


def test_a_view_waits_for_input_to_settle(monkeypatch):
    view = View()
    assert not view.settled()
    view.zoom(1)
    assert not view.settled()
    monkeypatch.setattr(preview, "SETTLE_S", 0.0)
    assert view.settled()
    view.dragging = True
    assert not view.settled()


@pytest.mark.parametrize(
    ("value", "pixel_format", "expected"),
    [
        (0x8000, "Mono16", 128),
        (512, "Mono10", 128),
        (2048, "Mono12", 128),
        (512, "Mono10g40IDS", 128),
        # A 12-bit format delivered left-aligned in 16 bits.
        (0x8000, "Mono12", 128),
    ],
)
def test_deeper_formats_are_scaled_to_eight_bits(value, pixel_format, expected):
    shown = eight_bit(np.full((2, 3), value, np.uint16), pixel_format)
    assert shown.dtype == np.uint8 and (shown == expected).all()


def test_eight_bit_pixels_are_shown_as_they_are():
    pixels = np.arange(6, dtype=np.uint8).reshape(2, 3)
    assert eight_bit(pixels, "Mono8") is pixels


def test_a_camera_error_ends_the_viewer_with_its_message(opencv, capsys):
    camera = SimulatedCamera()
    opencv(None, None, camera=camera)

    def fail(**kwargs):
        raise DeviceError("the camera went away")

    camera.preview_frame = fail
    assert main(["preview", "simulated", "--window", "200", "--frames", "5"]) == 1
    assert "the camera went away" in capsys.readouterr().err


def test_frames_keep_coming_while_the_window_waits_for_input(opencv):
    camera = SimulatedCamera()
    fake = opencv(*[None] * 20, camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    assert len(fake.shown) >= 20


def test_a_drag_works_without_its_press_or_release_arriving(opencv):
    camera = SimulatedCamera()
    asked = spy(camera)

    def drag(cv2):
        # Moves with the button down, then one with it up; no press, no release.
        cv2.mouse(cv2.EVENT_MOUSEMOVE, 100, 75, 1, None)
        cv2.mouse(cv2.EVENT_MOUSEMOVE, 60, 75, 1, None)
        cv2.mouse(cv2.EVENT_MOUSEMOVE, 60, 75, 0, None)

    def wander(cv2):
        cv2.mouse(cv2.EVENT_MOUSEMOVE, 10, 10, 0, None)

    opencv("+", "+", drag, wander, "q", camera=camera)
    assert main(["preview", "simulated", "--window", "200"]) == 0
    regions = [region for region, _ in asked[1:]]
    assert len(regions) == 3
    # 40 of 200 pixels: a fifth of the region's 0.64, and only that.
    assert regions[2][0] - regions[1][0] == pytest.approx(0.2 * 0.64)
    assert regions[2][1] == regions[1][1]


def test_the_image_is_kept_within_the_screen(opencv, monkeypatch):
    # A 640x480 image would be 1000x750, taller than this screen has room for.
    monkeypatch.setattr(preview._macos, "visible_screen_size", lambda: (1500, 640))
    fake = opencv(None, None, camera=SimulatedCamera())
    assert main(["preview", "simulated", "--frames", "2"]) == 0
    height, width = fake.shown[-1].shape[:2]
    assert height == 640 - preview.TITLE_BAR
    assert width == round(height * 640 / 480)


def test_macos_helpers_do_nothing_elsewhere(monkeypatch):
    from educelab.devices.app import _macos

    monkeypatch.setattr(sys, "platform", "linux")
    assert _macos.visible_screen_size() is None
    assert _macos.activate() is None
    assert _macos.accept_first_clicks() is None
