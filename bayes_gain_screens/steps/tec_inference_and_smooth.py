import os
import sys
import numpy as np
import pylab as plt
import argparse
from timeit import default_timer
from jax import numpy as jnp, jit, random, vmap
import logging
import astropy.units as au

from bayes_gain_screens.utils import chunked_pmap

logger = logging.getLogger(__name__)

from bayes_gain_screens.plotting import animate_datapack, add_colorbar_to_axes

from h5parm import DataPack
from h5parm.utils import make_soltab

from jaxns.prior_transforms import UniformPrior, PriorChain, HalfLaplacePrior, DeterministicTransformPrior
from jaxns.nested_sampling import NestedSampler


def polyfit(x, y, deg):
    """
    x : array_like, shape (M,)
        x-coordinates of the M sample points ``(x[i], y[i])``.
    y : array_like, shape (M,) or (M, K)
        y-coordinates of the sample points. Several data sets of sample
        points sharing the same x-coordinates can be fitted at once by
        passing in a 2D-array that contains one dataset per column.
    deg : int
        Degree of the fitting polynomial
    Returns
    -------
    p : ndarray, shape (deg + 1,) or (deg + 1, K)
        Polynomial coefficients, highest power first.  If `y` was 2-D, the
        coefficients for `k`-th data set are in ``p[:,k]``.
    """
    order = int(deg) + 1
    if deg < 0:
        raise ValueError("expected deg >= 0")
    if x.ndim != 1:
        raise TypeError("expected 1D vector for x")
    if x.size == 0:
        raise TypeError("expected non-empty vector for x")
    if y.ndim < 1 or y.ndim > 2:
        raise TypeError("expected 1D or 2D array for y")
    if x.shape[0] != y.shape[0]:
        raise TypeError("expected x and y to have same length")
    rcond = len(x) * jnp.finfo(x.dtype).eps
    lhs = jnp.stack([x ** (deg - i) for i in range(order)], axis=1)
    rhs = y
    scale = jnp.sqrt(jnp.sum(lhs * lhs, axis=0))
    lhs /= scale
    c, resids, rank, s = jnp.linalg.lstsq(lhs, rhs, rcond)
    c = (c.T / scale).T  # broadcast scale coefficients
    return c


def poly_smooth(x, y, deg=5):
    """
    Smooth y(x) with a `deg` degree polynomial in x
    Args:
        x: [N]
        y: [N]
        deg: int

    Returns: smoothed y [N]
    """
    coeffs = polyfit(x, y, deg=deg)
    return sum([p * x ** (deg - i) for i, p in enumerate(coeffs)])


def get_data(solution_file):
    logger.info("Getting DDS4 data.")
    with DataPack(solution_file, readonly=True) as h:
        select = dict(pol=slice(0, 1, 1))
        h.select(**select)
        phase, axes = h.phase
        phase = phase[0, ...]
        amp, axes = h.amplitude
        amp = amp[0, ...]
        _, freqs = h.get_freqs(axes['freq'])
        freqs = freqs.to(au.Hz).value
        _, times = h.get_times(axes['time'])
        times = times.mjd / 86400.
        logger.info("Shape: {}".format(phase.shape))

        (Nd, Na, Nf, Nt) = phase.shape

        @jit
        def smooth(amp):
            '''
            Smooth amplitudes
            Args:
                amp: [Nt, Nf]
            '''
            log_amp = jnp.log(amp)
            log_amp = vmap(lambda log_amp: poly_smooth(times, log_amp, deg=3))(log_amp.T).T
            log_amp = vmap(lambda log_amp: poly_smooth(freqs, log_amp, deg=3))(log_amp)
            amp = jnp.exp(log_amp)
            return amp

        logger.info("Smoothing amplitudes")
        amp = chunked_pmap(smooth, amp.reshape((Nd * Na, Nf, Nt)).transpose((0, 2, 1)))  # Nd*Na,Nt,Nf
        amp = amp.transpose((0, 2, 1)).reshape((Nd, Na, Nf, Nt))
        Y_obs = jnp.concatenate([amp * jnp.cos(phase), amp * jnp.sin(phase)], axis=2)
    return Y_obs, times, freqs


def prepare_soltabs(dds4_h5parm, dds5_h5parm):
    logger.info("Creating sol000/phase000+amplitude000+tec000+const000+clock000")
    make_soltab(dds4_h5parm, from_solset='sol000', to_solset='sol000', from_soltab='phase000',
                to_soltab=['phase000', 'amplitude000', 'tec000', 'const000', 'clock000'], remake_solset=True,
                to_datapack=dds5_h5parm)


