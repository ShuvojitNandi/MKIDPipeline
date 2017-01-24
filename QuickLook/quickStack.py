'''
Author: Seth Meeker        Date: Nov 20, 2016

Quick routine to take a series of files from multiple dither positions,
align them, and median add them

load params from quickStack.cfg

TODO:
Pipe unocculted centroids into occulted version

'''

import sys, os, time, struct
import numpy as np
####################################
import tables
from tables import *
import h5py
#####################################
from scipy import ndimage
from scipy import signal
import astropy
import cPickle as pickle
from PyQt4 import QtCore
from PyQt4 import QtGui
import matplotlib
from matplotlib.backends.backend_qt4agg import FigureCanvasQTAgg as FigureCanvas
#from matplotlib.backends.backend_qt4agg import NavigationToolbar2QTAgg as NavigationToolbar
from matplotlib.figure import Figure
from functools import partial
import mpfit
from parsePacketDump2 import parsePacketData
from arrayPopup import plotArray
from readDict import readDict
from img2fitsExample import writeFits
import hotpix.hotPixels as hp
import headers.TimeMask as tm
from utilsM82 import *
from readFITStest import readFITS
#import imRegFFT
import image_registration as ir

#################################################           
h5file = h5py.File('StackedImg_Masks_Centroids.h5', "w",)
MaskGroup=h5file.create_group('MaskTables')
StackGroup=h5file.create_group('UnstackedImages')
CentroidGroup=h5file.create_group('Centroid Positions')
CalGroup=h5file.create_group('Cal Files')

#class CalInfo(IsDescription):
ColdPixMask=[]
HotPixMask=[]
DeadPixMask=[]
RawImgs=[]
RoughShiftsx=[]
RoughShiftsy=[]
FineShiftsx=[]
FineShiftsy=[]
#################################################

def aperture(startpx,startpy,radius, nRows, nCols):
        r = radius
        length = 2*r 
        height = length
        allx = xrange(startpx-int(np.ceil(length/2.0)),startpx+int(np.floor(length/2.0))+1)
        ally = xrange(startpy-int(np.ceil(height/2.0)),startpy+int(np.floor(height/2.0))+1)
        mask=np.zeros((nRows,nCols))
        
        for x in allx:
            for y in ally:
                if (np.abs(x-startpx))**2+(np.abs(y-startpy))**2 <= (r)**2 and 0 <= y and y < nRows and 0 <= x and x < nCols:
                    mask[y,x]=1.
        return mask

def loadStack(dataDir, start, stop, useImg = False, nCols=80, nRows=125):
    frameTimes = np.arange(start, stop+1)
    frames = []
    for iTs,ts in enumerate(frameTimes):
        try:
            if useImg==False:
                imagePath = os.path.join(dataDir,str(ts)+'.bin')
                print imagePath
                with open(imagePath,'rb') as dumpFile:
                    data = dumpFile.read()

                nBytes = len(data)
                nWords = nBytes/8 #64 bit words
                
                #break into 64 bit words
                words = np.array(struct.unpack('>{:d}Q'.format(nWords), data),dtype=object)
                parseDict = parsePacketData(words,verbose=False)
                image = parseDict['image']

            else:
                imagePath = os.path.join(dataDir,str(ts)+'.img')
                print imagePath
                image = np.fromfile(open(imagePath, mode='rb'),dtype=np.uint16)
                image = np.transpose(np.reshape(image, (nCols, nRows)))

        except (IOError, ValueError):
            print "Failed to load ", imagePath
            image = np.zeros((nRows, nCols),dtype=np.uint16)  
        frames.append(image)
    stack = np.array(frames)
    return stack

def medianStack(stack):
    return np.nanmedian(stack, axis=0)


#configFileName = 'quickStack_test.cfg'
#configFileName = 'quickStack_coron_20161119.cfg'
#configFileName = 'quickStack_20161122g_hp.cfg'
#configFileName = 'quickStack_20161121b.cfg'
configFileName = 'quickStack_20161122e.cfg'
configData = readDict()
configData.read_from_file(configFileName)

