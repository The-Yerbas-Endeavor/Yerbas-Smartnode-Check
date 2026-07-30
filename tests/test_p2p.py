import struct
import unittest

from yerbas_p2p import (
    YERBAS_MAINNET_MAGIC,
    build_message,
    build_version_payload,
    decode_varint,
    encode_varint,
    parse_version_payload,
    sha256d,
)


class VarIntTests(unittest.TestCase):
    def test_round_trip_boundaries(self):
        for value in (0, 252, 253, 65535, 65536, 2**32, 2**63):
            encoded = encode_varint(value)
            decoded, offset = decode_varint(encoded, 0)
            self.assertEqual(value, decoded)
            self.assertEqual(len(encoded), offset)

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            encode_varint(-1)


class MessageTests(unittest.TestCase):
    def test_header_uses_yerbas_magic_and_checksum(self):
        payload = b"hello"
        message = build_message("ping", payload)
        self.assertEqual(YERBAS_MAINNET_MAGIC, message[:4])
        self.assertEqual(b"ping" + b"\x00" * 8, message[4:16])
        self.assertEqual(len(payload), struct.unpack_from("<I", message, 16)[0])
        self.assertEqual(sha256d(payload)[:4], message[20:24])
        self.assertEqual(payload, message[24:])

    def test_invalid_magic_rejected(self):
        with self.assertRaises(ValueError):
            build_message("verack", magic=b"bad")


class VersionTests(unittest.TestCase):
    def test_own_payload_can_be_parsed(self):
        payload = build_version_payload(
            "127.0.0.1",
            15420,
            protocol=70223,
            start_height=1_234_567,
            user_agent=b"/Yerbas-Test:1.0/",
        )
        parsed = parse_version_payload(payload)
        self.assertEqual(70223, parsed["protocol"])
        self.assertEqual("/Yerbas-Test:1.0/", parsed["user_agent"])
        self.assertEqual(1_234_567, parsed["start_height"])
        self.assertFalse(parsed["relay"])

    def test_truncated_payload_rejected(self):
        with self.assertRaises(ValueError):
            parse_version_payload(b"short")


if __name__ == "__main__":
    unittest.main()
