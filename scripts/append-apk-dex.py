"""Append DEX while preserving the AAPT2 local headers read by Android AssetManager."""
import struct
import sys
import zipfile


def append_dex(apk, dex):
    with zipfile.ZipFile(apk, "a") as archive:
        if "classes.dex" in archive.namelist():
            raise ValueError("APK already contains classes.dex")
        archive.write(dex, "classes.dex", compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(apk) as archive:
        for entry in archive.infolist():
            archive.fp.seek(entry.header_offset)
            header = archive.fp.read(30)
            flags = struct.unpack_from("<H", header, 6)[0]
            crc, compressed, size = struct.unpack_from("<III", header, 14)
            if flags & 8 or (crc, compressed, size) != (entry.CRC, entry.compress_size, entry.file_size):
                raise ValueError("APK local header mismatch: " + entry.filename)


if __name__ == "__main__":
    append_dex(*sys.argv[1:])
