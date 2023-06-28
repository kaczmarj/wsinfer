from pathlib import Path
from typing import Optional
from typing import Tuple
from typing import Union

import numpy as np
import openslide
import tifffile

from .errors import CannotReadSpacing

PathType = Union[str, Path]


def _get_mpp_openslide(slide_path: PathType) -> Tuple[Optional[float], Optional[float]]:
    slide = openslide.OpenSlide(slide_path)
    mppx: Optional[float] = None
    mppy: Optional[float] = None
    if (
        openslide.PROPERTY_NAME_MPP_X in slide.properties
        and openslide.PROPERTY_NAME_MPP_Y in slide.properties
    ):
        mppx = slide.properties[openslide.PROPERTY_NAME_MPP_X]
        mppy = slide.properties[openslide.PROPERTY_NAME_MPP_Y]
        if mppx is None or mppy is None:
            raise ValueError(
                "Cannot infer slide spacing because MPPX or MPPY is None:"
                f" {mppx} and {mppy}"
            )
        else:
            mppx = float(mppx)
            mppy = float(mppy)

    return mppx, mppy


def _get_biggest_series(tif: tifffile.TiffFile) -> int:
    max_area: int = 0
    max_index: Optional[int] = None
    for index, s in enumerate(tif.series):
        area = np.prod(s.shape)
        if area > max_area and "X" in s.axes and "Y" in s.axes:
            max_area = area
            max_index = index
    if max_index is None:
        raise ValueError("Cannot find largest series in the slide")
    return max_index


def _get_mpp_tiff(slide_path: PathType) -> Tuple[Optional[float], Optional[float]]:
    # Enum ResolutionUnit value to the number of micrometers in that unit.
    # 2: inch (25,400 microns in an inch)
    # 3: centimeter (10,000 microns in a cm)
    resunit_to_microns = {2: 25400, 3: 10000}
    um_x: Optional[float] = None
    um_y: Optional[float] = None

    with tifffile.TiffFile(slide_path) as tif:
        biggest_series = _get_biggest_series(tif)
        s = tif.series[biggest_series]
        page = s.pages[0]
        unit = resunit_to_microns[page.tags["ResolutionUnit"].value.real]
        if page.tags["XResolution"].value[1] >= 100:
            um_x = (
                unit
                * page.tags["XResolution"].value[1]
                / page.tags["XResolution"].value[0]
            )
        if page.tags["YResolution"].value[1] >= 100:
            um_y = (
                unit
                * page.tags["YResolution"].value[1]
                / page.tags["YResolution"].value[0]
            )
    return um_x, um_y


def get_avg_mpp(slide_path: PathType) -> float:
    """Return the average MPP of a whole slide image."""

    mppx, mppy = _get_mpp_openslide(slide_path)
    if mppx is not None and mppy is not None:
        return (mppx + mppy) / 2

    # Try tifffile now.
    mppx, mppy = _get_mpp_tiff(slide_path)
    if mppx is not None and mppy is not None:
        return (mppx + mppy) / 2

    raise CannotReadSpacing(f"Could not read the spacing of slide {slide_path}")
