#!/bin/env python3
import os
import ast
import sys
import atexit
import logging
import argparse
import warnings
import numpy as np
import progressbar as pb
import multiprocessing as mp
from datetime import datetime
from astropy.constants import c, h
from distutils.spawn import find_executable
from six.moves.configparser import ConfigParser
import matplotlib
from matplotlib import gridspec
from matplotlib.widgets import Button, Slider
from matplotlib import cm, lines, pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from mpl_toolkits.axes_grid1 import axes_size, make_axes_locatable

from mkidpipeline.hdf import bin2hdf
import mkidcore.corelog as pipelinelog
from mkidpipeline.hdf.photontable import ObsFile
import mkidpipeline.calibration.wavecal_models as wm


log = pipelinelog.getLogger('mkidpipeline.calibration.wavecal', setup=False)


def setup_logging(configuration, time_stamp=None):
    """
    Set up logging for the wavelength calibration module for running from the command
    line.

    Args:
        configuration: wavelength calibration Configuration object
        time_stamp: utc time stamp to name the log file
    """
    wavecal_name = 'mkidpipeline.calibration.wavecal'
    models_name = 'mkidpipeline.calibration.wavecal_models'
    wavecal_log = pipelinelog.getLogger(wavecal_name, setup=False)
    wavecal_models_log = pipelinelog.getLogger(models_name, setup=False)
    if time_stamp is None:
        time_stamp = int(datetime.utcnow().timestamp())
    if configuration.verbose:
        log_format = "%(levelname)s : %(message)s"
        wavecal_log = pipelinelog.create_log(wavecal_name, console=True, fmt=log_format,
                                             level="INFO")
        wavecal_models_log = pipelinelog.create_log(models_name, console=True,
                                                    fmt=log_format, level="INFO")
    if configuration.logging:
        log_directory = os.path.join(configuration.out_directory, 'logs')
        log_file = os.path.join(log_directory, '{:.0f}.log'.format(time_stamp))
        log_format = '%(asctime)s : %(funcName)s : %(levelname)s : %(message)s'
        wavecal_log = pipelinelog.create_log(wavecal_name, logfile=log_file,
                                             console=False, fmt=log_format, level="DEBUG")
        wavecal_models_log = pipelinelog.create_log(models_name, logfile=log_file,
                                                    console=False, fmt=log_format,
                                                    level="DEBUG")
    return wavecal_log, wavecal_models_log


class Configuration(object):
    """Configuration class for the wavelength calibration analysis."""
    def __init__(self, configuration_path, solution_name='solution.npz'):
        # parse arguments
        self.solution_name = solution_name
        self.configuration_path = configuration_path

        assert os.path.isfile(self.configuration_path), \
            self.configuration_path + " is not a valid configuration file"

        # load in the configuration file
        self.config = ConfigParser()
        self.config.read(self.configuration_path)

        # check the configuration file format and load the parameters
        self.check_sections()

        # load in the parameters
        self.x_pixels = ast.literal_eval(self.config['Data']['x_pixels'])
        self.y_pixels = ast.literal_eval(self.config['Data']['y_pixels'])
        self.bin_directory = ast.literal_eval(self.config['Data']['bin_directory'])
        self.start_times = ast.literal_eval(self.config['Data']['start_times'])
        self.exposure_times = ast.literal_eval(self.config['Data']['exposure_times'])
        self.beam_map_path = ast.literal_eval(self.config['Data']['beam_map_path'])
        self.h5_directory = ast.literal_eval(self.config['Data']['h5_directory'])
        self.h5_file_names = ast.literal_eval(self.config['Data']['h5_file_names'])
        self.wavelengths = ast.literal_eval(self.config['Data']['wavelengths'])
        self.histogram_model_names = ast.literal_eval(
            self.config['Fit']['histogram_model_names'])
        self.bin_width = ast.literal_eval(self.config['Fit']['bin_width'])
        self.histogram_fit_attempts = ast.literal_eval(
            self.config['Fit']['histogram_fit_attempts'])
        self.calibration_model_names = ast.literal_eval(
            self.config['Fit']['calibration_model_names'])
        self.dt = ast.literal_eval(self.config['Fit']['dt'])
        self.parallel = ast.literal_eval(self.config['Fit']['parallel'])
        self.out_directory = ast.literal_eval(self.config['Output']['out_directory'])
        self.summary_plot = ast.literal_eval(self.config['Output']['summary_plot'])
        self.templar_configuration_path = ast.literal_eval(
            self.config['Output']['templar_configuration_path'])
        self.verbose = ast.literal_eval(self.config['Output']['verbose'])
        self.logging = ast.literal_eval(self.config['Output']['logging'])

        # check the parameter formats
        self.check_parameters()
        
        # enforce consistency between h5 and bin file start times
        self._config_changed = False
        self.enforce_consistency()

        # write new config file if enforce_consistency() updated any parameters
        if self._config_changed:
            while True:
                if os.path.isfile(self.configuration_path):
                    directory = os.path.dirname(self.configuration_path)
                    base_name = "".join(
                        os.path.basename(self.configuration_path).split(".")[:-1])
                    suffix = str(os.path.basename(self.configuration_path).split(".")[-1])
                    self.configuration_path = os.path.join(directory,
                                                           base_name + "_new." + suffix)
                else:
                    break
            self.write(self.configuration_path)

    def check_sections(self):
        """Check if all sections and parameters exist in the configuration file."""
        section = "'{0}' must be a configuration file section"
        param = "'{0}' must be a parameter in the '{1}' section of the configuration file"

        assert 'Data' in self.config.sections(), section.format('Data')
        assert 'x_pixels' in self.config['Data'].keys(), \
            param.format('x_pixels', 'Data')
        assert 'y_pixels' in self.config['Data'].keys(), \
            param.format('y_pixels', 'Data')
        assert 'bin_directory' in self.config['Data'].keys(), \
            param.format('bin_directory', 'Data')
        assert 'start_times' in self.config['Data'].keys(), \
            param.format('start_times', 'Data')
        assert 'exposure_times' in self.config['Data'].keys(), \
            param.format('exposure_times', 'Data')
        assert 'beam_map_path' in self.config['Data'].keys(), \
            param.format('beam_map_path', 'Data')
        assert 'h5_directory' in self.config['Data'].keys(), \
            param.format('h5_directory', 'Data')
        assert 'h5_file_names' in self.config['Data'].keys(), \
            param.format('h5_file_names', 'Data')
        assert 'wavelengths' in self.config['Data'].keys(), \
            param.format('wavelengths', 'Data')

        assert 'Fit' in self.config.sections(), section.format('Fit')
        assert 'histogram_model_names' in self.config['Fit'].keys(), \
            param.format('histogram_model_names', 'Fit')
        assert 'bin_width' in self.config['Fit'].keys(), \
            param.format('bin_width', 'Fit')
        assert 'histogram_fit_attempts' in self.config['Fit'].keys(), \
            param.format('histogram_fit_attempts', 'Fit')
        assert 'calibration_model_names' in self.config['Fit'].keys(), \
            param.format('calibration_model_names', 'Fit')
        assert 'dt' in self.config['Fit'].keys(), \
            param.format('dt', 'Fit')
        assert 'parallel' in self.config['Fit'].keys(), \
            param.format('parallel', 'Fit')

        assert 'Output' in self.config.sections(), section.format('Output')
        assert 'out_directory' in self.config['Output'], \
            param.format('out_directory', 'Output')
        assert 'summary_plot' in self.config['Output'], \
            param.format('summary_plot', 'Output')
        assert 'templar_configuration_path' in self.config['Output'], \
            param.format('templar_configuration_path', 'Output')
        assert 'verbose' in self.config['Output'], \
            param.format('verbose', 'Output')
        assert 'logging' in self.config['Output'], \
            param.format('logging', 'Output')

    def check_parameters(self):
        """Type check configuration file parameters."""
        assert type(self.x_pixels) is int, "x_pixels parameter must be an integer"
        assert type(self.y_pixels) is int, "y_pixels parameter must be an integer"
        assert os.path.isdir(self.bin_directory),\
            "bin_directory parameter must be a string and a valid directory"
        message = "start_times parameter must be a list of integers."
        assert type(self.start_times) is list, message
        for st in self.start_times:
            assert type(st) is int, message
        message = "exposure_times parameter must be a list of integers"
        assert type(self.exposure_times) is list, message
        for et in self.exposure_times:
            assert type(et) is int, message
        assert os.path.isfile(self.beam_map_path),\
            "beam_map_path parameter must be a string and a valid path to a file"
        assert os.path.isdir(self.h5_directory), \
            "h5_directory parameter must be a string and a valid directory"
        message = "h5_file_names parameter must be a list of strings or None."
        assert isinstance(self.h5_file_names, (list, type(None))), message
        if isinstance(self.h5_file_names, list):
            for name in self.h5_file_names:
                assert isinstance(name, str), message
        message = "wavelengths parameter must be a list of numbers"
        assert isinstance(self.wavelengths, (list, np.ndarray)), message
        try:
            self.wavelengths = np.array([float(wavelength)
                                         for wavelength in self.wavelengths])
        except ValueError:
            raise AssertionError(message)
        message = ("histogram_model_names parameter must be a list of subclasses of "
                   "PartialLinearModel and be in wavecal_models.py")
        assert isinstance(self.histogram_model_names, list), message
        for model in self.histogram_model_names:
            assert issubclass(getattr(wm, model), wm.PartialLinearModel), message
        try:
            self.bin_width = float(self.bin_width)
        except ValueError:
            raise AssertionError("bin_width parameter must be an integer or float")
        assert isinstance(self.histogram_fit_attempts, int), \
            "histogram_fit_attempts parameter must be an integer"
        message = ("calibration_model_names parameter must be a list of subclasses of "
                   "XErrorsModel and be in wavecal_models.py")
        assert isinstance(self.calibration_model_names, list), message
        for model in self.calibration_model_names:
            assert issubclass(getattr(wm, model), wm.XErrorsModel), message
        try:
            self.dt = float(self.dt)
        except ValueError:
            raise AssertionError("dt parameter must be an integer or float")
        assert type(self.parallel) is bool, "parallel parameter must be a boolean"
        assert os.path.isdir(self.out_directory), \
            "out_directory parameter must be a string and a valid directory"
        assert type(self.summary_plot) is bool, "summary_plot parameter must be a boolean"
        assert os.path.isfile(self.templar_configuration_path),\
            "templar_configuration_path parameter must be a string and a valid file path"
        assert type(self.verbose) is bool, "verbose parameter bust be a boolean"
        assert type(self.logging) is bool, "logging parameter must be a boolean"

    def hdf_exist(self):
        """Check if all hdf5 files specified exist."""
        file_paths = [os.path.join(self.h5_directory, file_)
                      for file_ in self.h5_file_names]
        return all(map(os.path.isfile, file_paths))

    def enforce_consistency(self):
        """Make sure a partially specified configuration is fully defined"""
        # check to see if h5 files were specified and compute their names otherwise
        if self.h5_file_names is None:
            self._config_changed = True
            self.h5_file_names = self._compute_hdf_names()
        # check that wavelengths are in ascending order and sort otherwise
        if (sorted(self.wavelengths) != self.wavelengths).all():
            self._config_changed = True
            self._sort_wavelengths()

    def write(self, file_):
        """Save the configuration to a file"""
        with open(file_, 'w') as f:
            f.write('[Data]' + os.linesep +
                    'x_pixels = {}'.format(self.x_pixels) + os.linesep +
                    'y_pixels = {}'.format(self.y_pixels) + os.linesep +
                    'bin_directory = "{}"'.format(self.bin_directory) + os.linesep +
                    'start_times = {}'.format(self.start_times) + os.linesep +
                    'exposure_times = {}'.format(self.exposure_times) + os.linesep +
                    'beam_map_path = "{}"'.format(self.beam_map_path) + os.linesep +
                    'h5_directory = "{}"'.format(self.h5_directory) + os.linesep +
                    'h5_file_names = {}'.format(self.h5_file_names) + os.linesep +
                    'wavelengths = {}'.format(list(self.wavelengths)) + os.linesep +
                    os.linesep +
                    '[Fit]' + os.linesep +
                    'histogram_model_names = {}'.format(self.histogram_model_names) +
                    os.linesep +
                    'bin_width = {}'.format(self.bin_width) + os.linesep +
                    'histogram_fit_attempts = {}'.format(self.histogram_fit_attempts) +
                    os.linesep +
                    'calibration_model_names = {}'.format(self.calibration_model_names) +
                    os.linesep +
                    'dt = {}'.format(self.dt) + os.linesep +
                    'parallel = {}'.format(self.parallel) + os.linesep +
                    os.linesep +
                    '[Output]' + os.linesep +
                    'out_directory = "{}"'.format(self.out_directory) + os.linesep +
                    'summary_plot = {}'.format(self.summary_plot) + os.linesep +
                    ('templar_configuration_path = "{}"'
                     .format(self.templar_configuration_path)) + os.linesep +
                    'verbose = {}'.format(self.verbose) + os.linesep +
                    'logging = {}'.format(self.logging))

    def _compute_hdf_names(self):
        return ['%d' % st + '.h5' for st in self.start_times]

    def _sort_wavelengths(self):
        indices = np.argsort(self.wavelengths)
        self.wavelengths = list(np.array(self.wavelengths)[indices])
        self.exposure_times = list(np.array(self.exposure_times)[indices])
        self.start_times = list(np.array(self.start_times)[indices])
        self.h5_file_names = list(np.array(self.h5_file_names)[indices])


