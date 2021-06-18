import pkg_resources
from collections import defaultdict
import mkidpipeline.config as config

_i = defaultdict(lambda: 0)


def _namer(name='Thing'):
    ret = f"{name}{_i[name]}"
    _i[name] = _i[name] + 1
    return ret


SAMPLEDATA = {'default': (
    config.MKIDObservation(name=_namer('HIP109427'), start=1602048875, duration=10, wavecal='wavecal0',
                           dark=config.MKIDTimerange(name=_namer(), start=1602046500, duration=10),
                           flatcal='flatcal0', wcscal='wcscal0', speccal='speccal0'),
    # a wavecal
    config.MKIDWavecalDescription(name=_namer('wavecal'),
                                  data=(config.MKIDTimerange(name='850 nm', start=1602040820, duration=60,
                                                             dark=config.MKIDTimerange(name=_namer(), start=1602046500,
                                                                                       duration=10),
                                                             header=dict(laser='on', other='fits_key')),
                                        config.MKIDTimerange(name='950 nm', start=1602040895, duration=60,
                                                             dark=config.MKIDTimerange(name=_namer(), start=1602046500,
                                                                                       duration=10)),
                                        config.MKIDTimerange(name='1.1 um', start=1602040970, duration=60,
                                                             dark=config.MKIDTimerange(name=_namer(), start=1602046500,
                                                                                       duration=10)),
                                        config.MKIDTimerange(name='1.25 um', start=1602041040, duration=60,
                                                             dark=config.MKIDTimerange(name=_namer(), start=1602046500,
                                                                                       duration=10)),
                                        config.MKIDTimerange(name='13750 AA', start=1602041110, duration=60))
                                  ),
    # Flatcals
    config.MKIDFlatcalDescription(name=_namer('flatcal'),
                                  comment='Open dark that is being used for testing of white light flats while none are'
                                          ' available - should NOT be used as an actual flat calibration!',
                                  data=config.MKIDObservation(name='open_dark', start=1576498833, duration=30.0,
                                                              wavecal='wavecal0')),
    config.MKIDFlatcalDescription(name=_namer('flatcal'), wavecal_duration=50.0, wavecal_offset=2.1, data='wavecal0'),
    # Speccal
    config.MKIDSpeccalDescription(name=_namer('speccal'),
                                  data=config.MKIDObservation(name=_namer('star'), start=1602049166, duration=10,
                                                              wavecal='wavecal0', spectrum='qualified/path/or/relative/'
                                                                                           'todatabase/refspec.file'),
                                  aperture=('15h22m32.3', '30.32 deg', '200 mas')),

    # WCS cal
    config.MKIDWCSCalDescription(name=_namer('wcscal'), pixel_ref=[107, 46], conex_ref=[-0.16, -0.4],
                                 data='10.40 mas'),
    config.MKIDWCSCalDescription(name=_namer('wcscal'),
                                 comment='ob wcscals may be used to manually determine '
                                         'WCS parameters. They are not yet supported for '
                                         'automatic WCS parameter computation',
                                 data=config.MKIDObservation(name=_namer('star'), start=1602047935,
                                                             duration=10, wavecal='wavecal0',
                                                             dark=config.MKIDTimerange(name=_namer(), start=1602046500,
                                                                                       duration=10)),
                                 pixel_ref=(107, 46), conex_ref=(-0.16, -0.4)),
    # Dithers
    config.MKIDDitherDescription(name=_namer('dither'), data=1602047815, wavecal='wavecal0',
                                 header=dict(OBJECT="HIP 109427"),
                                 flatcal='flatcal0', speccal='speccal0', use='0,2,4-9', wcscal='wcscal0'),
    config.MKIDDitherDescription(name=_namer('dither'),
                                 data=pkg_resources.resource_filename('mkidpipeline', 'dither_sample.log'),
                                 wavecal='wavecal0', flatcal='flatcal0', speccal='speccal0', use=(1,),
                                 wcscal='wcscal0'),
    config.MKIDDitherDescription(name=_namer('dither'), flatcal='', speccal='', wcscal='', wavecal='',
                                 header=dict(OBJECT='HIP 109427'),
                                 data=(config.MKIDObservation(name=_namer('HIP109427_'), start=1602047815,
                                                              duration=10, wavecal='wavecal0',
                                                              header=dict(M_CONEXX=.2, M_CONEXY=.3,
                                                                          OBJECT='HIP 109427'),
                                                              dark=config.MKIDTimerange(name=_namer(), start=1602046500,
                                                                                        duration=10)),
                                       config.MKIDObservation(name=_namer('HIP109427_'), start=1602047825, duration=10,
                                                              wavecal='wavecal0', header=dict(M_CONEXX=.1, M_CONEXY=.1),
                                                              wcscal='wcscal0'),
                                       config.MKIDObservation(name=_namer('HIP109427_'), start=1602047835,
                                                              duration=10, wavecal='wavecal0', wcscal='wcscal0',
                                                              header=dict(M_CONEXX=-.1, M_CONEXY=-.1))
                                       )
                                 )
)}


def get_sample_data(dataset='default'):
    return SAMPLEDATA[dataset]


def get_sample_output(dataset='default'):
    data = [config.MKIDOutput(name=_namer('out'), data='dither0', min_wave='850 nm', max_wave='1375 nm', kind='image')]
    return data