def log_laplace(x, mean, uncert):
    dx = (x - mean) / uncert
    return - jnp.log(2. * uncert) - jnp.abs(dx)


def unconstrained_solve(freqs, key, Y_obs, amp):
    TEC_CONV = -8.4479745e6  # mTECU/Hz

    def log_likelihood(tec, const, clock, uncert, **kwargs):
        phase = tec * (TEC_CONV / freqs) + const + clock * (1e-9 * 2. * jnp.pi) * freqs
        return jnp.sum(log_laplace(amp * jnp.cos(phase), Y_obs[:freqs.size], uncert)
                       + log_laplace(amp * jnp.sin(phase), Y_obs[freqs.size:], uncert))

    prior_chain = PriorChain() \
        .push(UniformPrior('tec', -300., 300.)) \
        .push(UniformPrior('const', -jnp.pi, jnp.pi)) \
        .push(UniformPrior('clock', -1., 1.)) \
        .push(HalfLaplacePrior('uncert', 0.2))

    ns = NestedSampler(log_likelihood, prior_chain,
                       sampler_name='slice',
                       uncert_mean=lambda uncert, **kwargs: uncert,
                       tec_mean=lambda tec, **kwargs: tec,
                       const_mean=lambda const, **kwargs: jnp.concatenate([jnp.cos(const), jnp.sin(const)]),
                       clock_mean=lambda clock, **kwargs: clock,
                       )

    results = ns(key=key,
                 num_live_points=100,
                 max_samples=1e5,
                 collect_samples=False,
                 termination_frac=0.01,
                 sampler_kwargs=dict(depth=1, num_slices=3))
    const_mean = jnp.arctan2(results.marginalised['const_mean'][1], results.marginalised['const_mean'][0])
    clock_mean = results.marginalised['clock_mean']
    return (const_mean, clock_mean)


def constrained_solve(freqs, key, Y_obs, amp, const_mu, clock_mu):
    TEC_CONV = -8.4479745e6  # mTECU/Hz

    def log_likelihood(Y, uncert, **kwargs):
        return jnp.sum(log_laplace(Y, Y_obs, uncert))

    tec = UniformPrior('tec', -300., 300.)

    def Y_transform(tec):
        phase = tec * (TEC_CONV / freqs) + const_mu + clock_mu * 1e-9 * (2. * jnp.pi * freqs)
        return jnp.concatenate([amp * jnp.cos(phase), amp * jnp.sin(phase)])

    prior_chain = PriorChain() \
        .push(tec) \
        .push(HalfLaplacePrior('uncert', 0.2)) \
        .push(DeterministicTransformPrior('Y', Y_transform, (freqs.size * 2,), tec))

    ns = NestedSampler(log_likelihood, prior_chain,
                       sampler_name='slice',
                       tec_mean=lambda tec, **kwargs: tec,
                       tec2_mean=lambda tec, **kwargs: tec ** 2,
                       # Y_mean=lambda Y, **kwargs: Y,
                       # Y2_mean=lambda Y, **kwargs: Y ** 2
                       )

    results = ns(key=key,
                 num_live_points=100,
                 max_samples=1e5,
                 collect_samples=False,
                 termination_frac=0.01,
                 sampler_kwargs=dict(depth=1, num_slices=3))

    tec_mean = results.marginalised['tec_mean']
    tec_std = jnp.sqrt(results.marginalised['tec2_mean'] - results.marginalised['tec_mean'] ** 2)
    phase_mean = tec_mean * (TEC_CONV / freqs) + const_mu + clock_mu * 1e-9 * (2. * jnp.pi * freqs)
    # Y_mean = results.marginalised['Y_mean']
    # Y_std = jnp.sqrt(results.marginalised['Y2_mean'] - results.marginalised['Y_mean'] ** 2)

    return (tec_mean, tec_std, phase_mean)


