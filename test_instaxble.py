import pytest
from unittest.mock import MagicMock, patch
from struct import unpack_from
from io import BytesIO
from PIL import Image

from InstaxBLE.InstaxBLE import InstaxBLE
from InstaxBLE.Types import EventType


@pytest.fixture(scope='module')
def instax():
    mock_adapter = MagicMock()
    mock_adapter.identifier.return_value = 'hci0'
    with patch('simplepyble.Adapter.get_adapters', return_value=[mock_adapter]):
        return InstaxBLE(dummy_printer=True, quiet=True)


# ── _create_packet ───────────────────────────────────────────────────────────

class TestCreatePacket:

    def test_header(self, instax):
        packet = instax._create_packet(EventType.BLE_CONNECT)
        assert packet[:2] == b'\x41\x62'

    def test_length_no_payload(self, instax):
        packet = instax._create_packet(EventType.BLE_CONNECT)
        length = unpack_from('>H', packet, 2)[0]
        assert length == InstaxBLE._PACKET_OVERHEAD
        assert len(packet) == InstaxBLE._PACKET_OVERHEAD

    def test_length_with_payload(self, instax):
        payload = b'\x01\x02\x03'
        packet = instax._create_packet(EventType.BLE_CONNECT, payload)
        length = unpack_from('>H', packet, 2)[0]
        assert length == InstaxBLE._PACKET_OVERHEAD + len(payload)
        assert len(packet) == InstaxBLE._PACKET_OVERHEAD + len(payload)

    def test_opcode_bytes(self, instax):
        packet = instax._create_packet(EventType.BLE_CONNECT)
        assert packet[4] == 1   # BLE_CONNECT = (1, 3)
        assert packet[5] == 3

    def test_payload_embedded(self, instax):
        payload = b'\xAA\xBB\xCC'
        packet = instax._create_packet(EventType.BLE_CONNECT, payload)
        assert packet[6:9] == payload

    def test_checksum_validates(self, instax):
        for event in [EventType.BLE_CONNECT, EventType.PRINT_IMAGE, EventType.SHUT_DOWN]:
            packet = instax._create_packet(event, b'\x01\x02')
            assert instax._validate_checksum(packet), f'Checksum failed for {event}'

    def test_enum_and_tuple_equivalent(self, instax):
        assert instax._create_packet(EventType.BLE_CONNECT) == instax._create_packet((1, 3))


# ── _pil_image_to_bytes ──────────────────────────────────────────────────────

def _img(w, h, mode='RGB'):
    if mode == 'L':
        return Image.new('L', (w, h), color=100)
    if mode == 'RGBA':
        return Image.new('RGBA', (w, h), color=(128, 64, 32, 200))
    return Image.new('RGB', (w, h), color=(128, 64, 32))


class TestPilImageToBytes:

    def test_returns_bytearray_with_jpeg_magic(self, instax):
        result = instax._pil_image_to_bytes(_img(600, 800))
        assert isinstance(result, bytearray)
        assert result[:2] == b'\xff\xd8'

    def test_size_limit_respected(self, instax):
        result = instax._pil_image_to_bytes(_img(600, 800), max_size_kb=50)
        assert len(result) / 1024 <= 50

    def test_rgba_produces_valid_jpeg(self, instax):
        result = instax._pil_image_to_bytes(_img(600, 800, mode='RGBA'))
        assert result[:2] == b'\xff\xd8'

    def test_grayscale_produces_valid_jpeg(self, instax):
        result = instax._pil_image_to_bytes(_img(600, 800, mode='L'))
        assert result[:2] == b'\xff\xd8'

    def test_landscape_auto_rotated_to_portrait(self, instax):
        result = instax._pil_image_to_bytes(_img(800, 600))  # landscape → mini needs portrait
        out = Image.open(BytesIO(bytes(result)))
        assert out.width == 600
        assert out.height == 800

    def test_output_dimensions_match_printer(self, instax):
        result = instax._pil_image_to_bytes(_img(1200, 1600))
        out = Image.open(BytesIO(bytes(result)))
        assert (out.width, out.height) == instax._image_size

    def test_no_size_limit_produces_valid_jpeg(self, instax):
        result = instax._pil_image_to_bytes(_img(600, 800), max_size_kb=None)
        assert isinstance(result, bytearray)
        assert result[:2] == b'\xff\xd8'
