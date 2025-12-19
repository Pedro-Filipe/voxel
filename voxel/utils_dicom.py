import numpy as np
from PIL import Image as PILImage

import pydicom
from pydicom.pixel_data_handlers.util import (
    apply_voi_lut,
    apply_modality_lut,
    convert_color_space,
)


def is_dicom_file(path):
    # Prefer signature check at offset 128, then metadata fallback
    try:
        with open(path, "rb") as f:
            head = f.read(132)
        if len(head) >= 132 and head[128:132] == b"DICM":
            return True
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
        return hasattr(ds, "SOPClassUID")
    except Exception:
        return False


def dicom_to_display_image(ds, frame_index=0, window_center=None, window_width=None):
    """
    Convert a pydicom Dataset to a PIL.Image suitable for display.
    Applies modality LUT (once) for grayscale, handles MONOCHROME1 inversion,
    and handles basic RGB/YBR color images.
    """
    pixel_array = ds.pixel_array
    if pixel_array.ndim >= 3 and getattr(ds, "NumberOfFrames", 1) > 1:
        total_frames = pixel_array.shape[0]
        frame_index = frame_index if 0 <= frame_index < total_frames else 0
        frame = pixel_array[frame_index]
    else:
        frame = pixel_array

    photometric = getattr(ds, "PhotometricInterpretation", "").upper()
    is_color = getattr(ds, "SamplesPerPixel", 1) == 3

    if is_color:
        arr = frame
        if photometric.startswith("YBR"):
            try:
                arr = convert_color_space(arr, photometric, "RGB")
            except Exception:
                pass
        if arr.dtype != np.uint8:
            arr = arr.astype(np.float32)
            amin = arr.min()
            amax = arr.max()
            amax = amin + 1.0 if amax == amin else amax
            arr = (arr - amin) / (amax - amin) * 255.0
            arr = arr.clip(0, 255).astype(np.uint8)
        img = PILImage.fromarray(arr, mode="RGB")
    else:
        arr = frame.astype(np.float32)
        try:
            arr = apply_modality_lut(arr, ds)
        except Exception:
            pass

        ds_wc = getattr(ds, "WindowCenter", None)
        ds_ww = getattr(ds, "WindowWidth", None)

        if isinstance(ds_wc, (list, tuple)):
            ds_wc = ds_wc[0]
        if isinstance(ds_ww, (list, tuple)):
            ds_ww = ds_ww[0]

        c = window_center if window_center is not None else ds_wc
        w = window_width if window_width is not None else ds_ww

        if c is not None and w not in (None, 0):
            c = float(c)
            w = float(w)
            lower = c - w / 2.0
            upper = c + w / 2.0
            arr = (arr - lower) / (upper - lower) * 255.0
            arr = arr.clip(0, 255)
        else:
            amin = float(np.min(arr))
            amax = float(np.max(arr))
            amax = amin + 1.0 if amax <= amin else amax
            arr = (arr - amin) / (amax - amin) * 255.0

        if photometric == "MONOCHROME1":
            arr = 255.0 - arr

        arr = arr.clip(0, 255).astype(np.uint8)
        img = PILImage.fromarray(arr, mode="L")

    return img


def format_tag(tag):
    try:
        return f"({tag.group:04X},{tag.element:04X})"
    except Exception:
        return str(tag)


def safe_str(value, max_len=256):
    try:
        s = str(value)
    except Exception:
        s = repr(value)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s
