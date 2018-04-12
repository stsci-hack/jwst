import os
import pytest
from astropy.io import fits as pf
from jwst.jump.jump_step import JumpStep

from ..helpers import add_suffix

BIGDATA = os.environ['TEST_BIGDATA']

def test_jump_miri():
    """

    Regression test of jump step performed on MIRI data.

    """
    output_file_base, output_file = add_suffix('jump1_output.fits', 'jump')

    try:
        os.remove(output_file)
    except:
        pass



    JumpStep.call(BIGDATA+'/miri/test_jump/jw00001001001_01101_00001_MIRIMAGE_linearity.fits',
                  rejection_threshold=200.0,
                  output_file=output_file_base
                  )
    h = pf.open(output_file)
    href = pf.open(BIGDATA+'/miri/test_jump/jw00001001001_01101_00001_MIRIMAGE_jump.fits')
    newh = pf.HDUList([h['primary'],h['sci'],h['err'],h['pixeldq'],h['groupdq']])
    newhref = pf.HDUList([href['primary'],href['sci'],href['err'],href['pixeldq'],href['groupdq']])
    result = pf.diff.FITSDiff(newh,
                              newhref,
                              ignore_keywords = ['DATE','CAL_VER','CAL_VCS','CRDS_VER','CRDS_CTX'],
                              rtol = 0.00001
    )
    result.report()
    try:
        assert result.identical == True
    except AssertionError as e:
        print(result.report())
        raise AssertionError(e)
