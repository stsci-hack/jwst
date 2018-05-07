import logging
from collections import namedtuple
import copy
import json
import math

import numpy as np
from astropy.modeling import polynomial
from .. import datamodels
from ..datamodels import dqflags
from .. assign_wcs import niriss        # for specifying spectral order number
from . import extract1d
from . import ifu
from . import spec_wcs

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

WFSS_EXPTYPES = ['NIS_WFSS', 'NRC_GRISM', 'NRC_WFSS']

# These values are used to indicate whether the input reference file
# (if any) is JSON or IMAGE.
FILE_TYPE_JSON = "JSON"
FILE_TYPE_IMAGE = "IMAGE"
FILE_TYPE_OTHER = "N/A"

# For full-frame input data, keyword SLTNAME may not be populated, so use
# the following string to indicate that the first slit in the reference
# file should be used.
# A slit name in the reference file can also be ANY, in which case that
# slit can be used with any slit name from the input data.
ANY = "ANY"

# The spectral order number is an integer, so we can't use ANY as a wildcard.
# The value below is greater than any spectral order for any JWST mode, so
# if a value of spectral order is greater than or equal to this, we'll
# interpret that to mean that any value will match.
ANY_ORDER = 1000

# Dispersion direction, predominantly horizontal or vertical.  These values
# are to be compared with keyword DISPAXIS from the input header.
HORIZONTAL = 1
VERTICAL = 2

# This is intended to be larger than any possible distance (in pixels)
# between the target and any point in the image; used by locn_from_wcs().
HUGE_DIST = 1.e20

# These values are assigned in get_extract_parameters, using key "match".
# If there was an aperture in the reference file for which the "id" key
# matched, that's (at least) a partial match.  If "spectral_order" also
# matched, that's an exact match.

NO_MATCH = "no match"
PARTIAL = "partial match"
EXACT = "exact match"

DUMMY = "dummy"


Aperture = namedtuple('Aperture', ['xstart', 'ystart', 'xstop', 'ystop'])


class Extract1dError(Exception):
    pass

class InvalidSpectralOrderNumberError(Extract1dError):
    """The spectral order number was invalid or off the detector."""
    def __init__(self, message=None):
        super().__init__()
        self.message = message


def load_ref_file(refname):
    """Open the reference file.

    Parameters
    ----------
    refname: string
        The name of the reference file.  This file is expected to be
        either a JSON file giving extraction information, or a file
        containing one or more images that are to be used as masks that
        define the extraction region and optionally background regions.

    Returns
    -------
    ref_dict: dictionary
        If the reference file is in JSON format, ref_dict will be the
        dictionary returned by json.load(), except that the file type
        ('JSON') will also be included with key 'ref_file_type'.
        If the reference file is an image, ref_dict will be a
        dictionary with two keys:  ref_dict['ref_file_type'] = 'IMAGE'
        and ref_dict['ref_model'].  The latter will be the open file
        handle for the jwst.datamodels object for the reference file.
    """

    if refname == "N/A":
        ref_dict = None
    else:
        # Try reading the file as JSON.
        fd = open(refname)
        try:
            ref_dict = json.load(fd)
            ref_dict['ref_file_type'] = FILE_TYPE_JSON
            fd.close()
        except UnicodeDecodeError:
            fd.close()
            # Try opening the file as a reference image.
            try:
                fd = datamodels.MultiExtract1dImageModel(refname)
                ref_dict = {'ref_file_type': FILE_TYPE_IMAGE}
                ref_dict['ref_model'] = fd      # only used for images
            except OSError:
                log.info("The reference file should be JSON or FITS.")
                log.error("Don't know how to read %s.", refname)
                raise

    return ref_dict


def get_extract_parameters(ref_dict,
                           input_model, slitname, sp_order,
                           meta, smoothing_length, bkg_order):

    extract_params = {'match': NO_MATCH}        # initial value

    if ref_dict is None:
        # There is no reference file; use "reasonable" default values.
        extract_params['ref_file_type'] = FILE_TYPE_OTHER
        extract_params['match'] = EXACT
        shape = input_model.data.shape
        extract_params['spectral_order'] = sp_order
        extract_params['xstart'] = 0                    # first pixel in X
        extract_params['xstop'] = shape[-1] - 1         # last pixel in X
        extract_params['ystart'] = 0                    # first pixel in Y
        extract_params['ystop'] = shape[-2] - 1         # last pixel in Y
        extract_params['extract_width'] = None
        extract_params['src_coeff'] = None
        extract_params['bkg_coeff'] = None
        extract_params['nod_correction'] = 0
        extract_params['independent_var'] = 'pixel'
        extract_params['smoothing_length'] = 0  # because no background sub.
        extract_params['bkg_order'] = 0         # because no background sub.
        # Note that extract_params['dispaxis'] is not assigned.  This will
        # be done later by calling find_dispaxis().

    elif ref_dict['ref_file_type'] == FILE_TYPE_JSON:
        extract_params['ref_file_type'] = ref_dict['ref_file_type']
        for aper in ref_dict['apertures']:
            if 'id' in aper and aper['id'] != "dummy" and \
               (aper['id'] == slitname or aper['id'] == "ANY" or
                slitname == "ANY"):
                extract_params['match'] = PARTIAL
                # region_type is retained for backward compatibility; it is
                # not required to be present.
                # spectral_order is a secondary selection criterion.  The
                # default is the expected value, so if the key is not present
                # in the JSON file, the current aperture will be selected.
                # If the current aperture in the JSON file has
                # "spectral_order": "ANY", that aperture will be selected.
                region_type = aper.get("region_type", "target")
                if region_type != "target":
                    continue
                spectral_order = aper.get("spectral_order", sp_order)
                if spectral_order == sp_order or spectral_order == ANY:
                    extract_params['match'] = EXACT
                    extract_params['spectral_order'] = sp_order
                    disp = aper.get('dispaxis')
                    if disp is None:
                        # Will be found later by calling find_dispaxis().
                        log.warning("dispaxis not specified in reference file")
                    elif disp != HORIZONTAL and disp != VERTICAL:
                        log.error("dispaxis = %d is not valid.", disp)
                        raise ValueError('dispaxis must be 1 or 2.')
                    else:
                        extract_params['dispaxis'] = disp
                    extract_params['src_coeff'] = aper.get('src_coeff')
                    extract_params['bkg_coeff'] = aper.get('bkg_coeff')
                    extract_params['independent_var'] = \
                          aper.get('independent_var', 'pixel').lower()
                    if smoothing_length is None:
                        extract_params['smoothing_length'] = \
                              aper.get('smoothing_length', 0)
                    else:
                        # If the user supplied a value, use that value.
                        extract_params['smoothing_length'] = smoothing_length
                    if bkg_order is None:
                        extract_params['bkg_order'] = aper.get('bkg_order', 0)
                    else:
                        # If the user supplied a value, use that value.
                        extract_params['bkg_order'] = bkg_order
                    extract_params['xstart'] = aper.get('xstart')
                    extract_params['xstop'] = aper.get('xstop')
                    extract_params['ystart'] = aper.get('ystart')
                    extract_params['ystop'] = aper.get('ystop')
                    extract_params['extract_width'] = aper.get('extract_width')
                    extract_params['nod_correction'] = 0        # default value
                    break

    elif ref_dict['ref_file_type'] == FILE_TYPE_IMAGE:
        extract_params['ref_file_type'] = ref_dict['ref_file_type']
        foundit = False
        for im in ref_dict['ref_model'].images:
            if im.name == slitname or im.name == ANY or slitname == ANY:
                extract_params['match'] = PARTIAL
                if (im.spectral_order == sp_order or
                    im.spectral_order >= ANY_ORDER):
                    extract_params['match'] = EXACT
                    extract_params['spectral_order'] = sp_order
                    foundit = True
                    break

        if foundit:
            extract_params['ref_image'] = im
            if hasattr(im, "dispersion_axis"):
                if (im.dispersion_axis == HORIZONTAL or
                    im.dispersion_axis == VERTICAL):
                    extract_params['dispaxis'] = im.dispersion_axis
            # else dispaxis will be set by find_dispaxis()
            if smoothing_length is None:
                extract_params['smoothing_length'] = im.smoothing_length
            else:
                # The user-supplied value takes precedence.
                extract_params['smoothing_length'] = smoothing_length
            extract_params['nod_correction'] = 0

    else:
        log.error("Reference file type %s not recognized",
                  ref_dict['ref_file_type'])

    return extract_params


def find_dispaxis(slit, spectral_order, extract_params):
    """Get the location of the spectrum, based on the WCS.

    Parameters:
    -----------
    slit: data model
        This can be a slit from a MultiSlitModel (or similar), or it
        could be a SlitModel, perhaps a full image, including a
        multi-integration exposure.
        We use the meta.wcs and wavelength attributes.

    spectral_order: int
        The spectral order number.

    extract_params: dict, may be modified in-place
        Parameters read from the extraction reference file.  If key
        'dispaxis' has already been assigned to a known value, this
        function will return without doing anything.  Otherwise, the
        dispersion direction will be determined by comparing the
        increment in wavelength from one pixel to the next, in the
        horizontal and vertical directions, and extract_params['dispaxis']
        will be assigned the value.
    """

    if 'dispaxis' in extract_params:
        initial_value = extract_params['dispaxis']
    else:
        initial_value = None

    if hasattr(slit, 'meta') and hasattr(slit.meta, 'wcs'):
        wcs = slit.meta.wcs
    else:
        log.warning("find_dispaxis:  WCS not found")
        return

    if hasattr(slit, "wavelength"):
        wl_array = slit.wavelength
    else:
        wl_array = None
    if wl_array is None or len(wl_array) == 0:
        got_wavelength = False
    else:
        got_wavelength = True                   # may be reset later
    if not got_wavelength or wl_array.min() == 0. and wl_array.max() == 0.:
        got_wavelength = False

    bb = wcs.bounding_box
    if bb is None:
        shape = slit.data.shape
        x_cent = shape[-1] // 2
        y_cent = shape[-2] // 2
    else:
        x_cent = float(bb[0][0] + bb[0][1]) / 2.
        y_cent = float(bb[1][0] + bb[1][1]) / 2.
        x_cent = math.floor(x_cent)
        y_cent = math.floor(y_cent)

    # Find which is the dispersion axis, by comparing the increment in
    # wavelength from one pixel to the next, in the horizontal and
    # vertical directions.
    dispaxis = None                             # pessimistic value
    if got_wavelength:
        dwlx = wl_array[:, 1:-1] - wl_array[:, 0:-2]
        dwly = wl_array[1:-1, :] - wl_array[0:-2, :]
        dwlx = np.nanmean(dwlx)
        dwly = np.nanmean(dwly)
        log.debug("find_dispaxis:  dwlx = %s dwly = %s", str(dwlx), str(dwly))

        dwlx = np.abs(dwlx)
        dwly = np.abs(dwly)
        if dwlx > dwly:
            dispaxis = HORIZONTAL
        elif dwlx < dwly:
            dispaxis = VERTICAL
    else:
        transform = wcs.forward_transform
        if transform.n_inputs > 2:
            stuff = transform(x_cent, y_cent, spectral_order)
            wl_00 = stuff[2]
            stuff = transform(x_cent, y_cent + 1, spectral_order)
            wl_01 = stuff[2]
            stuff = transform(x_cent + 1, y_cent, spectral_order)
            wl_10 = stuff[2]
        else:
            stuff = transform(x_cent, y_cent)
            wl_00 = stuff[2]
            stuff = transform(x_cent, y_cent + 1)
            wl_01 = stuff[2]
            stuff = transform(x_cent + 1, y_cent)
            wl_10 = stuff[2]
        dwlx = abs(wl_10 - wl_00)
        dwly = abs(wl_01 - wl_00)
        if np.isnan(dwlx) or np.isnan(dwly):
            log.warning("wavelength from WCS is NaN within the bounding box")
            shape = slit.data.shape
            grid = np.indices(shape[-2:], dtype=np.float64)
            # The arguments are the X and Y pixel coordinates.
            stuff = slit.meta.wcs(grid[1], grid[0])
            wl_wcs = stuff[2]
            del grid, stuff
            dwlx = wl_wcs[:, 1:-1] - wl_wcs[:, 0:-2]
            dwly = wl_wcs[1:-1, :] - wl_wcs[0:-2, :]
            dwlx = np.nanmean(dwlx)
            dwly = np.nanmean(dwly)
        log.debug("find_dispaxis:  dwlx = %s dwly = %s", str(dwlx), str(dwly))
        if np.isnan(dwlx) or np.isnan(dwly):
            log.warning("dwlx and/or dwly is STILL NaN!  "
                        "Can't determine dispaxis from WCS")
        else:
            dwlx = np.abs(dwlx)
            dwly = np.abs(dwly)
            if dwlx > dwly:
                dispaxis = HORIZONTAL
            elif dwlx < dwly:
                dispaxis = VERTICAL

    if dispaxis is None:
        log.warning("Can't determine dispaxis from the WCS.")
    elif initial_value is None:
        # Only assign this value if dispaxis wasn't present in the
        # reference file, or if there was no reference file.
        extract_params['dispaxis'] = dispaxis

    log.debug("find_dispaxis:  dispaxis from ref file = %s; from wcs = %s",
              str(initial_value), str(dispaxis))