class Calibrator(object):
    """
    Class for creating wavelength calibrations from ObsFile formatted data. After the
    Calibrator object is initialized with the configuration object, run() should be called
    to compute the calibration solution. All methods modify self.solution which is an
    instance of the Solution class.

    Args:
        configuration: wavecal.py Configuration object

    Created by: Nicholas Zobrist, January 2018
    """
    def __init__(self, configuration):
        # save configuration
        self.cfg = configuration

        # get beam map
        obs_files = []
        for index, _ in enumerate(self.cfg.wavelengths):
            file_name = os.path.join(self.cfg.h5_directory,
                                     self.cfg.h5_file_names[index])
            obs_files.append(ObsFile(file_name))
            message = "The beam map does not match the array dimensions"
            assert (obs_files[-1].beamImage.shape ==
                    (self.cfg.x_pixels, self.cfg.y_pixels)), message
        beam_map = obs_files[0].beamImage.copy()
        beam_map_flags = np.array(obs_files[0].beamFlagImage)
        del obs_files

        # initialize fit array
        fit_array = np.empty((self.cfg.x_pixels, self.cfg.y_pixels), dtype=object)
        self.solution = Solution(fit_array=fit_array, configuration=self.cfg,
                                 beam_map=beam_map, beam_map_flags=beam_map_flags)
        self.progress = None
        self.progress_iteration = None
        self._acquired = 0
        self._max_queue_size = None

    def run(self, pixels=None, wavelengths=None, verbose=True, parallel=True, save=True,
            plot=True):
        """
        Compute the wavelength calibration for the data specified in the configuration
        object. This method runs make_histograms(), fit_histograms(), and
        fit_calibrations() sequentially.

        Args:
            pixels: list of (x, y) coordinates to compute calibrations for. If None, all
                    pixels in the array are used.
            wavelengths: a list of wavelengths to compute calibrations for. If None, all
                         wavelengths specified in the configuration object are used.
            verbose: a boolean specifying whether to print a progress bar to the stdout.
            parallel: a boolean specifying whether to use more than one core in the
                      computation.
            save: a boolean specifying if the result will be saved.
            plot: a boolean specifying if a summary plot for the computation will be
                  saved.
        """
        # check inputs don't set up the progress bar yet
        pixels, wavelengths = self._setup(pixels, wavelengths)
        try:
            log.info("Computing phase histograms")
            self._run("make_histograms", pixels=pixels, wavelengths=wavelengths,
                      parallel=parallel, verbose=verbose, h5_safe=True)
            log.info("Fitting phase histograms")
            self._run("fit_histograms", pixels=pixels, wavelengths=wavelengths,
                      parallel=parallel, verbose=verbose)
            log.info("Fitting phase-energy calibration")
            self._run("fit_calibrations", pixels=pixels, wavelengths=wavelengths,
                      parallel=parallel, verbose=verbose)
            if save:
                self.solution.save()
            if plot:
                save_name = self.cfg.solution_name.split(".")[0] + ".pdf"
                self.solution.plot_summary(save_name=save_name)
        except KeyboardInterrupt:
            log.info("Keyboard shutdown requested ... exiting")

    def make_histograms(self, pixels=None, wavelengths=None, verbose=True):
        """
        Compute the phase pulse-height histograms for the data specified in the
        configuration file.

        Args:
            pixels: list of (x, y) coordinates to compute calibrations for. If None, all
                    pixels in the array are used.
            wavelengths: a list of wavelengths to compute calibrations for. If None, all
                         wavelengths specified in the configuration object are used.
            verbose: a boolean specifying whether to print a progress bar to the stdout.
        """
        # TODO: parsebin should be much faster than multiprocessing getPixelPhotonList()
        # check inputs and setup progress bar
        pixels, wavelengths = self._setup(pixels, wavelengths)
        self._update_progress(number=pixels.shape[1], initialize=True, verbose=verbose)
        obs_files = []
        try:
            # make ObsFiles
            for wavelength in wavelengths:
                # wavelengths might be unordered, so we get the right order of h5 files
                index = np.where(wavelength == self.cfg.wavelengths)[0].squeeze()
                file_name = os.path.join(self.cfg.h5_directory,
                                         self.cfg.h5_file_names[index])
                obs_files.append(ObsFile(file_name))
            # make histograms for each pixel in pixels and wavelength in wavelengths
            for pixel in pixels.T:
                wavelength = None
                try:
                    # update progress bar
                    self._update_progress(verbose=verbose)
                    # histogram get models
                    models = self.solution.histogram_models(wavelengths, pixel=pixel)
                    for index, wavelength in enumerate(wavelengths):
                        model = models[index]
                        # load the data
                        photon_list = obs_files[index].getPixelPhotonList(*pixel)
                        if photon_list.size < 2:
                            model.flag = 1
                            message = "({}, {}) : {} nm : there are no photons"
                            log.debug(message.format(pixel[0], pixel[1], wavelength))
                            continue
                        # remove hot pixels
                        rate = (len(photon_list['Wavelength']) * 1e6 /
                                (max(photon_list['Time']) - min(photon_list['Time'])))
                        if rate > 2000:
                            model.flag = 2
                            message = ("({}, {}) : {} nm : removed for being too hot "
                                       "({:.2f} > 2000 cps)")
                            log.debug(message.format(pixel[0], pixel[1], wavelength,
                                                     rate))
                            continue
                        # remove photons too close together in time
                        photon_list = self._remove_tail_riding_photons(photon_list)
                        if photon_list.size == 0:
                            model.flag = 3
                            message = ("({}, {}) : {} nm : all the photons were removed "
                                       "after the arrival time cut")
                            log.debug(message.format(pixel[0], pixel[1], wavelength))
                            continue
                        # remove photons with positive peak heights
                        phase_list = photon_list['Wavelength']
                        phase_list = phase_list[phase_list < 0]
                        if phase_list.size == 0:
                            model.flag = 4
                            message = ("({}, {}) : {} nm : all the photons were removed "
                                       "after the negative phase only cut")
                            log.debug(message.format(pixel[0], pixel[1], wavelength))
                            continue
                        # make histogram
                        centers, counts = self._histogram(phase_list)
                        # assign x, y and variance data to the fit model
                        model.x = centers
                        model.y = counts
                        # gaussian mle for the variance of poisson distributed data
                        # https://doi.org/10.1016/S0168-9002(00)00756-7
                        model.variance = np.sqrt(counts**2 + 0.25) - 0.5
                        message = "({}, {}), : {} nm : histogram successfully computed"
                        log.debug(message.format(pixel[0], pixel[1], wavelength))
                except KeyboardInterrupt:
                    raise KeyboardInterrupt
                except Exception as error:
                    if wavelength is None:
                        message = "({}, {}) : ".format(pixel[0], pixel[1]) + str(error)
                    else:
                        message = ("({}, {}), : {} nm : ".format(pixel[0], pixel[1],
                                                                 wavelength) + str(error))
                    log.error(message)
                    raise error
            # update progress bar
            self._update_progress(finish=True, verbose=verbose)
        finally:
            # close obsFiles
            for obs_file in obs_files:
                obs_file.file.close()

    def fit_histograms(self, pixels=None, wavelengths=None, verbose=True):
        """
        Fit the phase pulse-height histograms to a model by fitting each specified in the
        configuration object and selecting the best one.

        Args:
            pixels: list of (x, y) coordinates to compute calibrations for. If None, all
                    pixels in the array are used.
            wavelengths: a list of wavelengths to compute calibrations for. If None, all
                         wavelengths specified in the configuration object are used.
            verbose: a boolean specifying whether to print a progress bar to the stdout.
        """
        # check inputs and setup progress bar
        pixels, wavelengths = self._setup(pixels, wavelengths)
        self._update_progress(number=pixels.shape[1], initialize=True, verbose=verbose)
        # fit histograms for each pixel in pixels and wavelength in wavelengths
        for pixel in pixels.T:
            wavelength = None
            try:
                # update progress bar
                self._update_progress(verbose=verbose)
                models = self.solution.histogram_models(wavelengths, pixel=pixel)
                # fit the histograms of the higher energy data sets first and use good
                # fits to inform the guesses to the lower energy data sets
                for index, wavelength in enumerate(wavelengths):
                    model = models[index]
                    if model.x is None or model.y is None:
                        message = ("({}, {}) : {} nm : histogram fit failed because "
                                   "there is no data")
                        log.debug(message.format(pixel[0], pixel[1], wavelength))
                        continue
                    if len(model.x) < model.max_parameters * 2:
                        model.flag = 5
                        message = ("({}, {}) : {} nm : histogram fit failed because "
                                   "there are less than 15 bins")
                        log.debug(message.format(pixel[0], pixel[1], wavelength))
                        continue
                    message = "({}, {}) : {} nm : beginning histogram fitting"
                    log.debug(message.format(pixel[0], pixel[1], wavelength))
                    # try models in order specified in the config file
                    tried_models = []
                    for histogram_model in self.solution.histogram_model_list:
                        # update the model if needed
                        if not isinstance(model, histogram_model):
                            model = self._update_histogram_model(wavelength,
                                                                 histogram_model, pixel)
                        # clear best_fit_result in case we are rerunning the fit
                        model.best_fit_result = None
                        model.best_fit_result_good = None
                        # if there are any good fits intelligently guess the signal_center
                        # parameter and set the other parameters equal to the average of
                        # those in the good fits
                        good_solutions = self.solution.has_good_histogram_solutions(
                            pixel=pixel)
                        wavelength_index = np.where(
                            wavelength == self.cfg.wavelengths)[0].squeeze()
                        if np.any(good_solutions):
                            guess = self._guess(pixel, wavelength_index, good_solutions)
                            model.fit(guess)
                            # if the fit worked continue with the next wavelength
                            if model.has_good_solution():
                                tried_models.append(model.copy())
                                message = ("({}, {}) : {} nm : histogram fit successful "
                                           "with computed guess and model '{}'")
                                log.debug(message.format(pixel[0], pixel[1], wavelength,
                                                         type(model).__name__))
                                continue
                        # try a guess based on the model if the computed guess didn't work
                        for fit_index in range(self.cfg.histogram_fit_attempts):
                            guess = model.guess(fit_index)
                            model.fit(guess)
                            if model.has_good_solution():
                                tried_models.append(model.copy())
                                message = ("({}, {}) : {} nm : histogram fit successful "
                                           "with guess number {} and model '{}'")
                                log.debug(message.format(pixel[0], pixel[1], wavelength,
                                                         fit_index, type(model).__name__))
                                break
                        else:
                            # trying next model since no good fit was found
                            tried_models.append(model.copy())
                            continue
                    # find model with the best fit and save that one
                    self._assign_best_histogram_model(tried_models, wavelength, pixel)

                # recheck fits that didn't work with better guesses if there exist
                # lower energy fits that did work
                good_solutions = self.solution.has_good_histogram_solutions(pixel=pixel)
                for index, wavelength in enumerate(wavelengths):
                    model = models[index]
                    if model.x is None or model.y is None:
                        continue
                    if model.has_good_solution():
                        continue
                    wavelength_index = np.where(
                        wavelength == self.cfg.wavelengths)[0].squeeze()
                    if np.any(good_solutions[wavelength_index + 1:]):
                        tried_models = []
                        for histogram_model in self.solution.histogram_model_list:
                            if not isinstance(model, histogram_model):
                                model = self._update_histogram_model(wavelength,
                                                                     histogram_model,
                                                                     pixel)
                            guess = self._guess(pixel, wavelength_index, good_solutions)
                            model.fit(guess)
                            if model.has_good_solution():
                                message = ("({}, {}) : {} nm : histogram fit recomputed "
                                           "and successful with model '{}'")
                                log.debug(message.format(pixel[0], pixel[1], wavelength,
                                                         type(model).__name__))
                                break
                            tried_models.append(model.copy())
                        else:
                            # find the model with the best bad fit and save that one
                            self._assign_best_histogram_model(tried_models, wavelength,
                                                              pixel)
            except KeyboardInterrupt:
                raise KeyboardInterrupt
            except Exception as error:
                if wavelength is None:
                    message = "({}, {}) : ".format(pixel[0], pixel[1]) + str(error)
                else:
                    message = ("({}, {}), : {} nm : ".format(pixel[0], pixel[1],
                                                             wavelength) + str(error))
                log.error(message)
                raise error
        # update progress bar
        self._update_progress(finish=True, verbose=verbose)

    def fit_calibrations(self, pixels=None, wavelengths=None, verbose=True):
        """
        Fit the phase to energy calibration for the detector by using the centers of each
        histogram fit.

        Args:
            pixels: list of (x, y) coordinates to compute calibrations for. If None, all
                    pixels in the array are used.
            wavelengths: a list of wavelengths to compute calibrations for. If None, all
                         wavelengths specified in the configuration object are used.
            verbose: a boolean specifying whether to print a progress bar to the stdout.
        """
        # check inputs and setup progress bar
        pixels, wavelengths = self._setup(pixels, wavelengths)
        self._update_progress(number=pixels.shape[1], initialize=True, verbose=verbose)
        for pixel in pixels.T:
            try:
                # update progress bar
                self._update_progress(verbose=verbose)
                model = self.solution.calibration_model(pixel=pixel)
                # get data from histogram fits
                histogram_models = self.solution.histogram_models(wavelengths, pixel)
                good = self.solution.has_good_histogram_solutions(wavelengths, pixel)
                phases, variance, energies, sigmas = [], [], [], []
                for index, wavelength in enumerate(wavelengths):
                    if good[index]:
                        histogram_model = histogram_models[index]
                        phases.append(histogram_model.signal_center.value)
                        variance.append(histogram_model.signal_center.stderr**2)
                        energies.append(h.to('eV s').value * c.to('nm/s').value /
                                        wavelength)
                        sigmas.append(histogram_model.signal_sigma.value)
                # give data to model
                if variance:
                    model.x = np.array(phases)
                    model.y = np.array(energies)
                    model.variance = np.array(variance)
                    arg_min = np.argmin(model.x)
                    arg_max = np.argmax(model.x)
                    model.min_x = model.x[arg_min] - 3 * np.sqrt(sigmas[arg_min])
                    model.max_x = model.x[arg_max] + 3 * np.sqrt(sigmas[arg_max])
                # don't fit if there's not enough data
                if len(variance) < 3:
                    model.flag = 11
                    message = ("({}, {}) : {} data points is not enough to make a "
                               "calibration")
                    log.debug(message.format(pixel[0], pixel[1], len(variance)))
                    continue
                diff = np.diff(phases)
                sigma = np.sqrt(variance)
                if (diff < -4 * (sigma[:-1] + sigma[1:])).any():
                    model.flag = 12
                    message = ("({}, {}) : fitted phase values are not monotonic enough "
                               "to make a calibration")
                    log.debug(message.format(pixel[0], pixel[1]))
                # fit the data
                message = "({}, {}) : beginning phase-energy calibration fitting"
                log.debug(message.format(pixel[0], pixel[1]))
                tried_models = []
                for calibration_model in self.solution.calibration_model_list:
                    # update the model if needed
                    if not isinstance(model, calibration_model):
                        model = self._update_calibration_model(calibration_model, pixel)
                    guess = model.guess()
                    model.fit(guess)
                    tried_models.append(model.copy())
                    if model.has_good_solution():
                        message = ("({}, {}) : phase-energy calibration fit successful "
                                   "with model '{}'")
                        log.debug(message.format(pixel[0], pixel[1],
                                                 type(model).__name__))
                # find model with the best fit and save that one
                self._assign_best_calibration_model(tried_models, pixel)
            except KeyboardInterrupt:
                raise KeyboardInterrupt
            except Exception as error:
                log.error("({}, {}) : ".format(pixel[0], pixel[1]) + str(error))
                raise error
        # update progress bar
        self._update_progress(finish=True, verbose=verbose)

    def _run(self, method, pixels=None, wavelengths=None, verbose=True, parallel=True,
             h5_safe=False):
        if parallel:
            self._parallel(method, pixels=pixels, wavelengths=wavelengths,
                           verbose=verbose, h5_safe=h5_safe)
        else:
            getattr(self, method)(pixels=pixels, wavelengths=wavelengths,
                                  verbose=verbose)

    def _parallel(self, method, pixels=None, wavelengths=None, verbose=True,
                  h5_safe=False):
        # configure number of processes
        n_data = pixels.shape[1]
        if h5_safe:
            cpu_count = np.min([len(wavelengths), np.ceil(mp.cpu_count() / 2)])
            cpu_count = cpu_count.astype(int)
            n_data *= len(wavelengths)
        else:
            cpu_count = np.ceil(mp.cpu_count() / 2).astype(int)
        self._max_queue_size = int(np.ceil(max(50, 750 / len(wavelengths))))
        # make input, output and progress queues
        workers = []
        progress_worker = None
        input_queues = []
        output_queue = mp.Queue(maxsize=self._max_queue_size)
        progress_queue = mp.Queue(maxsize=self._max_queue_size)
        if h5_safe:
            for _ in range(cpu_count):
                input_queues.append(mp.Queue(maxsize=self._max_queue_size))
        else:
            input_queues.append(mp.Queue(maxsize=self._max_queue_size))
        queue_length = len(input_queues)
        # make stopping events
        events = []
        for _ in range(cpu_count + 1):
            events.append(mp.Event())
        try:
            # make cpu_count number of workers to process the data
            for index in range(cpu_count):
                workers.append(Worker(self.cfg, method, events[index],
                                      input_queues[index % queue_length], output_queue,
                                      progress_queue))
            # make a worker to handle the progress bar and start the progress bar
            progress_worker = Worker(self.cfg, "_update_progress", events[-1],
                                     progress_queue)
            progress_queue.put({"number": n_data, "initialize": True, "verbose": verbose})

            # assign data to workers
            for pixel in pixels.T:
                if h5_safe:
                    for wavelength_index, wavelength in enumerate(wavelengths):
                        kwargs = {"pixel": pixel, "wavelengths": wavelength,
                                  "verbose": verbose}
                        input_queue = input_queues[wavelength_index % queue_length]
                        while input_queue.qsize() > self._max_queue_size / 2:
                            self._acquire_data(h5_safe, n_data, output_queue)
                        input_queue.put(kwargs)
                else:
                    fit_element = self.solution[pixel[0], pixel[1]]
                    kwargs = {"pixel": pixel, "wavelengths": wavelengths,
                              "fit_element": fit_element, "verbose": verbose}
                    while input_queues[0].qsize() > self._max_queue_size / 2:
                        self._acquire_data(h5_safe, n_data, output_queue)
                    input_queues[0].put(kwargs)
            # tell each worker to stop after all the data has been processed
            for index in range(cpu_count):
                input_queue = input_queues[index % queue_length]
                while input_queue.qsize() > self._max_queue_size / 2:
                    self._acquire_data(h5_safe, n_data, output_queue)
                input_queue.put({"stop": True})
            # collect data from workers and assign to solution
            while self._acquire_data(h5_safe, n_data, output_queue):
                pass
            # close processes when done
            for w in workers:
                w.join()
            progress_queue.put({"finish": True, "verbose": verbose})
            progress_queue.put({"stop": True})
            progress_worker.join()
        except KeyboardInterrupt:
            self._clean_up(input_queues, output_queue, progress_queue, workers,
                           progress_worker, events, h5_safe, n_data)
            raise KeyboardInterrupt

    def _remove_tail_riding_photons(self, photon_list):
        indices = np.argsort(photon_list['Time'])
        photon_list = photon_list[indices]

        logic = np.hstack([True, np.diff(photon_list['Time']) > self.cfg.dt])
        photon_list = photon_list[logic]
        return photon_list

    def _histogram(self, phase_list):
        # initialize variables
        min_phase = np.min(phase_list)
        max_phase = np.max(phase_list)
        max_count = 0
        update = 0
        centers = None
        counts = None
        # make histogram
        while max_count < 400 and update < 3:
            # update bin_width
            bin_width = self.cfg.bin_width * (2 ** update)

            # define bin edges being careful to start at the threshold cut
            bin_edges = np.arange(max_phase, min_phase - bin_width,
                                  -bin_width)[::-1]

            # make histogram
            counts, x0 = np.histogram(phase_list, bins=bin_edges)
            centers = (x0[:-1] + x0[1:]) / 2.0

            # update counters
            max_count = np.max(counts)
            update += 1

        return centers, counts

    def _update_histogram_model(self, wavelength, histogram_model_class, pixel):
        model = self.solution.histogram_models(wavelength, pixel)[0]
        # save old data
        x = model.x
        y = model.y
        variance = model.variance
        saved_pixel = model.pixel
        res_id = model.res_id
        # swap model
        model = histogram_model_class(pixel=saved_pixel, res_id=res_id)
        self.solution.set_histogram_models(model, wavelength, pixel)
        # set new data
        model.x = x
        model.y = y
        model.variance = variance
        return model

    def _update_calibration_model(self, calibration_model_class, pixel):
        # save old data
        model = self.solution.calibration_model(pixel=pixel)
        x = model.x
        y = model.y
        variance = model.variance
        saved_pixel = model.pixel
        res_id = model.res_id
        min_x = model.min_x
        max_x = model.max_x
        # swap model
        model = calibration_model_class(pixel=saved_pixel, res_id=res_id)
        self.solution.set_calibration_model(model, pixel=pixel)
        # set new data
        model.x = x
        model.y = y
        model.variance = variance
        model.min_x = min_x
        model.max_x = max_x
        return model

    def _guess(self, pixel, wavelength_index, good_solutions):
        """If there are any good fits for this pixel intelligently guess the
        signal_center parameter and set the other parameters equal to the average of
        those in the good fits."""
        # get initial guess
        wavelengths = self.cfg.wavelengths
        histogram_models = self.solution.histogram_models(pixel=pixel)
        parameters = self.solution.histogram_parameters(pixel=pixel)
        model = histogram_models[wavelength_index]
        guess = model.guess()
        # get index of closest shorter wavelength good solution
        shorter_index = None
        for index, good in enumerate(good_solutions[:wavelength_index]):
            if good:
                shorter_index = index
        # get index of closest longer wavelength good solution
        longer_index = None
        for index, good in enumerate(good_solutions[wavelength_index + 1:]):
            if good:
                longer_index = wavelength_index + 1 + index
                break
        # get data from shorter fit
        if shorter_index is not None:
            shorter_model = histogram_models[shorter_index]
            shorter_params = parameters[shorter_index]
            shorter_center = (shorter_model.signal_center.value *
                              wavelengths[shorter_index] / wavelengths[wavelength_index])
            shorter_guesses = {}
            if isinstance(shorter_model, type(model)):
                for parameter in shorter_params.values():
                    if parameter.name != shorter_model.signal_center.name:
                        shorter_guesses.update({parameter.name: parameter.value})
        else:
            shorter_center = None
            shorter_guesses = {}
        # get data from longer fit
        if longer_index is not None:
            longer_model = histogram_models[longer_index]
            longer_params = parameters[longer_index]
            longer_center = (longer_model.signal_center.value *
                             wavelengths[longer_index] / wavelengths[wavelength_index])
            longer_guesses = {}
            if isinstance(longer_model, type(model)):
                for parameter in longer_params.values():
                    if parameter.name != longer_model.signal_center.name:
                        longer_guesses.update({parameter.name: parameter.value})
        else:
            longer_center = None
            longer_guesses = {}
        if shorter_index is None and longer_index is None:
            # should never happen
            raise RuntimeError("There were no good solutions to base a fit guess on.")

        # set center parameter
        if shorter_center is not None and longer_center is not None:
            guess[model.signal_center.name].set(
                value=np.mean([shorter_center, longer_center]))
        elif shorter_center is not None:
            guess[model.signal_center.name].set(value=shorter_center)
        elif longer_center is not None:
            guess[model.signal_center.name].set(value=longer_center)
        # set other parameters
        for parameter in guess.values():
            name = parameter.name
            if name in shorter_guesses.keys() and name in longer_guesses.keys():
                guess[name].set(value=np.mean([longer_guesses[name],
                                               shorter_guesses[name]]))
            elif name in shorter_guesses.keys():
                guess[name].set(value=shorter_guesses[name])
            elif name in longer_guesses.keys():
                guess[name].set(value=longer_guesses[name])

        return guess

    def _assign_best_histogram_model(self, tried_models, wavelength, pixel):
        best_model = tried_models[0]
        lowest_aic_model = tried_models[0]
        for model in tried_models[1:]:
            lower_aic = model.best_fit_result.aic < best_model.best_fit_result.aic
            good_fit = model.has_good_solution()
            if lower_aic and good_fit:
                best_model = model
            if lower_aic:
                lowest_aic_model = model

        if best_model.has_good_solution():
            best_model.flag = 0
            self.solution.set_histogram_models(best_model, wavelength, pixel=pixel)
            message = ("({}, {}) : {} nm : histogram model '{}' chosen as "
                       "the best successful fit")
            log.debug(message.format(pixel[0], pixel[1], wavelength,
                                     type(best_model).__name__))
        else:
            if not lowest_aic_model.best_fit_result.success:
                lowest_aic_model.flag = 6  # did not converge
            else:
                lowest_aic_model.flag = 7  # converged but failed validation
            self.solution.set_histogram_models(lowest_aic_model, wavelength, pixel=pixel)
            message = ("({}, {}) : {} nm : histogram fit failed with all "
                       "models")
            log.debug(message.format(pixel[0], pixel[1], wavelength))

    def _assign_best_calibration_model(self, tried_models, pixel):
        best_model = tried_models[0]
        lowest_aic_model = tried_models[0]
        for model in tried_models[1:]:
            lower_aic = model.best_fit_result.aic < best_model.best_fit_result.aic
            good_fit = model.has_good_solution()
            if lower_aic and good_fit:
                best_model = model
            if lower_aic:
                lowest_aic_model = model
        if best_model.has_good_solution():
            best_model.flag = 10
            self.solution.set_calibration_model(best_model, pixel=pixel)
            message = ("({}, {}) : energy-phase calibration model '{}' chosen as "
                       "the best successful fit")
            log.debug(message.format(pixel[0], pixel[1], type(best_model).__name__))
        else:
            if not lowest_aic_model.best_fit_result.success:
                lowest_aic_model.flag = 13  # did not converge
            else:
                lowest_aic_model.flag = 14  # converged but failed validation
            self.solution.set_calibration_model(lowest_aic_model, pixel=pixel)
            message = "({}, {}) : energy-phase calibration fit failed with all models"
            log.debug(message.format(pixel[0], pixel[1]))

    def _update_progress(self, number=None, initialize=False, finish=False, verbose=True):
        if verbose:
            if initialize:
                percentage = pb.Percentage()
                bar = pb.Bar()
                timer = pb.Timer()
                eta = pb.ETA()
                self.progress = pb.ProgressBar(widgets=[percentage, bar, '  (',
                                                        timer, ') ', eta, ' '],
                                               max_value=number).start()
                self.progress_iteration = -1
            elif finish:
                self.progress_iteration += 1
                self.progress.update(self.progress_iteration)
                self.progress.finish()
            else:
                self.progress_iteration += 1
                self.progress.update(self.progress_iteration)

    def _setup(self, pixels, wavelengths):
        # check inputs
        pixels = self.solution._parse_pixels(pixels)
        wavelengths = self.solution._parse_wavelengths(wavelengths)
        return pixels, wavelengths

    def _acquire_data(self, h5_safe, n_data, output_queue):
        if self._acquired == n_data:
            not_complete = False
            self._acquired = 0
        else:
            not_complete = True
            result = output_queue.get()
            computed_pixel = result["pixel"]
            computed_wavelengths = result["wavelengths"]
            fit_element = result["fit_element"]
            if h5_safe:
                dictionary = self.solution[computed_pixel[0], computed_pixel[1]]
                histogram_models = dictionary['histograms']
                for wavelength in computed_wavelengths:
                    logic = (wavelength == self.cfg.wavelengths)
                    model = fit_element['histograms'][logic][0]
                    histogram_models[logic] = model
            else:
                self.solution[computed_pixel[0], computed_pixel[1]] = fit_element
            self._acquired += 1
        return not_complete

    def _clean_up(self, input_queues, output_queue, progress_queue, workers,
                  progress_worker, events, h5_safe, n_data):
        # send an extra stop flag just in case
        for index, input_queue in enumerate(input_queues):
            while input_queue.qsize() > max(0, self._max_queue_size - 2):
                input_queue.get()
            input_queue.put({"stop": True})
        progress_queue.put({"stop": True})
        # check that all process have stopped
        finished = False
        while not finished:
            finished_list = [event.is_set() for event in events]
            finished = len(finished_list) == 0 or np.all(finished_list)
        # add sentinels to queues
        for input_queue in input_queues:
            input_queue.put(None)
        while output_queue.qsize() > max(0, self._max_queue_size - 1):
            # grab the last bit of data so that there is room in the queue for closing
            self._acquire_data(h5_safe, n_data, output_queue)
        output_queue.put(None)
        progress_queue.put(None)
        # clean up input queues
        for input_queue in input_queues:
            while input_queue.get() is not None:
                pass
            input_queue.close()
        # clean up output queue
        while output_queue.get() is not None:
            pass
        output_queue.close()
        # close progress queue
        while progress_queue.get() is not None:
            pass
        progress_queue.close()
        # let all processes finish
        for event in events:
            event.set()
        # close and join workers
        for worker in workers:
            log.info("PID {0} ... exiting".format(worker.pid))
            worker.terminate()
            worker.join()
        if progress_worker is not None:
            log.info("PID {0} ... exiting".format(progress_worker.pid))
            progress_worker.terminate()
            progress_worker.join()


