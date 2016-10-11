"""test_level3_dithers: Test of spectrographic rules."""
from __future__ import absolute_import
from glob import glob
import pytest
import re

from .helpers import (
    BasePoolRule,
    PoolParams,
    combine_pools,
    generate_params,
    t_path
)

from .. import (AssociationRegistry, generate)
from ..main import constrain_on_candidates


class TestLevel3Spectrographic(BasePoolRule):

    pools = [
        PoolParams(
            path=glob(t_path('data/pool_*_spec_*.csv')),
            n_asns=7,
            n_orphaned=0
        ),
    ]

    valid_rules = [
        'Asn_MIRI_LRS_FIXEDSLIT',
        'Asn_NIR_SO_SLITLESS',
        'Asn_NRS_FIXEDSLIT',
        'Asn_NRS_MSA',
        'Asn_MIRI_IFU',
        'Asn_NRS_IFU'
    ]


@pytest.fixture(
    scope='module',
    params=[
        (
            'o001',
            'spec',
            'jw99009-o001_spec_\d{3}_asn',
            'jw99009-o001_t001_nirspec_f100lp-g140m',
        ),
        (
            'o002',
            'spec',
            'jw99009-o002_spec_\d{3}_asn',
            'jw99009-o002_t003_nirspec_f100lp-g140h',
        ),
        (
            'o003',
            'nrsifu',
            'jw99009-o003_nrsifu_\d{3}_asn',
            'jw99009-o003_t002_nirspec_clear'
        ),
    ]
)
def nirspec_params(request):
    cid, asn_type, asn_name, product_name = request.param
    pool = combine_pools(t_path('data/pool_006_spec_nirspec.csv'))
    gc = {
        'asn_candidate': constrain_on_candidates((cid,))
    }
    rules = AssociationRegistry(global_constraints=gc)
    asns, orphaned = generate(pool, rules)
    return asns, asn_type, asn_name, product_name


def test_nirspec_modes(nirspec_params):
    asns, asn_type, asn_name, product_name = nirspec_params

    assert len(asns) == 1
    asn = asns[0]
    assert asn['asn_type'] == asn_type
    assert re.match(asn_name, asn.asn_name)
    assert asn['products'][0]['name'] == product_name

@pytest.fixture(
    scope='module',
    params=[
        (
            'o005',
            'mirifu',
            'jw99009-o005_mirifu_\d{3}_asn',
            'jw99009-o005_t001_miri',
        ),
        (
            'o006',
            'mirifu',
            'jw99009-o006_mirifu_\d{3}_asn',
            'jw99009-o006_t001_miri',
        ),
        (
            'o007',
            'mirifu',
            'jw99009-o007_mirifu_\d{3}_asn',
            'jw99009-o007_t001_miri'
        ),
        (
            'o008',
            'mirifu',
            'jw99009-o008_mirifu_\d{3}_asn',
            'jw99009-o008_t001_miri'
        ),
        (
            'o009',
            'mirifu',
            'jw99009-o009_mirifu_\d{3}_asn',
            'jw99009-o009_t001_miri'
        ),
    ]
)
def miri_params(request):
    cid, asn_type, asn_name, product_name = request.param
    pool = combine_pools(t_path('data/pool_007_spec_miri.csv'))
    gc = {
        'asn_candidate': constrain_on_candidates((cid,))
    }
    rules = AssociationRegistry(global_constraints=gc)
    asns, orphaned = generate(pool, rules)
    return asns, asn_type, asn_name, product_name


def test_miri_modes(miri_params):
    asns, asn_type, asn_name, product_name = miri_params

    assert len(asns) == 1
    asn = asns[0]
    assert asn['asn_type'] == asn_type
    assert re.match(asn_name, asn.asn_name)
    assert asn['products'][0]['name'] == product_name
