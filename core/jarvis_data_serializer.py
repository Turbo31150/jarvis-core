#!/usr/bin/env python3
"""
jarvis_data_serializer — Multi-format serialization with schema versioning
Serialize/deserialize between JSON, MessagePack, CBOR-lite, and binary formats
"""

import asyncio
import base64
import json
import logging
import struct
import time
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.data_serializer")


class Format(str, Enum):
    JSON = "json"
    JSON_GZ = "json_gz"  # JSON + gzip
    MSGPACK = "msgpack"  # if msgpack available, else fallback
    BINARY = "binary"  # custom binary envelope
    BASE64 = "base64"  # base64-encoded JSON


@dataclass
class SerializedPayload:
    format: Format
    data: bytes
    schema_version: int = 1
    checksum: int = 0
    original_size: int = 0
    compressed_size: int = 0

    @property
    def compression_ratio(self) -> float:
        if self.original_size == 0:
            return 1.0
        return self.compressed_size / self.original_size

    def to_envelope(self) -> bytes:
        """Pack into binary envelope: magic(4) + version(1) + format(1) + schema(2) + checksum(4) + size(4) + data"""
        magic = b"JRVS"
        fmt_byte = list(Format).index(self.format)
        header = struct.pack(
            ">4sBBHII",
            magic,
            1,  # envelope version
            fmt_byte,
            self.schema_version,
            self.checksum,
            len(self.data),
        )
        return header + self.data

    @classmethod
    def from_envelope(cls, envelope: bytes) -> "SerializedPayload":
        header_size = 4 + 1 + 1 + 2 + 4 + 4  # 16 bytes
        if len(envelope) < header_size:
            raise ValueError("Envelope too short")
        magic, env_ver, fmt_byte, schema_ver, checksum, data_len = struct.unpack(
            ">4sBBHII", envelope[:header_size]
        )
        if magic != b"JRVS":
            raise ValueError(f"Invalid magic: {magic}")
        data = envelope[header_size : header_size + data_len]
        fmt = list(Format)[fmt_byte]
        return cls(format=fmt, data=data, schema_version=schema_ver, checksum=checksum)


class DataSerializer:
    def __init__(
        self, default_format: Format = Format.JSON, compress_threshold_bytes: int = 1024
    ):
        self._default_format = default_format
        self._compress_threshold = compress_threshold_bytes
        self._stats: dict[str, int] = {
            "serialized": 0,
            "deserialized": 0,
            "checksum_errors": 0,
            "bytes_saved": 0,
        }
        self._schema_registry: dict[str, int] = {}

    def register_schema(self, type_name: str, version: int):
        self._schema_registry[type_name] = version

    def _compute_checksum(self, data: bytes) -> int:
        return zlib.crc32(data) & 0xFFFFFFFF

    def serialize(
        self,
        obj: Any,
        fmt: Format | None = None,
        schema_version: int = 1,
        auto_compress: bool = True,
    ) -> SerializedPayload:
        use_fmt = fmt or self._default_format
        self._stats["serialized"] += 1

        # Auto-promote to compressed if large
        raw_json = json.dumps(obj, ensure_ascii=False).encode()
        original_size = len(raw_json)

        if (
            auto_compress
            and use_fmt == Format.JSON
            and original_size > self._compress_threshold
        ):
            use_fmt = Format.JSON_GZ

        if use_fmt == Format.JSON:
            data = raw_json

        elif use_fmt == Format.JSON_GZ:
            data = zlib.compress(raw_json, level=6)

        elif use_fmt == Format.BASE64:
            data = base64.b64encode(raw_json)

        elif use_fmt == Format.MSGPACK:
            try:
                import msgpack

                data = msgpack.packb(obj, use_bin_type=True)
            except ImportError:
                data = raw_json
                use_fmt = Format.JSON

        elif use_fmt == Format.BINARY:
            # Simple: tag(1) + json
            data = b"\x01" + raw_json

        else:
            data = raw_json

        compressed_size = len(data)
        self._stats["bytes_saved"] += max(0, original_size - compressed_size)

        return SerializedPayload(
            format=use_fmt,
            data=data,
            schema_version=schema_version,
            checksum=self._compute_checksum(data),
            original_size=original_size,
            compressed_size=compressed_size,
        )

    def deserialize(
        self, payload: SerializedPayload, verify_checksum: bool = True
    ) -> Any:
        self._stats["deserialized"] += 1

        if verify_checksum:
            expected = self._compute_checksum(payload.data)
            if expected != payload.checksum and payload.checksum != 0:
                self._stats["checksum_errors"] += 1
                log.warning(f"Checksum mismatch: {expected} != {payload.checksum}")

        fmt = payload.format

        if fmt == Format.JSON:
            return json.loads(payload.data)

        elif fmt == Format.JSON_GZ:
            return json.loads(zlib.decompress(payload.data))

        elif fmt == Format.BASE64:
            return json.loads(base64.b64decode(payload.data))

        elif fmt == Format.MSGPACK:
            try:
                import msgpack

                return msgpack.unpackb(payload.data, raw=False)
            except ImportError:
                return json.loads(payload.data)

        elif fmt == Format.BINARY:
            return json.loads(payload.data[1:])

        return json.loads(payload.data)

    def roundtrip(self, obj: Any, fmt: Format | None = None) -> Any:
        payload = self.serialize(obj, fmt=fmt)
        return self.deserialize(payload)

    def to_bytes(self, obj: Any, **kwargs) -> bytes:
        payload = self.serialize(obj, **kwargs)
        return payload.to_envelope()

    def from_bytes(self, data: bytes) -> Any:
        payload = SerializedPayload.from_envelope(data)
        return self.deserialize(payload)

    def stats(self) -> dict:
        return {
            **self._stats,
            "default_format": self._default_format.value,
            "compress_threshold_bytes": self._compress_threshold,
        }


async def main():
    import sys

    ser = DataSerializer(default_format=Format.JSON)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        obj = {
            "model": "qwen3.5-27b-claude",
            "messages": [{"role": "user", "content": "Hello " * 100}],
            "temperature": 0.7,
            "max_tokens": 512,
            "metadata": {"request_id": "abc123", "user": "turbo", "ts": time.time()},
        }

        formats = [Format.JSON, Format.JSON_GZ, Format.BASE64]
        print(f"Object size: {len(json.dumps(obj))} chars\n")
        print(f"{'Format':<12} {'Size':>8} {'Ratio':>8} {'RT OK':>6}")
        print("-" * 40)
        for fmt in formats:
            payload = ser.serialize(obj, fmt=fmt, auto_compress=False)
            rt = ser.deserialize(payload)
            ok = rt == obj
            ratio = f"{payload.compression_ratio:.2f}x"
            print(
                f"  {fmt.value:<12} {payload.compressed_size:>8} {ratio:>8} {'✅' if ok else '❌':>6}"
            )

        # Envelope round-trip
        raw = ser.to_bytes(obj, fmt=Format.JSON_GZ)
        recovered = ser.from_bytes(raw)
        print(
            f"\nEnvelope round-trip: {'✅' if recovered == obj else '❌'} ({len(raw)} bytes)"
        )

        print(f"\nStats: {json.dumps(ser.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(ser.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
