"""
Extended Kalman filtering and smoothing for nonlinear Gaussian state-space models.
"""

from typing import Callable, List, Optional, Tuple

import jax.numpy as jnp
import jax.random as jr
from jax import jacfwd, lax
from jaxtyping import Array, Float
from tensorflow_probability.substrates.jax.distributions import (
    MultivariateNormalFullCovariance as MVN,
)

from dynamax.linear_gaussian_ssm.inference import (
    PosteriorGSSMFiltered,
    PosteriorGSSMSmoothed,
)
from dynamax.nonlinear_gaussian_ssm.models import ParamsNLGSSM
from dynamax.types import PRNGKeyT
from dynamax.utils.utils import psd_solve, symmetrize

# Helper functions
_get_params = lambda x, dim, t: x[t] if x.ndim == dim + 1 else x
_process_fn = lambda f, u: (lambda x, y: f(x)) if u is None else f
_process_input = lambda x, y: jnp.zeros((y, 1)) if x is None else x


def _predict(
    prior_mean: Float[Array, " state_dim"],
    prior_cov: Float[Array, "state_dim state_dim"],
    dynamics_func: Callable,
    dynamics_jacobian: Callable,
    dynamics_cov: Float[Array, "state_dim state_dim"],
    inpt: Float[Array, " input_dim"],
) -> Tuple[Float[Array, " state_dim"], Float[Array, "state_dim state_dim"]]:
    r"""Predict next mean and covariance using first-order additive EKF

        p(z_{t+1}) = \int N(z_t | m, S) N(z_{t+1} | f(z_t, u), Q)
                    = N(z_{t+1} | f(m, u), F(m, u) S F(m, u)^T + Q)

    Returns:
        mu_pred (D_hid,): predicted mean.
        Sigma_pred (D_hid,D_hid): predicted covariance.
    """
    F_x = dynamics_jacobian(prior_mean, inpt)
    mu_pred = dynamics_func(prior_mean, inpt)
    Sigma_pred = F_x @ prior_cov @ F_x.T + dynamics_cov
    return mu_pred, Sigma_pred


def _condition_on(
    prior_mean: Float[Array, " state_dim"],
    prior_cov: Float[Array, "state_dim state_dim"],
    emission_func: Callable,
    emission_jacobian: Callable,
    emission_cov: Float[Array, "emission_dim emission_dim"],
    inpt: Float[Array, " input_dim"],
    emission: Float[Array, " emission_dim"],
    num_iter: int,
):
    r"""Condition a Gaussian potential on a new observation.

      p(z_t | y_t, u_t, y_{1:t-1}, u_{1:t-1})
        propto p(z_t | y_{1:t-1}, u_{1:t-1}) p(y_t | z_t, u_t)
        = N(z_t | m, S) N(y_t | h_t(z_t, u_t), R_t)
        = N(z_t | mm, SS)
    where
        mm = m + K*(y - yhat) = mu_cond
        yhat = h(m, u)
        S = R + H(m,u) * P * H(m,u)'
        K = P * H(m, u)' * S^{-1}
        SS = P - K * S * K' = Sigma_cond
    **Note! This can be done more efficiently when R is diagonal.**

      Returns:
          mu_cond (D_hid,): filtered mean.
          Sigma_cond (D_hid,D_hid): filtered covariance.
    """

    def _step(carry, _):
        """Iteratively re-linearize around posterior mean and covariance."""
        prior_mean, prior_cov = carry
        H_x = emission_jacobian(prior_mean, inpt)

        # * original dynamax code
        # S = emission_cov + H_x @ prior_cov @ H_x.T
        # K = psd_solve(S, H_x @ prior_cov).T
        # posterior_cov = prior_cov - K @ S @ K.T

        # * Joseph Form: taken from JSL for subspace neural bandits.
        # * S doesn't do much. K and posterior_cov resulted in better performance.
        I = jnp.eye(prior_mean.shape[0])
        S = emission_cov + H_x @ prior_cov @ H_x.T + jnp.eye(emission_cov.shape[0]) * 1e-3
        K = prior_cov @ H_x.T @ jnp.linalg.inv(S)
        posterior_cov = (I - K @ H_x) @ prior_cov @ (I - K @ H_x).T + K @ emission_cov @ K.T

        posterior_mean = prior_mean + K @ (emission - emission_func(prior_mean, inpt))
        return (posterior_mean, posterior_cov), None

    # Iterate re-linearization over posterior mean and covariance
    carry = (prior_mean, prior_cov)
    (mu_cond, Sigma_cond), _ = lax.scan(_step, carry, jnp.arange(num_iter))
    return mu_cond, symmetrize(Sigma_cond)


