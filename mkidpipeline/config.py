import astropy.units.core
import numpy as np
import os
from glob import glob
import hashlib
from datetime import datetime
import multiprocessing as mp
import pkg_resources as pkg
import json
import astropy.units as u
from astropy.coordinates import SkyCoord
import ruamel.yaml.comments
from collections import defaultdict

from typing import Set

from mkidcore.utils import parse_ditherlog
from mkidcore.legacy import parse_dither_log
import mkidcore.config
from mkidcore.corelog import getLogger, create_log, MakeFileHandler
from mkidcore.utils import getnm, derangify
from mkidcore.objects import Beammap
from mkidcore.instruments import InstrumentInfo

# Ensure that the beammap gets registered with yaml, the import does this
# but without this note an IDE or human might remove the import
Beammap()

config = None
_dataset = None
_parsed_dither_logs = {}
_metadata = {}

yaml = mkidcore.config.yaml

STANDARD_KEYS = (
    'ra', 'dec', 'airmass', 'az', 'el', 'ha', 'equinox', 'parallactic', 'target', 'utctcs', 'laser', 'flipper',
    'filter', 'observatory', 'utc', 'comment', 'device_orientation', 'instrument', 'dither_ref', 'dither_home',
    'dither_pos', 'platescale')

REQUIRED_KEYS = ('ra', 'dec', 'target', 'observatory', 'instrument', 'dither_ref', 'dither_home', 'platescale',
                 'device_orientation')


def get_ditherinfo(time, path=None):
    if path is None:
        path = config.paths.dithers
    global _parsed_dither_logs
    if not _parsed_dither_logs:
        for f in glob(os.path.join(path, 'dither_*.log')):
            parsed_log = parse_ditherlog(f)
            _parsed_dither_logs.update(parsed_log)

    if isinstance(time, datetime):
        time = time.timestamp()

    for (t0, t1), v in _parsed_dither_logs.items():
        if t0 - (t1 - t0) <= time <= t1:
            return v
    raise ValueError('No dither found for time {}'.format(time))


def dump_dataconfig(data, file):
    with open(file, 'w') as f:
        mkidcore.config.yaml.dump(data, f)
    # patch bug in yaml export
    with open(file, 'r') as f:
        lines = f.readlines()
    for l in (l for l in lines if ' - !' in l and ':' in l):
        x = list(l.partition(l.partition(':')[0].split()[-1] + ':'))
        x.insert(1, '\n' + ' ' * l.index('!'))
        lines[lines.index(l)] = ''.join(x)
    with open(file, 'w') as f:
        f.writelines(lines)


# Note that in contrast to the Keys or DataBase these don't work quite the same way
# required keys specify items that the resulting object is required to have, not that use
# user is required to pass, they are
class BaseStepConfig(mkidcore.config.ConfigThing):
    REQUIRED_KEYS = tuple()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k, v, c in self.REQUIRED_KEYS:
            self.register(k, v, comment=c, update=False)

    @classmethod
    def from_yaml(cls, loader, node):
        ret = super().from_yaml(loader, node)
        errors = ret._verify_attribues() + ret._vet_errors()

        if errors:
            raise ValueError(f'{ret.yaml_tag} collected errors: \n' + '\n\t'.join(errors))
        return ret

    def _verify_attribues(self):
        missing = [key for key, default, comment in self.REQUIRED_KEYS if key not in self]
        return ['Missing required keys: ' + ', '.join(missing)] if missing else []

    def _vet_errors(self):
        return []


class PipeConfig(BaseStepConfig):
    yaml_tag = u'!pipe_cfg'
    REQUIRED_KEYS = (('ncpu', 1, 'number of cpus'),
                     ('verbosity', 0, 'level of verbosity'),
                     ('flow', ('metadata', 'wavecal', 'lincal', 'flatcal', 'cosmiccal', 'photcal'),
                      'Calibration steps to apply'),
                     ('paths.dithers', '/darkdata/MEC/logs/', 'dither log location'),
                     ('paths.data', '/darkdata/ScienceData/Subaru/', 'bin file parent folder'),
                     ('paths.database', '/work/temp/database/', 'calibrations will be retrieved/stored here'),
                     ('paths.obslog', '/work/temp/database/obslog', 'obslog.json go here'),
                     ('paths.out', '/work/temp/out/', 'root of output'),
                     ('paths.tmp', '/work/temp/scratch/', 'use for data intensive temp files'),
                     ('beammap', None, 'A Beammap to use'),
                     ('instrument', None, 'An mkidcore.instruments.InstrumentInfo instance')
                     )

    def __init__(self, *args, defaults: dict = None, instrument='MEC', **kwargs):
        super().__init__(*args, **kwargs)
        self.register('beammap', Beammap(specifier=instrument), update=True)
        self.register('instrument', InstrumentInfo(instrument), update=True)
        if defaults is not None:
            for k, v in defaults.items():
                self.register(k, v, update=True)


mkidcore.config.yaml.register_class(PipeConfig)


def PipelineConfigFactory(step_defaults: dict = None, cfg=None, ncpu=None, copy=True):
    """
    Return a pipeline config with the specified step.
    cfg will take precedence over an existing pipeline config
    ncpu will take precedence (at the root level only so if a step has defaults those will control for the step!)
    the step defaults will only be used if the step is not configured
    if copy is set is returned such that it is safe to edit, if not set any defaults will be updated
    into cfg (if passed) or the global config (if extant)
    """
    global config
    if cfg is None:
        cfg = PipeConfig(instrument='MEC') if config is None else config
    if copy:
        cfg = cfg.copy()
    if step_defaults:
        for name, defaults in step_defaults.items():
            cfg.register(name, defaults, update=False)
    if ncpu is not None:
        config.update('ncpu', ncpu)
    return cfg


def configure_pipeline(pipeline_config):
    """ Load a pipeline config, configuring the pipeline. Any existing configuration will be replaced"""
    global config
    config = mkidcore.config.load(pipeline_config, namespace=None)
    return config


def update_paths(d):
    global config
    for k, v in d.items():
        config.update(f'paths.{k}', v)


def make_paths(config=None, output_collection=None):
    if config is None:
        config = globals()['config']

    output_dirs = [] if output_collection is None else [os.path.dirname(o.output_file) for o in output_collection]
    paths = set([config.paths.out, config.paths.database, config.paths.tmp] + list(output_dirs))

    for p in filter(os.path.exists, paths):
        getLogger(__name__).info(f'"{p}" exists, and will be used.')

    for p in filter(lambda p: not os.path.exists(p), paths):
        if not p:
            continue
        getLogger(__name__).info(f'Creating "{p}"')
        os.makedirs(p, exist_ok=True)


