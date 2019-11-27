import os

os.environ['OMP_NUM_THREADS'] = "1"
import numpy as np
import pylab as plt
from scipy.interpolate import griddata
from scipy.optimize import brute, minimize
from scipy.ndimage import median_filter
from bayes_gain_screens import logging
from bayes_gain_screens.datapack import DataPack
from bayes_gain_screens.misc import make_soltab, great_circle_sep
from bayes_gain_screens.plotting import animate_datapack
from bayes_gain_screens.nlds_smoother import Update, NLDSSmoother, update_step, build_get_params
from dask.multiprocessing import get
from scipy.optimize import least_squares
import argparse
from timeit import default_timer
import networkx as nx

"""
This script is still being debugged/tested. 
Get's TEC from gains.
"""

def sequential_solve(Yreal, Yimag, freqs, working_dir):
    """
    Run on blocks of time.

    :param Yreal:
        [D, Nf, Nt]
    :param Yimag:
    :param freqs:
    :return:
        [D, Nt], [D, Nt]
    """
    loss_dir = os.path.join(working_dir, 'losses')
    os.makedirs(loss_dir, exist_ok=True)

    D, Nf, N = Yreal.shape

    tec_mean_array = np.zeros((D, N))
    tec_uncert_array = np.zeros((D, N))

    update = Update(freqs, S=200)

    for d in range(D):
        Sigma_0 = 1 ** 2 * np.eye(2 * Nf)
        Omega_0 = np.diag([50., 0.1]) ** 2
        mu_0 = np.array([0., 0.])
        Gamma_0 = np.diag([200., 2 * np.pi]) ** 2
        ###
        # warm-up
        # B, Nf
        Y_warmup = np.transpose(Yreal[d, :, : 50] + 1j * Yimag[d, :, :50])
        res = NLDSSmoother(2, Nf, N, update=update, momentum=0.9).run(Y_warmup, Sigma_0, Omega_0, mu_0,
                                                                      Gamma_0, 10)
        Sigma_0 = res['Sigma']
        Omega_0 = res['Omega']

        for t in range(0, N, 50):
            start = t
            stop = min(t + 50, N)
            # N, Nf
            Y = np.transpose(Yreal[d, :, start:stop] + 1j * Yimag[d, :, start:stop])
            res = NLDSSmoother(2, Nf, N, update=update, momentum=0.1).run(Y, Sigma_0, Omega_0,
                                                                          mu_0,
                                                                          Gamma_0, 1)
            Sigma_0 = res['Sigma']
            Omega_0 = res['Omega']
            mu_0 = res['post_mu'][-1,:]
            Gamma_0 = res['post_Gamma'][-1,:,:] + np.diag([200., 2 * np.pi]) ** 2
            tec_mean_array[d, start:stop] = res['post_mu'][:, 0]
            tec_uncert_array[d, start:stop] = np.sqrt(res['post_Gamma'][:, 0, 0])


    return tec_mean_array, tec_uncert_array


def smoothamps(amps):
    freqkernel = 3
    timekernel = 31
    idxh = np.where(amps > 5.)
    idxl = np.where(amps < 0.15)
    median = np.tile(np.nanmedian(amps, axis=-1, keepdims=True), (1, 1, 1, 1, amps.shape[-1]))
    amps[idxh] = median[idxh]
    amps[idxl] = median[idxl]
    ampssmoothed = np.exp((median_filter(np.log(amps), size=(1, 1, 1, freqkernel, timekernel), mode='reflect')))
    return ampssmoothed