class Worker(mp.Process):
    """Worker class for running methods in the wavelength calibration in parallel."""
    def __init__(self, configuration, method, event, input_queue, output_queue=None,
                 progress_queue=None):
        super(Worker, self).__init__()
        self.calibrator = Calibrator(configuration)
        self.method = method
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.progress_queue = progress_queue
        self.finished = event
        self.start()

    def run(self):
        """This method gets called on the instantiation of the object."""
        try:
            while not self.finished.is_set():
                # get next bit of data to analyze
                kwargs = self.input_queue.get()
                # check for stopping condition
                stop = kwargs.pop("stop", False)
                if stop:
                    self.finished.set()
                else:
                    # pop kwargs that may not be arguments to the requested method
                    fit_element = kwargs.pop("fit_element", False)
                    pixel = kwargs.pop("pixel", False)
                    wavelengths = kwargs.pop("wavelengths", False)
                    # get the verbose keyword to defer to the progress worker
                    verbose = kwargs.get("verbose", False)

                    # supplying pixel means we are running one of the three main methods
                    if pixel is not False:
                        pixel, wavelengths = self.calibrator._setup(pixel, wavelengths)
                        kwargs['pixels'] = pixel
                        kwargs['wavelengths'] = wavelengths
                        kwargs['verbose'] = False

                    # supplying fit_element means we are overloading the local fit element
                    if fit_element is not False:
                        self.calibrator.solution[pixel[0], pixel[1]] = fit_element

                    # run the requested method
                    getattr(self.calibrator, self.method)(**kwargs)

                    # output data into queue if we are running one of the main methods
                    if pixel is not False and self.output_queue is not None:
                        fit_element = self.calibrator.solution[pixel[0], pixel[1]]
                        self.output_queue.put({"pixel": pixel, "wavelengths": wavelengths,
                                               "fit_element": fit_element})
                        self.progress_queue.put({"verbose": verbose})
        except KeyboardInterrupt:
            self.finished.set()
            self.finished.wait()
        except Exception as error:
            self.finished.set()
            raise error