class H5Subset:
    def __init__(self, timerange, duration=None, start=None, relative=False):
        """if relative the start is taken as an offset relative to the timerange"""
        self.timerange = timerange
        self.h5start = int(timerange.start)
        if relative and start is not None:
            start = float(start) + float(self.h5start)
        self.start = float(self.h5start) if start is None else float(start)
        self.duration = timerange.duration if duration is None else float(duration)

    @property
    def photontable(self):
        from photontable import Photontable
        return Photontable(self.timerange.h5)

    @property
    def first_second(self):
        return self.start - self.h5start

    def __str__(self):
        return f'{os.path.basename(self.timerange.h5)} @ {self.start} for {self.duration}s'


class Key:
    def __init__(self, name='', default=None, comment='', dtype=None):
        self.name = str(name)
        self.default = default
        self.comment = str(comment)
        self.dtype = dtype


class DataBase:
    KEYS = tuple()
    REQUIRED = tuple()  # May set individual elements to tuples of keys if they are alternates e.g. stop/duration
    EXPLICIT_ALLOW = tuple()  # Set to names that are allowed keys and are also used as properties

    def __init__(self, *args, **kwargs):
        from collections import defaultdict
        self._key_errors = defaultdict(list)
        self._keys = {k.name: k for k in self.KEYS}
        self.extra_keys = []

        # Check disallowed
        for k in kwargs:
            if getattr(self, k, None) is not None and k not in self.EXPLICIT_ALLOW or k.startswith('_'):
                self._key_errors[k] += ['Not an allowed key']

        self.name = kwargs.get('name', f'Unnamed !{self.yaml_tag}')  # yaml_tag defined by subclass
        self.extra_keys = [k for k in kwargs if k not in self.key_names]

        # Check for the existence of all required keys (or key sets)
        for key_set in self.REQUIRED:
            if isinstance(key_set, str):
                key_set = (key_set,)
            found = 0
            for k in key_set:
                found += int(k in kwargs)
            if len(key_set) == 1:
                key_set = key_set[0]
            if not found:
                self._key_errors[key_set] += ['missing']
            elif found > 1:
                if not found:
                    self._key_errors[key_set] += ['multiple specified']

        # Process keys
        for k, v in kwargs.items():
            if k in self._keys:
                required_type = self._keys[k].dtype
                if required_type == tuple and isinstance(v, list):
                    v = tuple(v)
                if required_type == float and isinstance(v, str) and v.endswith('inf'):
                    try:
                        v = float(v)
                    except ValueError:
                        pass
                if required_type is not None and not isinstance(v, required_type):
                    self._key_errors[k] += [f'not an instance of {required_type}']

            if isinstance(v, str):
                try:
                    v = u.Quantity(v)
                except (TypeError, ValueError):
                    if v.startswith('_'):
                        raise ValueError(f'Keys may not start with an underscore: "{v}". Check {self.name}')
            try:
                setattr(self, k, v)
            except AttributeError:
                try:
                    setattr(self, '_' + k, v)
                    getLogger(__name__).debug(f'Storing {k} as _{k} for use by subclass')
                except AttributeError:
                    pass

        # Set defaults
        for key in (key for key in self.KEYS if key.name not in kwargs and key.name not in self.EXPLICIT_ALLOW):
            try:
                if key.default is None and key.dtype is not None:
                    default = key.dtype[0]() if isinstance(key.dtype, tuple) else key.dtype()
                else:
                    default = key.default
            except Exception:
                default = None
                getLogger(__name__).debug(f'Unable to create default instance of {key.dtype} for '
                                          f'{key.name}, using None')
            try:
                setattr(self, key.name, default)
            except Exception:
                getLogger(__name__).debug(f'Key {key.name} is shadowed by property, prepending _')
                setattr(self, '_' + key.name, default)

        # # Check types
        # for k:
        #     if key.dtype is not None:
        #         try:
        #             if not isinstance(getattr(self, key.name), key.dtype):
        #                 self._key_errors[key.name] += [f'not an instance of {key.dtype}']
        #         except AttributeError:
        #             pass

    def _vet(self):
        def joiner(x):
            return ', '.join(x)

        errors = [f'{k}:{joiner(v)}' for k, v in self._key_errors.items()]
        return f"{type(self).__name__}: {errors}" if errors else ''

    def extra(self):
        return {k: getattr(self, k) for k in self.extra_keys}

    @classmethod
    def from_yaml(cls, loader, node):
        return cls(**dict(loader.construct_pairs(node, deep=True)))

    @classmethod
    def to_yaml(cls, representer, node):
        d = node.__dict__.copy()

        # We want to write out all the keys needed to recreate the definition
        #  keys that are explicitly allowed are used in __init__ to support dual definition (e.g. stop/duration)
        #  we exclude th to prevent redundancy
        #  we want to include any user defined keys
        keys = [k for k in node._keys if k not in cls.EXPLICIT_ALLOW] + d.pop('extra_keys')
        store = {}
        for k in keys:
            if type(d[k]) not in representer.yaml_representers:
                getLogger(__name__).debug(f'{node.name} ({cls.__name__}.{k}) is a {type(d[k])} and '
                                          f'will be cast to string ({str(d[k])}) for yaml representation ')
                store[k] = str(d[k])
            else:
                # getLogger(__name__).debug(f'{node.name} ({cls.__name__}.{k}) is a {type(d[k])} and '
                #                           f'will be stored as ({d[k]}) for yaml representation ')
                store[k] = d[k]
        cm = ruamel.yaml.comments.CommentedMap(store)
        for k in store:
            cm.yaml_add_eol_comment(node._keys[k].comment if k in node._keys else 'User added key', key=k)
        return representer.represent_mapping(cls.yaml_tag, cm)

    @property
    def key_names(self):
        return tuple([k.name for k in self.KEYS])