def solve_and_smooth(Y_obs, times, freqs):
    Nd, Na, twoNf, Nt = Y_obs.shape
    Nf = twoNf // 2
    Y_obs = Y_obs.transpose((0, 1, 3, 2)).reshape((Nd * Na * Nt, 2 * Nf))  # Nd*Na*Nt, 2*Nf
    amp = jnp.sqrt(Y_obs[:, :freqs.size] ** 2 + Y_obs[:, freqs.size:] ** 2)
    logger.info("Min/max amp: {} {}".format(jnp.min(amp), jnp.max(amp)))
    logger.info("Number of nan: {}".format(jnp.sum(jnp.isnan(Y_obs))))
    logger.info("Number of inf: {}".format(jnp.sum(jnp.isinf(Y_obs))))
    T = Y_obs.shape[0]
    logger.info("Performing solve for tec, const, clock.")
    # print(freqs, random.split(random.PRNGKey(int(default_timer())), T)[223], Y_obs[223], amp[223])
    # print(unconstrained_solve(freqs, random.split(random.PRNGKey(int(default_timer())), T)[223], Y_obs[223], amp[223]))
    # return
    # from jax import disable_jit
    # with disable_jit():
    const_mean, clock_mean = chunked_pmap(lambda *args: unconstrained_solve(freqs, *args),
                                          random.split(random.PRNGKey(int(746583)), T),
                                          Y_obs, amp, chunksize=1)

    def smooth(y):
        y = y.reshape((Nd * Na, Nt))  # Nd*Na,Nt
        y = chunked_pmap(lambda y: poly_smooth(times, y, deg=3), y).reshape(
            (Nd * Na * Nt,))  # Nd*Na*Nt
        return y

    logger.info("Smoothing const and clock (a strong prior).")
    # Nd*Na*Nt
    clock_mean = smooth(clock_mean)
    const_mean = smooth(const_mean)

    logger.info("Performing tec-only solve, with fixed const and clock.")
    (tec_mean, tec_std, phase_mean) = \
        chunked_pmap(lambda *args: constrained_solve(freqs, *args),
                     random.split(random.PRNGKey(int(default_timer())), T), Y_obs,
                     amp, const_mean, clock_mean)
    phase_mean = phase_mean.reshape((Nd, Na, Nt, Nf)).transpose((0, 1, 3, 2))
    amp = amp.reshape((Nd, Na, Nt, Nf)).transpose((0, 1, 3, 2))
    tec_mean = tec_mean.reshape((Nd, Na, Nt))
    tec_std = tec_std.reshape((Nd, Na, Nt))
    const_mean = const_mean.reshape((Nd, Na, Nt))
    clock_mean = clock_mean.reshape((Nd, Na, Nt))

    return phase_mean, amp, tec_mean, tec_std, const_mean, clock_mean


def link_overwrite(src, dst):
    if os.path.islink(dst):
        print("Unlinking pre-existing sym link {}".format(dst))
        os.unlink(dst)
    print("Linking {} -> {}".format(src, dst))
    os.symlink(src, dst)


def wrap(phi):
    return (phi + jnp.pi) % (2 * jnp.pi) - jnp.pi