# Extract parameters from config file
nPos = int(configData['nPos'])
startTimes = np.array(configData['startTimes'], dtype=int)
stopTimes = np.array(configData['stopTimes'], dtype=int)
darkSpan = np.array(configData['darkSpan'], dtype=int)
flatSpan = np.array(configData['flatSpan'], dtype=int)
xPos = np.array(configData['xPos'], dtype=int)
yPos = np.array(configData['yPos'], dtype=int)
numRows = int(configData['numRows'])
numCols = int(configData['numCols'])
upSample = int(configData['upSample'])
fitPos = bool(configData['fitPos'])
target = str(configData['target'])
date = str(configData['date'])
imgDir = str(configData['imgDir'])
binDir = str(configData['binDir'])
outputDir = str(configData['outputDir'])
useImg = bool(configData['useImg'])
hpm = str(configData['hpm'])
refFile = str(configData['refFile'])

imgPath = os.path.join(imgDir,date)
binPath = os.path.join(binDir,date)

timeMaskPath = "/mnt/data0/darknessCalFiles/timeMasks"
hpPath = os.path.join(timeMaskPath,date)

if useImg == True:
    dataDir = imgPath
    print "Loading data from .img files"
else:
    dataDir = binPath
    print "Loading data from .bin files"

print startTimes
print stopTimes
print darkSpan
print flatSpan

#load dark frames
print "Loading dark frame"
darkStack = loadStack(dataDir, darkSpan[0], darkSpan[1],useImg = useImg, nCols=numCols, nRows=numRows)
dark = medianStack(darkStack)
darkMedset=CalGroup.create_dataset('MedianDarkFile',(1,numRows,numCols,),'f')
print('this is dark')
print(dark[20,20])
darkMedset[0,0:numRows,0:numCols]=dark
#plotArray(dark,title='Dark',origin='upper')

#dark = signal.medfilt(dark,3)
#plotArray(dark,title='Med Filt Dark',origin='upper')

#load flat frames
print "Loading flat frame"
flatStack = loadStack(dataDir, flatSpan[0], flatSpan[1],useImg = useImg, nCols=numCols, nRows=numRows)
flat = medianStack(flatStack)
flatMedset=CalGroup.create_dataset('MedianFlatFile',(1,numRows,numCols,),'d')
flatMedset[0,0:numRows,0:numCols]=flat
#plotArray(flat,title='Flat',origin='upper')
flatSub = flat-dark

#determine the x and y shifts that subsequent frames must be moved to align with first frame
dXs = xPos[0]-xPos
dYs = yPos[0]-yPos

#initialize hpDict so we can just check if it exists and only make it once
hpDict=None

intTime = min(stopTimes-startTimes)
print "Shortest Integration time = ", intTime


