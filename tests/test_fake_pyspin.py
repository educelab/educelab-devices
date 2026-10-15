"""The fake PySpin punishes the mistakes the real SDK punishes."""

import pytest

from fakes.pyspin import FakeCamera, FakeImage, FakePySpin, FrameSpec


@pytest.fixture
def pyspin():
    return FakePySpin([FakeCamera("1"), FakeCamera("2"), FakeCamera("3")])


def test_release_instance_succeeds_after_clean_teardown(pyspin):
    system = pyspin.System.GetInstance()
    cameras = system.GetCameras()
    for i in range(cameras.GetSize()):
        cam = cameras.GetByIndex(i)
        cam.Init()
        cam.DeInit()
        del cam
    cameras.Clear()
    system.ReleaseInstance()
    assert pyspin.system.instances == 0


def test_release_instance_fails_with_uncleared_list(pyspin):
    system = pyspin.System.GetInstance()
    system.GetCameras()
    with pytest.raises(pyspin.SpinnakerException, match="not cleared"):
        system.ReleaseInstance()


def test_release_instance_fails_while_a_camera_reference_is_held(pyspin):
    system = pyspin.System.GetInstance()
    cameras = system.GetCameras()
    for _cam in cameras:
        pass
    cameras.Clear()
    with pytest.raises(pyspin.SpinnakerException, match="reference"):
        system.ReleaseInstance()
    del _cam
    system.ReleaseInstance()


def test_clearing_a_list_mid_iteration_skips_the_rest(pyspin):
    # The pgs-scan #90 pattern: only the first camera gets de-initialized.
    system = pyspin.System.GetInstance()
    cameras = system.GetCameras()
    for i in range(cameras.GetSize()):
        cameras.GetByIndex(i).Init()
    visited = []
    for cam in cameras:
        visited.append(cam.GetUniqueID())
        cam.DeInit()
        cameras.Clear()
    del cam
    assert visited == ["1"]
    with pytest.raises(pyspin.SpinnakerException, match="still initialized"):
        system.ReleaseInstance()


def test_nodes_need_init_but_transport_layer_nodes_do_not(pyspin):
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    assert pyspin.IsReadable(cam.TLDevice.DeviceSerialNumber)
    assert cam.TLDevice.DeviceSerialNumber.ToString() == "1"
    assert not pyspin.IsReadable(cam.ExposureTime)
    cam.Init()
    assert pyspin.IsReadable(cam.ExposureTime)


def test_auto_exposure_makes_exposure_time_read_only(pyspin):
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.ExposureAuto.SetValue(pyspin.ExposureAuto_Continuous)
    assert not pyspin.IsWritable(cam.ExposureTime)
    cam.ExposureAuto.SetValue(pyspin.ExposureAuto_Off)
    cam.ExposureTime.SetValue(5000.0)
    assert cam.ExposureTime.GetValue() == 5000.0
    with pytest.raises(pyspin.SpinnakerException):
        cam.ExposureTime.SetValue(cam.ExposureTime.GetMax() + 1)


def test_pixel_format_is_read_only_while_acquiring(pyspin):
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.BeginAcquisition()
    assert not pyspin.IsWritable(cam.PixelFormat)
    cam.EndAcquisition()
    cam.PixelFormat.SetValue(pyspin.PixelFormat_Mono16)
    assert cam.PixelFormat.GetCurrentEntry().GetSymbolic() == "Mono16"


def test_end_acquisition_twice_raises(pyspin):
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.BeginAcquisition()
    cam.EndAcquisition()
    with pytest.raises(pyspin.SpinnakerException):
        cam.EndAcquisition()


def test_unreleased_buffers_exhaust_the_pool():
    camera = FakeCamera(buffer_count=2)
    pyspin = FakePySpin([camera])
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.BeginAcquisition()
    cam.GetNextImage(1000)
    image = cam.GetNextImage(1000)
    with pytest.raises(pyspin.SpinnakerException, match="no free buffers"):
        cam.GetNextImage(1000)
    image.Release()
    cam.GetNextImage(1000)
    assert len(camera.held) == 2


def test_released_buffers_are_overwritten(pyspin):
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.BeginAcquisition()
    image = cam.GetNextImage()
    view = image.GetNDArray()
    copy = view.copy()
    image.Release()
    assert (view == FakeImage.RELEASED_FILL).all()
    assert not (copy == FakeImage.RELEASED_FILL).all()
    with pytest.raises(pyspin.SpinnakerException):
        image.GetNDArray()


def test_queued_frames_come_first_and_carry_status(pyspin):
    camera = pyspin.system.cameras[0]
    camera.queue_frames(
        FrameSpec(timestamp_ns=5, status=pyspin.IMAGE_DATA_INCOMPLETE), FrameSpec()
    )
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.BeginAcquisition()
    stale = cam.GetNextImage()
    assert stale.GetTimeStamp() == 5
    assert stale.IsIncomplete()
    fresh = cam.GetNextImage()
    assert not fresh.IsIncomplete()
    assert fresh.GetTimeStamp() > 5


def test_timestamp_latch_follows_the_host_clock():
    now = [1_000_000.0]
    camera = FakeCamera(clock=lambda: now[0], uptime=50.0)
    cam = FakePySpin([camera]).System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.TimestampLatch.Execute()
    assert cam.Timestamp.GetValue() == 50_000_000_000
    now[0] += 1.5
    cam.TimestampLatch.Execute()
    assert cam.Timestamp.GetValue() == 51_500_000_000


def test_color_cameras_produce_rgb_arrays():
    camera = FakeCamera(color=True, width=4, height=3)
    pyspin = FakePySpin([camera])
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    assert cam.PixelColorFilter.GetValue() == pyspin.PixelColorFilter_BayerRG
    cam.PixelFormat.SetValue(pyspin.PixelFormat_RGB8)
    cam.BeginAcquisition()
    assert cam.GetNextImage().GetNDArray().shape == (3, 4, 3)


def test_detached_cameras_fail(pyspin):
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    pyspin.detach("1")
    with pytest.raises(pyspin.SpinnakerException, match="lost"):
        cam.TLDevice.DeviceSerialNumber.GetValue()
    with pytest.raises(pyspin.SpinnakerException, match="lost"):
        cam.Init()


def test_get_by_serial_for_a_missing_camera_is_invalid(pyspin):
    cameras = pyspin.System.GetInstance().GetCameras()
    assert cameras.GetBySerial("2").IsValid()
    assert not cameras.GetBySerial("nope").IsValid()


def test_trigger_mode_needs_a_trigger_per_fresh_frame(pyspin):
    camera = pyspin.system.cameras[0]
    camera.queue_frames(FrameSpec(timestamp_ns=5))
    cam = pyspin.System.GetInstance().GetCameras().GetByIndex(0)
    cam.Init()
    cam.TriggerMode.SetValue(pyspin.TriggerMode_Off)
    cam.TriggerSource.SetValue(pyspin.TriggerSource_Software)
    cam.TriggerMode.SetValue(pyspin.TriggerMode_On)
    assert not pyspin.IsWritable(cam.TriggerSource)
    cam.BeginAcquisition()
    cam.GetNextImage().Release()  # already waiting, no trigger needed
    with pytest.raises(pyspin.SpinnakerException, match="no frame was triggered"):
        cam.GetNextImage()
    before = camera.device_time_ns()
    cam.TriggerSoftware.Execute()
    image = cam.GetNextImage()
    assert image.GetTimeStamp() >= before
    image.Release()
    camera.respond_to_triggers = False
    cam.TriggerSoftware.Execute()
    with pytest.raises(pyspin.SpinnakerException):
        cam.GetNextImage()