def main(data_dir, working_dir, obs_num, ncpu):
    os.environ['XLA_FLAGS'] = f"--xla_force_host_platform_device_count={ncpu}"
    logger.info("Performing data smoothing via tec+const+clock inference.")
    dds4_h5parm = os.path.join(data_dir, 'L{}_DDS4_full_merged.h5'.format(obs_num))
    dds5_h5parm = os.path.join(working_dir, 'L{}_DDS5_full_merged.h5'.format(obs_num))
    linked_dds5_h5parm = os.path.join(data_dir, 'L{}_DDS5_full_merged.h5'.format(obs_num))
    logger.info("Looking for {}".format(dds4_h5parm))
    link_overwrite(dds5_h5parm, linked_dds5_h5parm)
    prepare_soltabs(dds4_h5parm, dds5_h5parm)
    Y_obs, times, freqs = get_data(solution_file=dds4_h5parm)
    phase_mean, amp_mean, tec_mean, tec_std, const_mean, clock_mean = solve_and_smooth(Y_obs, times, freqs)
    logger.info("Storing smoothed phase, amplitudes, tec, const, and clock")
    with DataPack(dds5_h5parm, readonly=False) as h:
        h.current_solset = 'sol000'
        h.select(pol=slice(0, 1, 1))
        h.phase = np.asarray(phase_mean)[None, ...]
        h.amplitude = np.asarray(amp_mean)[None, ...]
        h.tec = np.asarray(tec_mean)[None, ...]
        h.weights_tec = np.asarray(tec_std)[None, ...]
        h.const = np.asarray(const_mean)[None, ...]
        h.clock = np.asarray(clock_mean)[None, ...]
        axes = h.axes_phase
        patch_names, _ = h.get_directions(axes['dir'])
        antenna_labels, _ = h.get_antennas(axes['ant'])

    Y_mean = jnp.concatenate([amp_mean * jnp.cos(phase_mean), amp_mean * jnp.sin(phase_mean)], axis=-2)

    logger.info("Plotting results.")
    data_plot_dir = os.path.join(working_dir, 'data_plots')
    os.makedirs(data_plot_dir, exist_ok=True)
    Nd, Na, Nf, Nt = phase_mean.shape

    for ia in range(Na):
        for id in range(Nd):
            fig, axs = plt.subplots(3, 1, sharex=True)

            axs[0].plot(times, tec_mean[:, ia, id], c='black', label='tec')
            axs[0].plot(times, tec_mean[:, ia, id] + tec_std[:, ia, id], ls='dotted', c='black')
            axs[0].plot(times, tec_mean[:, ia, id] - tec_std[:, ia, id], ls='dotted', c='black')

            axs[1].plot(times, const_mean[:, ia, id], c='black', label='const')
            # axs[1].plot(times, const_mean[:, ia, id] + const_std[:, ia, id], ls='dotted', c='black')
            # axs[1].plot(times, const_mean[:, ia, id] - const_std[:, ia, id], ls='dotted', c='black')

            axs[2].plot(times, clock_mean[:, ia, id], c='black', label='clock')
            # axs[2].plot(times, clock_mean[:, ia, id] + clock_std[:, ia, id], ls='dotted', c='black')
            # axs[2].plot(times, clock_mean[:, ia, id] - clock_std[:, ia, id], ls='dotted', c='black')

            axs[0].legend()
            axs[1].legend()
            axs[2].legend()

            axs[0].set_ylabel("DTEC [mTECU]")
            axs[1].set_ylabel("phase [rad]")
            axs[2].set_ylabel("delay [ns]")
            axs[2].set_xlabel("time [s]")

            fig.savefig(os.path.join(data_plot_dir, 'sol_ant{:02d}_dir{:02d}.png'.format(ia, id)))
            plt.close("all")

            fig, axs = plt.subplots(3, 1)

            vmin = jnp.percentile(Y_mean[:, :, ia, id], 2)
            vmax = jnp.percentile(Y_mean[:, :, ia, id], 98)

            axs[0].imshow(Y_obs[:, :, ia, id].T, vmin=vmin, vmax=vmax, cmap='PuOr', aspect='auto',
                          interpolation='nearest')
            axs[0].set_title("Y_obs")
            add_colorbar_to_axes(axs[0], "PuOr", vmin=vmin, vmax=vmax)

            axs[1].imshow(Y_mean[:, :, ia, id].T, vmin=vmin, vmax=vmax, cmap='PuOr', aspect='auto',
                          interpolation='nearest')
            axs[1].set_title("Y mean")
            add_colorbar_to_axes(axs[1], "PuOr", vmin=vmin, vmax=vmax)

            # vmin = jnp.percentile(Y_std[:, :, ia, id], 2)
            # vmax = jnp.percentile(Y_std[:, :, ia, id], 98)
            #
            # axs[2].imshow(Y_std[:, :, ia, id].T, vmin=vmin, vmax=vmax, cmap='PuOr', aspect='auto',
            #               interpolation='nearest')
            # axs[2].set_title("Y std")
            # add_colorbar_to_axes(axs[2], "PuOr", vmin=vmin, vmax=vmax)

            phase_obs = jnp.arctan2(Y_obs[:, freqs.size:, ia, id], Y_obs[:, :freqs.size, ia, id])
            phase = jnp.arctan2(Y_mean[:, freqs.size:, ia, id], Y_mean[:, :freqs.size, ia, id])
            dphase = wrap(phase - phase_obs)

            vmin = -0.3
            vmax = 0.3

            axs[2].imshow(dphase.T, vmin=vmin, vmax=vmax, cmap='coolwarm', aspect='auto',
                          interpolation='nearest')
            axs[2].set_title("diff phase")
            add_colorbar_to_axes(axs[2], "coolwarm", vmin=vmin, vmax=vmax)

            fig.savefig(os.path.join(data_plot_dir,'gains_ant{:02d}_dir{:02d}.png'.format(ia, id)))
            plt.close("all")

    animate_datapack(dds5_h5parm, os.path.join(working_dir, 'tec_plots'), num_processes=ncpu,
                     solset='sol000',
                     observable='tec', vmin=-60., vmax=60., plot_facet_idx=True,
                     labels_in_radec=True, plot_crosses=False, phase_wrap=False,
                     flag_outliers=False)

    animate_datapack(dds5_h5parm, os.path.join(working_dir, 'const_plots'), num_processes=ncpu,
                     solset='sol000',
                     observable='const', vmin=-np.pi, vmax=np.pi, plot_facet_idx=True,
                     labels_in_radec=True, plot_crosses=False, phase_wrap=True,
                     flag_outliers=False)

    animate_datapack(dds5_h5parm, os.path.join(working_dir, 'clock_plots'), num_processes=ncpu,
                     solset='sol000',
                     observable='clock', vmin=-1., vmax=1., plot_facet_idx=True,
                     labels_in_radec=True, plot_crosses=False, phase_wrap=False,
                     flag_outliers=False)

    animate_datapack(dds5_h5parm, os.path.join(working_dir, 'tec_uncert_plots'), num_processes=ncpu,
                     solset='sol000',
                     observable='weights_tec', vmin=0., vmax=10., plot_facet_idx=True,
                     labels_in_radec=True, plot_crosses=False, phase_wrap=False,
                     flag_outliers=False)

    animate_datapack(dds5_h5parm, os.path.join(working_dir, 'smoothed_amp_plots'), num_processes=ncpu,
                     solset='sol000',
                     observable='amplitude', plot_facet_idx=True,
                     labels_in_radec=True, plot_crosses=False, phase_wrap=False)