class MKIDTimerange(DataBase):
    yaml_tag = u'!MKIDTimerange'
    KEYS = (
        Key(name='name', default=None, comment='A name', dtype=str),
        Key('start', None, 'The start unix time, float ok, rounded down for H5 creation.', (float, int)),
        Key('duration', None, 'A duration in seconds, float ok. If not specified stop must be', (float, int)),
        Key('stop', None, 'A stop unit time, float ok. If not specified duration must be', (float, int)),
        Key('dark', None, 'An MKIDTimerange to use for a dark reference.', None)
    )
    REQUIRED = ('name', 'start', ('duration', 'stop'))
    EXPLICIT_ALLOW = ('duration',)  # if a key is allows AND is a property or method name it must be listed here

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self, '_duration'):
            self.stop = self.start + self._duration

    def __str__(self):
        return f'{self.name} ({type(self).__name__}): {self.duration}s @ {self.start}'

    def __hash__(self):
        """Note that this mease that a set of MKIDTimeranges where one has a dark and the other does makes no
        promises about which you get, DO NOT add to this to include self.dark without factoring in that sets of
        timeranges would now contain members with identical (start, stop) (i.e. much of the pipeline execution path"""
        return hash((self.start, self.stop))

    def _vet(self):
        if self.duration > 43200:
            getLogger(__name__).warning(f'Duration of {self.name} longer than 12h!')
        if self.stop < self.start:
            self._key_errors['stop'] += [f'Stop ({self.stop}) must come after start ({self.start})']
        return super()._vet()

    def _metadata(self):
        """ Return a dict of the metadata unique to self"""
        d = dict(start=self.start, stop=self.stop,
                 dark=f'{self.dark.duration}@{self.dark.start}' if self.dark else 'None')
        d.update({k: getattr(self, k) for k in self.extra_keys})
        return d

    @property
    def date(self):
        return datetime.utcfromtimestamp(self.start)

    @property
    def beammap(self):
        return config.beammap

    @property
    def duration(self):
        return self.stop - self.start

    @property
    def timerange(self):
        return self

    @property
    def input_timeranges(self):
        yield self.timerange
        if self.dark is not None:
            yield self.dark.timerange

    @property
    def h5(self):
        return os.path.join(config.paths.out, '{}.h5'.format(int(self.start)))

    @property
    def photontable(self):
        """Convenience method for a photontable, file must exist, creates a new photon table on every call"""
        from mkidpipeline.photontable import Photontable
        return Photontable(self.h5)

    @property
    def metadata(self):
        mdl = observing_metadata_for_timerange(self)

        if not mdl:
            mdl = [mkidcore.config.ConfigThing()]
        bad = False
        for md in mdl:
            md.registerfromkvlist(self._metadata.items(), namespace='')
            bad |= validate_metadata(md, warn=True, error=False)
        if bad:
            raise RuntimeError("Did not specify all the necessary metadata")
        return mdl


class MKIDObservation(MKIDTimerange):
    """requires keys name, wavecal, flatcal, wcscal, and all the things from ob"""
    yaml_tag = u'!MKIDObservation'
    KEYS = MKIDTimerange.KEYS + (
        Key('wavecal', '', 'A MKIDWavedata or name of the same', None),
        Key('flatcal', '', 'A MKIDFlatdata or name of the same', None),
        Key('wcscal', '', 'A MKIDWCSCal or name of the same', None),
        Key('speccal', '', 'A MKIDSpecdata or name of the same', None),
    )
    REQUIRED = MKIDTimerange.REQUIRED + ('wavecal', 'flatcal', 'wcscal', 'speccal')
    EXPLICIT_ALLOW = MKIDTimerange.EXPLICIT_ALLOW

    # OPTIONAL = ('standard', 'conex_pos')

    @property
    def _metadata(self):
        d = super()._metadata
        try:
            wc = self.wavecal.id
        except AttributeError:
            wc = 'None'
        try:
            fc = self.flatcal.id
        except AttributeError:
            fc = 'None'
        try:
            sc = self.speccal.id
        except AttributeError:
            sc = 'None'
        try:
            wcsd = dict(platescale=self.wcscal.platescale, dither_ref=self.wcscal.dither_ref,
                        dither_home=self.wcscal.dither_home, device_orientation=self.wcscal.device_orientation)
        except AttributeError:
            wcsd = {}

        d2 = dict(wavecal=wc, flatcal=fc, speccal=sc)
        d.update(d2)
        d.update(wcsd)
        return d

    @property
    def obs(self):
        yield self

    @property
    def input_timeranges(self):
        """Return all of the MKIDTimeranges(NB this, by definition includes subclasses) go in to making the obs"""
        for tr in self.input_timeranges:
            yield tr
        if self.wavecal is not None:
            for tr in self.wavecal.input_timeranges:
                yield tr
        if self.flatcal is not None:
            for tr in self.flatcal.input_timeranges:
                yield tr
        if self.wcscal is not None:
            for tr in self.wcscal.input_timeranges:
                yield tr
        if self.speccal is not None:
            for tr in self.speccal.input_timeranges:
                yield tr

    def associate(self, **kwargs):
        """ Call with dicts for wavecal, flatcal, speccal, and wcscal"""
        for k in ('wavecal', 'flatcal', 'speccal', 'wcscal'):
            if k not in kwargs:
                continue
            if isinstance(getattr(self, k), str):
                setattr(self, f'_{k}', getattr(self, k))
                setattr(self, k, kwargs.get(k, getattr(self, k)))


class CalDefinitionMixin:
    @property
    def path(self):
        return os.path.join(config.paths.database, self.id + '.npz')

    @property
    def timeranges(self):
        for o in self.obs:
            yield o.timerange

    @property
    def input_timeranges(self):
        try:
            x = self.obs
        except AttributeError:
            x = self.data
        for o in x:
            for tr in o.input_timeranges:
                yield tr

    def id(self, cfg=None):
        """
        Compute a wavecal id string from a wavedata id string and either the active or a specified wavecal config
        """
        id = str(self) + '_' + hashlib.md5(str(self).encode()).hexdigest()[-8:]
        if cfg is None:
            global config
            cfg = config.get(self.STEPNAME)
        config_hash = hashlib.md5(str(cfg).encode()).hexdigest()
        return f'{self.STEPNAME}_{id}_{config_hash[-8:]}'


class MKIDWavecalDescription(DataBase, CalDefinitionMixin):
    """requires keys name and data"""
    yaml_tag = u'!MKIDWavecalDescription'
    KEYS = (
        Key(name='name', default='', comment='A name', dtype=str),
        Key('data', None, 'List of MKIDTimerange named like 950 nm', tuple),
    )
    REQUIRED = ('name', 'data')
    STEPNAME = 'wavecal'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for d in self.data:
            if not isinstance(d, MKIDTimerange):
                self._key_errors['data'] += [f'Element {d} of data is not an MKIDTimerange']
        if not self.obs:
            self._key_errors['data'] += ['obs must be a list of MKIDTimerange']

    def __str__(self):
        start = min(x.start for x in self.data)
        stop = min(x.stop for x in self.data)
        date = datetime.utcfromtimestamp(start).strftime('%Y-%m-%d-%H%M_')
        return f'{self.name} (MKIDWavecalDescription): {start}-{stop}\n' + '\n '.join(str(x) for x in self.obs)

    @property
    def wavelengths(self):
        return tuple([getnm(x.name) for x in self.obs])

    @property
    def darks(self):
        return {w: ob.dark for w, ob in zip(self.wavelengths, self.obs)}


