import os
import tables
import time
import numpy as np
import psutil
import multiprocessing as mp
from datetime import datetime
from glob import glob
import warnings
from io import StringIO
from mkidcore.headers import ObsFileCols, ObsHeader
from mkidcore.corelog import getLogger
from mkidcore.config import yaml, yaml_object
import mkidcore.utils
from mkidcore.objects import Beammap

from mkidpipeline.photontable import Photontable
import mkidpipeline.config

_datadircache = {}

PHOTON_BIN_SIZE_BYTES = 8


class StepConfig(mkidpipeline.config.BaseStepConfig):
    yaml_tag = u'!hdf_cfg'
    REQUIRED_KEYS = (('remake', False, 'Remake H5 even if they exist'),
                     ('include_baseline', False, 'Include the baseline in H5 phase/wavelength column'))
    OPTIONAL_KEYS = (('chunkshape', None, 'HDF5 Chunkshape to use'),)  # nb propagates to kwargs of build_pytables


mkidcore.config.yaml.register_class(StepConfig)


def _get_dir_for_start(base, start):
    global _datadircache

    if not base.endswith(os.path.sep):
        base = base + os.path.sep

    try:
        nmin = _datadircache[base]
    except KeyError:
        try:
            nights_times = glob(os.path.join(base, '*', '*.bin'))
            with warnings.catch_warnings():  # ignore warning for nights_times = []
                warnings.simplefilter("ignore", UserWarning)
                nights, times = np.genfromtxt(list(map(lambda s: s[len(base):-4], nights_times)),
                                              delimiter=os.path.sep, dtype=int).T
            nmin = {times[nights == n].min(): str(n) for n in set(nights)}
            _datadircache[base] = nmin
        except ValueError:  # for not pipeline oriented bin file storage
            return base

    keys = np.array(list(nmin))
    try:
        return os.path.join(base, nmin[keys[keys < start].max()])
    except ValueError:
        raise ValueError('No directory in {} found for start {}'.format(base, start))


def estimate_ram_gb(directory, start, inttime):
    files = [os.path.join(directory, '{}.bin'.format(t)) for t in
             range(int(start - 1), int(np.ceil(start) + inttime + 1))]
    files = filter(os.path.exists, files)
    n_max_photons = int(np.ceil(sum([os.stat(f).st_size for f in files]) / PHOTON_BIN_SIZE_BYTES))
    return n_max_photons * PHOTON_BIN_SIZE_BYTES / 1024 ** 3


