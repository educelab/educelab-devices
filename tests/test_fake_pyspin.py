"""The fake PySpin punishes the mistakes the real SDK punishes.

The camera behind it is `fakes.genicam.FakeCamera`, tested on its own; these
tests cover only what PySpin adds.
"""

import pytest

from educelab.devices.cameras._genicam import BufferStatus
from fakes import pyspin as fake
from fakes.pyspin import FakeImage, FakePySpin, FrameSpec, flir_camera


@pytest.fixture
def pyspin():
    return FakePySpin([flir_camera("1"), flir_camera("2"), flir_camera("3")])


def first(pyspin):
    return pyspin.System.GetInstance().GetCameras().GetByIndex(0)


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
    cam = first(pyspin)
    assert pyspin.IsReadable(cam.TLDevice.DeviceSerialNumber)
    assert cam.TLDevice.DeviceSerialNumber.ToString() == "1"
    assert not pyspin.IsReadable(cam.ExposureTime)
    cam.Init()
    assert pyspin.IsReadable(cam.ExposureTime)


def test_enums_are_valued_by_the_module_constants(pyspin):
    cam = first(pyspin)
    cam.Init()
    cam.ExposureAuto.SetValue(pyspin.ExposureAuto_Off)
    assert cam.ExposureAuto.GetValue() == pyspin.ExposureAuto_Off
    assert cam.ExposureAuto.ToString() == "Off"
    entry = pyspin.CEnumerationPtr(cam.PixelFormat).GetEntryByName("Mono16")
    assert entry.GetValue() == pyspin.PixelFormat_Mono16
    assert pyspin.CEnumerationPtr(cam.PixelFormat).GetEntryByName("RGB8") is None


def test_integer_nodes_take_only_ints(pyspin):
    cam = first(pyspin)
    cam.Init()
    limit = cam.DeviceLinkThroughputLimit
    with pytest.raises(pyspin.SpinnakerException) as info:
        limit.SetValue(float(limit.GetMax()))
    assert info.value.errorcode == pyspin.SPINNAKER_ERR_INVALID_PARAMETER


def test_camera_failures_carry_spinnaker_error_codes(pyspin):
    cam = first(pyspin)
    with pytest.raises(pyspin.SpinnakerException) as info:
        cam.BeginAcquisition()
    assert info.value.errorcode == pyspin.SPINNAKER_ERR_NOT_INITIALIZED
    cam.Init()
    with pytest.raises(pyspin.SpinnakerException) as info:
        cam.ExposureTime.SetValue(1.0)  # locked by auto exposure
    assert info.value.errorcode == pyspin.SPINNAKER_ERR_ACCESS_DENIED
    cam.BeginAcquisition()
    with pytest.raises(pyspin.SpinnakerException) as info:
        cam.DeInit()
    assert info.value.errorcode == pyspin.SPINNAKER_ERR_RESOURCE_IN_USE
    pyspin.system.cameras[0].buffer_count = 0
    with pytest.raises(pyspin.SpinnakerException) as info:
        cam.GetNextImage(1000)
    assert info.value.errorcode == pyspin.SPINNAKER_ERR_TIMEOUT


def test_a_camera_held_elsewhere_refuses_init(pyspin):
    pyspin.system.cameras[0].busy = True
    with pytest.raises(pyspin.SpinnakerException) as info:
        first(pyspin).Init()
    assert info.value.errorcode == pyspin.SPINNAKER_ERR_ACCESS_DENIED


def test_released_images_are_overwritten_under_their_arrays(pyspin):
    cam = first(pyspin)
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


def test_image_status_follows_the_frame(pyspin):
    pyspin.system.cameras[0].queue_frames(
        FrameSpec(timestamp_ns=5, status=BufferStatus.INCOMPLETE),
        FrameSpec(status=BufferStatus.MISSING_TRAILER),
        FrameSpec(),
    )
    pyspin.image_status[BufferStatus.INCOMPLETE] = (
        pyspin.SPINNAKER_IMAGE_STATUS_MISSING_PACKETS
    )
    cam = first(pyspin)
    cam.Init()
    cam.BeginAcquisition()
    statuses = []
    for _ in range(3):
        image = cam.GetNextImage()
        statuses.append((image.GetImageStatus(), image.IsIncomplete()))
        image.Release()
    assert statuses == [
        (pyspin.SPINNAKER_IMAGE_STATUS_MISSING_PACKETS, True),
        (pyspin.SPINNAKER_IMAGE_STATUS_MISSING_TRAILER, True),
        (pyspin.SPINNAKER_IMAGE_STATUS_NO_ERROR, False),
    ]