class MKIDFlatcalDescription(DataBase, CalDefinitionMixin):
    yaml_tag = u'!MKIDFlatcalDescription'
    KEYS = (
        Key(name='name', default=None, comment='A name', dtype=str),
        Key('data', None, 'An MKIDObservation (for a whitelight flat) or an MKIDWavedata '
                          '(or name) for a lasercal flat', None),
        Key('wavecal_duration', None, 'Number of seconds of the wavecal to use, float ok. '
                                      'Required if using wavecal', float),
        Key('wavecal_offset', None, 'An offset in seconds (>=1) from the start of the wavecal '
                                    'timerange. Required if not ob', int),
        Key('lincal', False, 'Apply lincal to h5s ', bool),
        Key('pixcal', True, 'Apply pixcal to data ', bool),
        Key('cosmiccal', False, 'Apply cosmiccal to data ', bool)
    )
    REQUIRED = ('name', 'data',)
    STEPNAME = 'flatcal'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.data, (MKIDWavecalDescription, str)):
            try:
                if self.wavecal_offset < 1:
                    self._key_errors['wavecal_offset'] += ['must be >= 1s']
            except AttributeError:
                self._key_errors['wavecal_offset'] += ['required for a wavecal flat (i.e. no ob specified)']
            except TypeError:
                pass  # covered by super init
            try:
                if self.wavecal_duration < 1:
                    self._key_errors['wavecal_duration'] += ['must be >= 1s']
            except AttributeError:
                self._key_errors['wavecal_duration'] += ['required for a wavecal flat (i.e. no ob specified)']
            except TypeError:
                pass  # covered by super init

        else:
            if not isinstance(self.data, MKIDObservation):
                self._key_errors['data'] += [
                    'must be an MKIDObservation, MKIDWavecalDescription, or name of the latter']
            if not isinstance(getattr(self.data, 'wavecal', None), (MKIDWavecalDescription, str)):
                self._key_errors['data'] += ['data must specify a wavecal when an MKIDObservation']
            self.wavecal_offset = None
            self.wavecal_duration = None

    def __str__(self):
        return '{}: {}'.format(self.name, self.data.start if self.method != 'laser' else self.data.id)

    @property
    def method(self):
        return 'white' if isinstance(self.data, MKIDObservation) else 'laser'

    @property
    def h5s(self):
        """Returns MKIDObservations for the wavelengths of the wavecal, will raise errors for whitelight flats"""
        if self.method != 'laser':
            raise NotImplementedError('h5s only available for laser flats')
        return {w: ob for w, ob in zip(self.data.wavelengths, self.obs)}

    @property
    def obs(self):
        if isinstance(self.data, MKIDObservation):
            yield self.data
        else:
            if isinstance(self.data, str):
                raise RuntimeError(f'Must associate wavecal {self.data} prior to calling')
            for tr in self.data.input_timeranges:
                o = MKIDObservation(f'{self.name}_{tr.name}', tr.start + self.wavecal_offset,
                                    duration=min(self.wavecal_duration, tr.duration - self.wavecal_offset),
                                    dark=tr.dark, wavecal=self.data, **tr.extra())
                yield o

    def associate(self, **kwargs):
        if isinstance(self.data, str):
            self.data = kwargs['wavecal'].get(self.data, self.data)
        else:
            self.data.associate(**kwargs)


class MKIDSpeccalDescription(DataBase, CalDefinitionMixin):
    yaml_tag = u'!MKIDSpeccalDescription'
    KEYS = (
        Key(name='name', default=None, comment='A name', dtype=str),
        Key('data', None, 'MKIDObservation or MKIDDither', None),
        Key('aperture', 'satellite', 'A 3-tuple (x/RA, y/Dec, r) or "satellite"', None),
    )
    REQUIRED = ('name', 'data', 'aperture')
    STEPNAME = 'speccal'

    def __init__(self, *args, **kwargs):
        self.aperture_info = None
        super().__init__(*args, **kwargs)
        if not isinstance(self.data, (MKIDObservation, MKIDDitherDescription, str)):
            self._key_errors['data'] += ['Much be an MKIDObservation, an MKIDDitherDescription, or name of the latter']
        if isinstance(self.aperture, str):
            if self.aperture != 'satellite':
                self._key_errors['aperture'] += ['satellite is the only acceptable string']
        else:
            try:
                if len(self.aperture) != 3:
                    raise IndexError
                try:
                    self._aperture_info = tuple(map(float, self.aperture))
                except ValueError:
                    self._aperture_info = (SkyCoord(self.aperture[0], self.aperture[1]), u.Quantity(self.aperture[2]))
            except (TypeError, ValueError, IndexError) as e:
                getLogger(__name__).debug(f'Conversion of {self.aperture} failed: {e}')
                self._key_errors['aperture'] += ['3-tuple must in the form of (x/RA, y/Dec, radius) and '
                                                 'be parsable by float or SkyCoord+Quantity']

    @property
    def obs(self):
        if isinstance(self.data, MKIDObservation):
            yield self.data
        else:
            for o in self.data.obs:
                yield o

    def associate(self, **kwargs):
        if isinstance(self.data, str):
            self.data = kwargs['dither'].get(self.data, self.data)
        else:
            self.data.associate(**kwargs)


