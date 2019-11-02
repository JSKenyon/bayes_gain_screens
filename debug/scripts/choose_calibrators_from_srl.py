from astropy.io import fits
import astropy.coordinates as ac
import astropy.units as au
from astropy import wcs
import pylab as plt
import numpy as np
from matplotlib.patches import Circle
import argparse
import os

def great_circle_sep(ra1, dec1, ra2, dec2):
    dra = np.abs(ra1 - ra2)
    # ddec = np.abs(dec1-dec2)
    num2 = (np.cos(dec2) * np.sin(dra)) ** 2 + (
                np.cos(dec1) * np.sin(dec2) - np.sin(dec1) * np.cos(dec2) * np.cos(dra)) ** 2
    den = np.sin(dec1) * np.sin(dec2) + np.cos(dec1) * np.cos(dec2) * np.cos(dra)
    return np.arctan2(np.sqrt(num2), den)


def get_screen_directions(image_fits, flux_limit=0.1, max_N=None, min_spacing_arcmin=1.,
                                     seed_directions=None, fill_in_distance=None,
                                     fill_in_flux_limit=0.):
    """Given a srl file containing the sources extracted from the apparent flux image of the field,
    decide the screen directions

    :param srl_fits: str
        The path to the srl file, typically created by pybdsf
    :return: float, array [N, 2]
        The `N` sources' coordinates as an ``astropy.coordinates.ICRS`` object
    """
    print("Getting screen directions from image.")
    if max_N is not None:
        if seed_directions is not None:
            max_N -= seed_directions.shape[0]
    with fits.open(image_fits) as hdul:
        # ra,dec, _, freq
        data = hdul[0].data
        w = wcs.WCS(hdul[0].header)
        #         Nra, Ndec,_,_ = data.shape
        where_limit = np.where(data >= flux_limit)
        arg_sort = np.argsort(data[where_limit])[::-1]

        ra = []
        dec = []
        f = []
        if seed_directions is not None:
            print("Using seed directions.")
            ra = list(seed_directions[:, 0])
            dec = list(seed_directions[:, 1])
        idx = []
        for i in arg_sort:
            pix = [where_limit[3][i], where_limit[2][i], where_limit[1][i], where_limit[0][i]]
            #             logging.info("{} -> {}".format(i, pix))
            #             pix = np.reshape(np.array(np.unravel_index(i, [Nra, Ndec, 1, 1])), (1, 4))
            coords = w.wcs_pix2world([pix], 1)  # degrees
            ra_ = coords[0, 0] * np.pi / 180.
            dec_ = coords[0, 1] * np.pi / 180.

            if len(ra) == 0:
                ra.append(ra_)
                dec.append(dec_)
                f.append(data[pix[3], pix[2], pix[1], pix[0]])
                print("Found {} at {} {}".format(f[-1], ra[-1] * 180. / np.pi, dec[-1] * 180. / np.pi))

                idx.append(i)
                continue
            dist = great_circle_sep(np.array(ra), np.array(dec), ra_, dec_) * 180. / np.pi
            if np.all(dist > min_spacing_arcmin / 60.):
                ra.append(ra_)
                dec.append(dec_)
                f.append(data[pix[3], pix[2], pix[1], pix[0]])
                print("Found {} at {} {}".format(f[-1], ra[-1] * 180. / np.pi, dec[-1] * 180. / np.pi))
                idx.append(i)
                continue
            if max_N is not None:
                if len(idx) > max_N:
                    break

        first_found = len(idx)

        if fill_in_distance is not None:
            where_limit = np.where(np.logical_and(data < np.min(f), data >= fill_in_flux_limit))
            arg_sort = np.argsort(data[where_limit])[::-1]
            # use remaining brightest sources to get fillers
            for i in arg_sort:
                pix = [where_limit[3][i], where_limit[2][i], where_limit[1][i], where_limit[0][i]]
                #                 logging.info("{} -> {}".format(i, pix))
                coords = w.wcs_pix2world([pix], 1)  # degrees

                ra_ = coords[0, 0] * np.pi / 180.
                dec_ = coords[0, 1] * np.pi / 180.

                dist = great_circle_sep(np.array(ra), np.array(dec), ra_, dec_) * 180. / np.pi
                if np.all(dist > fill_in_distance / 60.):
                    ra.append(ra_)
                    dec.append(dec_)
                    f.append(data[pix[3], pix[2], pix[1], pix[0]])
                    print(
                        "Found filler {} at {} {}".format(f[-1], ra[-1] * 180. / np.pi, dec[-1] * 180. / np.pi))
                    idx.append(i)
                    continue
                if max_N is not None:
                    if len(idx) > max_N:
                        break

        if max_N is not None:
            arg = np.argsort(f)[::-1][:max_N]
            f = np.array(f)[arg]
            ra = np.array(ra)[arg]
            dec = np.array(dec)[arg]
        sizes = np.ones(len(idx))
        sizes[:first_found] = 120.
        sizes[first_found:] = 240.

    print("Found {} sources.".format(len(ra)))
    if seed_directions is not None:
        ra = list(seed_directions[:, 0]) + list(ra)
        dec = list(seed_directions[:, 1]) + list(dec)
    return ac.ICRS(ra=ra * au.rad, dec=dec * au.rad), sizes


