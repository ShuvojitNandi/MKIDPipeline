# The MKID Pipeline
Data reduction pipeline for Mazinlab MKID instruments.

Installation
============
See confluence

Pipeline Quick Start Guide
==========================

Move to a directory you would like to play around in, activate your `pipeline` envronment and run: 

`mkidpipe --init`

This will create three YAML config files (NB "_default" will be apended if the file already exists):
1. pipe.yaml - The pipeline global configuration file.
1. data.yaml - A sample dataset. You'll need to redefine this with your actual data.
1. out.yaml - A sample output configuration. You'll need to redefine this as well.

Each of these files contains extensive comments and irrelevant settings (or those for which the defaults are fine) may 
be omitted. More details for pipe.yaml are in mkidpipeline.pipeline and mkidpipeline.steps.<stepname>. More details for 
the other two are in mkidpipeline.config. Data and output yaml files can be vetted with helpful errors by running

`mkidpipe --vet` in the directory containing your three files. 

To build and reduce this dataset open pipe.yaml and make sure you are happy with the default paths, these are sensible if you are working on GLADoS. On dark you'll want to change the `darkdata` folder to `data`. If the various output paths don't exist they will be created, though permissions issues could cause unexpected results. Using a shared database location might save you some time and is strongly encouraged at least across all of your pipeline runs (consider collaborating even with other users)! Outputs will be placed into a generated directory structure under `out` and WILL clobber existing files with the same name.

See `mkidpipe --help` for more options.

After a while (~TODO hours with the defaults) you should have some outputs to look at. To really get going you'll now need to use observing logs to figure out what your data.yaml and out.yaml should contain you want to work with. Look for good seeing conditions and note the times of the nearest laser cals.

## Pipeline flow

When run the pipeline goes through several steps, some only as needed, and only as needed for the requeted outputs, so it won't slow you down to have all your data defined in one place. 


1. Photontables (`mkidpipeline.photontable.Photontable`) files _for the defined outputs_ are created as needed by `mkidpipeline.steps.buildhdf`.
   
1. Observing metadata defined in the data definition and observing logs is attached to tables by `mkidpipeline.pipeline.batch_apply_metadata`.
   
1. Any wavecals not already in the database are generated by `mkidpipeline.steps.wavecal.fetch`. There is some intelligence here so if the global config for the step or the start/stop times of the data  the solution will be regnerated. 
1. Photon tables are wavelength calibrated `mkidpipeline.steps.wavecal.apply`.

Wavelength Calibration (mkidpipeline.calibration.wavecal)
----------------------------------------------
The wavelength calibration code is in the calibration folder. You shouldn't need to use this 
directly. Wavelength calibrations are determined as needed using wavecal.py. The processing file 
must have XXX TODO so that the pipeline can determine the appropriate calibration info



#### Applying the wavelength calibration
After the wavelength calibration .h5 solution file is made, it can be applied to an obs file by using this code snippet

    # path to your wavecal solution file
    file_name = '/path/to/calsol_timestamp.h5'
    obs.apply_wavecal(file_name)
where obs is your obs file object. The method apply_wavecal() will change all of the phase heights to wavelengths in nanometers. For pixels where no calibration is available, the phase heights are not changed and a flag is applied to mark the pixel as uncalibrated. Warning, the wavelength calibration can not be undone after applied and permanently alters the .h5 file. Make a backup .h5 file if you are testing different calibrations.

Flat Fielding
----------------------------------------------
The flatfield calibration code is in the Calibration/FlatCal/ folder. These two files run the FlatCal:

    FlatCal.py -- main file that contains the calibration code and the FlatCal plotting functions
    ./Params/default.cfg -- default config file, used for reference.  

The FlatCal can be run on a single flat h5 file or several flat h5 files.  You will make use of different parameters in the config file depending on whether your flat h5 files are in the ScienceData directory or your own working directory (see the section on how to use the config file).

FlatCal will default to the reference config file.  I recommend copying it over to your working directory, renaming it, and editing it as appropriate for your specific calibration