def test_stream_buffer_handling_is_a_transport_layer_setting(pyspin):
    cam = first(pyspin)
    node = pyspin.CEnumerationPtr(
        cam.GetTLStreamNodeMap().GetNode("StreamBufferHandlingMode")
    )
    assert pyspin.IsWritable(node)
    node.SetIntValue(pyspin.StreamBufferHandlingMode_NewestOnly)
    stream = pyspin.transport(pyspin.system.cameras[0]).stream
    assert stream.nodes["StreamBufferHandlingMode"].value == "NewestOnly"


def test_detached_cameras_fail(pyspin):
    cam = first(pyspin)
    pyspin.detach("1")
    with pytest.raises(pyspin.SpinnakerException, match="lost") as info:
        cam.TLDevice.DeviceSerialNumber.GetValue()
    assert info.value.errorcode == pyspin.SPINNAKER_ERR_IO
    with pytest.raises(pyspin.SpinnakerException, match="lost"):
        cam.Init()


def test_get_by_serial_for_a_missing_camera_is_invalid(pyspin):
    cameras = pyspin.System.GetInstance().GetCameras()
    assert cameras.GetBySerial("2").IsValid()
    assert not cameras.GetBySerial("nope").IsValid()


# --- Against the real PySpin --------------------------------------------------

# The fake's stand-ins and the real classes they imitate. PySpin's API names start
# with a capital letter; the fake's own helpers don't.
_STAND_INS = {
    fake.CameraPtr: ("CameraPtr",),
    fake.CameraList: ("CameraList",),
    fake.FakeImage: ("ImagePtr",),
    fake.FakeSystem: ("System", "SystemPtr"),
    fake.Image: ("Image",),
    fake.NodeMap: ("INodeMap",),
    fake.EnumNode: ("IEnumerationT_PixelFormatEnums",),
    fake.EnumerationPtr: ("CEnumerationPtr",),
    fake.EnumEntry: ("CEnumEntryPtr",),
    fake.FloatNode: ("CFloatPtr",),
    fake.IntegerNode: ("CIntegerPtr",),
    fake.StringNode: ("CStringPtr",),
    fake.CommandNode: ("CCommandPtr",),
}


def _api_names(obj):
    return sorted(n for n in dir(obj) if n[:1].isupper() and not n.isupper())


@pytest.fixture(scope="module")
def real():
    return pytest.importorskip("PySpin")


def test_fake_module_names_exist_in_pyspin(real):
    names = [n for n in dir(FakePySpin()) if n[:1].isupper()]
    assert [n for n in names if not hasattr(real, n)] == []


def test_fake_node_names_exist_in_pyspin(real):
    camera = flir_camera(color=True, throughput_mode=True)
    transport = FakePySpin([camera]).transport(camera)
    for nodes, owner in (
        (camera.nodes, real.CameraPtr),
        (transport.device.nodes, real.TransportLayerDevice),
        (transport.stream.nodes, real.TransportLayerStream),
    ):
        assert [n for n in nodes if not hasattr(owner, n)] == []


def test_fake_constants_match_pyspin(real):
    fake_module = FakePySpin()
    exact = [
        n
        for n in dir(fake_module)
        if n.startswith(("SPINNAKER_", "EVENT_TIMEOUT_"))
        or n in ("NI", "NA", "WO", "RO", "RW")
    ]
    assert exact
    wrong = {
        n: getattr(fake_module, n)
        for n in exact
        if getattr(fake_module, n) != getattr(real, n)
    }
    assert wrong == {}


@pytest.mark.parametrize("stand_in", list(_STAND_INS), ids=lambda c: c.__name__)
def test_fake_methods_exist_in_pyspin(real, stand_in):
    real_names = set()
    for name in _STAND_INS[stand_in]:
        real_names |= set(dir(getattr(real, name)))
    assert [n for n in _api_names(stand_in) if n not in real_names] == []


def test_fake_library_version_and_exception_match_pyspin(real):
    assert all(
        hasattr(real.LibraryVersion, f) for f in ("major", "minor", "type", "build")
    )
    exception = real.SpinnakerException
    for attribute in ("errorcode", "message", "fullmessage"):
        assert hasattr(exception, attribute) or attribute in dir(exception)
