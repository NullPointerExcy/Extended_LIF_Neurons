import numpy as np
import torch
import torch.nn as nn

from lif.sg.spike_function import SpikeFunction
from lif.probability.dynamic_spike_probability import DynamicSpikeProbability


def get_surrogate_fn(name, alpha):
    if name == "heaviside":
        return lambda x: SpikeFunction.apply(x, "heaviside", alpha)
    elif name == "fast_sigmoid":
        return lambda x: SpikeFunction.apply(x, "fast_sigmoid", alpha)
    elif name == "gaussian":
        return lambda x: SpikeFunction.apply(x, "gaussian", alpha)
    elif name == "arctan":
        return lambda x: SpikeFunction.apply(x, "arctan", alpha)
    else:
        raise ValueError(f"Unknown surrogate gradient function: {name}")


class LIFNeuronGroup(nn.Module):
    """
    A vectorized LIF neuron model for multiple neurons.
    Because the LIFNeuron is inefficient for large neuron counts.
    """

    def __init__(self,
                 num_neurons: int,
                 V_th: float = 1.0,
                 V_reset: float = 0.0,
                 tau: float = 20.0,
                 dt: float = 1.0,
                 eta: float = 0.1,
                 use_adaptive_threshold: bool = True,
                 noise_std: float = 0.1,
                 stochastic: bool = True,
                 min_threshold: float = 0.5,
                 max_threshold: float = 2.0,
                 batch_size: int = 1,
                 device: str = "cpu",
                 surrogate_gradient_function: str = "heaviside",
                 alpha: float = 1.0,
                 allow_dynamic_spike_probability: bool = True,
                 base_alpha: float = 2.0,
                 tau_adapt: float = 20.0,
                 adaptation_decay: float = 0.9,
                 spike_increase: float = 0.5,
                 depression_rate: float = 0.1,
                 recovery_rate: float = 0.05,
                 neuromod_transform=None,
                 learnable_threshold: bool = True,
                 learnable_tau: bool = False,
                 learnable_eta: bool = False):
        """
        Initialize the LIF neuron group with its parameters.

        :param num_neurons: Number of neurons in the group.
        :param V_th: Initial threshold voltage for all neurons.
        :param V_reset: Reset voltage after a spike.
        :param tau: Membrane time constant, controlling decay rate.
        :param dt: Time step for updating the membrane potential.
        :param eta: Adaptation rate for the threshold voltage.
        :param noise_std: Standard deviation of Gaussian noise added to the membrane potential.
        :param stochastic: Whether to enable stochastic firing.
        :param min_threshold: Minimum threshold value.
        :param max_threshold: Maximum threshold value.
        :param batch_size: Batch size for the input data.
        :param device: Device to run the simulation on.
        :param surrogate_gradient_function: Surrogate gradient function for backpropagation.
        :param alpha: Parameter for the surrogate gradient function.
        :param allow_dynamic_spike_probability: Whether to allow dynamic spike probability, this takes the last spike into account. Works like a self-locking mechanism.
        :param base_alpha: Base alpha value for the dynamic sigmoid function.
        :param tau_adapt: Time constant for the adaptation.
        :param adaptation_decay: Decay rate for the adaptation current.
        :param spike_increase: Increment for the adaptation current on spike.
        :param depression_rate: Rate of synaptic depression on spike.
        :param recovery_rate: Rate of synaptic recovery after spike.
        :param neuromod_transform: A function or module that takes an external modulation tensor (e.g. reward/error signal)
            and returns a transformed tensor (e.g. modulation factors in [0,1]).
            If None, a default sigmoid transformation will be applied.
        :param learnable_threshold: Whether the threshold voltage should be learnable.
        :param learnable_tau: Whether the membrane time constant should be learnable.
        :param learnable_eta: Whether the adaptation rate should be learnable.
        """
        assert num_neurons > 0, "Number of neurons must be positive."

        if stochastic:
            assert noise_std > 0, "Noise standard deviation must be positive in stochastic mode."

        assert tau > 0.0, "Membrane time constant must be positive."
        assert min_threshold > 0, "Minimum threshold must be positive."
        assert max_threshold > min_threshold, "Maximum threshold must be greater than the minimum threshold."
        assert dt > 0, "Time step (dt) must be positive."
        assert batch_size > 0, "Batch size must be positive."
        assert device in ["cpu", "cuda"], "Device must be either 'torch.device('cpu')' or 'torch.device('cuda')'."
        assert surrogate_gradient_function in ["heaviside", "fast_sigmoid", "gaussian", "arctan"], \
            "Surrogate gradient function must be one of 'heaviside', 'fast_sigmoid', 'gaussian', 'arctan'."
        assert alpha > 0, "Alpha must be positive."
        assert adaptation_decay >= 0, "adaptation_decay must be non-negative."
        assert spike_increase >= 0, "spike_increase must be non-negative."
        assert 0 <= depression_rate <= 1, "depression_rate must be in [0, 1]."
        assert recovery_rate >= 0, "recovery_rate must be non-negative."

        super(LIFNeuronGroup, self).__init__()
        self.device = torch.device(device)
        self.num_neurons = num_neurons

        shape = (1, num_neurons)
        self.V_th = nn.Parameter(torch.full(shape, V_th)) if learnable_threshold else torch.full(shape, V_th)
        self.tau = nn.Parameter(torch.tensor(tau)) if learnable_tau else torch.tensor(tau)
        self.eta = nn.Parameter(torch.tensor(eta)) if learnable_eta else torch.tensor(eta)

        self.V_reset = V_reset
        self.dt = dt
        self.noise_std = noise_std
        self.stochastic = stochastic
        self.use_adaptive_threshold = use_adaptive_threshold
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.adaptation_decay = adaptation_decay
        self.spike_increase = spike_increase
        self.depression_rate = depression_rate
        self.recovery_rate = recovery_rate

        self.allow_dynamic_spike_probability = allow_dynamic_spike_probability
        self.dynamic_spike_probability = DynamicSpikeProbability(
            base_alpha=base_alpha,
            tau_adapt=tau_adapt,
            batch_size=1,
            num_neurons=num_neurons
        ) if allow_dynamic_spike_probability else None

        self.neuromod_transform = neuromod_transform
        self.surrogate_fn = get_surrogate_fn(surrogate_gradient_function, alpha)

        self.register_buffer("V", torch.zeros(shape))
        self.register_buffer("spikes", torch.zeros(shape, dtype=torch.bool))
        self.register_buffer("adaptation_current", torch.zeros(shape))
        self.register_buffer("synaptic_efficiency", torch.ones(shape))
        self.register_buffer("neuromodulator", torch.ones(shape))

    def resize(self, batch_size):
        shape = (batch_size, self.num_neurons)
        self.V = torch.zeros(shape, device=self.device)
        self.spikes = torch.zeros(shape, dtype=torch.bool, device=self.device)
        self.adaptation_current = torch.zeros(shape, device=self.device)
        self.synaptic_efficiency = torch.ones(shape, device=self.device)
        self.neuromodulator = torch.ones(shape, device=self.device)
        if isinstance(self.V_th, nn.Parameter):
            self.V_th = nn.Parameter(torch.full(shape, self.V_th.data.mean(), device=self.device))
        else:
            self.V_th = torch.full(shape, self.V_th.mean(), device=self.device)
        if self.dynamic_spike_probability:
            self.dynamic_spike_probability.reset(batch_size)

    def initialize_states(self, batch_size):
        """
        Initialize or reset internal states for a given batch size.
        """
        self.batch_size = batch_size

        self.V = torch.zeros((batch_size, self.num_neurons), device=self.device)
        self.spikes = torch.zeros((batch_size, self.num_neurons), dtype=torch.bool, device=self.device)
        self.adaptation_current = torch.zeros((batch_size, self.num_neurons), device=self.device)
        self.synaptic_efficiency = torch.ones((batch_size, self.num_neurons), device=self.device)
        self.neuromodulator = torch.ones((batch_size, self.num_neurons), device=self.device)

    def reset(self):
        self.V.zero_()
        self.spikes.zero_()
        self.adaptation_current.zero_()
        self.synaptic_efficiency.fill_(1.0)
        self.neuromodulator.fill_(1.0)
        if self.dynamic_spike_probability:
            self.dynamic_spike_probability.reset(self.V.shape[0])

    def forward(self, I: torch.Tensor, external_modulation: torch.Tensor = None) -> torch.Tensor:
        """
        Simulate one time step for all neurons in the group.

        :param I: Tensor of input currents with shape (batch_size, num_neurons).
        :param external_modulation: Tensor of external neuromodulatory signals with shape
                                    (batch_size, num_neurons) or broadcastable shape.
                                    For example, this could encode a reward signal for dopamine modulation.
        :return: Spike tensor (binary) of shape (batch_size, num_neurons).
        """
        if I.shape != self.V.shape:
            self.resize(I.shape[0])

        if external_modulation is not None:
            self.neuromodulator = (
                self.neuromod_transform(external_modulation)
                if self.neuromod_transform else torch.sigmoid(external_modulation)
            )

        noise = torch.randn_like(I) * self.noise_std if self.stochastic else 0.0

        I_eff = I * self.synaptic_efficiency + self.neuromodulator - self.adaptation_current
        dV = (I_eff - self.V) / self.tau
        self.V = self.V + dV * self.dt + noise

        if self.stochastic:
            delta = self.V - self.V_th
            spike_prob, _ = self.dynamic_spike_probability(delta, self.spikes) \
                if self.allow_dynamic_spike_probability else torch.sigmoid(delta)
            self.spikes = torch.rand_like(self.V) < spike_prob
        else:
            spike_out = self.surrogate_fn(self.V - self.V_th)
            self.spikes = spike_out.bool()

        self.V = torch.where(self.spikes, torch.tensor(self.V_reset, device=self.device), self.V)

        self.adaptation_current = self.adaptation_current * self.adaptation_decay + self.spike_increase * self.spikes.float()
        self.synaptic_efficiency = (
                self.synaptic_efficiency * (1 - self.depression_rate * self.spikes.float()) +
                self.recovery_rate * (1 - self.synaptic_efficiency)
        )

        if self.use_adaptive_threshold:
            if isinstance(self.V_th, nn.Parameter):
                self.V_th.data = torch.clamp(self.V_th.data, self.min_threshold, self.max_threshold)
            else:
                self.V_th = torch.clamp(self.V_th, self.min_threshold, self.max_threshold)

        return self.spikes