#### Before running the FlatCal:
     Generate an .h5 file from the .bin files for the desired timespan of the flatfield. It may be named with a timestamp or descriptive name (e.g. 20171004JBandEndofNightFlat.h5)
     See the section "Creating HDF5 files from the .bin files" for more info.

     Make a wavecal solution file and apply it to the flat h5 file.  See Wavelength Calibration for more details on how to do that.

#### Configuration file
The default.cfg file is a reference configuration file and has detailed comments in it describing each parameter. The configuration file is setup using python syntax. (i.e. if a parameter is a string, it must be surrounded by quotation marks.) The most important parameters will be described here.  There are is a function in FlatCal which will check each parameter to confirm it is the correct format (string, int, etc).
NOTE that some of these parameters are conditional and some are required.  See notes below and default.cfg for which ones are required, which are conditional.

    Data Section:  This section provides the location and description of the FlatCal data being processed.
----------------------------------------------------------------------------------------------------------------------------------------------
    Important:  Fill these four parameters out ONLY if your flat h5 files are in the dark ScienceData directory AND are named their starting timestamps.  
    If it has been copied to your personal data reduction directory, leave them as empty strings, ''
    If these are not empty strings, the code will search in '/mnt/data0/ScienceData/Run/Date for flatObsTstamps.h5 and generate a FlatCalSoln file:
    '/mnt/data0/ScienceData/Run/Date/flatCalTstamp_calSoln.h5'

    run                      -- e.g. PAL2017b, which observing run is the flatcal file from? (string)  CONDITIONAL
    date                     -- e.g. 20171005, which night is the flatcal file from (string)   CONDITIONAL
    flatCalTstamp            -- Timestamp which will be prefix of FlatCalSolution file (string)   CONDITIONAL
    flatObsTstamps           -- List of starting timestamps for the flat calibration, one for each Flat h5 file used
                                (list of strings, even if there is just one file being used) [] CONDITIONAL
------------------------------------------------------------------------------------------------------------------------------------------------
    wvlDate                  -- Wavelength Sunset Date (string).  Leave '' if wavecal is already applied     CONDITIONAL
    wvlCalFile               -- Wavecal Solution Directory Path + File (To be used in plotting)   REQUIRED
------------------------------------------------------------------------------------------------------------------------------------------------   
    Important:  Fill these two parameters out IF your flat h5 file has been copied to your personal data reduction directory

    flatPath                 -- Path to your flat h5 file (string) CONDITIONAL
    calSolnPath              -- Output Cal Soln path (string).  Include the path and a basename for what you want the Flat Cal Solution files to be titled
                                (e.g '/mnt/data0/isabel/DeltaAnd/Flats/DeltaAndFlatSoln.h5')
                                You will get EXPTIME/INTTIME number of files titled DeltaAndFlatSoln#.h5     CONDITIONAL
------------------------------------------------------------------------------------------------------------------------------------------------
    intTime                  -- Integration time to break up larger h5 files into in seconds (number) (5 sec recommended)   REQUIRED
    expTime                  -- Total exposure time (number)   REQUIRED

    Instrument Section:  This section provides information about the specific wavelength and energy parameters for this instrument   
----------------------------------------------------------------------------------------------------------------------------------------------
    deadtime                 -- (number) REQUIRED
    energyBinWidth           -- Energy Bin width in eV (number)  REQUIRED
    wvlStart                 -- Starting wavelength in nanometers (number)  REQUIRED
    wvlStop                  -- Final wavelength in nanometers (number)  REQUIRED

    Calibration Section:  This section provides the parameters specific to the flat calibration function   
----------------------------------------------------------------------------------------------------------------------------------------------
    countRateCutoff          -- Count rate cutoff in seconds (number)  REQUIRED
    fractionOfChunksToTrim   -- Fraction of Chunks to trim (integer)  REQUIRED
    timeMaskFileName         -- Time mask file name (string)  CONDITIONAL
    timeSpacingCut           -- Time spacing cut (never used)  (string)  REQUIRED 'None'


