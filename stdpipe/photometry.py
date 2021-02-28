from __future__ import absolute_import, division, print_function, unicode_literals

import os, posixpath, shutil, tempfile
import numpy as np

from astropy.wcs import WCS
from astropy.io import fits
from astropy.stats import mad_std
from astropy.table import Table

import warnings
from astropy.wcs import FITSFixedWarning
# warnings.simplefilter(action='ignore', category=FITSFixedWarning)
# warnings.simplefilter(action='ignore', category=FutureWarning)

import sep
import photutils

import statsmodels.api as sm
from esutil import htm

try:
    import cv2
    # Much faster dilation
    dilate = lambda image,mask: cv2.dilate(image.astype(np.uint8), mask).astype(np.bool)
except:
    from scipy.signal import fftconvolve
    dilate = lambda image,mask: fftconvolve(image, mask, mode='same') > 0.9

convolve = lambda x,y: fftconvolve(x, y, mode='same')

def make_kernel(r0=1.0, ext=1.0):
    x,y = np.mgrid[np.floor(-ext*r0):np.ceil(ext*r0+1), np.floor(-ext*r0):np.ceil(ext*r0+1)]
    r = np.hypot(x,y)
    image = np.exp(-r**2/2/r0**2)

    return image

def get_objects_sep(image, header=None, mask=None, thresh=4.0, aper=3.0, bkgann=None, r0=0.5, gain=1, edge=0, minnthresh=2, minarea=5, relfluxradius=2.0, wcs=None, use_fwhm=False, use_mask_large=False, subtract_bg=True, npix_large=100, sn=10.0, verbose=True, **kwargs):
    if r0 > 0.0:
        kernel = make_kernel(r0)
    else:
        kernel = None

    if verbose:
        print("Preparing background mask")

    if mask is None:
        mask = np.zeros_like(image, dtype=np.bool)

    mask_bg = np.zeros_like(mask)
    mask_segm = np.zeros_like(mask)

    if verbose:
        print("Building background map")

    bg = sep.Background(image, mask=mask|mask_bg, bw=64, bh=64)
    if subtract_bg:
        image1 = image - bg.back()
    else:
        image1 = image.copy()

    sep.set_extract_pixstack(image.shape[0]*image.shape[1])

    if use_mask_large:
        # Mask regions around huge objects as they are most probably corrupted by saturation and blooming
        if verbose:
            print("Extracting initial objects")

        obj0,segm = sep.extract(image1, err=bg.rms(), thresh=thresh, minarea=minarea, mask=mask|mask_bg, filter_kernel=kernel, segmentation_map=True)

        if verbose:
            print("Dilating large objects")

        mask_segm = np.isin(segm, [_+1 for _,npix in enumerate(obj0['npix']) if npix > npix_large])
        mask_segm = dilate(mask_segm, np.ones([10, 10]))

    if verbose:
        print("Extracting final objects")

    obj0 = sep.extract(image1, err=bg.rms(), thresh=thresh, minarea=minarea, mask=mask|mask_bg|mask_segm, filter_kernel=kernel, **kwargs)

    if use_fwhm:
        # Estimate FHWM and use it to get optimal aperture size
        idx = obj0['flag'] == 0
        fwhm = 2.0*np.sqrt(np.hypot(obj0['a'][idx], obj0['b'][idx])*np.log(2))
        fwhm = 2.0*sep.flux_radius(image1, obj0['x'][idx], obj0['y'][idx], relfluxradius*fwhm*np.ones_like(obj0['x'][idx]), 0.5, mask=mask)[0]
        fwhm = np.median(fwhm)

        aper = max(1.5*fwhm, aper)

        if verbose:
            print("FWHM = %.2g, aperture = %.2g" % (fwhm, aper))

    # Windowed positional parameters are often biased in crowded fields, let's avoid them for now
    # xwin,ywin,flag = sep.winpos(image1, obj0['x'], obj0['y'], 0.5, mask=mask)
    xwin,ywin = obj0['x'], obj0['y']

    # Filter out objects too close to frame edges
    idx = (np.round(xwin) > edge) & (np.round(ywin) > edge) & (np.round(xwin) < image.shape[1]-edge) & (np.round(ywin) < image.shape[0]-edge) # & (obj0['flag'] == 0)

    if minnthresh:
        idx &= (obj0['tnpix'] >= minnthresh)

    if verbose:
        print("Measuring final objects")

    flux,fluxerr,flag = sep.sum_circle(image1, xwin[idx], ywin[idx], aper, err=bg.rms(), gain=gain, mask=mask|mask_bg|mask_segm, bkgann=bkgann)
    # For debug purposes, let's make also the same aperture photometry on the background map
    bgflux,bgfluxerr,bgflag = sep.sum_circle(bg.back(), xwin[idx], ywin[idx], aper, err=bg.rms(), gain=gain, mask=mask|mask_bg|mask_segm)

    bgnorm = bgflux/np.pi/aper**2

    # Fluxes to magnitudes
    mag,magerr = np.zeros_like(flux), np.zeros_like(flux)
    mag[flux>0] = -2.5*np.log10(flux[flux>0])
    # magerr[flux>0] = 2.5*np.log10(1.0 + fluxerr[flux>0]/flux[flux>0])
    magerr[flux>0] = 2.5/np.log(10)*fluxerr[flux>0]/flux[flux>0]

    # FWHM estimation - FWHM=HFD for Gaussian
    fwhm = 2.0*sep.flux_radius(image1, xwin[idx], ywin[idx], relfluxradius*aper*np.ones_like(xwin[idx]), 0.5, mask=mask)[0]

    flag |= obj0['flag'][idx]

    # Quality cuts
    fidx = (flux > 0) & (magerr < 1.0/sn)

    if wcs is None and header is not None:
        # If header is provided, we may build WCS from it
        wcs = WCS(header)

    if wcs is not None:
        # If WCS is provided we may convert x,y to ra,dec
        ra,dec = wcs.all_pix2world(obj0['x'][idx], obj0['y'][idx], 0)
    else:
        ra,dec = np.zeros_like(obj0['x'][idx]),np.zeros_like(obj0['y'][idx])

    if verbose:
        print("All done")

    obj = Table({'x':xwin[idx][fidx], 'y':ywin[idx][fidx],
                 'xerr': np.sqrt(obj0['errx2'][idx][fidx]), 'yerr': np.sqrt(obj0['erry2'][idx][fidx]),
                 'flux':flux[fidx], 'fluxerr':fluxerr[fidx],
                 'mag':mag[fidx], 'magerr':magerr[fidx],
                 'flags':obj0['flag'][idx][fidx]|flag[fidx],
                 'ra':ra[fidx], 'dec':dec[fidx],
                 'bg':bgnorm[fidx], 'fwhm':fwhm[fidx],
                 'a':obj0['a'][idx][fidx], 'b':obj0['b'][idx][fidx],
                 'theta':obj0['theta'][idx][fidx]})

    obj.meta['aper'] = aper
    obj.meta['bkgann'] = bkgann

    obj.sort('flux', reverse=True)

    return obj