#load dithered science frames
ditherFrames = []
for i in range(nPos):
    stack = loadStack(dataDir, startTimes[i], startTimes[i]+intTime,useImg = useImg, nCols=numCols, nRows=numRows)
    print('this is what we want')
    print(range(len(stack)))
    #flatNorm = darkSub/flatSub
    for f in range(len(stack)):
        hpPklFile = os.path.join(hpPath,"%s.pkl"%(startTimes[i]+f))
        darkSub = stack[f]-dark
        #flatNorm = darkSub/flatSub
        #plotArray(med,title='Dither Pos %i'%i,origin='upper')
        #plotArray(embedInLargerArray(darkSub),title='Dither Pos %i - dark'%i,origin='upper')
        if i==0 and f==0:
            plotArray(darkSub,title='Dither Pos %i - dark'%i,origin='upper',vmin=0)

        if (hpm == 'one' and hpDict==None) or (hpm=='all'):
            if not os.path.exists(hpPklFile):
                print "Creating time mask: %s"%(hpPklFile)
                hpDict = hp.checkInterval(image=darkSub,fwhm=2.5, boxSize=5, nSigmaHot=3.0, dispToPickle=hpPklFile)
                hpMask = hpDict['mask']
                #plotArray(hpMask,title='12 = hot, 19 = OK', origin='upper',vmin=0, vmax=13)
            else:
                print "Loading time mask: %s"%(hpPklFile)
                pklDict = pickle.load(open(hpPklFile,"rb"))
                hpMask = np.empty((numRows, numCols),dtype=int)
                hpMask.fill(tm.timeMaskReason['none'])
		print('hot')
		print(tm.timeMaskReason['hot pixel'])
		print('cold')
		print(tm.timeMaskReason['cold pixel'])
		print('dead')
		print(tm.timeMaskReason['dead pixel'])
                hpMask[pklDict["hotMask"]] = tm.timeMaskReason['hot pixel']
                hpMask[pklDict["coldMask"]] = tm.timeMaskReason['cold pixel']
                hpMask[pklDict["deadMask"]] = tm.timeMaskReason['dead pixel']
		print(i)
		print(f)
		print(i+f)
		HotPix=pklDict["hotMask"]
		HotPix[pklDict["hotMask"]] = tm.timeMaskReason['hot pixel']
		ColdPix=pklDict["coldMask"]
		DeadPix=pklDict["deadMask"]
		HotPixMask.append(HotPix)
		ColdPixMask.append(ColdPix)
		DeadPixMask.append(DeadPix)
		RoughShiftx=dXs[f]
		RoughShifty=dYs[f]
		dataraw=stack[f]
		RawImgs.append(dataraw)
		RoughShiftsx.append(RoughShiftx)
		RoughShiftsy.append(RoughShifty)
        else:
            print "No hot pixel masking specified in config file"
            hpMask = np.zeros((numRows, numCols),dtype=int)
	    print(hpMask)

	
        #apply hp mask to image
        darkSub[np.where(hpMask==12)]=np.nan
        
        if f==0 and i==0:
            plotArray(darkSub,title='Dither Pos %i HP Masked'%i,origin='upper',vmin=0)

        paddedFrame = embedInLargerArray(darkSub,frameSize=0.40)


        print "Shifting dither %i, frame %i by x=%i, y=%i"%(i,f, dXs[i], dYs[i])
        shiftedFrame = rotateShiftImage(paddedFrame,0,dXs[i],dYs[i])

        #cut out cold/dead pixels
        shiftedFrame[np.where(shiftedFrame<=3)]=np.nan

        #shiftedFrame = ndimage.generic_filter(shiftedFrame, np.nanmedian, size=5)
        #shiftedFrame = ndimage.median_filter(shiftedFrame,3)
        #shiftedFrame = astropy.stats.sigma_clip(shiftedFrame)
        #if f<1:
        #    plotArray(shiftedFrame,title='Sigma Clipped Frame',origin='upper')

        upSampledFrame = upSampleIm(shiftedFrame,upSample)

        ditherFrames.append(upSampledFrame)

    print "Loaded dither position %i"%i