def log_initial_parameters(extract_params):
    """Log some of the initial extraction parameters."""

    if not "xstart" in extract_params:
        return

    log.debug("dispaxis = %d", extract_params["dispaxis"])
    log.debug("spectral order = %d", extract_params["spectral_order"])
    log.debug("independent_var = %s", extract_params["independent_var"])
    log.debug("smoothing_length = %d", extract_params["smoothing_length"])
    log.debug("initial xstart = %s", str(extract_params["xstart"]))
    log.debug("initial xstop = %s", str(extract_params["xstop"]))
    log.debug("initial ystart = %s", str(extract_params["ystart"]))
    log.debug("initial ystop = %s", str(extract_params["ystop"]))
    log.debug("extract_width = %s", str(extract_params["extract_width"]))
    log.debug("initial src_coeff = %s", str(extract_params["src_coeff"]))
    log.debug("initial bkg_coeff = %s", str(extract_params["bkg_coeff"]))
    log.debug("bkg_order = %d", extract_params["bkg_order"])


def get_aperture(im_shape, wcs, extract_params):
    """Get the extraction limits xstart, xstop, ystart, ystop.

    Parameters
    ----------
    im_shape: tuple
        The shape (2-D) of the input data.  This will be for the current
        integration, if the input contains more than one integration.

    wcs: a WCS object, or None
        The wcs (if any) for the input data or slit.

    extract_params: dictionary
        Parameters read from the reference file.

    Returns
    -------
    ap_ref: namedtuple
        Keys are 'xstart', 'xstop', 'ystart', and 'ystop'.
    """

    if extract_params['ref_file_type'] == FILE_TYPE_IMAGE:
        return {}

    ap_ref = aperture_from_ref(extract_params, im_shape)

    (ap_ref, truncated) = update_from_shape(ap_ref, im_shape)
    if truncated:
        log.warning("Extraction limits extended outside the input image "
                    "borders; limits have been truncated.")

    if wcs is not None:
        ap_wcs = aperture_from_wcs(wcs)
    else:
        ap_wcs = None

    # If the xstart, etc., values were not specified for the dispersion
    # direction, the extraction region should be centered within the
    # WCS bounding box (domain).
    ap_ref = update_from_wcs(ap_ref, ap_wcs, extract_params["extract_width"],
                             extract_params["dispaxis"])
    ap_ref = update_from_width(ap_ref, extract_params["extract_width"],
                               extract_params["dispaxis"])

    return ap_ref


def aperture_from_ref(extract_params, im_shape):
    """Get extraction region from reference file or image shape.

    Parameters
    ----------
    extract_params: dictionary
        Parameters read from the reference file.

    im_shape: tuple of int
        The last two elements are the height and width of the input image
        (slit).

    Returns
    -------
    ap_ref: namedtuple
        Keys are 'xstart', 'xstop', 'ystart', and 'ystop'.
    """

    nx = im_shape[-1]
    ny = im_shape[-2]

    xstart = extract_params.get('xstart', None)
    xstop = extract_params.get('xstop', None)
    ystart = extract_params.get('ystart', None)
    ystop = extract_params.get('ystop', None)

    if xstart is None:
        xstart = 0
    if xstop is None:
        xstop = nx - 1                  # limits are inclusive
    if ystart is None:
        ystart = 0
    if ystop is None:
        ystop = ny - 1

    ap_ref = Aperture(xstart=xstart, xstop=xstop, ystart=ystart, ystop=ystop)

    return ap_ref


def update_from_width(ap_ref, extract_width, direction):
    """Update XD extraction limits based on extract_width.

    If extract_width was specified, that value should override
    ystop - ystart (or xstop - xstart, depending on dispersion direction).

    Parameters
    ----------
    ap_ref: namedtuple
        Contains xstart, xstop, ystart, ystop.  These are the initial
        values as read from the reference file, except that they may
        have been truncated at the image borders.

    extract_width: int
        The number of pixels in the cross-dispersion direction to add
        together to make a 1-D spectrum from a 2-D image.

    direction: int
        HORIZONTAL (1) if the dispersion direction is predominantly
        horizontal.  VERTICAL (2) if the dispersion direction is
        predominantly vertical.

    Returns
    -------
    ap_width: namedtuple
        Keys are 'xstart', 'xstop', 'ystart', and 'ystop'.
    """

    if extract_width is None:
        return ap_ref

    if direction == HORIZONTAL:
        temp_width = ap_ref.ystop - ap_ref.ystart + 1
    else:
        temp_width = ap_ref.xstop - ap_ref.xstart + 1

    if extract_width == temp_width:
        return ap_ref                                   # OK as is

    # An integral value corresponds to the center of a pixel.  If the
    # extraction limits were not specified via polynomial coefficients,
    # assign_polynomial_limits will create polynomial functions using
    # values from an Aperture, and these lower and upper limits will be
    # expanded by 0.5 to give polynomials (constant functions) for the
    # lower and upper edges of the bounding pixels.
    width = float(extract_width)
    if direction == HORIZONTAL:
        lower = float(ap_ref.ystart)
        upper = float(ap_ref.ystop)
        lower = (lower + upper) / 2. - (width - 1.) / 2.
        upper = lower + (width - 1.)
        ap_width = Aperture(xstart=ap_ref.xstart, xstop=ap_ref.xstop,
                            ystart=lower, ystop=upper)
    else:
        lower = float(ap_ref.xstart)
        upper = float(ap_ref.xstop)
        lower = (lower + upper) / 2. - (width - 1.) / 2.
        upper = lower + (width - 1.)
        ap_width = Aperture(xstart=lower, xstop=upper,
                            ystart=ap_ref.ystart, ystop=ap_ref.ystop)

    return ap_width


def update_from_shape(ap, im_shape):
    """Truncate extraction region based on input image shape.

    Parameters
    ----------
    ap: namedtuple
        Extraction region.

    im_shape: tuple of int
        The last two elements are the height and width of the input image.

    Returns
    -------
    tuple: (ap_shape, truncated)
        ap_shape is a namedtuple with keys 'xstart', 'xstop', 'ystart',
        and 'ystop'.
        `truncated` is a boolean, True if any value was truncated at an
        image edge.
    """

    nx = im_shape[-1]
    ny = im_shape[-2]

    xstart = ap.xstart
    xstop = ap.xstop
    ystart = ap.ystart
    ystop = ap.ystop

    truncated = False
    if ap.xstart < 0:
        xstart = 0
        truncated = True
    if ap.xstop >= nx:
        xstop = nx - 1                          # limits are inclusive
        truncated = True
    if ap.ystart < 0:
        ystart = 0
        truncated = True
    if ap.ystop >= ny:
        ystop = ny - 1
        truncated = True

    ap_shape = Aperture(xstart=xstart, xstop=xstop,
                        ystart=ystart, ystop=ystop)

    return (ap_shape, truncated)


def aperture_from_wcs(wcs):
    """Get the limits over which the WCS is defined.

    Parameters
    ----------
    wcs: data model
        The world coordinate system interface.

    Returns
    -------
    tuple ap_wcs
        namedtuple or None
        Keys are 'xstart', 'xstop', 'ystart', and 'ystop'.  These are the
        limits copied directly from wcs.bounding_box.
    """

    got_bounding_box = False
    try:
        bounding_box = wcs.bounding_box
        got_bounding_box = True
        log.debug("Using wcs.bounding_box.")
    except AttributeError:
        log.info("wcs.bounding_box not found; using wcs.domain instead.")
        bounding_box = ((wcs.domain[0]['lower'], wcs.domain[0]['upper']),
                        (wcs.domain[1]['lower'], wcs.domain[1]['upper']))

    if got_bounding_box and bounding_box is None:
        log.warning("wcs.bounding_box is None")
        return None

    # bounding_box should be a tuple of tuples, each of the latter
    # consisting of (lower, upper) limits.
    if len(bounding_box) < 2:
        log.warning("wcs.bounding_box has the wrong shape")
        return None

    # These limits are float, and they are inclusive.
    xstart = bounding_box[0][0]
    xstop = bounding_box[0][1]
    ystart = bounding_box[1][0]
    ystop = bounding_box[1][1]

    ap_wcs = Aperture(xstart=xstart, xstop=xstop, ystart=ystart, ystop=ystop)

    return ap_wcs


def update_from_wcs(ap_ref, ap_wcs, extract_width, direction):
    """Limit the extraction region to the WCS bounding box.

    Parameters
    ----------
    ap_ref: namedtuple
        Contains xstart, xstop, ystart, ystop.  These are the values of
        the extraction region as specified by the reference file or the
        image size.

    ap_wcs: namedtuple
        These are the bounding box limits.

    extract_width: int
        The number of pixels in the cross-dispersion direction to add
        together to make a 1-D spectrum from a 2-D image.

    direction: int
        HORIZONTAL (1) if the dispersion direction is predominantly
        horizontal.  VERTICAL (2) if the dispersion direction is
        predominantly vertical.

    Returns
    -------
    ap: namedtuple
        Keys are 'xstart', 'xstop', 'ystart', and 'ystop'.
    """

    if ap_wcs is None:
        return ap_ref

    # If the wcs limits don't pass the sanity test, ignore the bounding box.
    if not sanity_check_limits(ap_ref, ap_wcs):
        return ap_ref

    # ap_wcs has the limits over which the WCS transformation is defined;
    # take those as the outer limits over which we will extract.
    xstart = compare_start(ap_ref.xstart, ap_wcs.xstart)
    ystart = compare_start(ap_ref.ystart, ap_wcs.ystart)
    xstop = compare_stop(ap_ref.xstop, ap_wcs.xstop)
    ystop = compare_stop(ap_ref.ystop, ap_wcs.ystop)

    if extract_width is not None:
        if direction == HORIZONTAL:
            width = ystop - ystart + 1
        else:
            width = xstop - xstart + 1
        if width < extract_width:
            log.warning("extract_width was truncated from %g to %g",
                        extract_width, width)

    ap = Aperture(xstart=xstart, xstop=xstop, ystart=ystart, ystop=ystop)

    return ap