class MKIDWCSCalDescription(DataBase, CalDefinitionMixin):
    """
    The MKIDWCSCalDescription defines the coordinate relation between

    Keys are
    name - required

    Either:
    data - The name of nn MKIDObservation from which to extract platescale dirter_ref, and dither_home.
        Presently unsupported
    Or   (the platescale in mas, though note that TODO is the authoratative def. on units)
    dither_ref - 2 tuple (dither controller position for dither_hope)
    dither_home - 2 tuple (pixel position of optical axis at dither_ref)
    """
    yaml_tag = '!MKIDWCSCalDescription'
    KEYS = (
        Key(name='name', default=None, comment='A name', dtype=str),
        Key('data', None, 'MKIDObservation, MKIDDither, or platescale (e.g. 10 mas)', None),
        Key('dither_home', None, 'The pixel position of the target centroid when on '
                                 'axis and the conex is at dither_home', tuple),
        Key('dither_ref', None, 'The conex (x,y) position, [0, 1.0], when the target is at dither_ref ', tuple),
    )
    REQUIRED = ('name', 'data',)
    STEPNAME = 'wcscal'

    def __init__(self, *args, **kwargs):
        super(MKIDWCSCalDescription, self).__init__(*args, **kwargs)

        if isinstance(self.data, u.Quantity):
            try:
                self.self.data.to('arcsec')
            except Exception:
                self._key_errors['platescale'] += ['must be a valid angular unit e.g. "10 mas"']
        elif isinstance(self.data, (MKIDObservation, MKIDDitherDescription)):
            if self.dither_home is None:
                self._key_errors['dither_ref'] += ['must be an (x,y) position for the central source at dither_home']
            if self.dither_ref is None:
                self._key_errors['dither_home'] += ['must be a conex (x,y) position when the target is at dither_ref']
        else:
            self._key_errors['data'] += ['MKIDObservation, MKIDDither, or platescale (e.g. 10 mas)']

        if self.dither_ref is not None:
            try:
                assert (len(self.dither_ref) == 2 and
                        0 <= self.dither_ref[0] < 1.0 and
                        0 <= self.dither_ref[1] < 1.0)
            except Exception:
                self._key_errors['dither_ref'] += ['must be a valid conex position (x,y), x & y in [0,1.0]']

        if self.dither_home is not None:
            try:
                assert len(self.dither_home) == 2
                if config is None or config.beammap is None:
                    getLogger(__name__).debug(f'Beammap not configured not checking dither_home validity')
                else:
                    assert (0 <= self.dither_home[0] < config.beammap.ncols and
                            0 <= self.dither_home[1] < config.beammap.nrows)
            except (TypeError, AssertionError):
                getLogger(__name__).debug(f'Dither home {self.dither_home} not in beammap '
                                          f'domain {config.beammap.ncols},{config.beammap.nrows}')
                self._key_errors['dither_home'] += ['must be a valid pixel (x,y) position']

    @property
    def platescale(self):
        if not isinstance(self.data, u.Quantity):
            raise NotImplementedError('WCSCal not created with a defined platescale')
        return self.data.to('arcsec')

    @property
    def obs(self):
        if isinstance(self.data, u.Quantity):
            return
            yield
        else:
            for o in self.data.obs:
                yield self.data

    def associate(self, **kwargs):
        if isinstance(self.data, str):
            self.data = kwargs['dither'].get(self.data, self.data)
        elif isinstance(self.data, (MKIDObservation, MKIDDitherDescription)):
            self.data.associate(**kwargs)


class MKIDDitherDescription(DataBase):
    yaml_tag = '!MKIDDitherDescription'
    KEYS = (
        Key(name='name', default=None, comment='A name', dtype=str),
        Key('data', tuple(), 'A list of !sob composing the dither, a unix time that falls within the range of a '
                             'dither in a dither log in paths.dithers, or a legacy (starttimes, endtimes, xpos,ypos) '
                             'dither file name (relative to paths.dithers or fully qualified)', None),
        Key('wavecal', '', 'A MKIDWavedata or name of the same', str),
        Key('flatcal', '', 'A MKIDFlatdata or name of the same', str),
        Key('wcscal', '', 'A MKIDWCSCal or name of the same', str),
        Key('speccal', '', 'A MKIDSpecdata or name of the same', str),
        Key('use', None, 'Specify which dither obs to use, list or range specification string e.g. #,#-#,#,#', None),
    )
    REQUIRED = ('name', 'data', 'wavecal', 'flatcal', 'wcscal')
    STEPNAME = 'dither'

    def __init__(self, *args, **kwargs):
        """
        Obs, byLegacy, or byTimestamp must be specified. byTimestamp is normal.

        Obs must be a list of MKIDObservations
        byLegacyFile must be a legacy dither log file (starttimes, endtimes, xpos,ypos)
        byTimestamp mut be a timestamp or a datetime that falls in the range of a dither in a ditherlog on the path
        obs>byTimestamp>byLegacyFile
        """
        self.obs = None
        super().__init__(*args, **kwargs)

        try:
            dither_path = config.paths.dithers
        except AttributeError:
            dither_path = ''
            getLogger(__name__).warning('Pipeline config.paths.dithers not configured')

        def check_use(maxn):
            if self.use is None:
                self.use = list(range(maxn))
            else:
                try:
                    rspec = self.use
                    self.use = [self.use] if isinstance(self.use, int) else derangify(self.use)
                except Exception:
                    self.use = list(range(maxn))
                    self._key_errors['use'] += [f'Failed to derangify {rspec}, using all positions']
            if self.use and (min(self.use) < 0 or max(self.use) >= maxn):
                self._key_errors['use'] += [f'Values must be in [0, {maxn}]']
                getLogger(__name__).info('Clearing use due to illegal/out-of-range values.')
                self.use = list(range(maxn))

        try:
            if isinstance(self.data, str):  # by old file
                file = self.data
                if not os.path.isfile(file):
                    getLogger(__name__).info(f'Treating {file} as relative dither path.')
                    file = os.path.join(dither_path, file)

                try:
                    startt, endt, pos, inttime = parse_dither_log(file)
                except Exception as e:
                    self._key_errors['data'] += [f'Unable to load legacy dither {file}: {e}']
                    endt, startt, pos = [], [], []

            elif isinstance(self.data, (int, float)):  # by timestamp
                getLogger(__name__).info(f'Searching for dither containing time {self.data}')
                try:
                    startt, endt, pos = get_ditherinfo(self.data, path=dither_path)
                    getLogger(__name__).info(f'Dither associated ')
                except ValueError:
                    self._key_errors['data'] += [f'Unable to find a dither at time {self.data}']
                    getLogger(__name__).warning(f'No dither found for {self.name} @ {self.data} '
                                                f'in {dither_path}')
                    endt, startt, pos = [], [], []
            else:
                check_use(len(self.data))
                self.obs = [self.data[i] for i in self.use]

                for o in self.obs:
                    try:
                        assert len(o.dither_pos) == 2 and 0 <= o.dither_pos[0] <= 1 and 0 <= o.dither_pos[0] <= 1
                    except Exception:
                        self._key_errors['data'] += [f'{o} does not specify a dither_pos for the conex (x,y) [0,1]']
                return

            check_use(len(startt))

            startt = [startt[i] for i in self.use]
            endt = [endt[i] for i in self.use]
            pos = [pos[i] for i in self.use]

            self.obs = [MKIDObservation(f'{self.name}_{i}/{len(self.obs)}', b, stop=e, dither_pos=p,
                                        wavecal=self.wavecal, flatcal=self.flatcal, wcscal=self.wcscal,
                                        speccal=self.speccal, **self.extra())
                        for i, b, e, p in zip(self.use, startt, endt, pos)]
        except:
            getLogger(__name__).critical('During creation of dither definition: ', exc_info=True)
            pass

    def associate(self, **kwargs):
        for k in ('wavecal', 'flatcal', 'speccal', 'wcscal'):
            if k not in kwargs:
                continue
            if isinstance(getattr(self, k), str):
                setattr(self, f'_{k}', getattr(self, k))  # store the name
                setattr(self, k, kwargs.get(k, getattr(self, k)))  # pull the object from the kwargs if preset
        for o in self.obs:
            o.associate(kwargs)

    @property
    def inttime(self):
        return [o.duration for o in self.obs]

    @property
    def pos(self):
        return [o.dither_pos for o in self.obs]

    @property
    def timeranges(self):
        for o in self.obs:
            for tr in o.timeranges:
                yield tr

    @property
    def input_timeranges(self):
        for o in self.obs:
            for tr in o.input_timeranges:
                yield tr