def build_pytables(cfg, index=('ultralight', 6), timesort=False, chunkshape=250, shuffle=True, bitshuffle=False,
                   wait_for_ram=3600, ndx_shuffle=True, ndx_bitshuffle=False):
    """wait_for_ram speficies the number of seconds to wait for sufficient ram"""
    from mkidcore.hdf.mkidbin import extract
    from mkidpipeline.pipeline import PIPELINE_FLAGS, BEAMMAP_FLAGS    #here to prevent circular imports!

    if cfg.starttime < 1518222559:
        raise ValueError('Data prior to 1518222559 not supported without added fixtimestamps')

    def free_ram_gb():
        mem = psutil.virtual_memory()
        return (mem.free + mem.cached) / 1024 ** 3

    ram_est_gb = estimate_ram_gb(cfg.datadir, cfg.starttime, cfg.inttime) + 2  # add some headroom
    if free_ram_gb() < ram_est_gb:
        msg = 'Insufficint free RAM to build {}, {:.1f} vs. {:.1f} GB.'
        getLogger(__name__).warning(msg.format(cfg.h5file, free_ram_gb(), ram_est_gb))
        if wait_for_ram:
            getLogger(__name__).info('Waiting up to {} s for enough RAM'.format(wait_for_ram))
            while wait_for_ram and free_ram_gb() < ram_est_gb:
                sleeptime = np.random.uniform(1, 2)
                time.sleep(sleeptime)
                wait_for_ram -= sleeptime
                if wait_for_ram % 30:
                    getLogger(__name__).info('Still waiting (up to {} s) for enough RAM'.format(wait_for_ram))
    if free_ram_gb() < ram_est_gb:
        getLogger(__name__).error('Aborting build due to insufficient RAM.')
        return

    getLogger(__name__).debug('Starting build of {}'.format(cfg.h5file))

    photons = extract(cfg.datadir, cfg.starttime, cfg.inttime, cfg.beamfile, cfg.x, cfg.y,
                      include_baseline=cfg.include_baseline)

    getLogger(__name__).debug('Data Extracted for {}'.format(cfg.h5file))

    if timesort:
        photons.sort(order=('Time', 'ResID'))
        getLogger(__name__).warning('Sorting photon data on time for {}'.format(cfg.h5file))
    elif not np.all(photons['ResID'][:-1] <= photons['ResID'][1:]):
        getLogger(__name__).warning('binprocessor.extract returned data that was not sorted on ResID, sorting'
                                    '({})'.format(cfg.h5file))
        photons.sort(order=('ResID', 'Time'))

    h5file = tables.open_file(cfg.h5file, mode="a", title="MKID Photon File")
    group = h5file.create_group("/", 'Photons', 'Photon Information')
    filter = tables.Filters(complevel=1, complib='blosc:lz4', shuffle=shuffle, bitshuffle=bitshuffle, fletcher32=False)
    table = h5file.create_table(group, name='PhotonTable', description=ObsFileCols, title="Photon Datatable",
                                expectedrows=len(photons), filters=filter, chunkshape=chunkshape)
    table.append(photons)

    getLogger(__name__).debug('Table Populated for {}'.format(cfg.h5file))
    if index:
        index_filter = tables.Filters(complevel=1, complib='blosc:lz4', shuffle=ndx_shuffle, bitshuffle=ndx_bitshuffle,
                                      fletcher32=False)

        def indexer(col, index, filter=None):
            if isinstance(index, bool):
                col.create_csindex(filters=filter)
            else:
                col.create_index(optlevel=index[1], kind=index[0], filters=filter)

        indexer(table.cols.Time, index, filter=index_filter)
        getLogger(__name__).debug('Time Indexed for {}'.format(cfg.h5file))
        indexer(table.cols.ResID, index, filter=index_filter)
        getLogger(__name__).debug('ResID Indexed for {}'.format(cfg.h5file))
        indexer(table.cols.Wavelength, index, filter=index_filter)
        getLogger(__name__).debug('Wavelength indexed for {}'.format(cfg.h5file))
        getLogger(__name__).debug('Table indexed ({}) for {}'.format(index, cfg.h5file))
    else:
        getLogger(__name__).debug('Skipping Index Generation for {}'.format(cfg.h5file))

    bmap = Beammap(cfg.beamfile, xydim=(cfg.x, cfg.y))
    group = h5file.create_group("/", 'BeamMap', 'Beammap Information', filters=filter)
    h5file.create_array(group, 'Map', bmap.residmap.astype(int), 'resID map')

    def beammap_flagmap_to_h5_flagmap(flagmap):
        h5map = np.zeros_like(flagmap, dtype=int)
        for i, v in enumerate(flagmap.flat):  # convert each bit to the new bit
            bset = [f'beammap.{f.name}' for f in BEAMMAP_FLAGS.flags.values() if f.bit == int(v)]
            h5map.flat[i] = PIPELINE_FLAGS.bitmask(bset)
        return h5map

    h5file.create_array(group, 'Flag', beammap_flagmap_to_h5_flagmap(bmap.flagmap), 'flag map')
    getLogger(__name__).debug('Beammap Attached to {}'.format(cfg.h5file))

    h5file.create_group('/', 'header', 'Header')
    headerTable = h5file.create_table('/header', 'header', ObsHeader, 'Header')
    headerContents = headerTable.row
    headerContents['isWvlCalibrated'] = False
    headerContents['isFlatCalibrated'] = False
    headerContents['isFluxCalibrated'] = False
    headerContents['isLinearityCorrected'] = False
    headerContents['isPhaseNoiseCorrected'] = False
    headerContents['isPhotonTailCorrected'] = False
    headerContents['timeMaskExists'] = False
    headerContents['startTime'] = cfg.starttime
    headerContents['expTime'] = cfg.inttime
    headerContents['wvlBinStart'] = 700
    headerContents['wvlBinEnd'] = 1500
    headerContents['energyBinWidth'] = 0.1
    headerContents['target'] = ''
    headerContents['dataDir'] = cfg.datadir
    headerContents['beammapFile'] = cfg.beamfile
    headerContents['wvlCalFile'] = ''
    headerContents['fltCalFile'] = ''
    headerContents['metadata'] = ''
    out = StringIO()

    yaml.dump({'flags': PIPELINE_FLAGS.names}, out)
    out = out.getvalue().encode()
    if len(out) > mkidcore.headers.METADATA_BLOCK_BYTES:  # this should match mkidcore.headers.ObsHeader.metadata
        raise ValueError("Too much metadata! {} KB needed, {} allocated".format(len(out) // 1024,
                                                                                mkidcore.headers.METADATA_BLOCK_BYTES // 1024))
    headerContents['metadata'] = out

    headerContents.append()
    getLogger(__name__).debug('Header Attached to {}'.format(cfg.h5file))

    h5file.close()
    getLogger(__name__).debug('Done with {}'.format(cfg.h5file))


@yaml_object(yaml)
class Bin2HdfConfig(object):
    _template = ('{x} {y}\n'
                 '{datadir}\n'
                 '{starttime}\n'
                 '{inttime}\n'
                 '{beamfile}\n'
                 '1\n'
                 '{outdir}\n'
                 '{include_baseline}')

    def __init__(self, datadir='./', starttime=None, inttime=None, outdir='./', include_baseline=False, writeto=None,
                 beammap='MEC'):

        self.datadir = datadir
        self.starttime = int(starttime)
        self.inttime = int(np.ceil(inttime))
        self.include_baseline = include_baseline

        beammap = Beammap(beammap) if isinstance(beammap, str) else beammap
        self.beamfile = beammap.file
        self.x = beammap.ncols
        self.y = beammap.nrows

        self.outdir = outdir
        if writeto is not None:
            self.write(writeto)

    @property
    def h5file(self):
        try:
            return self.user_h5file
        except AttributeError:
            return os.path.join(self.outdir, str(self.starttime) + '.h5')

    def write(self, file):
        dir = self.datadir
        if not glob(os.path.join(dir, '*.bin')):
            dir = os.path.join(self.datadir, datetime.utcfromtimestamp(self.starttime).strftime('%Y%m%d'))
        else:
            getLogger(__name__).debug('bin files found in data directory. Will not append YYYMMDD')

        try:
            file.write(self._template.format(datadir=dir, starttime=self.starttime,
                                             inttime=self.inttime, beamfile=self.beamfile,
                                             outdir=self.outdir, x=self.x, y=self.y))
        except AttributeError:
            with open(file, 'w') as wavefile:
                wavefile.write(self._template.format(datadir=dir, starttime=self.starttime,
                                                     inttime=self.inttime, beamfile=self.beamfile,
                                                     outdir=self.outdir, x=self.x, y=self.y))

    def load(self):
        raise NotImplementedError


class HDFBuilder(object):
    def __init__(self, cfg, force=False, **kwargs):
        self.cfg = cfg
        self.done = False
        self.force = force
        self.kwargs = kwargs

    def handle_existing(self):
        """ Handles existing h5 files, deleting them if appropriate"""
        if os.path.exists(self.cfg.h5file):

            if self.force:
                getLogger(__name__).info('Remaking {} forced'.format(self.cfg.h5file))
                done = False
            else:
                try:
                    done = Photontable(self.cfg.h5file).duration >= self.cfg.inttime
                    if not done:
                        getLogger(__name__).info(('{} does not contain full duration, '
                                                  'will remove and rebuild').format(self.cfg.h5file))
                except:
                    done = False
                    getLogger(__name__).info(('{} presumed corrupt,'
                                              ' will remove and rebuild').format(self.cfg.h5file), exc_info=True)
            if not done:
                try:
                    os.remove(self.cfg.h5file)
                    getLogger(__name__).info('Deleted {}'.format(self.cfg.h5file))
                except FileNotFoundError:
                    pass
            else:
                getLogger(__name__).info('H5 {} already built. Remake not requested. Done.'.format(self.cfg.h5file))
                self.done = True

    def run(self, **kwargs):
        """kwargs is passed on to build_pytables"""
        self.kwargs.update(kwargs)
        self.handle_existing()
        if self.done:
            return

        tic = time.time()
        build_pytables(self.cfg, **self.kwargs)
        self.done = True
        getLogger(__name__).info('Created {} in {:.0f}s'.format(self.cfg.h5file, time.time() - tic))


def gen_configs(timeranges, config=None):
    cfg = mkidpipeline.config.config if config is None else config

    timeranges = list(set(timeranges))

    b2h_configs = []
    for start_t, end_t in timeranges:
        bc = Bin2HdfConfig(datadir=_get_dir_for_start(cfg.paths.data, start_t), beammap=cfg.beammap,
                           outdir=cfg.paths.out, starttime=start_t, inttime=end_t - start_t,
                           include_baseline=cfg.include_baseline)
        b2h_configs.append(bc)

    return b2h_configs


def buildtables(timeranges, config=None, ncpu=None, remake=None, **kwargs):
    """
    timeranges must be an iterable of (start, stop) or an object that hase a .timeranges attribute providing the same
    Pipeline must be configured or a loaded config passed
    ncpu and remake will be pulled from config if not specified
    kwargs my be used to pass settings on to pytables
    """
    try:
        timeranges = timeranges.timeranges
    except AttributeError:
        pass

    timeranges = list(set(timeranges))

    cfg = mkidpipeline.config.config if config is None else config
    if cfg is None:
        raise RuntimeError('Pipeline not configured')
    b2h_configs = []
    for start_t, end_t in timeranges:
        bc = Bin2HdfConfig(datadir=_get_dir_for_start(cfg.paths.data, start_t), beammap=cfg.beammap,
                           outdir=cfg.paths.out, starttime=start_t, inttime=end_t - start_t,
                           include_baseline=cfg.hdf.include_baseline)
        b2h_configs.append(bc)

    remake = mkidpipeline.config.config.hdf.get('remake', False) if remake is None else remake
    ncpu = mkidpipeline.config.config.hdf.get('ncpu', 1) if ncpu is None else ncpu
    for k in mkidpipeline.config.config.hdf.keys():
        if k not in kwargs and k not in ('ncpu', 'remake', 'include_baseline'):
            kwargs[k] = mkidpipeline.config.config.hdf.get(k)

    builders = [HDFBuilder(c, force=remake, **kwargs) for c in b2h_configs]

    if ncpu == 1:
        for b in builders:
            try:
                b.run(**kwargs)
            except MemoryError:
                getLogger(__name__).error('Insufficient memory to process {}'.format(b.h5file))
        return timeranges

    def runbuilder(b):
        getLogger(__name__).debug('Calling run on {}'.format(b.cfg.h5file))
        try:
            b.run()
        except Exception as e:
            getLogger(__name__).critical('Caught exception during run of {}'.format(b.cfg.h5file), exc_info=True)

    pool = mp.Pool(mkidpipeline.config.n_cpus_available(ncpu))
    pool.map(runbuilder, builders)
    pool.close()
    pool.join()