def sanity_check_limits(ap_ref, ap_wcs):
    """Sanity check.

    Parameters
    ----------
    ap_ref: namedtuple
        Contains xstart, xstop, ystart, ystop.  These are the values of
        the extraction region as specified by the reference file or the
        image size.

    ap_wcs: namedtuple
        These are the bounding box limits.

    Returns
    -------
    flag: boolean
        True if ap_ref and ap_wcs do overlap, i.e. if the sanity test passes.
    """

    if (ap_wcs.xstart >= ap_ref.xstop or ap_wcs.xstop <= ap_ref.xstart or
        ap_wcs.ystart >= ap_ref.ystop or ap_wcs.ystop <= ap_ref.ystart):
        log.warning("The WCS bounding box is outside the aperture:")
        log.warning("  aperture:  %s, %s, %s, %s",
                    str(ap_ref.xstart), str(ap_ref.xstop),
                    str(ap_ref.ystart), str(ap_ref.ystop))
        log.warning("  wcs:       %s, %s, %s, %s",
                    str(ap_wcs.xstart), str(ap_wcs.xstop),
                    str(ap_wcs.ystart), str(ap_wcs.ystop))
        log.warning(" so the wcs bounding box will be ignored.")
        flag = False
    else:
        flag = True

    return flag


def compare_start(start_ref, start_wcs):
    """Compare the start limit from the aperture with the WCS lower limit.

    The more restrictive (i.e. larger) limit is the one upon which the
    output value will be based.  If the WCS limit is larger, the value will
    be increased to an integer, on the assumption that WCS lower limits
    correspond to the lower edge of the bounding pixel.  If this value
    will actually be used for an extraction limit (i.e. if the limits were
    not already specified by polynomial coefficients), then
    assign_polynomial_limits will create a polynomial function using this
    value, except that it will be decreased by 0.5 to correspond to the
    lower edge of the bounding pixels.

    Parameters
    ----------
    start_ref: int or float
        xstart or ystart, as specified by the reference file or the image
        size.

    start_wcs: int or float
        The lower limit from the WCS bounding box.

    Returns
    -------
    value: int or float
        The start limit, possibly constrained by the WCS start limit.
    """

    if start_ref >= start_wcs:          # ref is inside WCS limit
        value = start_ref
    else:                               # outside (below) WCS limit
        value = math.ceil(start_wcs)

    return value


def compare_stop(stop_ref, stop_wcs):
    """Compare the stop limit from the aperture with the WCS upper limit.

    The more restrictive (i.e. smaller) limit is the one upon which the
    output value will be based.  If the WCS limit is smaller, the value
    will be truncated to an integer, on the assumption that WCS upper
    limits correspond to the upper edge of the bounding pixel.  If this
    value will actually be used for an extraction limit (i.e. if the
    limits were not already specified by polynomial coefficients), then
    assign_polynomial_limits will create a polynomial function using this
    value, except that it will be increased by 0.5 to correspond to the
    upper edge of the bounding pixels.

    Parameters
    ----------
    stop_ref: int or float
        xstop or ystop, as specified by the reference file or the image
        size.

    stop_wcs: int or float
        The upper limit from the WCS bounding box.

    Returns
    -------
    value: int or float
        The stop limit, possibly constrained by the WCS stop limit.
    """

    if stop_ref <= stop_wcs:            # ref is inside WCS limit
        value = stop_ref
    else:                               # outside (above) WCS limit
        value = math.floor(stop_wcs)

    return value


def create_poly(coeff):
    """Create a polynomial model from coefficients.

    Parameters
    ----------
    coeff: list of float
        The coefficients of the polynomial, constant term first, highest
        order term last.

    Returns
    -------
    astropy.modeling.polynomial.Polynomial1D object, or None if `coeff`
        is empty.
    """

    n = len(coeff)
    if n < 1:
        return None

    coeff_dict = {}
    for i in range(n):
        key = "c{}".format(i)
        coeff_dict[key] = coeff[i]

    return polynomial.Polynomial1D(degree=n - 1, **coeff_dict)