#### Running from the command line
Before running the Flat calibration, generate your flat h5 file and run the wavecal on it.
The Flat calibration can be run from the command line using the syntax:

    python /path/to/FlatCal.py /other/path/to/my_config.cfg
'/path/to/' and '/other/path/to/' are the full or relative paths to the FlatCal.py and my_config.cfg files respectively. 'my_config.cfg' is your custom configuration file. If no configuration file is specified, the default configuration file will be used. Never commit changes to the default configuration file to the repository.

The FlatCal will output a number of calsoln h5 files (EXPTIME/INTTIME) all with the name '.../calSolPath+[index].h5' where index spans EXPTIME/INTTIME.
It will also output three plot pdfs for each h5 calsoln file: '.../calSolPath+[index]__mask.pdf', '.../calSolPath+[index]__wvlSlices.pdf', '.../calSolPath+[index].pdf'

#### Running from a script or a Python shell
The calibration can also be run from a script or a python shell.

    from Calibration.FlatCal import FlatCal as F
    f = F.FlatCal(config_file='/path/to/my_config.cfg')
    f.loadFlatSpectra()
    f.checkCountRates()
    f.calculateWeights()

#### Plotting the results of the calibration
Plots of the results of a flatfield calibration will be done automatically in FlatCal.py

    plotWeightsByPixelWvlCompare() -- Plot weights of each wavelength bin for every single pixel
                                      Makes a plot of wavelength vs weights, twilight spectrum, and wavecal solution for each pixel
                                      '.../calSolPath+[index].pdf'

    plotWeightsWvlSlices()         -- Plot weights in images of a single wavelength bin (wavelength-sliced images)
                                      '.../calSolPath+[index]__wvlSlices.pdf'     

    plotMaskWvlSlices()            -- Plot mask in images of a single wavelength bin (wavelength-sliced images)
                                      '.../calSolPath+[index]_WavelengthCompare.pdf'

#### Applying the flatfield calibration
After the flatfield calibration .h5 solution files are made, they can be applied to an obs file by using this code

    ObsFN = '/path/to/obsfile.h5'

    #Path to the location of the FlatCal solution files should contain the base filename of these solution files
    #e.g '/mnt/data0/isabel/DeltaAnd/Flats/DeltaAndFlatSoln.h5')
    #Code will grab all files titled DeltaAndFlatSoln#.h5 from that directory

    calSolnPath='/path/to/calSolutionFile.h5'

    obsfile=obs(ObsFN, mode='write')
    obsfilecal=obs.apply_flatcal(obsfile,calSolnPath,verbose=True)

Weights are multiplied in and replaced; if "weights" are the contents of the "SpecWeight" column, weights = weights*weightArr. NOT reversible unless the original contents (or weightArr) is saved.
Will write plots of flatcal solution (5 second increments over a single flat exposure) with average weights overplotted to a pdf for pixels which have a successful FlatCal.
Written to the calSolnPath+'FlatCalSolnPlotsPerPixel.pdf'

Creating Image Cubes
----------------------------------------------


QuickLook
----------------------------------------------
#### Using quickLook.py
    Before you start:
        -create HDF5 file from .bin files as described above
        -apply wave cal to HDF5 as described above
        -(optional) set $MKID_DATA_DIR to the path where your HDF5 is located

    -In the command line, run
        >> python quickLook.py
    -Go to File>Open and select your HDF5 file, click OK.
        -The Beam Flag Image is automatically displayed, showing you what pixels are good and bad.
         See pipelineFlags.py for explanation of the flag values.
    -Specify your desired start/stop times and wavelengths.
    -To view an image, select the "Raw Counts" radio button, and click "Plot Image"
    -To view a single pixel's timestream, intensity histogram, or spectrum
        -click on the desired pixel. This is now your "Active Pixel", and is shown
         the bottom of the window.
        -Go to "Plot" menu and click on the type you want.
        -Selecting a new Active Pixel will update the subplots.



Making Speckle Statistics Maps
----------------------------------------------