shiftedFrames = np.array(ditherFrames)
#if fitPos==True, do second round of shifting using mpfit correlation
#using x and y pos from earlier as guess
if fitPos==True:
    reshiftedFrames=[]
    
    if refFile!=None and os.path.exists(refFile):
        refIm = readFITS(refFile)
        print "Loaded %s for fitting"%refFile
        plotArray(refIm,title='loaded reference FITS',origin='upper',vmin=0)
    else:
        refIm=shiftedFrames[0]

    cnt=0
    for im in shiftedFrames:
        print "\n\n------------------------------\n"
        print "Fitting frame %i of %i"%(cnt,len(shiftedFrames))
        pGuess=[0,1,1]
        pLowLimit=[-1,(dXs.min()-5)*upSample,(dYs.min()-5)*upSample]
        pUpLimit=[1,(dXs.max()+5)*upSample,(dYs.max()+5)*upSample]
        print "guess", pGuess, "limits", pLowLimit, pUpLimit

        #mask out background structure, only fit on known object location
        maskRad=24
        pMask = aperture(xPos[0]*upSample,yPos[0]*upSample,maskRad*upSample, numRows*upSample, numCols*upSample)
        pMask = embedInLargerArray(pMask,frameSize=0.40,padValue = 0)
        m1 = np.ma.make_mask(pMask)

        #mask parameters for SAO binary secondary
        maskRad=18
        sMask = aperture((xPos[0]+7)*upSample,(yPos[0]+20)*upSample,maskRad*upSample, numRows*upSample, numCols*upSample)
        #sMask = aperture((xPos[0])*upSample,(yPos[0])*upSample,maskRad*upSample, numRows*upSample, numCols*upSample)
        sMask = embedInLargerArray(sMask,frameSize=0.40,padValue = 0)
        m2 = np.ma.make_mask(sMask)

        #aperture mask with secondary
        apMask = np.ma.mask_or(m1,m2)

        #aperture mask for no primary
        #apMask = m2

        #aperture mask for no secondary
        #apMask = m1

        maskedRefIm = np.copy(refIm)
        maskedRefIm[np.where(~apMask)]=np.nan
        maskedIm = np.copy(im)
        maskedIm[np.where(~apMask)]=np.nan

        if cnt==0:
            #plotArray(sMask, title='aperture mask', origin='upper',vmin=0,vmax=1)
            #plot array with secondary mask as well
            plotArray(pMask+sMask, title='aperture mask', origin='upper',vmin=0,vmax=1)

            plotArray(maskedRefIm, title='masked Reference', origin='upper')
            plotArray(maskedIm, title='masked Image to be aligned', origin='upper') 

        #use mpfit and correlation fitting from Giulia's M82 code
        #mp = alignImages(maskedRefIm, maskedIm, parameterGuess=pGuess, parameterLowerLimit=pLowLimit, 				parameterUpperLimit=pUpLimit)

        # image registration by FFT returning all zeros for translation...
        #trans = imRegFFT.translation(refIm, im)
        #im2, scale, angle, trans = imRegFFT.similarity(np.nan_to_num(refIm),np.nan_to_num(im))
        #mp = [angle, trans[0],trans[1]]

        #try using keflavich image_registation repository
        dx, dy, ex, ey = ir.chi2_shifts.chi2_shift(maskedRefIm, maskedIm, zeromean=True)#,upsample_factor='auto')
        mp = [0,-1*dx,-1*dy]

        print "fitting output: ", mp

        newShiftedFrame = rotateShiftImage(im,mp[0],mp[1],mp[2])
        reshiftedFrames.append(newShiftedFrame)
	FineShiftx=mp[1]
	FineShifty=mp[2]
	FineShiftsx.append(FineShiftx)
	FineShiftsy.append(FineShifty)
	cnt+=1
	
	

    shiftedFrames = np.array(reshiftedFrames)

datafile = tables.openFile('Masks_RawImgs_Centroids.h5',mode='w')
calgroup = datafile.createGroup(datafile.root,'stackcal','Table of Hot/Bad/Cold Pixel Masks and Raw Images')

#take median stack of all shifted frames
finalImage = medianStack(shiftedFrames)# / 3.162277 #adjust for OD 0.5 difference between occulted/unocculted files
plotArray(finalImage,title='final',origin='upper')

nanMask = np.zeros(np.shape(finalImage))
nanMask[np.where(np.isnan(finalImage))]=1
#plotArray(nanMask,title='good=0, nan=1', origin='upper')
arr=[1,2,3]
Rawarray = tables.Array(calgroup,'Raw Images',RawImgs,title='Raw Images')
Hotarray = tables.Array(calgroup,'Hot Pixel Mask',HotPixMask,title='Hot Pixel Mask')
Coldarray = tables.Array(calgroup,'Cold Pixel Mask',ColdPixMask,title='Cold Pixel Mask')
Deadarray = tables.Array(calgroup,'Dead Pixel Mask',DeadPixMask,title='Dead Pixel Mask')
Roughxarray=tables.Array(calgroup,'Rough X Array',RoughShiftsx,title='Rough X Array')
Roughyarray=tables.Array(calgroup,'Rough Y Array',RoughShiftsy,title='Rough Y Array')
Finexarray=tables.Array(calgroup,'Fine X Array',FineShiftsx,title='Fine X Array')
Fineyarray=tables.Array(calgroup,'Fine Y Array',FineShiftsy,title='Fine Y Array')
############################################
datafile.flush()
datafile.close()
############################################


writeFits(finalImage, outputDir+'%s_%sDithers_%sxSamp_%sHPM_%s.fits'%(target,nPos,upSample,hpm,date))
print "Wrote to FITS: ", outputDir+'%s_%sDithers_%sxSamp_%sHPM_%s.fits'%(target,nPos,upSample,hpm,date)