def get_objects_sextractor(image, header=None, mask=None, thresh=2.0, aper=3.0, r0=0.5, bkgann=None, gain=1, edge=0, minarea=5, wcs=None, sn=3.0, verbose=False, checkimages=[], extra_params=[], extra_opts={}, catfile=None, _workdir=None, _tmpdir=None):
    # Find the binary
    binname = None
    for path in ['.', '/usr/bin', '/usr/local/bin', '/opt/local/bin']:
        for exe in ['sex', 'sextractor', 'source-extractor']:
            if os.path.isfile(posixpath.join(path, exe)):
                binname = posixpath.join(path, exe)
                break

    if binname is None:
        if verbose:
            print("Can't find SExtractor binary")
        return None

    workdir = _workdir if _workdir is not None else tempfile.mkdtemp(prefix='sex', dir=_tmpdir)
    obj = None

    # Prepare
    imagename = posixpath.join(workdir, 'image.fits')
    fits.writeto(imagename, image, header, overwrite=True)

    opts = {
        'VERBOSE_TYPE': 'QUIET',
        'DETECT_MINAREA': minarea,
        'GAIN': gain,
        'DETECT_THRESH': thresh,
        'WEIGHT_TYPE': 'BACKGROUND',
        'MASK_TYPE': 'NONE', # both 'CORRECT' and 'BLANK' seem to cause systematics?
    }

    if mask is None:
        mask = np.zeros_like(image, dtype=np.bool)

    flagsname = posixpath.join(workdir, 'flags.fits')
    fits.writeto(flagsname, mask.astype(np.int16), overwrite=True)
    opts['FLAG_IMAGE'] = flagsname

    if np.isscalar(aper):
        opts['PHOT_APERTURES'] = aper*2 # SExtractor expects diameters, not radii
        size = ''
    else:
        opts['PHOT_APERTURES'] = ','.join([str(_*2) for _ in aper])
        size = '[%d]' % len(aper)

    checknames = [posixpath.join(workdir, _.replace('-', 'M_') + '.fits') for _ in checkimages]
    if checkimages:
        opts['CHECKIMAGE_TYPE'] = ','.join(checkimages)
        opts['CHECKIMAGE_NAME'] = ','.join(checknames)

    params = ['MAG_APER'+size, 'MAGERR_APER'+size, 'FLUX_APER'+size, 'FLUXERR_APER'+size, 'X_IMAGE', 'Y_IMAGE', 'ERRX2_IMAGE', 'ERRY2_IMAGE', 'A_IMAGE', 'B_IMAGE', 'THETA_IMAGE', 'FLUX_RADIUS', 'FWHM_IMAGE', 'FLAGS', 'IMAFLAGS_ISO', 'BACKGROUND']
    params += extra_params
    paramname = posixpath.join(workdir, 'cfg.param')
    with open(paramname, 'w') as paramfile:
        paramfile.write("\n".join(params))
    opts['PARAMETERS_NAME'] = paramname

    catname = posixpath.join(workdir, 'out.cat')
    opts['CATALOG_NAME'] = catname
    opts['CATALOG_TYPE'] = 'FITS_LDAC'

    if not r0:
        opts['FILTER'] = 'N'
    else:
        kernel = make_kernel(r0, ext=1.0)
        kernelname = posixpath.join(workdir, 'kernel.txt')
        np.savetxt(kernelname, kernel/np.sum(kernel), fmt=b'%.6f', header='CONV NORM', comments='')
        opts['FILTER'] = 'Y'
        opts['FILTER_NAME'] = kernelname

    opts.update(extra_opts)

    # Build the command line
    # FIXME: quote strings!
    cmd = binname + ' ' + imagename + ' ' + ' '.join(['-%s %s' % (_,opts[_]) for _ in opts.keys()])
    if not verbose:
        cmd += ' > /dev/null 2>/dev/null'
    if verbose:
        print(cmd)

    # Run the command!

    res = os.system(cmd)

    if res == 0:
        data = fits.getdata(catname, -1)

        idx = (data['X_IMAGE'] > edge) & (data['X_IMAGE'] < image.shape[1] - edge)
        idx &= (data['Y_IMAGE'] > edge) & (data['Y_IMAGE'] < image.shape[0] - edge)

        if np.isscalar(aper):
            idx &= data['MAGERR_APER'] < 1.0/sn
            idx &= data['FLUX_APER'] > 0
        else:
            idx &= np.all(data['MAGERR_APER'] < 1.0/sn, axis=1)
            idx &= np.all(data['FLUX_APER'] > 0, axis=1)

        data = data[idx]

        if wcs is None and header is not None:
            wcs = WCS(header)

        if wcs is not None:
            ra,dec = wcs.all_pix2world(data['X_IMAGE'], data['Y_IMAGE'], 1)
        else:
            ra,dec = np.zeros_like(data['X_IMAGE']), np.zeros_like(data['Y_IMAGE'])

        data['FLAGS'][data['IMAFLAGS_ISO'] > 0] |= 256

        obj = Table({
            'x': data['X_IMAGE']-1, 'y': data['Y_IMAGE']-1,
            'xerr': np.sqrt(data['ERRX2_IMAGE']), 'yerr': np.sqrt(data['ERRY2_IMAGE']),
            'flux': data['FLUX_APER'], 'fluxerr': data['FLUXERR_APER'],
            'mag': data['MAG_APER'], 'magerr': data['MAGERR_APER'],
            'flags': data['FLAGS'], 'ra':ra, 'dec': dec,
            'bg': data['BACKGROUND'], 'fwhm': data['FWHM_IMAGE'],
            'a': data['A_IMAGE'], 'b': data['B_IMAGE'], 'theta': data['THETA_IMAGE'],
        })

        obj.meta['aper'] = aper

        obj.sort('flux', reverse=True)

        for _ in extra_params:
            obj[_] = data[_]

        if catfile is not None:
            shutil.copyfile(catname, catfile)
            if verbose:
                print("Catalogue stored to", catfile)

    else:
        if verbose:
            print("Error", res, "running SExtractor")

    result = obj

    if checkimages:
        result = [result]

        for name in checknames:
            result.append(fits.getdata(name))

    if _workdir is None:
        shutil.rmtree(workdir)

    return result