def test_main():
    os.chdir('/home/albert/data/gains_screen/working_dir/')
    # dds4_h5parm = os.path.join('/home/albert/data/gains_screen/data', 'L{}_DDS4_full_merged.h5'.format(100000))
    # Y_obs_good, _, _= get_data(solution_file=dds4_h5parm)
    # dds4_h5parm = os.path.join('/home/albert/data/gains_screen/data', 'L{}_DDS4_full_merged.h5'.format(342938))
    # Y_obs_bad, _, _ = get_data(solution_file=dds4_h5parm)
    #
    # Nd, Na, twoNf, Nt = Y_obs_good.shape
    # Nf = twoNf // 2
    # Y_obs_good = Y_obs_good.transpose((0, 1, 3, 2)).reshape((Nd * Na * Nt, 2 * Nf))  # Nd*Na*Nt, 2*Nf
    # amp_good = jnp.sqrt(Y_obs_good[:, :Nf] ** 2 + Y_obs_good[:, Nf:] ** 2)
    #
    # Y_obs_bad = Y_obs_bad.transpose((0, 1, 3, 2)).reshape((Nd * Na * Nt, 2 * Nf))  # Nd*Na*Nt, 2*Nf
    # amp_bad = jnp.sqrt(Y_obs_bad[:, :Nf] ** 2 + Y_obs_bad[:, Nf:] ** 2)
    #
    # print(Y_obs_bad[:224] - Y_obs_good[:224])
    # import pylab as plt
    #
    # plt.imshow(Y_obs_good[220:224], aspect='auto',interpolation='nearest')
    # plt.colorbar()
    # plt.show()
    # plt.imshow(Y_obs_bad[220:224], aspect='auto', interpolation='nearest')
    # plt.colorbar()
    # plt.show()
    # plt.imshow(Y_obs_bad[220:224]-Y_obs_good[220:224], aspect='auto', interpolation='nearest')
    # plt.colorbar()
    # plt.show()
    # return

    main('/home/albert/data/gains_screen/data', '/home/albert/data/gains_screen/working_dir/', 342938, 8)
    # main('/home/albert/data/gains_screen/data','/home/albert/data/gains_screen/working_dir/',100000, 8)


def add_args(parser):
    parser.register("type", "bool", lambda v: v.lower() == "true")
    parser.add_argument('--obs_num', help='Obs number L*',
                        default=None, type=int, required=True)
    parser.add_argument('--data_dir', help='Where are the ms files are stored.',
                        default=None, type=str, required=True)
    parser.add_argument('--working_dir', help='Where to perform the imaging.',
                        default=None, type=str, required=True)
    parser.add_argument('--ncpu', help='How many processors available.',
                        default=None, type=int, required=True)


if __name__ == '__main__':
    if len(sys.argv) == 1:
        test_main()
        exit(0)
    parser = argparse.ArgumentParser(
        description='Infer tec, const, clock and smooth the gains.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_args(parser)
    flags, unparsed = parser.parse_known_args()
    logger.info("Running with:")
    for option, value in vars(flags).items():
        logger.info("    {} -> {}".format(option, value))
    main(**vars(flags))