class Solution(object):
    """Solution class for the wavelength calibration. Initialize with either the file_name
    argument or both the fit_array and configuration arguments."""
    def __init__(self, file_path=None, fit_array=None, configuration=None, beam_map=None,
                 beam_map_flags=None):
        # load in solution and configuration objects
        if fit_array is not None and configuration is not None and beam_map is not None:
            self._fit_array = fit_array
            self.cfg = configuration
            # TODO: integrate beam map object
            self._beam_map = beam_map
            self._beam_map_flags = beam_map_flags
        elif file_path is not None:
            self.load(file_path)
        else:
            message = ('provide either a file_path or both the fit_array and '
                       'configuration arguments')
            raise ValueError(message)

        # load in fitting models
        self.histogram_model_list = [getattr(wm, name) for _, name in
                                     enumerate(self.cfg.histogram_model_names)]
        self.calibration_model_list = [getattr(wm, name) for _, name in
                                       enumerate(self.cfg.calibration_model_names)]

        self._parse = True
        self._color_map = cm.get_cmap('viridis')
        x_pixels = np.indices(self.beam_map.shape)[0, :, :].ravel()
        y_pixels = np.indices(self.beam_map.shape)[1, :, :].ravel()
        lookup_table = np.array([self.beam_map.ravel(), x_pixels, y_pixels])
        lookup_table = lookup_table[:, lookup_table[0, :].argsort()]
        res_ids = np.arange(0, lookup_table[0, :].max() + 1)
        self._reverse_beam_map = np.zeros((2, res_ids.size), dtype=int)
        indices = np.searchsorted(res_ids, lookup_table[0, :])
        self._reverse_beam_map[:, indices] = lookup_table[1:, :]
        self._type = np.vectorize(type, otypes=[str])

    def __getitem__(self, key):
        results = np.atleast_2d(self._fit_array[key])
        empty = (results == np.array([None]))
        if empty.any():
            new_key = key
            if not isinstance(key[0], slice):
                new_key = (slice(new_key[0], new_key[0] + 1), new_key[1])
            if not isinstance(key[1], slice):
                new_key = (new_key[0], slice(new_key[1], new_key[1] + 1))
            start0 = new_key[0].start if new_key[0].start is not None else 0
            start1 = new_key[1].start if new_key[1].start is not None else 0
            step0 = new_key[0].step if new_key[0].step is not None else 1
            step1 = new_key[1].step if new_key[1].step is not None else 1
            for index, entry in np.ndenumerate(results):
                if empty[index]:
                    pixel = np.array((index[0] * step0 + start0,
                                      index[1] * step1 + start1)).squeeze()
                    pixel = (pixel[0], pixel[1])  # ensures pixel is a tuple of integers
                    res_id = self.beam_map[pixel[0], pixel[1]]
                    histogram_models = np.array(
                        [self.histogram_model_list[0](pixel=pixel, res_id=res_id)
                         for _ in range(len(self.cfg.wavelengths))])
                    calibration_model = self.calibration_model_list[0](pixel=pixel,
                                                                       res_id=res_id)
                    self._fit_array[pixel] = {'histograms': histogram_models,
                                              'calibration': calibration_model}
        results = self._fit_array[key]
        if isinstance(results, np.ndarray) and results.size == 1:
            results = results[0]
        return results

    def __setitem__(self, key, value):
        self._fit_array[key] = value

    def save(self, save_name=None):
        """Save the solution to a file whose name is determined by the configuration."""
        # TODO: saving and loading is slow: only save parts needed to recreate solution
        if save_name is None:
            save_path = os.path.join(self.cfg.out_directory, self.cfg.solution_name)
        else:
            save_path = os.path.join(self.cfg.out_directory, save_name)
        # make sure the configuration is pickleable if created from __main__
        if self.cfg.__class__.__module__ == "__main__":
            from mkidpipeline.calibration.wavecal import Configuration
            self.cfg = Configuration(self.cfg.configuration_path,
                                     solution_name=self.cfg.solution_name)

        log.info("Saving solution to {}".format(save_path))
        np.savez(save_path, fit_array=self._fit_array,
                 configuration=self.cfg, beam_map=self._beam_map,
                 beam_map_flags=self._beam_map_flags)

    def load(self, file_path):
        log.info("Loading solution from {}".format(file_path))
        try:
            npz_file = np.load(file_path)
            self._fit_array = npz_file['fit_array']
            self.cfg = npz_file['configuration'].item()
            self._beam_map = npz_file['beam_map']
            self._beam_map_flags = npz_file['beam_map_flags']
        except (OSError, KeyError):
            message = "Failed to interpret '{}' as a wavecal solution object"
            raise OSError(message.format(file_path))

    @property
    def beam_map(self):
        """ResIDs for the x and y positions on the array (beam_map[x_ind, y_ind])."""
        return self._beam_map

    @property
    def beam_map_flags(self):
        """Beam map flags for the x and y positions on the array
         (beam_map_flags[x_ind, y_ind])."""
        return self._beam_map_flags

    def resolving_powers(self, pixel=None, res_id=None, wavelengths=None):
        """
        Returns the resolving power for a resonator specified by either its pixel
        (x_cord, y_cord) or its res_id.

        Args:
            pixel: the x and y position pixel in question. Can use res_id keyword-arg
                   instead. (length 2 list of integers)
            res_id: the resonator ID for the pixel in question. Can use pixel keyword-arg
                    instead. (integer)
            wavelengths: list of wavelengths to report resolving powers for. All
                         resolving powers for valid wavelengths will be returned if it is
                         not specified
        Returns:
            resolving_powers: array of resolving powers of the same length as wavelengths
        """
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        self._parse = False
        resolving_powers = np.zeros((len(wavelengths),))
        good = self.has_good_histogram_solutions(wavelengths, pixel=pixel)
        if self.has_good_calibration_solution(pixel=pixel):
            calibration_function = self.calibration_function(pixel=pixel)
            models = self.histogram_models(wavelengths, pixel=pixel)
            for index, wavelength in enumerate(wavelengths):
                if good[index]:
                    fwhm = (calibration_function(models[index].nhm) -
                            calibration_function(models[index].phm))
                    energy = calibration_function(models[index].signal_center.value)
                    resolving_powers[index] = energy / fwhm
                else:
                    resolving_powers[index] = np.nan
        else:
            resolving_powers[:] = np.nan
        self._parse = True
        return resolving_powers

    def find_resolving_powers(self, wavelengths=None, minimum=None, maximum=None,
                              feedline=None):
        """
        Returns a tuple containing an array of resolving powers and a corresponding res_id
        array.

        Args:
            wavelengths: list of wavelengths to report resolving powers for. All
                         resolving powers for valid wavelengths will be returned if it is
                         not specified
            minimum: only report median resolving powers above this value. No lower bound
                     is used if it is not specified.
            maximum: only report median resolving powers below this value. No upper bound
                     is used if it is not specified.
            feedline: integer corresponding to the feedline from which to use. All
                      feedlines are used if it is not specified.
        Returns:
            resolving_powers: an MxN array of resolving powers where M is the number of
                              res_ids the search criterion and N is the number of
                              wavelengths requested.
        """
        wavelengths = self._parse_wavelengths(wavelengths)
        res_ids = self._parse_res_ids()
        pixels, _ = self._parse_resonators(res_ids=res_ids)
        resolving_powers = np.empty((res_ids.size, len(wavelengths)))
        resolving_powers.fill(np.nan)
        for index, pixel in enumerate(pixels.T):
            in_feedline = (np.floor(res_ids[index] / 10000) == feedline
                           if feedline is not None else True)
            if in_feedline:
                r = self.resolving_powers(pixel=pixel, wavelengths=wavelengths)
                resolving_powers[index, :] = r
        with warnings.catch_warnings():
            # rows with all nan values will give an unnecessary RuntimeWarning
            warnings.simplefilter("ignore", category=RuntimeWarning)
            r_median = np.nanmedian(resolving_powers, axis=1)

        # find resolving powers
        if minimum is None and maximum is None:
            logic = slice(None)
        else:
            if minimum is None:
                minimum = -np.inf
            if maximum is None:
                maximum = np.inf
            with warnings.catch_warnings():
                # rows with all nan values will give an unnecessary RuntimeWarning
                warnings.simplefilter("ignore", category=RuntimeWarning)
                logic = np.logical_and(r_median >= minimum, r_median <= maximum)
        resolving_powers = resolving_powers[logic, :]
        res_ids = res_ids[logic]

        # remove res_ids with no resolving powers at any wavelength
        no_resolving_power = np.logical_not(np.isnan(resolving_powers).all(axis=1))
        res_ids = res_ids[no_resolving_power]
        resolving_powers = resolving_powers[no_resolving_power, :]

        # sort in descending order by resolving power
        sorted_indices = np.argsort(np.nanmedian(resolving_powers, axis=1))[::-1]
        resolving_powers = resolving_powers[sorted_indices, :]
        res_ids = res_ids[sorted_indices]

        return resolving_powers, res_ids

    def responses(self, pixel=None, res_id=None, wavelengths=None):
        """
        Returns the model phase distribution centers for a resonator specified by either
        its pixel (x_cord, y_cord) or its res_id.

        Args:
            pixel: the x and y position pixel in question. Can use res_id keyword-arg
                   instead. (length 2 list of integers)
            res_id: the resonator ID for the pixel in question. Can use pixel keyword-arg
                    instead. (integer)
            wavelengths: list of wavelengths to report resolving powers for. All
                         responses for valid wavelengths will be returned if it is
                         not specified
        Returns:
            responses: array of responses of the same length as wavelengths
        """
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        self._parse = False
        responses = np.zeros((len(wavelengths),))
        good = self.has_good_histogram_solutions(wavelengths, pixel=pixel)
        if self.has_good_calibration_solution(pixel=pixel):
            models = self.histogram_models(wavelengths, pixel=pixel)
            for index, wavelength in enumerate(wavelengths):
                if good[index]:
                    responses[index] = models[index].signal_center.value
                else:
                    responses[index] = np.nan
        else:
            responses[:] = np.nan
        self._parse = True
        return responses

    def find_responses(self, wavelengths=None, feedline=None):
        """
        Returns a tuple containing an array of model phase distribution centers and a
        corresponding res_id array.

        Args:
            wavelengths: list of wavelengths to report resolving powers for. All
                         resolving powers for valid wavelengths will be returned if it is
                         not specified
            feedline: integer corresponding to the feedline from which to use. All
                      feedlines are used if it is not specified.
        Returns:
            responses: an MxN array of responses where M is the number of res_ids the
                       search criterion and N is the number of wavelengths requested.
        """
        wavelengths = self._parse_wavelengths(wavelengths)
        res_ids = self._parse_res_ids()
        pixels, _ = self._parse_resonators(res_ids=res_ids)
        responses = np.empty((res_ids.size, len(wavelengths)))
        responses.fill(np.nan)
        for index, pixel in enumerate(pixels.T):
            in_feedline = (np.floor(res_ids[index] / 10000) == feedline
                           if feedline is not None else True)
            if in_feedline:
                r = self.responses(pixel=pixel, wavelengths=wavelengths)
                responses[index, :] = r

        # remove res_ids with no responses at any wavelength
        no_response = np.logical_not(np.isnan(responses).all(axis=1))
        res_ids = res_ids[no_response]
        responses = responses[no_response, :]

        # sort in ascending order by response
        sorted_indices = np.argsort(np.nanmedian(responses, axis=1))
        responses = responses[sorted_indices, :]
        res_ids = res_ids[sorted_indices]

        return responses, res_ids

    def set_calibration_model(self, model, pixel=None, res_id=None):
        """Set the calibration model to model for the specified resonator."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        model = self._parse_models(model, 'calibration')[0]
        self[pixel[0], pixel[1]]['calibration'] = model

    def calibration_model(self, pixel=None, res_id=None):
        """Returns the model used for the calibration fit for a particular resonator."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        return self[pixel[0], pixel[1]]['calibration']

    def calibration_parameters(self, pixel=None, res_id=None):
        """Returns the fit parameters for the calibration solution for a particular
         resonator."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        model = self.calibration_model(pixel=pixel)
        return model.best_fit_result.params

    def calibration_model_name(self, pixel=None, res_id=None):
        """Returns the name of the model used for the calibration fit for a particular
        resonator."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        model = self.calibration_model(pixel=pixel)
        return type(model).__name__

    def calibration_function(self, pixel=None, res_id=None):
        """Returns a function of one argument that converts phase to fitted energy for a
        particular resonator."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        model = self.calibration_model(pixel=pixel)

        return model.calibration_function

    def calibration(self, pixel=None, res_id=None):
        """Returns a tuple of the  phases, energies, and phase errors (1 sigma) data
        points for a particular resonator. Only includes points that have good histogram
        fits."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        model = self.calibration_model(pixel=pixel)
        return model.x, model.y, np.sqrt(model.variance)

    def has_good_calibration_solution(self, pixel=None, res_id=None):
        """Returns True if the resonator has a good wavelength calibration fit. Returns
         False otherwise."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        if not isinstance(self._fit_array[pixel[0], pixel[1]][0], dict):
            return False
        model = self.calibration_model(pixel=pixel)
        return model.has_good_solution()

    def calibration_flag(self, pixel=None, res_id=None):
        """Returns the numeric flag corresponding to the wavecal fit condition for a
        particular resonator."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        model = self.calibration_model(pixel=pixel)
        return model.flag

    def set_histogram_models(self, models, wavelengths=None, pixel=None, res_id=None):
        """Set the histogram models to models for a particular resonator at the specified
        wavelengths."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self._parse_models(models, 'histograms', wavelengths=wavelengths)
        logic = (wavelengths == self.cfg.wavelengths)
        self[pixel[0], pixel[1]]['histograms'][logic] = models

    def histogram_models(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a numpy array of models used for the histogram fit for a particular
        resonator at the specified wavelengths wavelength."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        logic = (wavelengths == self.cfg.wavelengths)
        models = self[pixel[0], pixel[1]]['histograms'][logic]
        return models

    def histogram_parameters(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a numpy array of the fit parameters for the histogram solutions for a
        particular resonator."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self.histogram_models(wavelengths, pixel=pixel)
        parameters = np.array([model.best_fit_result.params
                               if model.best_fit_result is not None else None
                               for model in models], dtype=object)
        return parameters

    def histogram_model_names(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a numpy array of the names of the models used for the histogram fits
        for a particular resonator at the specified wavelengths."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self.histogram_models(wavelengths, pixel=pixel)
        names = np.array([type(model).__name__ for model in models])
        return names

    def histogram_functions(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a numpy array of functions of one argument that convert phase to fitted
        histogram counts for a particular resonator at the specified wavelengths."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self.histogram_models(wavelengths, pixel=pixel)
        functions = np.array([model.histogram_function for model in models])
        return functions

    def histograms(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a numpy array of histogram tuples (bin centers, counts) for a
        particular resonator at the specified wavelengths."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self.histogram_models(wavelengths, pixel=pixel)
        data = np.empty(models.shape, dtype=object)
        for index, model in enumerate(models):
            data[index] = (model.x, model.y)
        return data

    def has_good_histogram_solutions(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a boolean numpy array. Each element is True if the resonator has a good
        histogram fit and False otherwise for the corresponding wavelength.
        """
        pixel, _ = self._parse_resonators(pixel, res_id)
        if not isinstance(self._fit_array[pixel[0], pixel[1]][0], dict):
            return False
        models = self.histogram_models(wavelengths, pixel=pixel)
        good = np.array([model.has_good_solution() for model in models])
        return good

    def has_data(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a boolean numpy array. Each element is True if the resonator has
        histogram data computed for it and False otherwise for the corresponding
        wavelength."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        if not isinstance(self._fit_array[pixel[0], pixel[1]][0], dict):
            return False
        models = self.histogram_models(wavelengths, pixel=pixel)
        data = np.array([model.x is not None and model.y is not None for model in models])
        return data

    def histogram_flags(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a numpy array of numeric flags corresponding to the histogram fit
        condition for a particular resonator at the specified wavelengths."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self.histogram_models(wavelengths, pixel=pixel)
        flags = np.array([model.flag for model in models])
        return flags

    def bin_widths(self, wavelengths=None, pixel=None, res_id=None):
        """Returns a numpy array of the histogram bin widths for a particular resonator at
        a the specified wavelengths."""
        pixel, _ = self._parse_resonators(pixel, res_id)
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self.histogram_models(wavelengths, pixel=pixel)
        widths = np.array([np.abs(np.diff(model.x))[0] for model in models])
        return widths

    def plot_calibration(self, axes=None, pixel=None, res_id=None, r_text=True,
                         **model_kwargs):
        """
        Plot the phase to energy calibration for a pixel from this solution object.
        Provide either the pixel location pixel=(x_coord, y_coord) or the res_id for the
        resonator.

        Args:
            axes: matplotlib Axes object on which to display the plot. If no axes object
                  is provided a new figure will be made.
            pixel: the x and y position for the plotted pixel. Can use res_id
                   keyword-arg instead. (length 2 list of integers)
            res_id: the resonator ID for the plotted pixel. Can use pixel keyword-arg
                    instead. (integer)
            r_text: optional boolean that controls whether information about the median
                    energy resolution is added to the plot
            model_kwargs: options to be passed to the model plot function
        Returns:
            axes: a matplotlib Axes object
        """
        pixel, _ = self._parse_resonators(pixel, res_id)
        message = "plotting calibration fit for pixel ({}, {})"
        log.debug(message.format(pixel[0][0], pixel[1][0]))
        model = self.calibration_model(pixel=pixel)
        axes = model.plot(axes=axes, **model_kwargs)
        if r_text:
            x_limit = axes.get_xlim()
            y_limit = axes.get_ylim()
            dx = x_limit[1] - x_limit[0]
            dy = y_limit[1] - y_limit[0]
            r = self.resolving_powers(pixel=pixel)
            with warnings.catch_warnings():
                # all nan values will give an unnecessary RuntimeWarning
                warnings.simplefilter("ignore", category=RuntimeWarning)
                r = np.nanmedian(r)
            text = model_kwargs.get("text", True)
            if text:
                position = 0.06
            else:
                position = 0.01
            axes.text(x_limit[0] + 0.01 * dx, y_limit[1] - position * dy,
                      "Median R = {0}".format(round(r, 2)), ha='left', va='top')
        return axes

    def plot_histograms(self, axes=None, pixel=None, res_id=None, wavelengths=None,
                        squeeze=False, **model_kwargs):
        """
        Plot the histogram fits for a pixel from this solution object. Provide either the
        pixel location pixel=(x_coord, y_coord) or the res_id for the resonator.

        Args:
            axes: matplotlib Axes object on which to display the plot. If no axes object
                  is provided a new figure will be made. If more than one wavelength is
                  requested, the figure associated with the axes will be cleared and used
                  for the required subplots.
            pixel: the x and y position for the plotted pixel. Can use res_id
                   keyword-arg instead. (length 2 list of integers)
            res_id: the resonator ID for the plotted pixel. Can use pixel keyword-arg
                    instead. (integer)
            wavelengths: list of wavelengths to plot. All are plotted if not specified.
            squeeze: optional boolean controlling whether the returned axes array is
                     squeezed
            model_kwargs: options to be passed to the model plot function
        Returns:
            axes: a matplotlib Axes object or an array of Axes objects
        """
        pixel, res_id = self._parse_resonators(pixel, res_id, return_res_ids=True)
        message = "plotting phase response histogram for pixel ({}, {})"
        log.debug(message.format(pixel[0][0], pixel[1][0]))
        wavelengths = self._parse_wavelengths(wavelengths)
        models = self.histogram_models(wavelengths, pixel=pixel)
        # just output the model plot if only one wavelength requested
        if wavelengths.size == 1:
            return models[0].plot(axes=axes, **model_kwargs)

        # determine geometry
        share_x = False
        share_y = False
        if len(wavelengths) > 6:
            n_rows = 3
            share_x = 'col'
            share_y = 'row'
        elif len(wavelengths) < 3:
            n_rows = 1
        else:
            n_rows = 2
        n_cols = int(np.ceil(len(wavelengths) / n_rows))
        figure_size = (4 * n_cols, 3 * n_rows)

        # setup subplots
        subplot_kwargs = {'nrows': n_rows,  # number of rows in the axes grid
                          'ncols': n_cols,  # number of columns in the axes grid
                          'sharex': share_x,  # sets behavior of the x axis
                          'sharey': share_y,  # sets behavior of the y axis
                          'figsize': figure_size,  # only used if figure hasn't been made
                          'squeeze': False}  # defer squeezing output array
        if axes is not None:
            figure_number = axes.figure.number
            subplot_kwargs['num'] = figure_number  # identifies current figure
            subplot_kwargs['clear'] = True  # clears current figure
        _, axes_grid = plt.subplots(**subplot_kwargs)

        # turn off some model plot defaults unless specified originally
        model_kwargs['text'] = model_kwargs.get('text', False)
        model_kwargs['legend'] = model_kwargs.get('legend', False)
        model_kwargs['title'] = model_kwargs.get('title', False)
        model_kwargs['x_label'] = model_kwargs.get('x_label', False)
        model_kwargs['y_label'] = model_kwargs.get('y_label', False)

        # add the plots to the subplots
        for index, axes in np.ndenumerate(axes_grid):
            linear_index = np.ravel_multi_index(index, dims=axes_grid.shape)
            if linear_index >= len(wavelengths):
                continue
            axes = models[linear_index].plot(axes=axes, **model_kwargs)
            if model_kwargs['text']:
                position = 0.08
            else:
                position = 0.01
            x_limit = axes.get_xlim()
            y_limit = axes.get_ylim()
            dx = x_limit[1] - x_limit[0]
            dy = y_limit[1] - y_limit[0]
            axes.text(x_limit[0] + 0.01 * dx, y_limit[1] - position * dy,
                      "{} nm".format(wavelengths[linear_index]), ha='left', va='top')

        # add figure labels
        rect = [.02, .05, .98, .95]
        axes_grid[0, 0].figure.text(rect[0], 0.5, 'counts per bin width',
                                    va='center', ha='right', rotation='vertical')
        axes_grid[0, 0].figure.text(0.5, rect[1], 'phase [degrees]', ha='center',
                                    va='top')
        title = "Pixel ({}, {}) : ResID {}"
        axes_grid[0, 0].figure.suptitle(title.format(pixel[0][0], pixel[1][0], res_id))

        # configure plot legend
        fit_accepted = lines.Line2D([], [], color='green', label='fit accepted')
        fit_rejected = lines.Line2D([], [], color='red', label='fit rejected')
        axes_grid[0, 0].legend(handles=[fit_accepted, fit_rejected], loc=3,
                               bbox_to_anchor=(0, 1.02, 1, .102), ncol=2)

        plt.tight_layout(rect=rect)
        if squeeze:
            axes_grid.squeeze()
        return axes_grid

    def plot_r_histogram(self, axes=None, wavelengths=None, feedline=None, r=None):
        """
        Plot a histogram of the energy resolution, R, for each wavelength in the
        wavelength calibration solution object.

        Args:
            axes: matplotlib Axes object on which to display the plot. If no axes object
                  is provided a new figure will be made.
            wavelengths: a list of wavelengths to include in the plot.
                         The default is to use all.
            feedline: only resonators from this feedline will be used to make the plot.
                      All are used if set to None.
            r: a NxM array of resolving powers to histogram where M corresponds to the
               wavelengths list. If none, they are calculated from the solution object.
               feedline is ignored if used.
        Returns:
            axes: a matplotlib Axes object
        """
        log.debug("plotting resolving power histogram")
        # check inputs
        wavelengths = self._parse_wavelengths(wavelengths)
        # make sure r is defined
        if r is None:
            r, _ = self.find_resolving_powers(wavelengths=wavelengths, feedline=feedline)
        max_r = np.nanmax(r)
        # make sure axes is defined
        if axes is None:
            _, axes = plt.subplots()
        # make a color bar if there are a lot of wavelengths
        color_bar = len(wavelengths) >= 10
        if color_bar:
            self._plot_color_bar(axes, wavelengths)
        # plot each histogram
        max_counts = []
        for index, wavelength in enumerate(wavelengths):
            # pull out relevant data
            r_wavelength = r[:, index]
            r_wavelength = r_wavelength[np.logical_not(np.isnan(r_wavelength))]
            # histogram data
            counts, edges = np.histogram(r_wavelength, bins=30, range=(0, 1.1 * max_r))
            bin_widths = np.diff(edges)
            centers = edges[:-1] + bin_widths[0] / 2.0
            bins = centers
            # calculate median
            if len(r_wavelength) > 0:
                median = np.round(np.median(r_wavelength), 2)
            else:
                median = np.nan
            # plot histogram
            label = "{0} nm, Median R = {1}".format(wavelength, median)
            scale = ((1 / wavelength - 1 / np.max(wavelengths)) /
                     (1 / np.min(wavelengths) - 1 / np.max(wavelengths)))
            color = self._color_map(scale)
            axes.step(bins, counts, color=color, linewidth=2, label=label, where="mid")
            axes.axvline(x=median, linestyle='--', color=color, linewidth=2)
            max_counts.append(np.max(counts))
        # set up axis
        if np.max(max_counts) != 0:
            axes.set_ylim([0, 1.2 * np.max(max_counts)])
        axes.set_xlabel(r'R [E/$\Delta$E]')
        axes.set_ylabel('counts per bin width')
        # add legend if there's no color bar
        if not color_bar:
            axes.legend(fontsize=6)
        # tighten up plot
        plt.tight_layout()

        return axes

    def plot_response_histogram(self, axes=None, wavelengths=None, feedline=None,
                                responses=None):
        """
        Plot a histogram of the model phase distribution centers for the solution object.

        Args:
            axes: matplotlib Axes object on which to display the plot. If no axes object
                  is provided a new figure will be made.
            wavelengths: a list of wavelengths to include in the plot.
                         The default is to use all.
            feedline: only resonators from this feedline will be used to make the plot.
                      All are used if set to None.
            responses: a NxM array of responses to histogram where M corresponds to the
                       wavelengths list. If none, they are calculated from the solution
                       object. feedline is ignored if used.
        Returns:
            axes: a matplotlib Axes object
        """
        log.debug("plotting pixel response histogram")
        # check inputs
        wavelengths = self._parse_wavelengths(wavelengths)
        # get the phase distribution centers
        if responses is None:
            responses, _ = self.find_responses(wavelengths, feedline=feedline)

        # make sure axes is defined
        if axes is None:
            _, axes = plt.subplots()
        # make a color bar if there are a lot of wavelengths
        color_bar = (len(wavelengths) >= 10)
        if color_bar:
            self._plot_color_bar(axes, wavelengths)

        max_counts = []
        bin_width = 0
        for wavelength_index, wavelength in enumerate(wavelengths):
            # collect the responses for each wavelength
            wavelength_responses = responses[:, wavelength_index]
            logic = np.logical_not(np.isnan(wavelength_responses))
            wavelength_responses = wavelength_responses[logic]

            # make histogram
            counts, edges = np.histogram(wavelength_responses, bins=30, range=(-150, -20))
            bin_width = np.diff(edges)[0]
            bin_centers = edges[:-1] + bin_width / 2.0
            if len(bin_centers) > 0:
                median = np.round(np.median(wavelength_responses), 2)
            else:
                median = np.nan
            # plot data
            label = "{0} nm, Median = {1}".format(wavelength, median)
            scale = ((1 / wavelength - 1 / np.max(wavelengths)) /
                     (1 / np.min(wavelengths) - 1 / np.max(wavelengths)))
            color = self._color_map(scale)
            axes.step(bin_centers, counts, color=color, linewidth=2, where="mid",
                      label=label)
            axes.axvline(x=median, linestyle='--', color=color, linewidth=2)
            max_counts.append(np.max(counts))
        # fix y axis
        if np.max(max_counts) != 0:
            axes.set_ylim([0, 1.2 * np.max(max_counts)])
        # make legend
        if not color_bar:
            axes.legend(fontsize=6)
        # make axis labels
        axes.set_xlabel('gaussian center [degrees]')
        axes.set_ylabel('counts per {:.2f} degrees'.format(bin_width))
        plt.tight_layout()
        return axes

    def plot_r_vs_f(self, axes=None, feedline=None, r=None, res_ids=None):
        """
        Plot the median energy resolution over all wavelengths against the resonance
        frequency.

        Args:
            axes: matplotlib Axes object on which to display the plot. If no axes object
                  is provided a new figure will be made.
            feedline: only resonators from this feedline will be used to make the plot.
                      All are used if set to None. Ignored if r and res_ids is used.
            r: a NxM array of resolving powers to histogram where M corresponds to the
               wavelengths list. If none, they are calculated from the solution object.
               res_ids must also be specified. feedline is ignored if used.
            res_ids: an array of length N that corresponds to the array of resolving
                     powers. r must also be specified. feedline is ignored if used.
        Returns:
            axes: a matplotlib Axes object
        """
        log.debug("plotting r vs f scatter plot")
        # check inputs
        if (res_ids is None and r is not None) or (res_ids is not None and r is None):
            raise ValueError("either specify both r and res_ids or neither")
        # make sure r and res_ids are defined
        if r is None:
            r, res_ids = self.find_resolving_powers(feedline=feedline)
        # make sure axes is defined
        if axes is None:
            _, axes = plt.subplots()
        # load in the data
        try:
            data = self.load_frequency_files(self.cfg.templar_configuration_path)
        except RuntimeError:
            data = np.array([[np.nan, np.nan]])
        # find the median r values for plotting
        with warnings.catch_warnings():
            # rows with all nan values will give an unnecessary RuntimeWarning
            warnings.simplefilter("ignore", category=RuntimeWarning)
            r = np.nanmedian(r, axis=1)
        # match res_ids with frequencies
        frequencies = []
        resolutions = []
        for id_index, id_ in enumerate(res_ids):
            index = np.where(id_ == data[:, 0])
            no_duplicates = (len(index[0]) == 1)
            not_nan = not np.isnan(r[id_index])
            good_solution = self.has_good_calibration_solution(res_id=id_)
            if no_duplicates and not_nan and good_solution:
                frequencies.append(data[index, 1])
                resolutions.append(r[id_index])
        frequencies = np.ndarray.flatten(np.array(frequencies))
        resolutions = np.ndarray.flatten(np.array(resolutions))
        # sort the resolutions by frequency
        indices = np.argsort(frequencies)
        frequencies = frequencies[indices]
        resolutions = resolutions[indices]
        # filter the data
        window = 0.3e9  # 200 MHz
        r = np.zeros(resolutions.shape)
        for index, _ in enumerate(resolutions):
            points = np.where(np.logical_and(frequencies > frequencies[index] -
                                             window / 2,
                                             frequencies < frequencies[index] +
                                             window / 2))
            if len(points[0]) > 0:
                r[index] = np.median(resolutions[points])
            else:
                r[index] = 0

        # plot the result
        axes.plot(frequencies / 1e9, r, color='k', label='median')
        axes.scatter(frequencies / 1e9, resolutions, s=3)
        axes.set_xlabel('resonance frequency [GHz]')
        axes.set_ylabel(r'R [E/$\Delta$E]')
        axes.legend(fontsize=6)
        plt.tight_layout()
        return axes

    def plot_resolution_image(self, axes=None, wavelength=None, r=None, res_ids=None):
        """
        Plots an image of the array with the energy resolution as a color for this
        solution object.

        Args:
            axes: matplotlib Axes object on which to display the plot. If no axes object
                  is provided a new figure will be made.
            wavelength: a specific wavelength to plot data for. If used the plot will not
                        be interactive. Specify zero to plot a boolean mask of good/bad
                        pixels
            r: a NxM array of resolving powers to histogram where M corresponds to the
               wavelengths list in the configuration file. If none, they are calculated
               from the solution object. If wavelength is specified, M needs to be 1.
               res_ids must also be specified for this parameter to be used.
            res_ids: an array of length N that corresponds to the array of resolving
                     powers. r must also be specified for this parameter to be used.
        Returns:
            axes: a matplotlib Axes object
            indexer: indexing class. The class must be kept in the current name space for
                     the buttons to work. Use plt.show(block=True) if the code is run
                     using a script.
        """
        log.debug("plotting resolution image")
        # check inputs
        if (res_ids is None and r is not None) or (res_ids is not None and r is None):
            raise ValueError("either specify both r and res_ids or neither")
        # make sure r and res_ids are defined
        if r is None:
            r, res_ids = self.find_resolving_powers()
        # get pixels
        pixels, res_ids = self._parse_resonators(res_ids=res_ids)
        not_interactive = False if wavelength is None else True
        if wavelength == 0:
            wavelengths = []
            number = 2
        else:
            wavelengths = self._parse_wavelengths(wavelength)
            number = 11

        shape = self.beam_map.shape
        r_cube = np.zeros((len(wavelengths) + 1, shape[1], shape[0]))
        for index, pixel in enumerate(pixels.T):
            if self.has_good_calibration_solution(pixel=pixel):
                for w_index, wavelength in enumerate(wavelengths):
                    r_cube[w_index, pixel[1], pixel[0]] = r[index, w_index]
                r_cube[-1, pixel[1], pixel[0]] = 1
        r_cube[np.isnan(r_cube)] = 0

        if axes is None:
            _, axes = plt.subplots(figsize=(8, 8))
        image = axes.imshow(r_cube[0])
        divider = make_axes_locatable(axes)
        width = axes_size.AxesY(axes, aspect=1. / 20)
        pad = axes_size.Fraction(0.5, width)
        cax = divider.append_axes("right", size=width, pad=pad)
        maximum = np.max(r_cube)
        color_bar_ticks = np.linspace(0., maximum, num=number)
        color_bar = axes.figure.colorbar(image, cax=cax, ticks=color_bar_ticks)
        color_bar.set_clim(vmin=0, vmax=maximum)
        if wavelength == 0:
            label = "Good / Bad"
        else:
            label = "R [E/$\Delta$E]"
        cax.get_yaxis().labelpad = 15
        cax.set_ylabel(label, rotation=270)
        color_bar.draw_all()

        plt.tight_layout()
        if not_interactive:
            if wavelength == 0:
                title = "Wavelength Calibrated Pixels"
            else:
                title = "Wavelength is {} nm".format(wavelength)
            axes.set_title(title)
            return axes, None

        plt.subplots_adjust(bottom=0.15)
        position = axes.get_position()
        middle = position.x0 + 3 * position.width / 4
        ax_prev = plt.axes([middle - 0.18, 0.05, 0.15, 0.03])
        ax_next = plt.axes([middle + 0.02, 0.05, 0.15, 0.03])
        ax_slider = plt.axes([position.x0, 0.05, position.width / 2, 0.03])

        class Index(object):
            def __init__(self, slider_axes, previous_axes, next_axes):
                self.ind = 0
                self.num = len(wavelengths)
                self.button_next = Button(next_axes, 'Next')
                self.button_next.on_clicked(self.next)
                self.button_previous = Button(previous_axes, 'Previous')
                self.button_previous.on_clicked(self.prev)
                self.slider = Slider(slider_axes, "Energy Resolution: {:.2f} nm"
                                     .format(wavelengths[0]), 0, self.num, valinit=0,
                                     valfmt='%d')
                self.slider.valtext.set_visible(False)
                self.slider.label.set_horizontalalignment('center')
                self.slider.on_changed(self.update)

                self.slider.label.set_position((0.5, -0.5))
                self.slider.valtext.set_position((0.5, -0.5))

            def next(self, event):
                log.debug("next button pressed " + str(event))
                i = (self.ind + 1) % (self.num + 1)
                self.slider.set_val(i)

            def prev(self, event):
                log.debug("previous button pressed " + str(event))
                i = (self.ind - 1) % (self.num + 1)
                self.slider.set_val(i)

            def update(self, i):
                self.ind = int(i)
                image.set_data(r_cube[self.ind])
                if self.ind != len(wavelengths):
                    self.slider.label.set_text("Energy Resolution: {:.2f} nm"
                                               .format(wavelengths[self.ind]))
                    cax.set_ylabel("R [E/$\Delta$E]", rotation=270)
                else:
                    self.slider.label.set_text("Wavelength Calibrated Pixels")
                    cax.set_ylabel("Good / Bad", rotation=270)
                if self.ind != len(wavelengths):
                    color_bar.set_clim(vmin=0, vmax=maximum)
                    ticks = np.linspace(0., maximum, num=11, endpoint=True)
                else:
                    color_bar.set_clim(vmin=0, vmax=1)
                    ticks = np.linspace(0., 1, num=2)
                color_bar.set_ticks(ticks)
                color_bar.draw_all()
                plt.draw()

        indexer = Index(ax_slider, ax_prev, ax_next)
        return axes, indexer

    def plot_summary(self, axes=None, feedline=None, save_name=None,
                     resolution_images=True, use_latex=True):
        """
        Plot a summary of the wavelength calibration solution object.

        Args:
            axes: matplotlib Axes object on which to display the plot. The figure
                  associated with the axes will be cleared and used for the required
                  subplots.
            feedline: only resonators from this feedline will be used to make the plot.
                      All are used if set to None.
            save_name: name of the pdf that's saved. No pdf is saved if set to None.
            resolution_images: boolean which determines if additional resolution_image
                               plots should be appended to the pdf. The figure axes for
                               these plots are appended to the returned axes array.
            use_latex: a boolean turning on or off latex compilation. Text will not print
                       nicely without a latex install.
        Returns:
            axes: an array of Axes objects if no save name is provided
        Notes:
            If save_name is not provided the matplotlib.rcParams will remain changed to
            use latex formatting until the python session has closed. They can be reset
            using 'matplotlib.rcParams.update(matplotlib.rcParamsDefault)', but doing so
            will break the ability to show the summary plot.

            The code may still not use latex even if use_latex is True if it is determined
            that the latex distribution is not compatible. However, the code may not be
            able to determine latex compatibility in all cases, so set use_latex=False if
            you know that latex compilation will not work on your system.
        """
        log.debug("making summary plot")
        # reversibly configure matplotlib rc if we can use latex
        tex_installed = (find_executable('latex') is not None and
                         find_executable('dvipng') is not None and
                         find_executable('ghostscript') is not None)
        if not tex_installed:
            log.warning("latex not configured to work with matplotlib")
        use_latex = use_latex and tex_installed
        old_rc = matplotlib.rcParams.copy()
        if use_latex:
            matplotlib.rc('text', usetex=True)
            matplotlib.rc('text.latex', unicode=True)
            preamble = (r"\usepackage{array}"  # for raggedright tables
                        r"\renewcommand{\arraystretch}{1.15}"  # table spacing increase
                        r"\setlength{\parindent}{0cm}"  # no paragraph indent
                        r"\catcode`\_=12")  # escape underscores for file names
            matplotlib.rc('text.latex', preamble=preamble)

        # setup subplots
        figure_size = (8.5, 11)
        if axes is not None:
            figure = axes.figure
            figure.clear()
        else:
            figure = plt.figure(figsize=figure_size)
        gs = gridspec.GridSpec(3, 2)
        axes_list = np.array([figure.add_subplot(gs[0, 0]), figure.add_subplot(gs[1, 0]),
                              figure.add_subplot(gs[2, 0]), figure.add_subplot(gs[:, 1])])

        # pre-calculate resolving powers and detector responses
        r, res_ids_r = self.find_resolving_powers(feedline=feedline)
        a, _ = self.find_responses(feedline=feedline)
        all_res_ids = self._parse_res_ids()
        if feedline is not None:
            all_res_ids = all_res_ids[np.floor(all_res_ids / 10000) == feedline]

        # plot the results
        self.plot_r_vs_f(axes=axes_list[0], r=r, res_ids=res_ids_r)
        self.plot_r_histogram(axes=axes_list[1], r=r)
        self.plot_response_histogram(axes=axes_list[2], responses=a)
        # get info on the solution
        histogram_names = []
        calibration_names = []
        photosensitive = 0
        completely_successful = 0
        n_wavelengths = len(self.cfg.wavelengths)
        beam_mapped = (self.beam_map_flags == 0).sum()
        n_pixels = len(all_res_ids)
        for res_id in res_ids_r:
            name = self.calibration_model_name(res_id=res_id)
            calibration_names.append(name)
            good_wavelengths = 0
            good_solutions = self.has_good_histogram_solutions(res_id=res_id)
            names = self.histogram_model_names(res_id=res_id)
            has_data = self.has_data(res_id=res_id)
            for index, wavelength in enumerate(self.cfg.wavelengths):
                if has_data[index]:
                    photosensitive += 1
                if good_solutions[index]:
                    good_wavelengths += 1
                    histogram_names.append(names[index])
            if good_wavelengths == len(self.cfg.wavelengths):
                completely_successful += 1
        histogram_success = len(histogram_names)
        calibration_success = len(calibration_names)
        # set up histogram table
        table_title = r"\textbf{Histogram Fits} \\"
        table_begin = r"\begin{tabular}{@{}>{\raggedright}p{2.5in} | p{0.7in}}"
        table = (r"number of histograms fit per pixel & {:d} \\"
                 r"number of successful fits & {:d} \\" +
                 r"pixels with all wavelength fits successful & {:.2f} \% \\ " +
                 r"fits successful & {:.2f} \% \\" +
                 r"fits successful out of photosensitive pixels & {:.2f} \% \\" +
                 r"fits successful out of beam-mapped pixels & {:.2f} \% \\" +
                 r"\hline ")
        for model_name in self.cfg.histogram_model_names:
            count = (np.array(histogram_names) == model_name).sum()
            table += (r"fits using {:s} & {:.2f} \% \\"
                      .format(model_name, count / len(histogram_names) * 100))
        table = table.format(n_wavelengths,
                             histogram_success,
                             completely_successful / n_pixels * 100,
                             histogram_success / (n_pixels * n_wavelengths) * 100,
                             histogram_success / photosensitive * 100,
                             histogram_success / (beam_mapped * n_wavelengths) * 100)
        table_end = r"\end{tabular} \\ \\ \\"
        histogram_table = table_title + table_begin + table + table_end
        # set up calibration table
        table_title = r"\textbf{Calibration Fits} \\"
        table = (r"number of successful fits & {:d} \\" +
                 r"fits successful & {:.2f} \% \\" +
                 r"fits successful out of photosensitive pixels & {:.2f} \% \\" +
                 r"fits successful out of beam-mapped pixels & {:.2f} \% \\" +
                 r"\hline ")
        for model_name in self.cfg.calibration_model_names:
            count = (np.array(calibration_names) == model_name).sum()
            table += (r"fits using {:s} & {:.2f} \% \\"
                      .format(model_name, count / len(calibration_names) * 100))
        table = table.format(calibration_success,
                             calibration_success / n_pixels * 100,
                             calibration_success / photosensitive * n_wavelengths * 100,
                             calibration_success / beam_mapped * 100)
        calibration_table = table_title + table_begin + table + table_end
        # set up additional text
        info = (r"\textbf{Solution File Name:} \\" +
                r"{} \\ \\ \\".format(self.cfg.solution_name))
        info += r" \begin{tabular}{@{}>{\raggedright}p{1.5in} | p{1.5in}}"
        info += r"\textbf{ObsFile Names:} & \textbf{Wavelengths [nm]:} \\"
        for index, file_name in enumerate(self.cfg.h5_file_names):
            info += r"{} & {} \\".format(file_name, self.cfg.wavelengths[index])
        info += table_end

        # add text to axes
        text = histogram_table + calibration_table + info
        x_limit = axes_list[3].get_xlim()
        y_limit = axes_list[3].get_ylim()
        axes_list[3].text(x_limit[0], y_limit[1], text, va="top",
                          ha="left", wrap=True)
        # turn off axes on the right side of the page
        axes_list[3].set_axis_off()
        # add title
        figure.suptitle("Wavelength Calibration Solution Summary", fontsize=15)
        # tighten up axes
        rect = [0, 0, 1, .95]
        plt.tight_layout(rect=rect)
        # plot resolution images
        figures = [figure]
        if resolution_images:
            figure, axes = plt.subplots(figsize=figure_size)
            axes, _ = self.plot_resolution_image(axes=axes, wavelength=0, r=r,
                                                 res_ids=res_ids_r)
            axes_list = np.append(axes_list, axes)
            figures.append(figure)
            for wavelength in self.cfg.wavelengths:
                figure, axes = plt.subplots(figsize=figure_size)
                axes, _ = self.plot_resolution_image(axes=axes, wavelength=wavelength,
                                                     r=r, res_ids=res_ids_r)
                axes_list = np.append(axes_list, axes)
                figures.append(figure)
        # save the plots
        if save_name is not None:
            file_path = os.path.join(self.cfg.out_directory, save_name)
            with PdfPages(file_path) as pdf:
                for figure in figures:
                    try:
                        pdf.savefig(figure)
                    except (KeyError, RuntimeError, FileNotFoundError) as error:
                        # fall back to use_latex=False if the figure save fails
                        if isinstance(error, KeyError):
                            message = ("Latex is missing a font. Falling back to"
                                       "use_latex=False. Check the matplotlib log for "
                                       "details.")
                        else:
                            message = ("Latex generated an exception. Falling back to "
                                       "use_latex=False.")
                        log.warning(message)
                        matplotlib.rcParams.update(old_rc)
                        axes_list = self.plot_summary(feedline=feedline,
                                                      save_name=save_name,
                                                      resolution_images=resolution_images,
                                                      use_latex=False)
                        return axes_list

            # if saving close all figures and reset the rcParams
            for axes in axes_list:
                plt.close(axes.figure)
            matplotlib.rcParams.update(old_rc)
            # don't return the axes since they no longer will plot
            return
        return axes_list

    @staticmethod
    def load_frequency_files(config_file):
        """
        Gets the res_ids and frequencies from the templar configuration file

        Args:
            config_file: full path and file name of the templar configuration file.
                         (string)
        Returns:
            a numpy array of the frequency files that could be loaded from the templar
            configuration file vertically stacked. The first column is the res_id and the
            second is the frequency.
        Raises:
            RuntimeError: if no frequency files could be loaded
        """
        # TODO: Move this to a more general templar configuration management class
        configuration = ConfigParser()
        configuration.read(config_file)
        data = []
        for roach in configuration.keys():
            if roach[:5] == 'Roach':
                freq_file = configuration[roach]['freqfile']
                log.info('loading frequency file: {0}'.format(freq_file))
                try:
                    frequency_array = np.loadtxt(freq_file)
                    data.append(frequency_array)
                except (OSError, ValueError, UnicodeDecodeError, IsADirectoryError):
                    log.warn('could not load file: {}'.format(freq_file))
        if len(data) == 0:
            raise RuntimeError('No frequency files could be loaded')
        data = np.vstack(data)
        if np.unique(data[:, 0]).size != data[:, 0].size:
            message = ("There are duplicate ResIDs in the frequency files. " +
                       "Check the templarconfig.cfg")
            log.warn(message)
        return data

    def _parse_resonators(self, pixels=None, res_ids=None, return_res_ids=False):
        if not self._parse:
            return pixels, res_ids
        if pixels is None and res_ids is None:
            message = "must specify a resonator location (x_cord, y_cord) or a res_id"
            raise ValueError(message)
        elif pixels is not None:
            pixels = self._parse_pixels(pixels)
            if return_res_ids:
                res_ids = self.beam_map[pixels[0], pixels[1]]
                res_ids = self._parse_res_ids(res_ids)
        else:
            res_ids = self._parse_res_ids(res_ids)
            pixels = self._reverse_beam_map[:, res_ids]
            pixels = self._parse_pixels(pixels)
        return pixels, res_ids

    def _parse_pixels(self, pixels=None):
        if not self._parse:
            return pixels
        if pixels is not None:
            if not isinstance(pixels, np.ndarray):
                pixels = np.atleast_2d(np.array(pixels)).T
            if len(pixels.shape) == 1:
                pixels = np.atleast_2d(pixels).T
            if pixels.size == 2 and pixels.shape[1] == 2:
                pixels = pixels.T
            bad_input = (not np.issubdtype(pixels.dtype, np.integer) or
                         pixels.shape[0] != 2)
            if bad_input:
                raise ValueError("pixels must be a list of pairs of integers")
        else:
            x_pixels = range(self.cfg.x_pixels)
            y_pixels = range(self.cfg.y_pixels)
            pixels = np.array([[x, y] for x in x_pixels for y in y_pixels]).T
        return pixels

    def _parse_res_ids(self, res_ids=None):
        if not self._parse:
            return res_ids
        if res_ids is None:
            res_ids = self.beam_map.ravel()
        else:
            res_ids = np.atleast_1d(np.array(res_ids))
            bad_input = not np.issubdtype(res_ids.dtype, np.integer)
            if bad_input:
                raise ValueError("res_ids must be an integer or array of integers")
            res_ids = res_ids.squeeze()
        return res_ids

    def _parse_wavelengths(self, wavelengths=None):
        if not self._parse:
            return wavelengths
        if wavelengths is None:
            wavelengths = self.cfg.wavelengths
            return wavelengths
        if not isinstance(wavelengths, (list, tuple, np.ndarray)):
            wavelengths = np.array([wavelengths])
        if not isinstance(wavelengths, np.ndarray):
            wavelengths = np.array(wavelengths)
        bad_wavelengths = np.logical_not(np.isin(wavelengths, self.cfg.wavelengths))
        if bad_wavelengths.any():
            message = "invalid wavelengths: {} nm"
            raise ValueError(message.format(wavelengths[bad_wavelengths]))
        if np.unique(wavelengths).size != wavelengths.size:
            raise ValueError("wavelengths must be unique")
        return wavelengths

    def _parse_models(self, models, model_type, wavelengths=None):
        if not isinstance(models, (list, tuple, np.ndarray)):
            models = np.array([models])
        elif not isinstance(models, np.ndarray):
            models = np.array(models)

        if model_type == 'calibration':
            model_list = self.calibration_model_list
        elif model_type == 'histograms':
            model_list = self.histogram_model_list
            if wavelengths is not None:
                message = ("models parameter must be the same length as the wavelengths "
                           "parameter")
                assert len(models) == len(wavelengths), message
        else:
            message = "model_type must be either 'calibration' or 'histogram' not {}"
            raise ValueError(message.format(model_type))
        message = "models parameter has an invalid model type: {}"
        assert not np.isin(self._type(models), model_list).any(), message.format(models)
        return models

    @staticmethod
    def _plot_color_bar(axes, wavelengths):
        z = [[0, 0], [0, 0]]
        levels = np.arange(min(wavelengths), max(wavelengths), 1)
        c = axes.contourf(z, levels, cmap=self._color_map)
        plt.colorbar(c, ax=axes, label='wavelength [nm]', aspect=50)
        axes.clear()

def fetch(solution_descriptors, config=None, ncpu=1, async=False, force_h5=False):
    cfg = mkidpipeline.config.config if config is None else config

    for sd in solution_descriptors:
        sf = os.path.join(cfg.paths.database, SolutionKey(sd).filename)
        if os.path.exists(sf):
            #Load solution
        else:
            #add to pot to compute

    #Spawn and compute alll necessary solutions

    # x_pixels = #TODO move to beammap
    # y_pixels = #TODO move to beammap
    # bin_directory = cfg.paths.data
    # start_times = [1530100392, 1530100506, 1530100622, 1530100736, 1530100850]
    # exposure_times = [100, 100, 100, 100, 100]
    # beam_map_path = config.beammap
    # h5_directory = config.paths.out
    # # file names in the same order as the wavelengths
    # # (list of strings, use None if making the h5 files directly from the bin files)
    # h5_file_names = None
    # # wavelengths in nanometers (list of numbers)
    # wavelengths = [850, 950, 1100, 1250, 1375]  #
    #
    # [Fit]
    # histogram_model_names = ['GaussianAndExponential']
    # bin_width = 2
    # histogram_fit_attempts = 3
    # calibration_model_names = ['Quadratic', 'Linear']
    # dt = 500
    # parallel = #todo number or remaining cpus
    # [Output]
    # out_directory = cfg.paths.work
    # summary_plot = cfg.outputs.wavecalplots.lower() in ('all', 'summary')
    # templar_configuration_path = cfg.templarconf
    # verbose = True
    # logging = True

    uncomputed_solutions=[]

    for each in uncomputed_solutions:
        cfg_file = ''

        config = Configuration(cfg_file, solution_name=sf)

        if not config.hdf_exist() or force_h5:
            b2h_configs = []
            for wave, start_t, int_t in zip(config.wavelengths, config.start_times,
                                            config.exposure_times):
                b2h_configs.append(bin2hdf.Bin2HdfConfig(datadir=config.bin_directory,
                                                         beamfile=config.beam_map_path,
                                                         outdir=config.h5_directory,
                                                         starttime=start_t, inttime=int_t,
                                                         x=config.x_pixels,
                                                         y=config.y_pixels))
            bin2hdf.makehdf(b2h_configs, maxprocs=min(ncpu, mp.cpu_count()))

        # run the wavelength calibration
        Calibrator(config).run(parallel=config.parallel, plot=config.summary_plot,
                               verbose=config.verbose)

    return

if __name__ == "__main__":
    timestamp = int(datetime.utcnow().timestamp())

    # read in command line arguments
    parser = argparse.ArgumentParser(description='MKID Wavelength Calibration Utility')
    parser.add_argument('cfg_file', type=str, help='The configuration file')
    parser.add_argument('--vet', action='store_true', dest='vet_only',
                        help='Only verify the configuration file')
    parser.add_argument('--h5', action='store_true', dest='h5_only',
                        help='Only make the h5 files')
    parser.add_argument('--force', action='store_true', dest='force_h5',
                        help='Force h5 file creation')
    parser.add_argument('-nc', type=int, dest='n_cpu', default=0,
                        help="Number of CPUs to use for bin2hdf, " 
                             "default is number of wavelengths")
    parser.add_argument('--quiet', action='store_true', dest='quiet',
                        help='Disable logging')
    args = parser.parse_args()

    # load the configuration file
    config = Configuration(args.cfg_file,
                           solution_name='wavecal_solution_{}.npz'.format(timestamp))
    # set up logging
    if not args.quiet:
        setup_logging(config, timestamp)

    # print execution time on exit
    atexit.register(lambda x: print('Execution took {:.2f} minutes'
                                    .format((datetime.utcnow().timestamp() - x) / 60)),
                    timestamp)

    # set up bin2hdf
    if args.n_cpu == 0:
        args.n_cpu = len(config.wavelengths)
    if args.vet_only:
        exit()
    if not config.hdf_exist() or args.force_h5:
        b2h_configs = []
        for wave, start_t, int_t in zip(config.wavelengths, config.start_times,
                                        config.exposure_times):
            b2h_configs.append(bin2hdf.Bin2HdfConfig(datadir=config.bin_directory,
                                                     beamfile=config.beam_map_path,
                                                     outdir=config.h5_directory,
                                                     starttime=start_t, inttime=int_t,
                                                     x=config.x_pixels,
                                                     y=config.y_pixels))
        bin2hdf.makehdf(b2h_configs, maxprocs=min(args.n_cpu, mp.cpu_count()))
    if args.h5_only:
        exit()
    # run the wavelength calibration
    Calibrator(config).run(parallel=config.parallel, plot=config.summary_plot,
                           verbose=config.verbose)