def extended_kalman_filter(
    params: ParamsNLGSSM,
    emissions: Float[Array, "num_timesteps emission_dim"],
    inputs: Optional[Float[Array, "num_timesteps input_dim"]] = None,
    num_iter: int = 1,
    output_fields: Optional[List[str]] = [
        "filtered_means",
        "filtered_covariances",
        "predicted_means",
        "predicted_covariances",
    ],
) -> PosteriorGSSMFiltered:
    r"""Run an (iterated) extended Kalman filter to produce the
    marginal likelihood and filtered state estimates.

    Args:
        params: model parameters.
        emissions: observation sequence.
        num_iter: number of linearizations around posterior for update step (default 1).
        inputs: optional array of inputs.
        output_fields: list of fields to return in posterior object.
            These can take the values "filtered_means", "filtered_covariances",
            "predicted_means", "predicted_covariances", and "marginal_loglik".

    Returns:
        post: posterior object.

    """
    num_timesteps = len(emissions)

    # Dynamics and emission functions and their Jacobians
    f, h = params.dynamics_function, params.emission_function
    F, H = jacfwd(f), jacfwd(h)
    f, h, F, H = (_process_fn(fn, inputs) for fn in (f, h, F, H))
    inputs = _process_input(inputs, num_timesteps)

    def _step(carry, t):
        """Iteratively update the state estimate and log likelihood."""
        ll, pred_mean, pred_cov = carry

        # Get parameters and inputs for time index t
        Q = _get_params(params.dynamics_covariance, 2, t)
        R = _get_params(params.emission_covariance, 2, t)
        u = inputs[t]
        y = emissions[t]

        # Update the log likelihood
        H_x = H(pred_mean, u)
        ll += MVN(h(pred_mean, u), H_x @ pred_cov @ H_x.T + R).log_prob(jnp.atleast_1d(y))

        # Condition on this emission
        filtered_mean, filtered_cov = _condition_on(pred_mean, pred_cov, h, H, R, u, y, num_iter)

        # Predict the next state
        pred_mean, pred_cov = _predict(filtered_mean, filtered_cov, f, F, Q, u)

        # Build carry and output states
        carry = (ll, pred_mean, pred_cov)
        outputs = {
            "filtered_means": filtered_mean,
            "filtered_covariances": filtered_cov,
            "predicted_means": pred_mean,
            "predicted_covariances": pred_cov,
            "marginal_loglik": ll,
        }
        outputs = {key: val for key, val in outputs.items() if key in output_fields}

        return carry, outputs

    # Run the extended Kalman filter
    carry = (0.0, params.initial_mean, params.initial_covariance)
    (ll, *_), outputs = lax.scan(_step, carry, jnp.arange(num_timesteps))
    outputs = {"marginal_loglik": ll, **outputs}
    posterior_filtered = PosteriorGSSMFiltered(
        **outputs,
    )
    return posterior_filtered