class MKIDObservingDataset:
    def __init__(self, yml):
        self.yml = yml
        self.meta = mkidcore.config.load(yml)
        names = [d.name for d in self.meta]
        if len(names) != len(set(names)):
            msg = 'Duplicate names not allowed in {}.'.format(yml)
            getLogger(__name__).critical(msg)
            raise ValueError(msg)

        wcdict = {w.name: w for w in self.wavecals}
        fcdict = {f.name: f for f in self.flatcals}
        wcsdict = {w.name: w for w in self.wcscals}
        scdict = {s.name: s for s in self.speccals}
        dithdict = {d.name: d for d in self.dithers}

        missing = defaultdict(lambda: set())

        for f in self.flatcals:
            f.associate(wavecal=wcdict, flatcal=fcdict, wcscal=wcsdict, speccal=scdict, dither=dithdict)

        for f in self.wcscals:
            f.associate(wavecal=wcdict, flatcal=fcdict, wcscal=wcsdict, speccal=scdict, dither=dithdict)

        for f in self.speccals:
            f.associate(wavecal=wcdict, flatcal=fcdict, wcscal=wcsdict, speccal=scdict, dither=dithdict)

        for f in self.dithers:
            f.associate(wavecal=wcdict, flatcal=fcdict, wcscal=wcsdict, speccal=scdict, dither=dithdict)

        for o in self.all_observations:
            o.associate(wavecal=wcdict, flatcal=fcdict, wcscal=wcsdict, speccal=scdict, dither=dithdict)

        try:
            for o in self.wavecalable:
                if isinstance(o.wavecal, str) and o.wavecal:
                    missing['wavecal'].add(o.wavecal)

            for o in self.flatcalable:
                if isinstance(o.flatcal, str) and o.flatcal:
                    missing['flatcal'].add(o.flatcal)

            for o in self.wcscalable:
                if isinstance(o.wcscal, str) and o.wcscal:
                    missing['wcscal'].add(o.wcscal)

            for o in self.speccalable:
                if isinstance(o.speccal, str) and o.speccal:
                    missing['speccal'].add(o.speccal)

        except:
            getLogger(__name__).error('Failure during name/data association', exc_info=True)

    def _find_nested(self, attr, kind, look_in):
        for r in self.meta:
            if isinstance(r, kind):
                yield r
            # This is necessary as we allow the user to define directly where they are used
            if isinstance(r, look_in):
                for o in r.obs:
                    x = getattr(o, attr, None)
                    if isinstance(x, kind):
                        yield x

    def by_name(self, name):
        d = [d for d in self.meta if d.name == name]
        try:
            if len(d) > 1:
                getLogger(__name__).warning(f'There are {len(d)} things named {name}, returning the first')
            return d[0]
        except IndexError:
            return None

    @property
    def all_timeranges(self) -> Set[MKIDTimerange]:
        tr = set([tr for x in self.meta for tr in x.input_timeranges])
        return tr

    @property
    def wavecals(self):
        yield self._find_nested('wavecal', MKIDWavecalDescription,
                                (MKIDObservation, MKIDWCSCalDescription, MKIDDitherDescription, MKIDFlatcalDescription,
                                 MKIDSpeccalDescription))

    @property
    def flatcals(self):
        yield self._find_nested('flatcal', MKIDFlatcalDescription, (MKIDObservation, MKIDWCSCalDescription,
                                                                    MKIDDitherDescription, MKIDSpeccalDescription))

    @property
    def wcscals(self):
        yield self._find_nested('wcscal', MKIDWCSCalDescription, (MKIDObservation, MKIDDitherDescription,
                                                                  MKIDSpeccalDescription))

    @property
    def dithers(self):
        for r in self.meta:
            if isinstance(r, MKIDDitherDescription):
                yield r
            if isinstance(r, MKIDSpeccalDescription) and isinstance(r.data, MKIDDitherDescription):
                yield r.data

    @property
    def speccals(self):
        yield self._find_nested('speccal', MKIDSpeccalDescription, (MKIDObservation, MKIDDitherDescription))

    @property
    def all_observations(self):
        for o in self.meta:
            if isinstance(o, MKIDObservation):
                yield o
        for d in self.meta:
            if isinstance(d, MKIDFlatcalDescription):
                for o in d.obs:
                    yield o
        for d in self.meta:
            if isinstance(d, MKIDWCSCalDescription):
                for o in d.obs:
                    if o:
                        yield o
        for d in self.meta:
            if isinstance(d, MKIDSpeccalDescription):
                for o in d.obs:
                    yield o
        for d in self.meta:
            if isinstance(d, MKIDDitherDescription):
                for o in d.obs:
                    yield o

    @property
    def wavecalable(self):
        """ must return EVERY item in the dataset that might have .wavecal"""
        return self.all_observations

    @property
    def pixcalable(self):
        return self.all_observations

    @property
    def cosmiccalable(self):
        return self.all_observations

    @property
    def lincalable(self):
        return self.all_observations

    @property
    def flatcalable(self):
        """ must return EVERY item in the dataset that might have .flatcal"""
        return ([o for o in self.meta if isinstance(o, MKIDObservation)] +
                [o for d in self.meta if isinstance(d, MKIDDitherDescription) for o in d.obs] +
                [o for d in self.meta if isinstance(d, MKIDSpeccalDescription) for o in d.obs] +
                [o for d in self.meta if isinstance(d, MKIDWCSCalDescription) for o in d.obs if o])

    @property
    def wcscalable(self):
        """ must return EVERY item in the dataset that might have .wcscal"""
        return ([o for d in self.meta if isinstance(d, MKIDSpeccalDescription) for o in d.obs] +
                [o for o in self.meta if isinstance(o, MKIDObservation)] +
                [o for d in self.meta if isinstance(d, MKIDDitherDescription) for o in d.obs])

    @property
    def speccalable(self):
        """ must return EVERY item in the dataset that might have .speccal"""
        return ([o for o in self.meta if isinstance(o, MKIDObservation)] +
                [o for d in self.meta if isinstance(d, MKIDDitherDescription) for o in d.obs])

    @property
    def description(self):
        """Return a string describing the data"""
        s = ("Wavecals:\n{wc}\n"
             "Flatcals:\n{fc}\n"
             "Dithers:\n{dithers}\n"
             "Single Obs:\n{obs}".format(wc=('\t-' + '\n\t-'.join([str(w).replace('\n', '\n\t')
                                                                   for w in
                                                                   self.wavecals])) if self.wavecals else '\tNone',
                                         fc=('\t-' + '\n\t-'.join(
                                             [str(f) for f in self.flatcals])) if self.flatcals else
                                         '\tNone',
                                         dithers='Not implemented',
                                         obs='Not implemented'))
        return s