def write_reg_file(filename, radius_arcsec, directions, color='green'):
    if not isinstance(radius_arcsec, (list, tuple)):
        radius_arcsec = [radius_arcsec] * len(directions)
    with open(filename, 'w') as f:
        f.write('# Region file format: DS9 version 4.1\n')
        f.write(
            'global color={color} dashlist=8 3 width=1 font="helvetica 10 normal roman" select=1 highlite=1 dash=0 fixed=0 edit=1 move=1 delete=1 include=1 source=1\n'.format(
                color=color))
        f.write('fk5\n')
        for r, d in zip(radius_arcsec, directions):
            f.write('circle({},{},{}")\n'.format(
                d.ra.to_string(unit=au.hour, sep=(":", ":"), alwayssign=False, precision=3),
                d.dec.to_string(unit=au.deg, sep=(":", ":"), alwayssign=True, precision=2), r))


def add_args(parser):
    parser.register("type", "bool", lambda v: v.lower() == "true")

    parser.add_argument('--working_dir', default='./', help='Where to store things like output', required=False,
                        type=str)
    parser.add_argument('--region_file', help='boxfile, required argument', required=True, type=str)
    parser.add_argument('--image_fits',
                        help='image of field.',
                        type=str, required=True)
    parser.add_argument('--flux_limit', help='Peak flux cut off for source selection.',
                        default=0.15, type=float)
    parser.add_argument('--max_N', help='Max num of sources',
                        default=None, type=int, required=False)

    parser.add_argument('--min_spacing_arcmin', help='Min distance in arcmin of sources.',
                        default=10., type=float, required=False)
    parser.add_argument('--plot', help='Whether to plot.',
                        default=False, type="bool", required=False)
    parser.add_argument('--fill_in_distance',
                        help='If not None then uses fainter sources to fill in some areas further than fill_in_distance from nearest selected source in arcmin.',
                        default=None, type=float, required=False)
    parser.add_argument('--min_spacing_arcmin',
                        help='If fill_in_distance is not None then this is the secondary peak flux cutoff for fill in sources.',
                        default=0.05, type=float, required=False)


def main(working_dir, region_file, image_fits,
         flux_limit, max_N, min_spacing_arcmin, plot, fill_in_distance, fill_in_flux_limit):
    region_file = os.path.join(os.path.abspath(working_dir), os.path.basename(region_file))
    directions, sizes = get_screen_directions(image_fits=image_fits,
                                              flux_limit=flux_limit, max_N=max_N, min_spacing_arcmin=min_spacing_arcmin,
                                              plot=plot,
                                              fill_in_distance=fill_in_distance, fill_in_flux_limit=fill_in_flux_limit)
    write_reg_file(region_file, sizes, directions, 'red')


def lockman_run():
    main(working_dir='./', region_file='LH_auto_select.reg', image_fits='lockman_deep_archive.pybdsm.srl.fits',
         flux_limit=0.15, max_N=None, min_spacing_arcmin=10., plot=False, fill_in_distance=60.,
         fill_in_flux_limit=0.05)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Keep soures inside box region, subtract everything else and create new ms',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_args(parser)
    flags, unparsed = parser.parse_known_args()
    print("Running with:")
    for option, value in vars(flags).items():
        print("    {} -> {}".format(option, value))
    main(**vars(flags))