def extended_kalman_smoother(
    params: ParamsNLGSSM,
    emissions: Float[Array, "num_timesteps emission_dim"],
    filtered_posterior: Optional[PosteriorGSSMFiltered] = None,
    inputs: Optional[Float[Array, "num_timesteps input_dim"]] = None,
) -> PosteriorGSSMSmoothed:
    r"""Run an extended Kalman (RTS) smoother.

    Args:
        params: model parameters.
        emissions: observation sequence.
        filtered_posterior: optional output from filtering step.
        inputs: optional array of inputs.

    Returns:
        post: posterior object.

    """
    num_timesteps = len(emissions)

    # Get filtered posterior
    if filtered_posterior is None:
        filtered_posterior = extended_kalman_filter(params, emissions, inputs=inputs)
    ll = filtered_posterior.marginal_loglik
    filtered_means = filtered_posterior.filtered_means
    filtered_covs = filtered_posterior.filtered_covariances

    # Dynamics and emission functions and their Jacobians
    f = params.dynamics_function
    F = jacfwd(f)
    f, F = (_process_fn(fn, inputs) for fn in (f, F))
    inputs = _process_input(inputs, num_timesteps)

    def _step(carry, args):
        """One step of the extended Kalman smoother."""
        # Unpack the inputs
        smoothed_mean_next, smoothed_cov_next = carry
        t, filtered_mean, filtered_cov = args

        # Get parameters and inputs for time index t
        Q = _get_params(params.dynamics_covariance, 2, t)
        R = _get_params(params.emission_covariance, 2, t)
        u = inputs[t]
        F_x = F(filtered_mean, u)

        # Prediction step
        m_pred = f(filtered_mean, u)
        S_pred = Q + F_x @ filtered_cov @ F_x.T
        G = psd_solve(S_pred, F_x @ filtered_cov).T

        # Compute smoothed mean and covariance
        smoothed_mean = filtered_mean + G @ (smoothed_mean_next - m_pred)
        smoothed_cov = filtered_cov + G @ (smoothed_cov_next - S_pred) @ G.T

        return (smoothed_mean, smoothed_cov), (smoothed_mean, smoothed_cov)

    # Run the extended Kalman smoother
    _, (smoothed_means, smoothed_covs) = lax.scan(
        _step,
        (filtered_means[-1], filtered_covs[-1]),
        (jnp.arange(num_timesteps - 1), filtered_means[:-1], filtered_covs[:-1]),
        reverse=True,
    )

    # Concatenate the arrays and return
    smoothed_means = jnp.vstack((smoothed_means, filtered_means[-1][None, ...]))
    smoothed_covs = jnp.vstack((smoothed_covs, filtered_covs[-1][None, ...]))

    return PosteriorGSSMSmoothed(
        marginal_loglik=ll,
        filtered_means=filtered_means,
        filtered_covariances=filtered_covs,
        smoothed_means=smoothed_means,
        smoothed_covariances=smoothed_covs,
    )


def extended_kalman_posterior_sample(
    key: PRNGKeyT,
    params: ParamsNLGSSM,
    emissions: Float[Array, "num_timesteps emission_dim"],
    inputs: Optional[Float[Array, "num_timesteps input_dim"]] = None,
) -> Float[Array, "num_timesteps state_dim"]:
    r"""Run forward-filtering, backward-sampling to draw samples.

    Args:
        key: random number key.
        params: model parameters.
        emissions: observation sequence.
        inputs: optional array of inputs.

    Returns:
        Float[Array, "ntime state_dim"]: one sample of $z_{1:T}$ from the posterior distribution on latent states.
    """
    num_timesteps = len(emissions)

    # Get filtered posterior
    filtered_posterior = extended_kalman_filter(params, emissions, inputs=inputs)
    ll = filtered_posterior.marginal_loglik
    filtered_means = filtered_posterior.filtered_means
    filtered_covs = filtered_posterior.filtered_covariances

    # Dynamics and emission functions and their Jacobians
    f = params.dynamics_function
    F = jacfwd(f)
    f, F = (_process_fn(fn, inputs) for fn in (f, F))
    inputs = _process_input(inputs, num_timesteps)

    def _step(carry, args):
        """One step of the extended Kalman sampler."""
        # Unpack the inputs
        next_state = carry
        key, filtered_mean, filtered_cov, t = args

        # Get parameters and inputs for time index t
        Q = _get_params(params.dynamics_covariance, 2, t)
        u = inputs[t]

        # Condition on next state
        smoothed_mean, smoothed_cov = _condition_on(filtered_mean, filtered_cov, f, F, Q, u, next_state, 1)
        state = MVN(smoothed_mean, smoothed_cov).sample(seed=key)
        return state, state

    # Initialize the last state
    key, this_key = jr.split(key, 2)
    last_state = MVN(filtered_means[-1], filtered_covs[-1]).sample(seed=this_key)

    _, states = lax.scan(
        _step,
        last_state,
        (
            jr.split(key, num_timesteps - 1),
            filtered_means[:-1],
            filtered_covs[:-1],
            jnp.arange(num_timesteps - 1),
        ),
        reverse=True,
    )
    return jnp.vstack([states, last_state])