def main(data_dir, working_dir, obs_num, ref_dir, ncpu):
    os.chdir(working_dir)
    logging.info("Performing TEC and constant variational inference.")
    merged_h5parm = os.path.join(data_dir, 'L{}_{}_merged.h5'.format(obs_num, 'DDS4_full'))
    select = dict(pol=slice(0, 1, 1))
    datapack = DataPack(merged_h5parm, readonly=False)
    logging.info("Creating directionally_referenced/tec000+const000")
    make_soltab(datapack, from_solset='sol000', to_solset='directionally_referenced', from_soltab='phase000',
                to_soltab=['tec000', 'const000'])
    logging.info("Getting raw phase")
    datapack.current_solset = 'sol000'
    datapack.select(**select)
    axes = datapack.axes_phase
    antenna_labels, antennas = datapack.get_antennas(axes['ant'])
    patch_names, directions = datapack.get_directions(axes['dir'])
    radec = np.stack([directions.ra.rad, directions.dec.rad], axis=1)
    timestamps, times = datapack.get_times(axes['time'])
    freq_labels, freqs = datapack.get_freqs(axes['freq'])
    pol_labels, pols = datapack.get_pols(axes['pol'])
    Npol, Nd, Na, Nf, Nt = len(pols), len(directions), len(antennas), len(freqs), len(times)
    phase_raw, axes = datapack.phase
    logging.info("Getting smooth phase and amplitude data")
    datapack.current_solset = 'smoothed000'
    datapack.select(**select)
    phase_smooth, axes = datapack.phase
    amp_smooth, axes = datapack.amplitude

    tec_conv = -8.4479745e6 / freqs
    tec_mean_array = np.zeros((Npol, Nd, Na, Nt))
    tec_uncert_array = np.zeros((Npol, Nd, Na, Nt))
    g = nx.complete_graph(radec.shape[0])
    for u, v in g.edges:
        g[u][v]['weight'] = great_circle_sep(*radec[u, :], *radec[v, :])
    h = nx.minimum_spanning_tree(g)
    walk_order = [(0,0)]+list(nx.bfs_edges(h, 0))

    for (ref_dir, solve_dir) in walk_order:
        logging.info("Solving dir: {}".format(solve_dir))
        phase_di = phase_smooth[:, ref_dir:ref_dir+1, ...]
        logging.info("Referencing dir: {}".format(ref_dir))
        phase_dd = phase_raw[:, solve_dir:solve_dir+1, ...] - phase_di

        # Npol, 1, Na, Nf, Nt
        Yimag = amp_smooth * np.sin(phase_dd)
        Yreal = amp_smooth * np.cos(phase_dd)
        Yimag = Yimag.reshape((-1, Nf, Nt))
        Yreal = Yreal.reshape((-1, Nf, Nt))

        logging.info("Building dask")
        D = Yimag.shape[0]
        num_processes = min(D, ncpu)
        dsk = {}
        keys = []
        for c,i in enumerate(range(0, D, D // num_processes)):
            start = i
            stop = min(i + (D // num_processes), D)
            dsk[str(c)] = (sequential_solve, Yreal[start:stop, :, :], Yimag[start:stop, :, :], freqs, working_dir)
            keys.append(str(c))
        logging.info("Running dask on {} processes".format(num_processes))
        results = get(dsk, keys, num_workers=num_processes)
        logging.info("Finished dask.")
        tec_mean = np.zeros((D, Nt))
        tec_uncert = np.zeros((D, Nt))
        for c, i in enumerate(range(0, D, D // num_processes)):
            start = i
            stop = min(i + (D // num_processes), D)
            tec_mean[start:stop, :] = results[c][0]
            tec_uncert[start:stop, :] = results[c][1]
        tec_mean = tec_mean.reshape((Npol, 1, Na, Nt))
        tec_uncert = tec_uncert.reshape((Npol, 1, Na, Nt))
        logging.info("Re-referencing to 0")
        phase_smooth[:, solve_dir:solve_dir+1, ...] = tec_mean[..., None, :] * tec_conv[:, None] + phase_di
        #Reference to ref_dir 0
        tec_mean_array[:, solve_dir:solve_dir+1,...] = tec_mean + tec_mean_array[:,ref_dir:ref_dir+1,...]
        tec_uncert_array[:, solve_dir:solve_dir+1, ...] = tec_uncert
    
    

    phase_smooth_uncert = np.abs(tec_conv[:, None] * tec_uncert_array[..., None, :])

    res_real = amp_smooth * (np.cos(phase_smooth) - np.cos(phase_raw))
    res_imag = amp_smooth * (np.sin(phase_smooth) - np.sin(phase_raw))

    logging.info("Updating smoothed phase")
    datapack.current_solset = 'smoothed000'
    datapack.select(**select)
    datapack.phase = phase_smooth
    datapack.weights_phase = phase_smooth_uncert
    logging.info("Storing TEC and const")
    datapack.current_solset = 'directionally_referenced'
    # Npol, Nd, Na, Nf, Nt
    datapack.select(**select)
    datapack.tec = tec_mean_array
    datapack.weights_tec = tec_uncert_array
    logging.info("Done ddtec VI.")

    animate_datapack(merged_h5parm, os.path.join(working_dir, 'tec_plots'), num_processes=ncpu,
                     solset='directionally_referenced',
                     observable='tec', vmin=-60., vmax=60., plot_facet_idx=True,
                     labels_in_radec=True, plot_crosses=False, phase_wrap=False,
                     flag_outliers=False)

    plot_results(Na, Nd, antenna_labels, working_dir, phase_smooth, phase_raw,
                 res_imag, res_real, tec_mean_array)

def wrap(p):
    return np.arctan2(np.sin(p), np.cos(p))

def plot_results(Na, Nd, antenna_labels, working_dir, phase_model,
                 phase_raw, res_imag, res_real, tec_mean_array):
    logging.info("Plotting results.")
    summary_dir = os.path.join(working_dir, 'summaries')
    os.makedirs(summary_dir, exist_ok=True)
    for i in range(Na):
        for j in range(Nd):
            slice_phase_data = wrap(phase_raw[0, j, i, :, :])
            slice_phase_model = wrap(phase_model[0, j, i, :, :])
            slice_res_real = res_real[0, j, i, :, :]
            slice_res_imag = res_imag[0, j, i, :, :]
            time_array = np.arange(slice_res_real.shape[-1])
            colors = plt.cm.jet(np.arange(slice_res_real.shape[-1]) / slice_res_real.shape[-1])
            # Nf, Nt
            _slice_res_real = slice_res_real - np.mean(slice_res_real, axis=0)
            _slice_res_imag = slice_res_imag - np.mean(slice_res_imag, axis=0)
            slice_tec = tec_mean_array[0, j, i, :]
            fig, axs = plt.subplots(2, 2, figsize=(15, 15))
            diff_phase = wrap(wrap(slice_phase_data) - wrap(slice_phase_model))
            for nu in range(slice_res_real.shape[-2]):
                f_c = plt.cm.binary((nu + 1) / slice_res_real.shape[-2])
                colors_ = (f_c + colors) / 2. * np.array([1., 1., 1., 1. - (nu + 1) / slice_res_real.shape[-2]])
                axs[0][0].scatter(time_array, np.abs(slice_res_real[nu, :]), c=colors_, marker='.')
                axs[0][0].scatter(time_array, -np.abs(slice_res_imag[nu, :]), c=colors_, marker='.')
            axs[0][0].set_title("Real and Imag residuals")
            axs[0][0].hlines(0., time_array.min(), time_array.max())
            axs[0][1].imshow(diff_phase, origin='lower', aspect='auto', cmap='coolwarm', vmin=-0.2,
                             vmax=0.2)
            axs[0][1].set_title('Phase residuals [-0.2,0.2]')
            axs[0][1].set_xlabel('Time')
            axs[0][1].set_ylabel('Freq')
            axs[1][0].scatter(time_array, slice_tec, c=colors)
            axs[1][0].set_title("TEC")
            axs[1][0].set_xlabel('Time')
            axs[1][0].set_ylabel('TEC [mTECU]')
            plt.tight_layout()
            plt.savefig(os.path.join(summary_dir, 'summary_{}_dir{:02d}.png'.format(antenna_labels[i].decode(), j)))
            plt.close('all')


def add_args(parser):
    parser.add_argument('--obs_num', help='Obs number L*',
                        default=None, type=int, required=True)
    parser.add_argument('--data_dir', help='Where are the ms files are stored.',
                        default=None, type=str, required=True)
    parser.add_argument('--working_dir', help='Where to perform the imaging.',
                        default=None, type=str, required=True)
    parser.add_argument('--ncpu', help='How many processors available.',
                        default=None, type=int, required=True)
    parser.add_argument('--ref_dir', help='The index of reference dir.',
                        default=0, type=int, required=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Variational inference of DDTEC and a constant term. Updates the smoothed000 solset too.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_args(parser)
    flags, unparsed = parser.parse_known_args()
    print("Running with:")
    for option, value in vars(flags).items():
        print("    {} -> {}".format(option, value))
    main(**vars(flags))