class ExtractBase:

    def __init__(self):
        self.exp_type = ""
        """
        Issue #1781.
        self.instrument_name = ""
        """
        self.dispaxis = None
        self.spectral_order = None
        self.xstart = None
        self.xstop = None
        self.ystart = None
        self.ystop = None
        self.extract_width = None
        self.independent_var = "pixel"
        self.src_coeff = None
        self.bkg_coeff = None
        self.p_src = None
        self.p_bkg = None
        self.smoothing_length = 0.
        self.bkg_order = 0
        self.nod_correction = 0.
        self.wcs = None


    def update_extraction_limits(self, ap):
        pass


    def assign_polynomial_limits(self):
        pass


    def locn_from_wcs(self, input_model, slit):
        """Get the location of the spectrum, based on the WCS.

        Parameters:
        -----------
        input_model: data model
            The input science model.  This will only be used if `slit`
            is "dummy".

        slit: one slit from a MultiSlitModel (or similar), or "dummy"
            The WCS and target coordinates will be gotten from `slit`
            unless `slit` is "dummy", and in that case they will be gotten
            from `input_model`.

        Returns:
        --------
        middle: int or None
            Pixel coordinate in the dispersion direction within the 2-D
            cutout (or the entire input image) at the middle of the WCS
            bounding box.  This is the point at which to determine the
            nominal extraction location, in case it varies along the
            spectrum.  The offset will then be the difference between
            `locn` (below) and the nominal location.

        middle_wl: float or None
            The wavelength at pixel `middle`.

        locn: int or None
            Pixel coordinate in the cross-dispersion direction within the
            2-D cutout (or the entire input image) that has right ascension
            and declination coordinates closest to the target location.
            The spectral extraction region should be centered here.
            None will be returned for `middle`, `middle_wl`, and `locn`
            if there was not sufficient information available, e.g. if the
            wavelength attribute or wcs function is not defined.
        """

        transform = self.wcs.forward_transform

        bb = self.wcs.bounding_box          # ((x0, x1), (y0, y1))

        if bb is None:
            log.warning("The WCS bounding box is None, "
                        "so we can't get target location using the WCS.")
            return None, None, None

        # Size of the bounding box.
        dx = int(bb[0][1] - bb[0][0] + 1)
        dy = int(bb[1][1] - bb[1][0] + 1)

        if self.dispaxis == HORIZONTAL:
            # Width in the cross-dispersion direction, from the start of
            # the 2-D cutout (or of the full image) to the upper limit of
            # the bounding box.  This may be smaller than the full width
            # of the image, but it's all we need to consider.
            width = bb[0][1] + 1
            width = int(round(width))           # must be an int
            # This is the middle of the bounding_box in the dispersion
            # direction.  The zero point is the first pixel of the input data.
            middle = int(bb[0][0] + dx // 2)
            x = np.empty(width, dtype=np.float64)
            x[:] = float(middle)
            y = np.arange(width, dtype=np.float64)
        else:                                   # dispaxis = VERTICAL
            width = bb[1][1] + 1                # in cross-dispersion direction
            middle = int(bb[1][0] + dy // 2)    # in dispersion direction
            width = int(round(width))           # must be an int
            x = np.arange(width, dtype=np.float64)
            y = np.empty(width, dtype=np.float64)
            y[:] = float(middle)

        if slit != DUMMY and hasattr(slit.meta, "target"):
            log.debug("Target coordinates were gotten from slit.meta")
            ra = slit.meta.target.ra
            dec = slit.meta.target.dec
        elif hasattr(input_model.meta, "target"):
            log.debug("Target coordinates were gotten from input_model.meta")
            ra = input_model.meta.target.ra
            dec = input_model.meta.target.dec
        else:
            log.warning("Target RA and Dec are not available, "
                        "so we can't get target location using the WCS.")
            return None, None, None
        log.debug("Target ra = %.5f dec = %.5f", ra, dec)

        if transform.n_inputs > 2:
            stuff = transform(x, y, self.spectral_order)
            ra_i = stuff[0]
            dec_i = stuff[1]
            middle_wl = stuff[2]
        else:
            stuff = transform(x, y)
            ra_i = stuff[0]
            dec_i = stuff[1]
            middle_wl = stuff[2]
        # Rough approximation for handling targets near a pole.
        dec_m = (dec_i + dec) / 2.
        dra = (ra_i - ra) * np.cos(dec_m * math.pi / 180.)
        ddec = dec_i - dec

        nan_ra = np.isnan(dra)
        nan_dec = np.isnan(ddec)
        nan_mask = np.logical_or(nan_ra, nan_dec)

        dist = np.where(nan_mask, HUGE_DIST, np.sqrt(dra**2 + ddec**2))
        del nan_ra, nan_dec, nan_mask

        indx = np.argsort(dist)
        # This should be the xd location of the spectrum:
        locn = int(indx[0])     # index of the minimum value in dist
        # If the minimum distance to the target is at the edge of the image
        # or at the edge of the non-NaN area, we can't use the WCS to find
        # the location of the target spectrum.
        if (locn <= 0 or locn >= width - 1 or
            dist[locn - 1] > HUGE_DIST / 2. or
            dist[locn + 1] > HUGE_DIST / 2.):
            log.warning("WCS implies the target is outside the bounding box, "
                        "so we can't get target location using the WCS.")
            if locn <= 0 or locn >= width - 1:
                log.debug("location = %d, at edge", locn)
            else:
                log.debug("location = %d, adjacent to NaN region", locn)
            if slit == DUMMY:
                shape = input_model.data.shape
            else:
                shape = slit.data.shape
            locn = None

        return middle, middle_wl, locn


    def nominal_locn(self, middle, middle_wl):
        # Implemented in the subclasses.
        pass


class ExtractModel(ExtractBase):
    """The extraction region was specified in a JSON file."""

    def __init__(self, input_model, slit,
                 ref_file_type=None,
                 match="unknown",
                 dispaxis=HORIZONTAL, spectral_order=1,
                 xstart=None, xstop=None, ystart=None, ystop=None,
                 extract_width=None, src_coeff=None, bkg_coeff=None,
                 independent_var="pixel",
                 smoothing_length=0, bkg_order=0, nod_correction=0.,
                 x_center=None, y_center=None,
                 inner_bkg=None, outer_bkg=None, method='subpixel'):
        """Create a polynomial model from coefficients.

        If InvalidSpectralOrderNumberError is raised, processing of the
        current slit or spectral order should be skipped.

        Parameters
        ----------
        input_model: data model
            The input science data.

        slit: an input slit, or a dummy value if not used
            For MultiSlit or MultiProduct data, `slit` is one slit from
            a list of slits in the input.  For other types of data, `slit`
            will not be used.
        """

        super().__init__()

        self.exp_type = input_model.meta.exposure.type

        self.dispaxis = dispaxis
        self.spectral_order = spectral_order

        # xstart, xstop, ystart, or ystop may be overridden with src_coeff,
        # they may be limited by the input image size or by the WCS bounding
        # box, or they may be modified if extract_width was specified
        # (because extract_width takes precedence).
        # If these values are specified, the limits in the cross-dispersion
        # direction should be integers, but they may later be replaced with
        # fractional values, depending on extract_width, in order to center
        # the extraction window in the originally specified xstart to xstop
        # (or ystart to ystop).
        if xstart is None:
            self.xstart = None
        elif self.dispaxis == VERTICAL:
            r = round(xstart)
            if xstart == r:
                self.xstart = xstart
            else:
                log.warning("xstart %s should have been an integer; "
                            "rounding to %s", str(xstart), str(r))
                self.xstart = r
        else:                           # dispaxis is HORIZONTAL
            self.xstart = xstart

        if xstop is None:
            self.xstop = None
        elif self.dispaxis == VERTICAL:
            r = round(xstop)
            if xstop == r:
                self.xstop = xstop
            else:
                log.warning("xstop %s should have been an integer; "
                            "rounding to %s", str(xstop), str(r))
                self.xstop = r
        else:
            self.xstop = xstop

        if ystart is None:
            self.ystart = None
        elif self.dispaxis == HORIZONTAL:
            r = round(ystart)
            if ystart == r:
                self.ystart = ystart
            else:
                log.warning("ystart %s should have been an integer; "
                            "rounding to %s", str(ystart), str(r))
                self.ystart = r
        else:
            self.ystart = ystart

        if ystop is None:
            self.ystop = None
        elif self.dispaxis == HORIZONTAL:
            r = round(ystop)
            if ystop == r:
                self.ystop = ystop
            else:
                log.warning("ystop %s should have been an integer; "
                            "rounding to %s", str(ystop), str(r))
                self.ystop = r
        else:
            self.ystop = ystop

        if extract_width is None:
            self.extract_width = None
        else:
            self.extract_width = int(round(extract_width))
        # 'wavelength' or 'pixel', the independent variable for functions
        # for lower and upper limits of source and background regions.
        self.independent_var = independent_var.lower()
        if (self.independent_var != "wavelength" and
            self.independent_var != "pixel" and
            self.independent_var != "pixels"):
            log.error("independent_var = '%s'; "
                      "specify 'wavelength' or 'pixel'", self.independent_var)
            raise RuntimeError("Invalid value for independent_var")

        # Coefficients for source (i.e. target) and background limits and
        # corresponding polynomial functions.
        self.src_coeff = copy.deepcopy(src_coeff)
        self.bkg_coeff = copy.deepcopy(bkg_coeff)

        # These functions will be assigned by assign_polynomial_limits.
        # The "p" in the attribute name indicates a polynomial function.
        self.p_src = None
        self.p_bkg = None

        if smoothing_length is None:
            smoothing_length = 0
        if smoothing_length > 0 and \
           smoothing_length // 2 * 2 == smoothing_length:
            log.warning("smoothing_length was even (%d), so incremented by 1",
                        smoothing_length)
            smoothing_length += 1               # must be odd
        self.smoothing_length = smoothing_length
        self.bkg_order = bkg_order
        self.nod_correction = nod_correction

        self.wcs = None                         # initial value
        if input_model.meta.exposure.type == "NIS_SOSS":
            if hasattr(input_model.meta, 'wcs'):
                try:
                    self.wcs = niriss.niriss_soss_set_input(
                                input_model, self.spectral_order)
                except ValueError:
                    raise InvalidSpectralOrderNumberError(
                                "Spectral order {} is not valid"
                                .format(self.spectral_order))
        elif slit == DUMMY:
            if hasattr(input_model.meta, 'wcs'):
                self.wcs = input_model.meta.wcs
        elif hasattr(slit, 'meta') and hasattr(slit.meta, 'wcs'):
            self.wcs = slit.meta.wcs
        if self.wcs is None:
            log.warning("WCS function not found in input.")


    def nominal_locn(self, middle, middle_wl):
        """Find the nominal cross-dispersion location of the target spectrum.

        This version is for the case that the reference file is a JSON file,
        or that there is no reference file.

        Parameters
        ----------
        middle: int
            The zero-indexed pixel number of the point in the dispersion
            direction at which `locn_from_wcs` determined the actual
            location (in the cross-dispersion direction) of the target
            spectrum.  This is used for evaluating the polynomial
            functions if the independent variable is pixel.

        middle_wl: float
            The wavelength at pixel `middle`.  This is only used if the
            independent variable for polynomial functions is wavelength.

        Returns
        -------
        location: float or None
            The nominal cross-dispersion location (i.e. unmodified by
            nod or dither offset) of the target spectrum.
        """

        if self.src_coeff is None:
            if self.dispaxis == HORIZONTAL:
                location = float(self.ystart + self.ystop) / 2.
            else:
                location = float(self.xstart + self.xstop) / 2.
        else:
            if self.independent_var.startswith("wavelength"):
                x = float(middle_wl)
            else:
                x = float(middle)
            # Create the polynomial functions.  We'll do this again later,
            # after adding the nod/dither offset to the coefficients, but
            # we need to evaluate them at x now in order to get the nominal
            # location of the spectrum.
            self.assign_polynomial_limits()
            n_srclim = len(self.p_src)
            sum_data = 0.
            sum_weights = 0.
            for i in range(n_srclim):
                lower = self.p_src[i][0](x)
                upper = self.p_src[i][1](x)
                weight = (upper - lower)
                sum_data += weight * (lower + upper) / 2.
                sum_weights += weight
            if sum_weights == 0.:
                location = None
            else:
                location = sum_data / sum_weights

        return location


    def add_nod_correction(self):
        """Add the nod offset to the extraction location (in-place).

        If source extraction coefficients src_coeff were specified, this
        method will add the nod offset correction to the first coefficient
        of every coefficient list; otherwise, the nod offset will be added
        to xstart & xstop or to ystart & ystop.
        If background extraction coefficients bkg_coeff were specified,
        this method will add the nod offset to the first coefficients.
        Note that background coefficients are handled independently of
        src_coeff.
        """

        if self.nod_correction == 0.:
            return

        if self.src_coeff is None:
            if self.dispaxis == HORIZONTAL:
                dir = "y"
                self.ystart += self.nod_correction
                self.ystop += self.nod_correction
            else:
                dir = "x"
                self.xstart += self.nod_correction
                self.xstop += self.nod_correction
            log.info("Applying nod/dither offset of %s to %sstart and %sstop",
                     str(self.nod_correction), dir, dir)

        if self.src_coeff is not None or self.bkg_coeff is not None:
            log.info("Applying nod/dither offset of %s "
                     "to polynomial coefficients", str(self.nod_correction))

        if self.src_coeff is not None:
            n_lists = len(self.src_coeff)
            for i in range(n_lists):
                coeff_list = self.src_coeff[i]
                coeff_list[0] += self.nod_correction
                self.src_coeff[i] = copy.copy(coeff_list)

        if self.bkg_coeff is not None:
            n_lists = len(self.bkg_coeff)
            for i in range(n_lists):
                coeff_list = self.bkg_coeff[i]
                coeff_list[0] += self.nod_correction
                self.bkg_coeff[i] = copy.copy(coeff_list)


    def update_extraction_limits(self, ap):
        """Update start and stop limits.

        Copy the values of xstart, etc., to the attributes.  Note, however,
        that if src_coeff was specified, that will override the values
        given by xstart, etc.
        The limits in the dispersion direction will be rounded to integer.
        """

        self.xstart = ap.xstart
        self.xstop = ap.xstop
        self.ystart = ap.ystart
        self.ystop = ap.ystop

        if self.dispaxis == HORIZONTAL:
            self.xstart = int(round(self.xstart))
            self.xstop = int(round(self.xstop))
        else:                           # vertical
            self.ystart = int(round(self.ystart))
            self.ystop = int(round(self.ystop))


    def log_extraction_parameters(self):
        """Log the updated extraction parameters."""

        log.debug("nod_correction = %s", str(self.nod_correction))
        note_x = ""
        note_y = ""
        if self.src_coeff is not None:
            # Since src_coeff was specified, that will be used instead of
            # xstart & xstop (or ystart & ystop).
            if self.dispaxis == HORIZONTAL:
                note_y = " (not actually used)"
            else:
                note_x = " (not actually used)"
        log.debug("xstart = %s%s", str(self.xstart), note_x)
        log.debug("xstop = %s%s", str(self.xstop), note_x)
        log.debug("ystart = %s%s", str(self.ystart), note_y)
        log.debug("ystop = %s%s", str(self.ystop), note_y)
        if self.src_coeff is not None:
            log.debug("src_coeff = %s", str(self.src_coeff))
        if self.bkg_coeff is not None:
            log.debug("bkg_coeff = %s", str(self.bkg_coeff))


    def assign_polynomial_limits(self):
        """Create polynomial functions for extraction limits.

        self.src_coeff and self.bkg_coeff contain lists of polynomial
        coefficients.  These will be used to create corresponding lists of
        polynomial functions, self.p_src and self.p_bkg.  Note, however,
        that the structures of those two lists are not the same.
        The coefficients lists have this form:
            [[1, 2], [3, 4, 5], [6], [7, 8]]
        which means:
            [1, 2] coefficients for the lower limit of the first region
            [3, 4, 5] coefficients for the upper limit of the first region
            [6] coefficient(s) for the lower limit of the second region
            [7, 8] coefficients for the upper limit of the second region
        The lists of coefficients must always be in pairs, for the lower
        and upper limits respectively, but they're not explicitly in an
        additional layer of two-element lists,
            i.e., not like this:  [[[1, 2], [3, 4, 5]], [[6], [7, 8]]]
        That seemed unnecessarily messy and harder for the user to specify.
        For the lists of polynomial functions, on the other hand, that
        additional layer of list is used:
            [[fcn_lower1, fcn_upper1], [fcn_lower2, fcn_upper2]]
        where:
            fcn_lower1 is 1 + 2 * x
            fcn_upper1 is 3 + 4 * x + 5 * x**2
            fcn_lower2 is 6
            fcn_upper2 is 7 + 8 * x
        """

        if self.src_coeff is None:
            # Create constant functions.

            if self.dispaxis == HORIZONTAL:
                lower = float(self.ystart) - 0.5
                upper = float(self.ystop) + 0.5
            else:
                lower = float(self.xstart) - 0.5
                upper = float(self.xstop) + 0.5
            log.debug("Converting extraction limits to [[%g], [%g]]",
                      lower, upper)
            self.p_src = [[create_poly([lower]), create_poly([upper])]]
        else:
            # The source extraction can include more than one region.
            n_lists = len(self.src_coeff)
            if n_lists // 2 * 2 != n_lists:
                raise RuntimeError("src_coeff must contain alternating lists "
                                   "of lower and upper limits.")
            self.p_src = []
            expect_lower = True                         # toggled in loop
            for coeff_list in self.src_coeff:
                if expect_lower:
                    lower = create_poly(coeff_list)
                else:
                    upper = create_poly(coeff_list)
                    self.p_src.append([lower, upper])
                expect_lower = not expect_lower

        if self.bkg_coeff is not None:
            n_lists = len(self.bkg_coeff)
            if n_lists // 2 * 2 != n_lists:
                raise RuntimeError("bkg_coeff must contain alternating lists "
                                   "of lower and upper limits.")
            self.p_bkg = []
            expect_lower = True                         # toggled in loop
            for coeff_list in self.bkg_coeff:
                if expect_lower:
                    lower = create_poly(coeff_list)
                else:
                    upper = create_poly(coeff_list)
                    self.p_bkg.append([lower, upper])
                expect_lower = not expect_lower


    def extract(self, data, wl_array):
        """
        Do the actual extraction, for the case that the reference file
        is a JSON file, or that there is no reference file.

        Parameters
        ----------
        data: array_like (2-D)
            Data array.

        wl_array: array_like (2-D) or None
            Wavelengths corresponding to `data`, or None if no WAVELENGTH
            was found in the input file.

        Returns
        -------
        (ra, dec, wavelength, net, background)
            ra and dec are floats, and the others are 1-D arrays.
            ra and dec are the right ascension and declination at the
            nominal center of the slit.  `wavelength` is the wavelength in
            micrometers at each pixel.  `net` is the count rate
            (counts / s) minus the background at each pixel.  `background`
            is the background count rate that was subtracted from the total
            source count rate to get `net`.
        """

        # If the wavelength attribute exists and is populated, use it
        # in preference to the wavelengths returned by the wcs function.
        if wl_array is None or len(wl_array) == 0:
            got_wavelength = False
        else:
            got_wavelength = True               # may be reset later

        # The default value is 0, so all 0 values means that the
        # wavelength attribute was not populated.
        if not got_wavelength or wl_array.min() == 0. and wl_array.max() == 0.:
            got_wavelength = False
        if got_wavelength:
            log.debug("Wavelengths are from wavelength attribute.")
            # We need a 1-D array of wavelengths, one element for each
            # output table row.
            # These are slice limits.
            sx0 = int(round(self.xstart))
            sx1 = int(round(self.xstop)) + 1
            sy0 = int(round(self.ystart))
            sy1 = int(round(self.ystop)) + 1
            # Convert non-positive values to NaN, to easily ignore them.
            wl = wl_array.copy()                # don't modify wl_array
            nan_flag = np.isnan(wl)
            # To avoid a warning about invalid value encountered in less_equal.
            wl[nan_flag] = -1000.
            wl = np.where(wl <= 0., np.nan, wl)
            if self.dispaxis == HORIZONTAL:
                wavelength = np.nanmean(wl[sy0:sy1, sx0:sx1], axis=0)
            else:
                wavelength = np.nanmean(wl[sy0:sy1, sx0:sx1], axis=1)

        # Now call the wcs function to compute the celestial coordinates.
        # Also use the returned wavelengths if we weren't able to get them
        # from the wavelength attribute.

        # Used for computing the celestial coordinates.
        if self.dispaxis == HORIZONTAL:
            slice0 = int(round(self.xstart))
            slice1 = int(round(self.xstop)) + 1
            x_array = np.arange(slice0, slice1, dtype=np.float64)
            y_array = np.empty(x_array.shape, dtype=np.float64)
            y_array.fill((self.ystart + self.ystop) / 2.)
        else:
            slice0 = int(round(self.ystart))
            slice1 = int(round(self.ystop)) + 1
            y_array = np.arange(slice0, slice1, dtype=np.float64)
            x_array = np.empty(y_array.shape, dtype=np.float64)
            x_array.fill((self.xstart + self.xstop) / 2.)

        if self.wcs is not None:
            if not got_wavelength:
                log.debug("Wavelengths are from the wcs function.")
            nelem = slice1 - slice0
            if self.exp_type in WFSS_EXPTYPES:
                # We expect two (x and y) or three (x, y, spectral order).
                n_inputs = self.wcs.forward_transform.n_inputs
                ra = np.zeros(nelem, dtype=np.float64)
                dec = np.zeros(nelem, dtype=np.float64)
                # Temporary variable so as not to clobber `wavelength`.
                wcs_wl = np.zeros(nelem, dtype=np.float64)
                transform = self.wcs.forward_transform
                if n_inputs == 2:
                    for i in range(nelem):
                        ra[i], dec[i], wcs_wl[i], _ = transform(
                                        x_array[i], y_array[i])
                elif n_inputs == 3:
                    for i in range(nelem):
                        ra[i], dec[i], wcs_wl[i], _ = transform(
                                x_array[i], y_array[i], self.spectral_order)
                else:
                    log.error("n_inputs for wcs function is %d", n_inputs)
                    log.error("WCS function was expected to take "
                              "either 2 or 3 arguments.")
                    ra[:] = -999.
                    dec[:] = -999.
                    wcs_wl[:] = -999.
            else:
                ra, dec, wcs_wl = self.wcs(x_array, y_array)
            # We need one right ascension and one declination, representing
            # the direction of pointing.
            mask = np.isnan(ra)
            not_nan = np.logical_not(mask)
            if np.any(not_nan):
                ra2 = ra[not_nan]
                min_ra = ra2.min()
                max_ra = ra2.max()
                ra = (min_ra + max_ra) / 2.
            else:
                log.warning("All right ascension values are NaN; "
                            "assigning dummy value -999.")
                ra = -999.
            mask = np.isnan(dec)
            not_nan = np.logical_not(mask)
            if np.any(not_nan):
                dec2 = dec[not_nan]
                min_dec = dec2.min()
                max_dec = dec2.max()
                dec = (min_dec + max_dec) / 2.
            else:
                log.warning("All declination values are NaN; "
                            "assigning dummy value -999.")
                dec = -999.

        else:
            (ra, dec, wcs_wl) = (None, None, None)

        if not got_wavelength:
            wavelength = wcs_wl                 # from wcs, or None

        # Range (slice) of pixel numbers in the dispersion direction.
        disp_range = [slice0, slice1]
        if self.dispaxis == HORIZONTAL:
            image = data
        else:
            image = np.transpose(data, (1, 0))
        if wavelength is None:
            log.warning("Wavelengths could not be determined.")
            if slice0 <= 0:
                wavelength = np.arange(1, slice1 - slice0 + 1,
                                       dtype=np.float64)
            else:
                wavelength = np.arange(slice0, slice1, dtype=np.float64)

        temp_wl = wavelength.copy()
        nan_mask = np.isnan(wavelength)
        n_nan = nan_mask.sum(dtype=np.intp)
        if n_nan > 0:
            log.warning("%d NaNs in wavelength array", n_nan)
            temp_wl[nan_mask] = 0.01            # because NaNs cause problems

        # src total flux, area, total weight
        (net, background) = \
        extract1d.extract1d(image, temp_wl, disp_range,
                            self.p_src, self.p_bkg, self.independent_var,
                            self.smoothing_length, self.bkg_order,
                            weights=None)
        del temp_wl

        dq = np.zeros(net.shape, dtype=np.int32)
        if n_nan > 0:
            (wavelength, net, background, dq) = \
                nans_at_endpoints(wavelength, net, background, dq)

        return (ra, dec, wavelength, net, background, dq)


class ImageExtractModel(ExtractBase):
    """This uses an image that specifies the extraction region."""

    def __init__(self, input_model, slit,
                 ref_file_type=None,
                 match="unknown",
                 spectral_order=1,
                 ref_image=None,
                 dispaxis=HORIZONTAL,
                 smoothing_length=0,
                 nod_correction=0):
        """Extract using a reference image to define the extraction and
           background regions."""

        super().__init__()

        self.exp_type = input_model.meta.exposure.type
        """
        issue #1781
        self.instrument_name = input_model.meta.instrument.name
        """
        # ref_model contains one or more images; ref_image is the one that
        # matches the current configuration (slit name and spectral order).
        self.ref_image = ref_image
        self.spectral_order = spectral_order
        self.dispaxis = dispaxis
        self.nod_correction = nod_correction

        if smoothing_length is None:
            smoothing_length = 0
        if (smoothing_length > 0 and
            smoothing_length // 2 * 2 == smoothing_length):
            log.warning("smoothing_length was even (%d), so incremented by 1",
                        smoothing_length)
            smoothing_length += 1               # must be odd
        self.smoothing_length = smoothing_length

        if self.exp_type == "NIS_SOSS":
            if hasattr(input_model.meta, 'wcs'):
                try:
                    self.wcs = niriss.niriss_soss_set_input(
                                input_model, self.spectral_order)
                except ValueError:
                    raise InvalidSpectralOrderNumberError(
                                "Spectral order {} is not valid"
                                .format(self.spectral_order))
        elif slit == DUMMY:
            if hasattr(input_model.meta, 'wcs'):
                self.wcs = input_model.meta.wcs
        elif hasattr(slit, 'meta') and hasattr(slit.meta, 'wcs'):
            self.wcs = slit.meta.wcs
        if self.wcs is None:
            log.warning("WCS function not found in input.")


    def nominal_locn(self, middle, middle_wl):
        """Find the nominal cross-dispersion location of the target spectrum.

        This version is for the case that the reference file is an image.

        Parameters
        ----------
        middle: int
            The zero-indexed pixel number of the point in the dispersion
            direction at which `locn_from_wcs` determined the actual
            location (in the cross-dispersion direction) of the target
            spectrum.

        middle_wl: float
            The wavelength at pixel `middle`.  This is not used.

        Returns
        -------
        location: float or None
            The nominal cross-dispersion location (i.e. unmodified by
            nod or dither offset) of the target spectrum.
            The value will be None if `middle` is outside the reference
            image or if the reference image does not specify any pixels
            to extract at `middle`.
        """

        shape = self.ref_image.data.shape

        if self.dispaxis == HORIZONTAL:
            if middle < 0 or middle >= shape[1]:
                return None
            middle_line = self.ref_image.data[:, middle]
        else:
            if middle < 0 or middle >= shape[0]:
                return None
            middle_line = self.ref_image.data[middle, :]

        mask_target = np.where(middle_line > 0., 1., 0.)
        x = np.arange(len(middle_line), dtype=np.float64)

        numerator = (x * mask_target).sum()
        denominator = mask_target.sum()
        if denominator > 0.:
            location = numerator / denominator
        else:
            location = None

        return location


    def add_nod_correction(self):
        """Shift the reference image (in-place)."""

        if self.nod_correction == 0:
            return

        log.info("Applying nod/dither offset of %s", str(self.nod_correction))

        # Shift the image in the cross-dispersion direction.
        ref = self.ref_image.data.copy()
        shift = self.nod_correction
        ishift = int(round(shift))
        if ishift != shift:
            log.info("Rounding nod/dither offset of %g to %d", shift, ishift)
        if self.dispaxis == HORIZONTAL:
            if abs(ishift) >= ref.shape[0]:
                log.error("Nod offset %d is too large, skipping ...", ishift)
                return
            self.ref_image.data[:, :] = 0.
            if ishift > 0:
                self.ref_image.data[ishift:, :] = ref[:-ishift, :]
            else:
                ishift = -ishift
                self.ref_image.data[:-ishift, :] = ref[ishift:, :]
        else:
            if abs(ishift) >= ref.shape[1]:
                log.error("Nod offset %d is too large, skipping ...", ishift)
                return
            self.ref_image.data[:, :] = 0.
            if ishift > 0:
                self.ref_image.data[:, ishift:] = ref[:, :-ishift]
            else:
                ishift = -ishift
                self.ref_image.data[:, :-ishift] = ref[:, ishift:]


    def log_extraction_parameters(self):

        log.debug("Using a reference image that defines extraction regions.")
        log.debug("dispaxis = %d", self.dispaxis)
        log.debug("spectral order = %s", str(self.spectral_order))
        log.debug("smoothing_length = %d", self.smoothing_length)
        log.debug("nod_correction = %s", str(self.nod_correction))


    def extract(self, data, wl_array):
        """
        Do the actual extraction, for the case that the reference file
        is an image.

        Parameters
        ----------
        data: array_like (2-D)
            Science data array.

        wl_array: array_like (2-D) or None
            Wavelengths corresponding to `data`, or None if no WAVELENGTH
            was found in the input file.

        Returns
        -------
        (ra, dec, wavelength, net, background)
            ra and dec are floats, and the others are 1-D arrays.
            ra and dec are the right ascension and declination at the
            nominal center of the slit.  `wavelength` is the wavelength in
            micrometers at each pixel.  `net` is the count rate
            (counts / s) minus the background at each pixel.  `background`
            is the background count rate that was subtracted from the total
            source count rate to get `net`.
        """

        shape = data.shape
        # Truncate or expand reference image to match the science data.
        ref = self.match_shape(shape)

        # This is the axis along which to add up the data.
        if self.dispaxis == HORIZONTAL:
            axis = 0
        else:
            axis = 1

        # The values of these arrays will be just 0 or 1.  If ref did not
        # define any background pixels, however, mask_bkg will be None.
        (mask_target, mask_bkg) = self.separate_target_and_background(ref)

        # This is the number of pixels in the cross-dispersion direction,
        # in the target extraction region.
        n_target = mask_target.sum(axis=axis, dtype=np.float)

        # Extract the data.
        gross = (data * mask_target).sum(axis=axis, dtype=np.float)

        # Extract the background.
        if mask_bkg is not None:
            n_bkg = mask_bkg.sum(axis=axis, dtype=np.float)
            # -1 is used as a flag, and also to avoid dividing by zero.
            n_bkg = np.where(n_bkg == 0., -1., n_bkg)
            background = (data * mask_bkg).sum(axis=axis, dtype=np.float)
            # Boxcar smoothing.
            if self.smoothing_length > 1:
                background = extract1d.bxcar(background, self.smoothing_length)
            scalefactor = n_target / n_bkg
            scalefactor = np.where(n_bkg > 0., scalefactor, 0.)
            background *= scalefactor
            net = gross - background
        else:
            background = np.zeros_like(gross)
            net = gross.copy()
        del gross

        if wl_array is None or len(wl_array) == 0:
            got_wavelength = False
        else:
            got_wavelength = True               # may be reset below
        # If wl_array has all 0 values, interpret that to mean that the
        # wavelength attribute was not populated.
        if not got_wavelength or wl_array.min() == 0. and wl_array.max() == 0.:
            got_wavelength = False

        # Used for computing the celestial coordinates and the 1-D array
        # of wavelengths.
        flag = (mask_target > 0.)
        grid = np.indices(shape)
        masked_grid = flag.astype(np.float) * grid[axis]
        g_sum = masked_grid.sum(axis=axis)
        f_sum = flag.sum(axis=axis, dtype=np.float)
        f_sum_zero = np.where(f_sum <= 0.)
        f_sum[f_sum_zero] = 1.                  # to avoid dividing by zero

        spectral_trace = g_sum / f_sum
        del f_sum, g_sum, masked_grid, grid, flag

        # We want x_array and y_array to be 1-D arrays, with the X values
        # initially running from 0 at the left edge of the input cutout to
        # the right edge, and the Y values being near the middle of
        # the spectral extraction region.  So the locations
        # (x_array[i], y_array[i]) should be the spectral trace.  Near the
        # left and right edges, there might not be any non-zero values in
        # mask_target, so a slice will be extracted from both x_array and
        # y_array in order to exclude pixels that are not within the
        # extraction region.
        if self.dispaxis == HORIZONTAL:
            x_array = np.arange(shape[1], dtype=np.float)
            y_array = spectral_trace
        else:
            x_array = spectral_trace
            y_array = np.arange(shape[0], dtype=np.float)

        # Trim off the ends, if there's no data there.  Save trim_slc.
        mask = np.where(n_target > 0.)
        if len(mask[0]) > 0:
            trim_slc = slice(mask[0][0], mask[0][-1] + 1)
            net = net[trim_slc]
            background = background[trim_slc]
            n_target = n_target[trim_slc]
            x_array = x_array[trim_slc]
            y_array = y_array[trim_slc]

        if got_wavelength:
            log.debug("Wavelengths are from wavelength attribute.")
            indx = np.around(x_array).astype(np.int)
            indy = np.around(y_array).astype(np.int)
            indx = np.where(indx < 0, 0, indx)
            indx = np.where(indx >= shape[1], shape[1] - 1, indx)
            indy = np.where(indy < 0, 0, indy)
            indy = np.where(indy >= shape[0], shape[0] - 1, indy)
            wavelength = wl_array[indy, indx]

        nelem = len(x_array)

        if self.wcs is not None:
            if not got_wavelength:
                log.debug("Wavelengths are from the wcs function.")
            if self.exp_type in WFSS_EXPTYPES:
                # We expect two (x and y) or three (x, y, spectral order).
                n_inputs = self.wcs.forward_transform.n_inputs
                ra = np.zeros(nelem, dtype=np.float)
                dec = np.zeros(nelem, dtype=np.float)
                # Temporary variable so as not to clobber `wavelength`.
                wcs_wl = np.zeros(nelem, dtype=np.float)
                transform = self.wcs.forward_transform
                if n_inputs == 2:
                    for i in range(nelem):
                        ra[i], dec[i], wcs_wl[i], _ = transform(
                                        x_array[i], y_array[i])
                elif n_inputs == 3:
                    for i in range(nelem):
                        ra[i], dec[i], wcs_wl[i], _ = transform(
                                x_array[i], y_array[i], self.spectral_order)
                else:
                    log.error("n_inputs for wcs function is %d", n_inputs)
                    log.error("WCS function was expected to take "
                              "either 2 or 3 arguments.")
                    ra[:] = -999.
                    dec[:] = -999.
                    wcs_wl[:] = -999.
            else:
                """
                See issue #1781
                if self.instrument_name == "NIRSPEC":
                    # xxx temporary:  NIRSpec wcs is one-based.
                    ra, dec, wcs_wl = self.wcs(x_array + 1., y_array + 1.)
                else:
                    ra, dec, wcs_wl = self.wcs(x_array, y_array)
                """
                ra, dec, wcs_wl = self.wcs(x_array, y_array)
            # We need one right ascension and one declination, representing
            # the direction of pointing.
            middle = ra.shape[0] // 2           # ra and dec have same shape
            mask = np.isnan(ra)
            not_nan = np.logical_not(mask)
            if not_nan[middle]:
                log.debug("Using midpoint of spectral trace for RA and Dec.")
                ra = ra[middle]
            else:
                if np.any(not_nan):
                    log.warning("Midpoint of coordinate array is NaN; using "
                                "the average of non-NaN min and max values.")
                    ra = (np.nanmin(ra) + np.nanmax(ra)) / 2.
                else:
                    log.warning("All right ascension values are NaN; "
                                "assigning dummy value -999.")
                    ra = -999.
            mask = np.isnan(dec)
            not_nan = np.logical_not(mask)
            if not_nan[middle]:
                dec = dec[middle]
            else:
                if np.any(not_nan):
                    dec = (np.nanmin(dec) + np.nanmax(dec)) / 2.
                else:
                    log.warning("All declination values are NaN; "
                                "assigning dummy value -999.")
                    dec = -999.

        else:
            (ra, dec, wcs_wl) = (None, None, None)

        if not got_wavelength:
            wavelength = wcs_wl                 # from wcs, or None
        if wavelength is None:
            if self.dispaxis == HORIZONTAL:
                wavelength = np.arange(shape[1], dtype=np.float)
            else:
                wavelength = np.arange(shape[0], dtype=np.float)
            wavelength = wavelength[trim_slc]

        dq = np.zeros(net.shape, dtype=np.int32)
        nan_mask = np.isnan(wavelength)
        n_nan = nan_mask.sum(dtype=np.intp)
        if n_nan > 0:
            log.warning("%d NaNs in wavelength array", n_nan)
            (wavelength, net, background, dq) = \
                nans_at_endpoints(wavelength, net, background, dq)

        return (ra, dec, wavelength, net, background, dq)


    def match_shape(self, shape):
        """Truncate or expand reference image to match the science data."""

        # The science data may be 2-D or 3-D (or more?), but the reference
        # image only needs to be 2-D.

        ref = self.ref_image.data

        ref_shape = ref.shape
        if shape == ref_shape:
            return ref

        # This is the shape of the last two axes of the science data.
        buf = np.zeros((shape[-2], shape[-1]), dtype=ref.dtype)
        y_max = min(shape[-2], ref_shape[0])
        x_max = min(shape[-1], ref_shape[1])
        slc0 = slice(0, y_max)
        slc1 = slice(0, x_max)
        buf[slc0, slc1] = ref[slc0, slc1].copy()

        return buf


    def separate_target_and_background(self, ref):

        mask_target = np.where(ref > 0., 1., 0.)

        if np.any(ref < 0.):
            mask_bkg = np.where(ref < 0., 1., 0.)
        else:
            mask_bkg = None

        return (mask_target, mask_bkg)


def interpolate_response(wavelength, relsens):
    """Interpolate within the relative response table.

    Parameters
    ----------
    wavelength: array_like, 1-D
        Wavelengths in the science data

    relsens: record array
        Contains two columns, 'wavelength' and 'response'.

    Returns
    -------
    r_factor: array_like
        The response, interpolated at `wavelength`, with extrapolated
        elements and zero or negative response values set to 1.  Divide
        the net count rate by r_factor to obtain the flux.
    """

    # "_relsens" indicates that the values were read from the RELSENS table.
    wl_relsens = relsens['wavelength']
    resp_relsens = relsens['response']
    MICRONS_100 = 1.e-4                 # 100 microns, in meters
    if wl_relsens.max() > 0. and wl_relsens.max() < MICRONS_100:
        log.warning("Converting RELSENS wavelengths to microns.")
        wl_relsens *= 1.e6

    bad = False
    if np.any(np.isnan(wl_relsens)):
        log.error("In RELSENS, the 'wavelength' column contains NaNs.")
        bad = True
    if np.any(np.isnan(resp_relsens)):
        log.error("In RELSENS, the 'response' column contains NaNs.")
        bad = True
    if bad:
        raise ValueError("Found NaNs in RELSENS table.")

    # `r_factor` is the response, interpolated at the wavelengths in the
    # science data.  -2048 is a flag value, to check for extrapolation.
    r_factor = np.interp(wavelength, wl_relsens, resp_relsens, -2048., -2048.)
    mask = np.where(r_factor == -2048.)
    if len(mask[0]) > 0:
        log.warning("Using RELSENS, %d elements were extrapolated; "
                    "these values will be set to 1.", len(mask[0]))
        r_factor[mask] = 1.
    mask = np.where(r_factor <= 0.)
    if len(mask[0]) > 0:
        log.warning("Using RELSENS, %d interpolated response values "
                    "were <= 0; these values will be set to 1.", len(mask[0]))
        r_factor[mask] = 1.

    return r_factor


def do_extract1d(input_model, refname, smoothing_length, bkg_order):

    output_model = datamodels.MultiSpecModel()
    output_model.update(input_model)

    # Read and interpret the reference file.
    ref_dict = load_ref_file(refname)

    # More generally, one could use '"Multi" in str(type(input_model))'.
    if isinstance(input_model, datamodels.MultiSlitModel) or \
       isinstance(input_model, datamodels.MultiProductModel):

        if isinstance(input_model, datamodels.MultiSlitModel):
            slits = input_model.slits
        else:                           # MultiProductModel
            slits = input_model.products

        # Loop over the slits in the input model
        for slit in slits:
            log.info('Working on slit %s', slit.name)
            if np.size(slit.data) <= 0:
                log.info('No data for slit %s, skipping ...', slit.name)
                continue
            sp_order = get_spectral_order(slit)
            extract_params = get_extract_parameters(
                                ref_dict,
                                slit, slit.name, sp_order,
                                input_model.meta, smoothing_length, bkg_order)
            if extract_params['match'] == NO_MATCH:
                log.critical('Missing extraction parameters.')
                raise ValueError('Missing extraction parameters.')
            elif extract_params['match'] == PARTIAL:
                log.info('Spectral order %d not found, skipping ...', sp_order)
                continue
            find_dispaxis(slit, sp_order, extract_params)

            try:
                (ra, dec, wavelength, net, background, dq) = \
                        extract_one_slit(input_model, slit, -1,
                                         verbose=True,
                                         **extract_params)
            except InvalidSpectralOrderNumberError as e:
                log.info(str(e) + ", skipping ...")
                continue
            got_relsens = True
            try:
                relsens = slit.relsens
            except AttributeError:
                got_relsens = False
            if got_relsens and len(relsens) == 0:
                got_relsens = False
            if got_relsens:
                r_factor = interpolate_response(wavelength, relsens)
                flux = net / r_factor
            else:
                log.warning("No relsens for current slit, "
                            "so can't compute flux.")
                flux = np.zeros_like(net)
            fl_error = np.ones_like(net)
            nerror = np.ones_like(net)
            berror = np.ones_like(net)
            spec = datamodels.SpecModel()
            otab = np.array(list(zip(wavelength, flux, fl_error, dq,
                                 net, nerror, background, berror)),
                            dtype=spec.spec_table.dtype)
            spec = datamodels.SpecModel(spec_table=otab)
            spec.meta.wcs = spec_wcs.create_spectral_wcs(ra, dec, wavelength)
            spec.spec_table.columns['wavelength'].unit = 'um'
            spec.spec_table.columns['flux'].unit = 'mJy'
            spec.spec_table.columns['error'].unit = 'mJy'
            spec.spec_table.columns['net'].unit = 'DN/s'
            spec.spec_table.columns['nerror'].unit = 'DN/s'
            spec.spec_table.columns['background'].unit = 'DN/s'
            spec.spec_table.columns['berror'].unit = 'DN/s'
            spec.slit_ra = ra
            spec.slit_dec = dec
            spec.spectral_order = sp_order
            copy_keyword_info(slit, slit.name, spec)
            output_model.spec.append(spec)
    else:
        slitname = input_model.meta.exposure.type
        if slitname is None:
            slitname = ANY
        if slitname == 'NIS_SOSS':
            slitname = input_model.meta.subarray.name
        log.debug('slitname=%s', slitname)

        # Loop over these spectral order numbers.
        if input_model.meta.exposure.type == "NIS_SOSS":
            # This list of spectral order numbers may need to be assigned
            # differently for other exposure types.
            spectral_order_list = [1, 2, 3]
        else:
            # For this case, we'll call get_spectral_order to get the order.
            spectral_order_list = ["not set yet"]

        if isinstance(input_model, (datamodels.ImageModel,
                                    datamodels.DrizProductModel)):
            for sp_order in spectral_order_list:
                if sp_order == "not set yet":
                    sp_order = get_spectral_order(input_model)

                extract_params = get_extract_parameters(
                                    ref_dict,
                                    input_model, slitname, sp_order,
                                    input_model.meta, smoothing_length,
                                    bkg_order)
                if extract_params['match'] == EXACT:
                    slit = DUMMY
                    find_dispaxis(input_model, sp_order, extract_params)
                    try:
                        (ra, dec, wavelength, net, background, dq) = \
                                extract_one_slit(input_model, slit, -1,
                                                 verbose=True,
                                                 **extract_params)
                    except InvalidSpectralOrderNumberError as e:
                        log.info(str(e) + ", skipping ...")
                        continue
                elif extract_params['match'] == PARTIAL:
                    log.info('Spectral order %d not found, skipping ...',
                             sp_order)
                    continue
                else:
                    log.critical('Missing extraction parameters.')
                    raise ValueError('Missing extraction parameters.')
                got_relsens = True
                try:
                    relsens = input_model.relsens
                except AttributeError:
                    got_relsens = False
                if got_relsens and len(relsens) == 0:
                    got_relsens = False
                if got_relsens:
                    r_factor = interpolate_response(wavelength, relsens)
                    flux = net / r_factor
                else:
                    log.warning("No relsens for input file, "
                                "so can't compute flux.")
                    flux = np.zeros_like(net)
                fl_error = np.ones_like(net)
                nerror = np.ones_like(net)
                berror = np.ones_like(net)
                spec = datamodels.SpecModel()
                otab = np.array(list(zip(wavelength, flux, fl_error, dq,
                                     net, nerror, background, berror)),
                                dtype=spec.spec_table.dtype)
                spec = datamodels.SpecModel(spec_table=otab)
                spec.meta.wcs = spec_wcs.create_spectral_wcs(
                                        ra, dec, wavelength)
                spec.spec_table.columns['wavelength'].unit = 'um'
                spec.spec_table.columns['flux'].unit = 'mJy'
                spec.spec_table.columns['error'].unit = 'mJy'
                spec.spec_table.columns['net'].unit = 'DN/s'
                spec.spec_table.columns['nerror'].unit = 'DN/s'
                spec.spec_table.columns['background'].unit = 'DN/s'
                spec.spec_table.columns['berror'].unit = 'DN/s'
                spec.slit_ra = ra
                spec.slit_dec = dec
                spec.spectral_order = sp_order
                if slitname is not None and slitname != "ANY":
                    spec.name = slitname
                output_model.spec.append(spec)

        elif isinstance(input_model, (datamodels.CubeModel,
                                      datamodels.SlitModel)):

            # NRS_BRIGHTOBJ exposures are instances of SlitModel.
            for sp_order in spectral_order_list:
                if sp_order == "not set yet":
                    sp_order = get_spectral_order(input_model)

                extract_params = get_extract_parameters(
                                    ref_dict,
                                    input_model, slitname, sp_order,
                                    input_model.meta, smoothing_length,
                                    bkg_order)
                if extract_params['match'] == NO_MATCH:
                    log.critical('Missing extraction parameters.')
                    raise ValueError('Missing extraction parameters.')
                elif extract_params['match'] == PARTIAL:
                    log.warning('Spectral order %d not found, skipping ...',
                                sp_order)
                    continue
                find_dispaxis(input_model, sp_order, extract_params)

                got_relsens = True
                try:
                    relsens = input_model.relsens
                except AttributeError:
                    got_relsens = False
                if got_relsens and len(relsens) == 0:
                    got_relsens = False
                if not got_relsens:
                    log.warning("No relsens for input file, "
                                "so can't compute flux.")

                # Loop over each integration in the input model
                slit = DUMMY
                verbose = True
                for integ in range(input_model.data.shape[0]):
                    # Extract spectrum
                    try:
                        (ra, dec, wavelength, net, background, dq) = \
                                extract_one_slit(input_model, slit, integ,
                                                 verbose=verbose,
                                                 **extract_params)
                    except InvalidSpectralOrderNumberError as e:
                        log.info(str(e) + ", skipping ...")
                        break
                    verbose = False
                    if got_relsens:
                        r_factor = interpolate_response(wavelength,
                                                        input_model.relsens)
                        flux = net / r_factor
                    else:
                        flux = np.zeros_like(net)
                    fl_error = np.ones_like(net)
                    nerror = np.ones_like(net)
                    berror = np.ones_like(net)
                    spec = datamodels.SpecModel()
                    otab = np.array(list(zip(wavelength, flux, fl_error, dq,
                                         net, nerror, background, berror)),
                                    dtype=spec.spec_table.dtype)
                    spec = datamodels.SpecModel(spec_table=otab)
                    spec.meta.wcs = spec_wcs.create_spectral_wcs(
                                        ra, dec, wavelength)
                    spec.spec_table.columns['wavelength'].unit = 'um'
                    spec.spec_table.columns['flux'].unit = 'mJy'
                    spec.spec_table.columns['error'].unit = 'mJy'
                    spec.spec_table.columns['net'].unit = 'DN/s'
                    spec.spec_table.columns['nerror'].unit = 'DN/s'
                    spec.spec_table.columns['background'].unit = 'DN/s'
                    spec.spec_table.columns['berror'].unit = 'DN/s'
                    spec.slit_ra = ra
                    spec.slit_dec = dec
                    spec.spectral_order = sp_order
                    output_model.spec.append(spec)

        elif isinstance(input_model, datamodels.IFUCubeModel):

            try:
                source_type = input_model.meta.target.source_type.lower()
            except AttributeError:
                source_type = "unknown"
            output_model = ifu.ifu_extract1d(input_model, refname, source_type)

        else:
            log.error("The input file is not supported for this step.")
            raise RuntimeError("Can't extract a spectrum from this file.")

    # Copy the integration time information from the INT_TIMES table
    # to keywords in the output file.
    populate_time_keywords(input_model, output_model)

    # See output_model.spec[i].meta.wcs instead.
    output_model.meta.wcs = None

    # If the reference file is an image, explicitly close it.
    if ref_dict is not None and 'ref_model' in ref_dict:
        ref_dict['ref_model'].close()

    return output_model


def populate_time_keywords(input_model, output_model):
    """Copy the integration times keywords to header keywords.

    Parameters
    ----------
    input_model: data model
        The input science model.

    output_model: data model
        The output science model.  This may be modified in-place.
    """

    nints = input_model.meta.exposure.nints
    int_start = input_model.meta.exposure.integration_start
    if int_start is None:
        log.warning("INTSTART not found; assuming a value of 1.")
        int_start = 1
    int_start -= 1                              # zero indexed
    int_end = input_model.meta.exposure.integration_end
    if int_end is None:
        log.warning("INTEND not found; assuming a value of %d.", nints)
        int_end = nints
    int_end -= 1                                # zero indexed
    if nints > 1:
        num_integrations = int_end - int_start + 1
    else:
        num_integrations = 1

    if hasattr(input_model, 'int_times') and input_model.int_times is not None:
        nrows = len(input_model.int_times)
    else:
        nrows = 0
    if nrows < 1:
        log.warning("There is no INT_TIMES table in the input file.")
        return

    # If we have a single plane (e.g. ImageModel or MultiSlitModel),
    # we will only populate the keywords if the corresponding uncal file
    # had one integration.  If the data were or might have been segmented,
    # we use the first and last integration numbers to determine whether
    # the data were in fact averaged over integrations, and if so, we
    # should not populate the int_times-related header keywords.

    skip = False                        # initial value

    if isinstance(input_model, (datamodels.MultiSlitModel,
                                datamodels.MultiProductModel,
                                datamodels.ImageModel,
                                datamodels.DrizProductModel)):
        if num_integrations > 1:
            log.warning("Not using INT_TIMES table because the data "
                        "have been averaged over integrations.")
            skip = True
    elif isinstance(input_model, (datamodels.CubeModel,
                                  datamodels.SlitModel)):
        shape = input_model.data.shape
        if len(shape) == 2 and num_integrations > 1:
            log.warning("Not using INT_TIMES table because the data "
                        "have been averaged over integrations.")
            skip = True
        elif len(shape) != 3 or shape[0] > nrows:
            # Later, we'll check that the integration_number column actually
            # has a row corresponding to every integration in the input.
            log.warning("Not using INT_TIMES table because the data shape "
                        "is not consistent with the number of table rows.")
            skip = True
    elif isinstance(input_model, datamodels.IFUCubeModel):
        log.warning("The INT_TIMES table will be ignored for IFU data.")
        skip = True

    if skip:
        return

    if nrows > 0:
        int_num = input_model.int_times['integration_number']
        start_utc = input_model.int_times['int_start_MJD_UTC']
        mid_utc = input_model.int_times['int_mid_MJD_UTC']
        end_utc = input_model.int_times['int_end_MJD_UTC']
        start_tdb = input_model.int_times['int_start_BJD_TDB']
        mid_tdb = input_model.int_times['int_mid_BJD_TDB']
        end_tdb = input_model.int_times['int_end_BJD_TDB']

        # Inclusive range of integration numbers in the input data,
        # zero indexed.
        data_range = (int_start, int_end)
        # Inclusive range of integration numbers in the INT_TIMES table,
        # zero indexed.
        table_range = (int_num[0] - 1, int_num[-1] - 1)
        offset = data_range[0] - table_range[0]
        if data_range[0] < table_range[0] or data_range[1] > table_range[1]:
            log.warning("Not using the INT_TIMES table because it does not "
                        "include rows for all integrations in the data.")
            return

        log.debug("Copying values from the INT_TIMES table.")

        if hasattr(input_model, 'data'):
            shape = input_model.data.shape
            if len(shape) == 2:
                num_integ = 1
            else:                               # len(shape) == 3
                num_integ = shape[0]
        else:                                   # e.g. MultiSlit data
            num_integ = 1

        n_output_spec = len(output_model.spec)

        # Assumptions about input_model:
        # If the number of integrations is one, there may be multiple
        # spectra.  These might be different spectral orders, and different
        # orders can be in the same image (e.g. NIRISS SOSS data) or in
        # different 2-D cutouts (e.g. MultiSlit data).  Or these might be
        # spectra through different slits but projected onto a single
        # image and subsequently (via extract_2d) separated into different
        # 2-D cutouts (MultiSlit data, again).  But there could be just
        # one spectrum, either in a single 2-D image (e.g. MIRI LRS),
        # subarray, or MultiSlit with just one slit.
        # If the number of integrations is greater than one (or if the
        # input is multi-integration format (3-D) but with a length of
        # 1 for the first axis), we will only extract the spectrum of
        # one object; the location will be the same for every integration,
        # and the number of output spectra will be equal to the number
        # of integrations (unless there was a problem with the extraction).

        # num_j is the number of different spectra, e.g. the number of
        # fixed-slit spectra, MSA spectra, or different spectral orders;
        # num_j = 1 for multi-integration data (meaning there's just one
        # spectrum in each integration).
        # The total number of output spectra n_output_spec = num_integ * num_j
        if num_integ > 1:
            num_j = 1
        else:
            num_j = n_output_spec

        log.debug("Number of integrations = %d; number of spectra for "
                  "one integration = %d", num_integ, num_j)

        # Note that either num_integ or num_j (or possibly both) will be one.
        # n is a counter for spectra in output_model.
        n = 0
        for k in range(num_integ):              # for each integration
            row = k + offset
            for j in range(num_j):                  # for each input spectrum
                spec = output_model.spec[n]         # n is incremented in loop
                spec.int_num = int_num[row]
                spec.start_utc = start_utc[row]
                spec.mid_utc = mid_utc[row]
                spec.end_utc = end_utc[row]
                spec.start_tdb = start_tdb[row]
                spec.mid_tdb = mid_tdb[row]
                spec.end_tdb = end_tdb[row]
                n += 1

    else:
        log.warning("There is no INT_TIMES table in the input file.")


def get_spectral_order(slit):

    if hasattr(slit.meta, 'wcsinfo'):
        sp_order = slit.meta.wcsinfo.spectral_order
        if sp_order is None:
            log.warning("spectral_order is None; using 1")
            sp_order = 1
    else:
        log.warning("slit.meta doesn't have attribute wcsinfo; "
                    "setting spectral order to 1")
        sp_order = 1

    return sp_order


def copy_keyword_info(slit, slitname, spec):
    """Copy metadata from the input to the output spectrum.

    Parameters
    ----------
    slit: An element of MultiSlitModel.slits or MultiProductModel.products
        A 2-D array containing a spectrum.

    slitname: string or None
        The name of the slit.

    spec: One element of MultiSpecModel.spec
        Metadata attributes will be updated in-place.
    """

    if slitname is not None and slitname != "ANY":
        spec.name = slitname

    if hasattr(slit, "slitlet_id"):
        spec.slitlet_id = slit.slitlet_id

    if hasattr(slit, "source_id"):
        spec.source_id = slit.source_id

    if hasattr(slit, "source_name") and slit.source_name is not None:
        spec.source_name = slit.source_name

    if hasattr(slit, "source_alias") and slit.source_alias is not None:
        spec.source_alias = slit.source_alias

    if hasattr(slit, "source_type") and slit.source_type is not None:
        spec.source_type = slit.source_type

    if hasattr(slit, "stellarity") and slit.stellarity is not None:
        spec.stellarity = slit.stellarity

    if hasattr(slit, "source_xpos"):
        spec.source_xpos = slit.source_xpos

    if hasattr(slit, "source_ypos"):
        spec.source_ypos = slit.source_ypos

    if hasattr(slit, "shutter_state"):
        spec.shutter_state = slit.shutter_state


def extract_one_slit(input_model, slit, integ, verbose, **extract_params):
    """Extract data for one slit, or spectral order, or plane.
    Parameters
    ----------
    input_model: data model
        The input science model.

    slit: one slit from a MultiSlitModel (or similar), or "dummy"
        If slit is "dummy", the data array is input_model.data; otherwise,
        the data array is slit.data.
        In the former case, if `integ` is zero or larger, the spectrum
        will be extracted from the 2-D slice input_model.data[integ].

    integ: int
        For the case that input_model is a SlitModel or a CubeModel,
        `integ` is the integration number.  If the integration number is
        not relevant (i.e. the data array is 2-D), `integ` should be -1.

    verbose: boolean
        If True, log more info (extraction parameters, in particular).

    extract_params: dictionary
        Parameters read from the reference file.

    Returns
    -------
    tuple (ra, dec, wavelength, net, background, dq)
        `ra` and `dec` are floats, and the others are 1-D arrays.
        `ra` and `dec` are the right ascension and declination at the
        nominal center of the slit.  `wavelength` is the wavelength in
        micrometers at each pixel.  `net` is the count rate (counts / s)
        minus the background at each pixel.  `background` is the background
        count rate that was subtracted from the total source count rate
        to get `net`.  `dq` is the data quality array.
    """

    if verbose:
        log_initial_parameters(extract_params)

    input_dq = None                             # possibly replaced below
    if integ > -1:
        data = input_model.data[integ]
        if hasattr(input_model, 'dq'):
            input_dq = input_model.dq[integ]
        try:
            wl_array = input_model.wavelength
        except AttributeError:
            wl_array = None
    elif slit == DUMMY:
        data = input_model.data
        if hasattr(input_model, 'dq'):
            input_dq = input_model.dq
        try:
            wl_array = input_model.wavelength
        except AttributeError:
            wl_array = None
    else:
        data = slit.data
        if hasattr(slit, 'dq'):
            input_dq = slit.dq
        try:
            wl_array = slit.wavelength
        except AttributeError:
            wl_array = None

    data = replace_bad_values(data, input_dq, fill=0.)

    if extract_params['ref_file_type'] == FILE_TYPE_IMAGE:
        # The reference file is an image.
        extract_model = ImageExtractModel(input_model, slit, **extract_params)
        ap = None
    else:
        # If there is a reference file (there doesn't have to be), it's in
        # JSON format.
        extract_model = ExtractModel(input_model, slit, **extract_params)
        ap = get_aperture(data.shape, extract_model.wcs, extract_params)
        extract_model.update_extraction_limits(ap)

    # Use the WCS function to find the cross-dispersion (XD) location that
    # is closest to the target coordinates.  This is the "actual" location
    # of the spectrum, so the extraction region should be centered here.
    (middle, middle_wl, locn) = extract_model.locn_from_wcs(input_model, slit)
    if middle is not None:
        log.debug("Spectrum location from WCS used column (or row) %d",
                  middle)

    # Find the nominal extraction location, i.e. the XD location specified
    # in the reference file prior to adding any nod/dither offset.  The
    # difference is the nod/dither offset.
    if middle is not None and locn is not None:
        nominal_location = extract_model.nominal_locn(middle, middle_wl)
        log.debug("Target spectrum is at %g in the cross-dispersion direction",
                  locn)
        if nominal_location is not None:
            log.debug("and the nominal XD location of the target spectrum "
                      "is %g", nominal_location)
            extract_model.nod_correction = locn - nominal_location
        else:
            log.debug("but couldn't determine the nominal XD location.")

    # Add the nod/dither offset to the polynomial coefficients, or shift
    # the reference image (depending on the type of reference file).
    extract_model.add_nod_correction()

    if verbose:
        extract_model.log_extraction_parameters()

    extract_model.assign_polynomial_limits()
    (ra, dec, wavelength, net, background, dq) = \
                extract_model.extract(data, wl_array)

    return (ra, dec, wavelength, net, background, dq)


def replace_bad_values(data, input_dq, fill=0.):
    """Replace NaNs and values flagged with DO_NOT_USE.

    Parameters
    ----------
    data: ndarray
        The input data array.

    input_dq: ndarray or None
        If not None, this will be checked for flag value DO_NOT_USE.

    fill: float
        Pixels that are NaN in `data` or are flagged in the `dq` array
        (if the latter is not None) will be assigned this value.

    Returns
    -------
    A possibly modified copy of the input data array.
    """

    mask = np.isnan(data)
    if input_dq is not None:
        bad_mask = np.bitwise_and(input_dq, dqflags.pixel['DO_NOT_USE']) > 0
        mask = np.logical_or(mask, bad_mask)

    if np.any(mask):
        mod_data = data.copy()
        mod_data[mask] = fill
        return mod_data
    else:
        return data

def nans_at_endpoints(wavelength, net, background, dq):
    """Flag NaNs in the wavelength array.

    All five input arrays should be 1-D and the same shape.
    If NaNs are present at endpoints of `wavelength`, the arrays will be
    trimmed to remove the NaNs.  NaNs at interior elements of `wavelength`
    will be left in place, but they will be flagged with DO_NOT_USE in the
    `dq` array.

    Parameters
    ----------
    wavelength: ndarray
        Array of wavelengths, possibly containing NaNs.

    net: ndarray
        Array of net count rates.

    background: ndarray
        Array of background values that were subtracted to get `net`.

    dq: ndarray
        Data quality array.

    Returns
    -------
    tuple (wavelength, net, background, dq)
        The returned `dq` array will have NaNs flagged with DO_NOT_USE,
        and all four arrays may have been trimmed at either or both ends.
    """

    # The input arrays will not be modified in-place.
    new_wl = wavelength.copy()
    new_net = net.copy()
    new_bkg = background.copy()
    new_dq = dq.copy()
    nelem = wavelength.shape[0]

    nan_mask = np.isnan(wavelength)

    new_dq[nan_mask] = np.bitwise_or(new_dq[nan_mask],
                                     dqflags.pixel['DO_NOT_USE'])
    not_nan = np.logical_not(nan_mask)
    flag = np.where(not_nan)
    if len(flag[0]) > 0:
        n_trimmed = flag[0][0] + nelem - (flag[0][-1] + 1)
        if n_trimmed > 0:
            log.info("Output arrays have been trimmed by %d elements",
                     n_trimmed)
            slc = slice(flag[0][0], flag[0][-1] + 1)
            new_wl = new_wl[slc]
            new_net = new_net[slc]
            new_bkg = new_bkg[slc]
            new_dq = new_dq[slc]
    else:
        new_dq |= dqflags.pixel['DO_NOT_USE']

    return (new_wl, new_net, new_bkg, new_dq)