class MKIDOutput(DataBase):
    yaml_tag = '!MKIDOutput'
    KEYS = (
        Key(name='name', default='', comment='A name', dtype=str),
        Key('data', '', 'An data name', str),
        Key('kind', 'image', "('stack', 'spatial', 'temporal', 'list', 'image', 'movie')", str),
        Key('min_wave', float('-inf'), 'Wavelength start for wavelength sensitive outputs', str),
        Key('max_wave', float('inf'), 'Wavelength stop for wavelength sensitive outputs, ', str),
        Key('filename', '', 'relative or fully qualified path, defaults to name+output type,'
                            'so set if making multiple outputs with different settings', str),
        Key('ssd', True, 'Use ssd TODO', bool),
        Key('noise', True, 'Use noise TODO', bool),
        Key('photom', True, 'Use photom TODO', bool),
        Key('lincal', False, 'Use lincal', bool),
        Key('exp_timestep', None, 'Duration of time bins in output cubes with a temporal axis (req. by temporal)',
            float)
    )
    REQUIRED = ('name', 'data', 'kind')
    EXPLICIT_ALLOW = ('filename',)

    # OPTIONAL = tuple

    def __init__(self, *args, **kwargs):
        """
        :param name: a name
        :param dataname: a name of a data association
        :param kind: stack|spatial|temporal|list|image|movie
        :param startw: wavelength start
        :param stopw: wavelength stop
        :param filename: an optional relative or fully qualified path, defaults to name+output type,
            so set if making multiple outputs with different settings

        Kind 'movie' requires _extra keys timestep and either frameduration or movieduration with frameduration
        taking precedence. startt and stopt may be included as well and are RELATIVE to the start of the file.

        image - uses photontable.get_fits to the a simple image of the data, applies to a single h5
        stack - uses drizzler.SpatialDrizzler
        spatial - uses drizzler.SpatialDrizzler
        temporal - uses drizzler.TemporalDrizzler
        list - drizzler.ListDrizzler to assign photons an RA and Dec
        movie - uses movie.make_movie to make an animation

        """
        super().__init__(*args, **kwargs)
        self.kind = self.kind.lower()
        opt = ('stack', 'spatial', 'temporal', 'list', 'image', 'movie')
        if self.kind not in opt:
            self._key_errors['kind'] += [f"Must be one of: {opt}"]
        # self.exp_timestep=1  # 'duration of time bins in the output cube, required by temporal only, nbins=frametime/exp_timestep '

    @property
    def startw(self):
        # TODO remove me and all that use me
        return self.min_wave

    @property
    def startw(self):
        # TODO remove me and all that use me
        return self.max_wave

    @property
    def wants_image(self):
        return self.kind == 'image'

    @property
    def wants_drizzled(self):
        return self.kind in ('stack', 'spatial', 'temporal', 'list')

    @property
    def wants_movie(self):
        return self.kind == 'movie'

    @property
    def input_timeranges(self) -> Set[MKIDTimerange]:
        return set(self.data.input_timeranges)

    @property
    def output_file(self):
        global config
        if self.filename:
            file = self.filename
        else:
            if self.kind in ('stack', 'spatial', 'temporal', 'image'):
                ext = 'fits'
            elif self.kind is 'movie':
                ext = 'gif'
            else:
                ext = 'h5'
            file = f'{self.name}_{self.kind}.{ext}'

        if os.pathsep in file:
            return file
        else:
            return os.path.join(config.paths.out,
                                self.data if isinstance(self.data, str) else self.data.name,
                                file)


