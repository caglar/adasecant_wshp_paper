"""
A module containing different learning rules for use with the SGD training
algorithm.
"""
import numpy as np
import warnings
import theano

from pylearn2.devtools.nan_guard import NanGuardMode
from theano.ifelse import ifelse
from theano import config
from theano import tensor as T

from theano.compat.python2x import OrderedDict
from pylearn2.space import NullSpace
from pylearn2.train_extensions import TrainExtension
from pylearn2.utils import sharedX
from pylearn2.utils import wraps
from pylearn2.training_algorithms.learning_rule import LearningRule

import logging
logger = logging.getLogger(__name__)

class Adasecant(LearningRule):
    """
    Adasecant:
        Based on the paper:
            Gulcehre, Caglar, and Yoshua Bengio.
            "ADASECANT: Robust Adaptive Secant Method for Stochastic Gradient."
            arXiv preprint arXiv:1412.7419 (2014).
    There are some small changes in this code.
    Parameters
    ----------

    gamma_clip : float, optional
        The clipping threshold for the gamma. In general 1.8 seems to
        work fine for several tasks.
    decay : float, optional
        Decay rate :math:`\\rho` in Algorithm 1 of the aforementioned
        paper. Decay 0.95 seems to work fine for several tasks.
    start_var_reduction: float, optional,
        How many updates later should the variance reduction start from?
    delta_clip: float, optional,
        The threshold to clip the deltas after.
    grad_clip: float, optional,
        Apply gradient clipping for RNNs (not necessary for feedforward networks). But this is
        a constraint on the norm of the gradient per layer.
        Based on:
            Pascanu, Razvan, Tomas Mikolov, and Yoshua Bengio. "On the difficulty of training
            recurrent neural networks." arXiv preprint arXiv:1211.5063 (2012).
    use_adagrad: bool, optional
        Either to use clipped adagrad or not.
    use_corrected_grad: bool, optional
        Either to use correction for gradients (referred as variance
        reduction in the workshop paper).
    """
    def __init__(self, decay=0.95,
                 gamma_clip=1.8,
                 grad_clip=None,
                 start_var_reduction=0,
                 delta_clip=None,
                 use_adagrad=False,
                 skip_nan_inf=False,
                 use_corrected_grad=True):

        assert decay >= 0.
        assert decay < 1.

        self.start_var_reduction = start_var_reduction
        self.delta_clip = delta_clip
        self.gamma_clip = gamma_clip
        self.grad_clip = grad_clip
        self.decay = sharedX(decay, "decay")
        self.use_corrected_grad = use_corrected_grad
        self.use_adagrad = use_adagrad
        self.damping = 1e-7

        # We have to bound the tau to prevent it to
        # grow to an arbitrarily large number, oftenwise
        # that causes numerical instabilities for very deep
        # networks. Note that once tau become very large, it will keep,
        # increasing indefinitely.
        self.skip_nan_inf = skip_nan_inf
        self.upper_bound_tau = 1e8
        self.lower_bound_tau = 1.5

    def get_updates(self, learning_rate, grads, lr_scalers=None):
        """
        .. todo::
            WRITEME
        Parameters
        ----------
        learning_rate : float
            Learning rate coefficient. Learning rate is not being used but, pylearn2 requires a
            learning rate to be defined.
        grads : dict
            A dictionary mapping from the model's parameters to their
            gradients.
        lr_scalers : dict
            A dictionary mapping from the model's parameters to a learning
            rate multiplier.
        """

        updates = OrderedDict({})
        eps = self.damping
        step = sharedX(0., name="step")

        if self.skip_nan_inf:
            #If norm of the gradients of a parameter is inf or nan don't update that parameter
            #That might be useful for RNNs.
            grads = OrderedDict({p: T.switch(T.or_(T.isinf(grads[p]),
                T.isnan(grads[p])), 0, grads[p]) for
                p in grads.keys()})

        #Block-normalize gradients:
        grads = OrderedDict({p: grads[p] / (grads[p].norm(2) + eps) for p in grads.keys()})
        nparams = len(grads.keys())

        #Apply the gradient clipping, this is only necessary for RNNs and sometimes for very deep
        #networks
        if self.grad_clip:
            assert self.grad_clip > 0.
            assert self.grad_clip <= 1., "Norm of the gradients per layer can not be larger than 1."
            gnorm = sum([g.norm(2) for g in grads.values()])

            grads = OrderedDict({p : T.switch(gnorm/nparams > self.grad_clip,
                g * self.grad_clip * nparams / gnorm , g)\
                    for p, g in grads.iteritems()})

        for param in grads.keys():
            grads[param].name = "grad_%s" % param.name
            mean_grad = sharedX(param.get_value() * 0. + eps, name="mean_grad_%s" % param.name)
            mean_corrected_grad = sharedX(param.get_value() * 0 + eps, name="mean_corrected_grad_%s" % param.name)
            slow_constant = 2.1

            if self.use_adagrad:
                # sum_square_grad := \sum_i g_i^2
                sum_square_grad = sharedX(param.get_value(borrow=True) * 0., name="sum_square_grad_%s" % param.name)

            """
            Initialization of accumulators
            """
            taus_x_t = sharedX((np.ones_like(param.get_value()) + eps) * slow_constant,
                               name="taus_x_t_" + param.name)
            self.taus_x_t = taus_x_t

            #Variance reduction parameters
            #Numerator of the gamma:
            gamma_nume_sqr = sharedX(np.zeros_like(param.get_value()) + eps,
                                     name="gamma_nume_sqr_" + param.name)

            #Denominator of the gamma:
            gamma_deno_sqr = sharedX(np.zeros_like(param.get_value()) + eps,
                                     name="gamma_deno_sqr_" + param.name)

            #For the covariance parameter := E[\gamma \alpha]_{t-1}
            cov_num_t = sharedX(np.zeros_like(param.get_value()) + eps,
                                  name="cov_num_t_" + param.name)

            # mean_squared_grad := E[g^2]_{t-1}
            mean_square_grad = sharedX(np.zeros_like(param.get_value()) + eps,
                                       name="msg_" + param.name)

            # mean_square_dx := E[(\Delta x)^2]_{t-1}
            mean_square_dx = sharedX(param.get_value() * 0., name="msd_" + param.name)
            if self.use_corrected_grad:
                old_grad = sharedX(param.get_value() * 0. + eps)

            #The uncorrected gradient of previous of the previous update:
            old_plain_grad = sharedX(param.get_value() * 0. + eps)
            mean_curvature = sharedX(param.get_value() * 0. + eps)
            mean_curvature_sqr = sharedX(param.get_value() * 0. + eps)

            # Initialize the E[\Delta]_{t-1}
            mean_dx = sharedX(param.get_value() * 0.)

            # Block-wise normalize the gradient:
            norm_grad = grads[param] #/ grads[param].norm(2)

            #For the first time-step, assume that delta_x_t := norm_grad
            cond = T.eq(step, 0)
            msdx = cond * norm_grad**2 + (1 - cond) * mean_square_dx
            mdx = cond * norm_grad + (1 - cond) * mean_dx

            """
            Compute the new updated values.
            """
            # E[g_i^2]_t
            new_mean_squared_grad = (
                mean_square_grad * (self.decay)  +
                T.sqr(norm_grad) * (1 - self.decay)
            )
            new_mean_squared_grad.name = "msg_" + param.name
            # E[g_i]_t
            new_mean_grad = (
                mean_grad * (self.decay) +
                norm_grad * (1 - self.decay)
            )
            new_mean_grad.name = "nmg_" + param.name

            mg = new_mean_grad
            mgsq = new_mean_squared_grad

            # Keep the rms for numerator and denominator of gamma.
            new_gamma_nume_sqr = (
                gamma_nume_sqr * (1 - 1 / taus_x_t) +
                T.sqr((norm_grad - old_grad) * (old_grad - mg)) / taus_x_t
            )
            new_gamma_nume_sqr.name = "ngammasqr_num_" + param.name

            new_gamma_deno_sqr = (
                gamma_deno_sqr * (1 - 1 / taus_x_t) +
                T.sqr((mg - norm_grad) * (old_grad - mg)) / taus_x_t
            )
            new_gamma_deno_sqr.name = "ngammasqr_den_" + param.name

            gamma = T.sqrt(gamma_nume_sqr) / T.sqrt(gamma_deno_sqr + eps)
            gamma.name = "gamma_" + param.name

            if self.gamma_clip:
                gamma = T.minimum(gamma, self.gamma_clip)

            momentum_step = gamma * mg
            corrected_grad_cand = (norm_grad + momentum_step) / (1 + gamma)

            #For starting the variance reduction.
            if self.start_var_reduction > -1:
                cond = T.le(self.start_var_reduction, step)
                corrected_grad = cond * corrected_grad_cand + (1 - cond) * norm_grad
            else:
                corrected_grad = norm_grad

            if self.use_adagrad:
                g = corrected_grad
                # Accumulate gradient
                new_sum_squared_grad = (
                    sum_square_grad + T.sqr(g)
                )

                rms_g_t = T.sqrt(new_sum_squared_grad)
                rms_g_t = T.maximum(rms_g_t, 1.0)

            # Use the gradients from the previous update
            # to compute the \nabla f(x_t) - \nabla f(x_{t-1})
            cur_curvature = norm_grad - old_plain_grad
            #cur_curvature = theano.printing.Print("Curvature: ")(cur_curvature)
            cur_curvature_sqr = T.sqr(cur_curvature)

            new_curvature_ave = (
                mean_curvature * (1 - 1 / taus_x_t) +
                (cur_curvature / taus_x_t)
            )
            new_curvature_ave.name = "ncurve_ave_" + param.name

            #Average average curvature
            nc_ave = new_curvature_ave

            new_curvature_sqr_ave = (
                mean_curvature_sqr * (1 - 1 / taus_x_t) +
                (cur_curvature_sqr / taus_x_t)
            )
            new_curvature_sqr_ave.name = "ncurve_sqr_ave_" + param.name

            #Unbiased average squared curvature
            nc_sq_ave = new_curvature_sqr_ave

            epsilon = lr_scalers.get(param, 1.) * learning_rate
            scaled_lr = lr_scalers.get(param, 1.) * sharedX(1.0)
            rms_dx_tm1 = T.sqrt(msdx + epsilon)

            rms_curve_t = T.sqrt(new_curvature_sqr_ave + epsilon)

            #This is where the update step is being defined
            delta_x_t = -scaled_lr * (rms_dx_tm1 / rms_curve_t - cov_num_t / (new_curvature_sqr_ave + epsilon))
            delta_x_t.name = "delta_x_t_" + param.name

            # This part seems to be necessary for only RNNs
            # For feedforward networks this does not seem to be important.
            if self.delta_clip:
                logger.info("Clipping will be applied on the adaptive step size.")
                delta_x_t = delta_x_t.clip(-self.delta_clip, self.delta_clip)
                if self.use_adagrad:
                    delta_x_t = delta_x_t * corrected_grad / rms_g_t
                else:
                    logger.info("Clipped adagrad is disabled.")
                    delta_x_t = delta_x_t * corrected_grad
            else:
                logger.info("Clipping will not be applied on the adaptive step size.")
                if self.use_adagrad:
                    delta_x_t = delta_x_t * corrected_grad / rms_g_t
                else:
                    logger.info("Clipped adagrad will not be used.")
                    delta_x_t = delta_x_t * corrected_grad

            new_taus_t = (1 - T.sqr(mdx) / (msdx + eps)) * taus_x_t + sharedX(1 + eps, "stabilized")

            #To compute the E[\Delta^2]_t
            new_mean_square_dx = (
                 msdx * (1 - 1 / taus_x_t) +
                 (T.sqr(delta_x_t) / taus_x_t)
             )

            #To compute the E[\Delta]_t
            new_mean_dx = (
                mean_dx * (1 - 1 / taus_x_t) +
                (delta_x_t / (taus_x_t))
            )

            #Perform the outlier detection:
            #This outlier detection is slightly different:
            new_taus_t = T.switch(T.or_(abs(norm_grad - mg) > (2 * T.sqrt(mgsq  - mg**2)),
                                        abs(cur_curvature - nc_ave) > (2 * T.sqrt(nc_sq_ave - nc_ave**2))),
                                        T.switch(new_taus_t > 2.5, sharedX(2.5), new_taus_t + sharedX(1.0) + eps), new_taus_t)

            #Apply the bound constraints on tau:
            new_taus_t = T.maximum(self.lower_bound_tau, new_taus_t)
            new_taus_t = T.minimum(self.upper_bound_tau, new_taus_t)

            new_cov_num_t = (
                cov_num_t * (1 - 1 / taus_x_t) +
                (delta_x_t * cur_curvature) * (1 / taus_x_t)
            )

            update_step = delta_x_t

            # Apply updates
            updates[mean_square_grad] = new_mean_squared_grad
            updates[mean_square_dx] = new_mean_square_dx
            updates[mean_dx] = new_mean_dx
            updates[gamma_nume_sqr] = new_gamma_nume_sqr
            updates[gamma_deno_sqr] = new_gamma_deno_sqr
            updates[taus_x_t] = new_taus_t
            updates[cov_num_t] = new_cov_num_t
            updates[mean_grad] = new_mean_grad
            updates[old_plain_grad] = norm_grad
            updates[mean_curvature] = new_curvature_ave
            updates[mean_curvature_sqr] = new_curvature_sqr_ave
            updates[param] = param + update_step
            updates[step] = step + 1

            if self.use_adagrad:
                updates[sum_square_grad] = new_sum_squared_grad

            if self.use_corrected_grad:
                updates[old_grad] = corrected_grad

        return updates