def make_series(mul=1.0, x=1.0, y=1.0, order=1, sum=False, zero=True):
    x = np.atleast_1d(x)
    y = np.atleast_1d(y)

    if zero:
        res = [np.ones_like(x)*mul]
    else:
        res = []

    for i in range(1,order+1):
        maxr = i+1

        for j in range(maxr):
            res.append(mul * x**(i-j) * y**j)
    if sum:
        return np.sum(res, axis=0)
    else:
        return res

def match(obj_ra, obj_dec, obj_mag, obj_magerr, obj_flags, cat_ra, cat_dec, cat_mag, cat_magerr=None, cat_color=None, sr=3/3600, obj_x=None, obj_y=None, spatial_order=0, threshold=5.0, verbose=False, robust=True):
    h = htm.HTM(10)

    oidx,cidx,dist = h.match(obj_ra, obj_dec, cat_ra, cat_dec, sr, maxmatch=0)

    if verbose:
        print(len(dist), 'initial matches between', len(obj_ra), 'objects and', len(cat_ra), 'catalogue stars, sr=', sr*3600, 'arcsec')
        print('Median separation is', np.median(dist)*3600, 'arcsec')

    omag, omag_err, oflags = obj_mag[oidx], obj_magerr[oidx], obj_flags[oidx]
    cmag = cat_mag[cidx].filled(fill_value=np.nan)
    cmag_err = cat_magerr[cidx].filled(fill_value=np.nan) if cat_magerr is not None else np.zeros_like(cmag)

    if obj_x is not None and obj_y is not None:
        x0, y0 = np.mean(obj_x[oidx]), np.mean(obj_y[oidx])
        x, y = obj_x[oidx] - x0, obj_y[oidx] - y0
    else:
        x0, y0 = 0, 0
        x, y = np.zeros_like(omag), np.zeros_like(omag)

    # Regressor
    X = make_series(1.0, x, y, order=spatial_order)

    if verbose:
        print('Fitting the model with spatial_order =', spatial_order)
        if robust:
            print('Using robust fitting')
        else:
            print('Using weighted fitting')

    if cat_color is not None:
        ccolor = cat_color[cidx].filled(fill_value=np.nan)
        X += make_series(ccolor, x, y, order=0)
        if verbose:
            print('Using color term')
    else:
        ccolor = np.zeros_like(cmag)

    X = np.vstack(X).T
    zero = cmag - omag # We will build a model for this definition of zero point
    zero_err = np.hypot(omag_err, cmag_err)
    weights = 1.0/zero_err**2

    idx0 = np.isfinite(omag) & np.isfinite(omag_err) & np.isfinite(cmag) & np.isfinite(cmag_err) & (oflags == 0) # initial mask
    if cat_color is not None:
        idx0 &= np.isfinite(ccolor)

    idx = idx0.copy()

    for iter in range(5):
        if np.sum(idx) < 3:
            if verbose:
                print("Fit failed - %d objects remaining" % np.sum(idx))
            return None

        if robust:
            C = sm.RLM(zero[idx], X[idx]).fit()
        else:
            C = sm.WLS(zero[idx], X[idx], weights=weights[idx]).fit()

        zero_model = np.sum(X*C.params, axis=1)

        idx = idx0.copy()
        if threshold:
            idx[idx0] &= (np.abs((zero - zero_model)/zero_err)[idx0] < threshold)

        if verbose:
            print('Iteration', iter, ':', np.sum(idx), '/', len(idx), '-', np.std((zero - zero_model)[idx0]), np.std((zero - zero_model)[idx]), '-', np.std((zero - zero_model)[idx]/zero_err[idx]))

        if not threshold:
            break

    if verbose:
        print(np.sum(idx), 'good matches')

    # Export the model
    def zero_fn(xx, yy):
        if xx is not None and yy is not None:
            x, y = xx - x0, yy - y0
        else:
            x, y = np.zeros_like(omag), np.zeros_like(omag)

        X = make_series(1.0, x, y, order=spatial_order)
        X = np.vstack(X).T

        return np.sum(X*C.params[0:X.shape[1]], axis=1)

    if cat_color is not None:
        X = make_series(order=spatial_order)
        color_term = C.params[len(X):][0]
        if verbose:
            print('Color term is', color_term)
    else:
        color_term = None

    return {'oidx': oidx, 'cidx': cidx, 'dist': dist,
            'omag': omag, 'omag_err': omag_err,
            'cmag': cmag, 'cmag_err': cmag_err,
            'color': ccolor, 'color_term': color_term,
            'zero': zero, 'zero_err': zero_err,
            'zero_model': zero_model, 'zero_fn': zero_fn,
            'obj_zero': zero_fn(obj_x, obj_y),
            'idx': idx, 'idx0': idx0}

def get_background(image, mask=None, method='sep', size=128, get_rms=False, **kwargs):
    if method == 'sep':
        bg = sep.Background(image, mask=mask, bw=size, bh=size, **kwargs)

        back,backrms = bg.back(), bg.rms()
    else: # photutils
        bg = photutils.Background2D(image, size, mask=mask, **kwargs)
        back,backrms = bg.background, bg.background_rms

    if get_rms:
        return back, back_rms
    else:
        return back