class MKIDOutputCollection:
    def __init__(self, file, datafile=''):
        self.file = file
        self.meta = mkidcore.config.load(file)
        self.dataset = MKIDObservingDataset(datafile) if datafile else None

        if self.dataset is not None:
            for o in self.meta:
                d = self.dataset.by_name(o.data)
                if d is not None:
                    o.data = d
                else:
                    getLogger(__name__).critical(f'Unable to find data description for "{o.data}"')

    def __iter__(self) -> MKIDOutput:
        for o in self.meta:
            yield o

    def __str__(self):
        return f'MKIDOutputCollection: {self.file}'

    @property
    def input_timeranges(self) -> Set[MKIDTimerange]:
        return set([r for o in self for r in o.input_timeranges])

    @property
    def wavecals(self):
        return [o.wavecal for o in self.to_wavecal if o.wavecal]

    @property
    def flatcals(self):
        return [o.flatcal for o in self.to_flatcal if o.flatcal]

    @property
    def speccals(self):
        return [o.speccal for o in self.to_speccal if o.speccal]

    @property
    def wcscals(self):
        for out in self:
            if out.data.wcscal:
                yield out.data.wcscal
            if out.data.speccal:
                for o in out.data.speccal.obs:
                    if o.wcscal:
                        yield o.wcscal

    @property
    def to_lincal(self):
        def input_observations(obs):
            for o in obs:
                if o.flatcal and o.flatcal.lincal:
                    for x in o.flatcal.obs:
                        yield x
                if o.speccal:
                    for x in o.speccal.obs:
                        if x.flatcal and x.flatcal.lincal:
                            for y in x.flatcal.obs:
                                yield y
                if o.wcscal:
                    for x in o.wcscal.obs:
                        if x.flatcal and x.flatcal.lincal:
                            for y in x.flatcal.obs:
                                yield y

        for out in self:
            if out.lincal:
                for o in out.data.obs:
                    yield o
            yield input_observations(out.data.obs)

    @property
    def to_wavecal(self):
        def input_observations(obs):
            for o in obs:
                if o.flatcal:
                    for x in o.flatcal.obs:
                        yield x
                if o.speccal:
                    for x in o.speccal.obs:
                        yield x
                        if x.flatcal:
                            for y in x.flatcal.obs:
                                yield y
                        if x.wcscal:
                            for y in x.wcscal.obs:
                                yield
                                if y.flatcal:
                                    for z in y.flatcal.obs:
                                        yield z
                if o.wcscal:
                    for x in o.wcscal.obs:
                        yield x
                        if x.flatcal:
                            for y in x.flatcal.obs:
                                yield y
        for out in self:
            if out.wavecal:
                for o in out.data.obs:
                    yield o
            yield input_observations(out.data.obs)

    @property
    def to_pixcal(self):
        def input_observations(obs):
            for o in obs:
                if o.flatcal and o.flatcal.pixcal:
                    for x in o.flatcal.obs:
                        yield x
                if o.speccal:
                    for x in o.speccal.obs:
                        yield x
                        if x.flatcal and x.flatcal.pixcal:
                            for y in x.flatcal.obs:
                                yield y
                        if x.wcscal:
                            for y in x.wcscal.obs:
                                yield y
                                if y.flatcal and y.flatcal.pixcal:
                                    for z in y.flatcal.obs:
                                        yield z
                if o.wcscal:
                    for x in o.wcscal.obs:
                        yield x
                        if x.flatcal and x.flatcal.pixcal:
                            for y in x.flatcal.obs:
                                yield y
        for out in self:
            if out.pixcal:
                for o in out.data.obs:
                    yield o
            yield input_observations(out.data.obs)

    @property
    def to_cosmiccal(self):
        def input_observations(obs):
            for o in obs:
                if o.flatcal and o.flatcal.cosmiccal:
                    for x in o.flatcal.obs:
                        yield x
                if o.speccal:
                    for x in o.speccal.obs:
                        #yield x
                        if x.flatcal and x.flatcal.cosmiccal:
                            for y in x.flatcal.obs:
                                yield y
                        if x.wcscal:
                            for y in x.wcscal.obs:
                                #yield y
                                if y.flatcal and y.flatcal.cosmiccal:
                                    for z in y.flatcal.obs:
                                        yield z
                if o.wcscal:
                    for x in o.wcscal.obs:
                        #yield x
                        if x.flatcal and x.flatcal.cosmiccal:
                            for y in x.flatcal.obs:
                                yield y
        for out in self:
            if out.cosmical:
                for o in out.data.obs:
                    yield o
            yield input_observations(out.data.obs)

    @property
    def to_flatcal(self):
        def input_observations(obs):
            for o in obs:
                if o.speccal:
                    for x in o.speccal.obs:
                        if x.flatcal:
                            yield x
                        if x.wcscal:
                            for y in x.wcscal.obs:
                                if y.flatcal:
                                    yield y
                if o.wcscal:
                    for x in o.wcscal.obs:
                        if x.flatcal:
                            yield x
        for out in self:
            if out.flatcal:
                for o in out.data.obs:
                    if o.flatcal:
                        yield o
            yield input_observations(out.data.obs)

    @property
    def to_speccal(self):
        for out in self:
            if out.speccal:
                for o in out.data.obs:
                    if o.speccal:
                        yield o

    @property
    def to_drizzle(self):
        for out in self:
            if isinstance(out.data, MKIDDitherDescription):
                yield out.data
            if out.data.speccal and isinstance(out.data.speccal.data, MKIDDitherDescription):
                yield out.data.speccal.data


def inspect_database(detailed=False):
    """Warning detailed=True will load each thing in the database for detailed inspection"""
    from glob import glob

    for f in glob(config.config.paths.database + '*'):
        print(f'{f}')


def validate_metadata(md, warn=True, error=False):
    fail = False
    for k in REQUIRED_KEYS:
        if k not in md:
            if error:
                raise KeyError(msg)
            fail = True
            msg = '{} missing from {}'.format(k, md)
            if warn:
                getLogger(__name__).warning(msg)
    return fail


def observing_metadata_for_timerange(timerange, metadata_source=None):
    """
    Metadata that goes into an H5 consists of records within the duration

    requires metadata_source be an indexable iterable with an attribute utc pointing to a datetime
    """
    if not metadata_source:
        metadata_source = load_observing_metadata()
    # Select the nearest metadata to the midpoint
    start = datetime.fromtimestamp(timerange.start)
    time_since_start = np.array([(md.utc - start).total_seconds() for md in metadata_source])
    ok, _ = np.where((time_since_start < timerange.duration) & (time_since_start >= 0))
    mdl = [metadata_source[i] for i in ok]
    return mdl


def parse_obslog(file):
    """Return a list of configthings for each record in the observing log filterable on the .utc attribute"""
    with open(file, 'r') as f:
        lines = f.readlines()
    ret = []
    for l in lines:
        ct = mkidcore.config.ConfigThing(json.loads(l).items())
        ct.register('utc', datetime.strptime(ct.utc, "%Y%m%d%H%M%S"), update=True)
        ret.append(ct)
    return ret


def load_observing_metadata(files=tuple(), include_database=True, use_cache=True):
    """Return a list of mkidcore.config.ConfigThings with the contents of the metadata from observing log files"""
    global config, _metadata

    # _metadata is a dict of file: parsed_file records
    files = list(files)
    if config is not None and include_database:
        files += glob(os.path.join(config.paths.obslog, 'obslog*.json'))
    elif include_database:
        getLogger(__name__).warning('No pipleline database configured.')
    files = set(files)
    if use_cache:
        for f in files:
            if f not in _metadata:
                _metadata[f] = parse_obslog(f)
        metad = _metadata
    else:
        metad = {f: parse_obslog(f) for f in files}

    metadata = []
    for f in files:
        metadata += metad[f]
    return metadata


def n_cpus_available(max=np.inf):
    """Returns n threads -4 modulo pipelinesettings"""
    global config
    mcpu = min(mp.cpu_count() * 2 - 4, max)
    try:
        mcpu = int(min(config.ncpu, mcpu))
    except Exception:
        pass
    return mcpu


def log_to_console(file='', **kwargs):
    logs = (create_log('mkidcore', **kwargs), create_log('mkidreadout', **kwargs), create_log('mkidpipeline', **kwargs),
            create_log('__main__', **kwargs))
    if file:
        import logging
        handler = MakeFileHandler(file)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s (pid=%(process)d)'))
        for l in logs:
            l.addHandler(handler)


yaml.register_class(MKIDTimerange)
yaml.register_class(MKIDObservation)
yaml.register_class(MKIDWavecalDescription)
yaml.register_class(MKIDFlatcalDescription)
yaml.register_class(MKIDSpeccalDescription)
yaml.register_class(MKIDWCSCalDescription)
yaml.register_class(MKIDDitherDescription)
yaml.register_class(MKIDOutput)
