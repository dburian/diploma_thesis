from __future__ import annotations
import torch
from typing import TYPE_CHECKING


from transformer_document_embedding.utils.torch.net_helpers import get_activation

if TYPE_CHECKING:
    from typing import Optional


class StaticContextualLoss(torch.nn.Module):
    def __init__(
        self,
        lam: Optional[float] = None,
        static_loss: Optional[torch.nn.Module] = None,
        contextual_loss: Optional[torch.nn.Module] = None,
        contextual_key: str = "sbert",
        static_key: str = "dbow",
        len_key: str = "length",
        contextual_max_length: int = 512,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self._contextual_key = contextual_key
        self._static_key = static_key
        self._contextual_max_length = contextual_max_length
        self._len_key = len_key
        self.static_loss = static_loss
        self.contextual_loss = contextual_loss
        self._lam = lam

    @property
    def contextual_max_length(self) -> int:
        return self._contextual_max_length

    def forward(
        self, inputs: torch.Tensor, targets: dict[str, torch.Tensor]
    ) -> dict[str, torch.Tensor]:
        outputs = {"loss": torch.tensor(0, device=inputs.device)}

        if self.contextual_loss is not None:
            contextual_mask = (
                torch.ones((targets[self._contextual_key].size(0), 1))
                if self._len_key is None
                else (targets[self._len_key] < self._contextual_max_length).unsqueeze(1)
            )
            # Multiply the inputs, as mse outputs a single number. Zero equals zero
            contextual_loss = torch.nn.functional.mse_loss(
                inputs * contextual_mask,
                targets[self._contextual_key] * contextual_mask,
            )

            if self._lam is not None:
                contextual_loss *= self._lam

            outputs["contextual_loss"] = contextual_loss
            outputs["loss"] += contextual_loss

        if self.static_loss is not None:
            static_loss_outputs = self.static_loss(inputs, targets[self._static_key])
            static_loss = torch.mean(static_loss_outputs.pop("loss"))

            outputs.update(static_loss_outputs)
            outputs["static_loss"] = static_loss
            outputs["loss"] += static_loss

        return outputs


class CCALoss(torch.nn.Module):
    def __init__(
        self,
        output_dimension: Optional[int] = None,
        regularization_constant: float = 1e-3,
        epsilon: float = 1e-9,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._reg_constant = regularization_constant
        self._eps = epsilon
        self._output_dim = output_dimension

    def _covariance(
        self,
        x: torch.Tensor,
        y: torch.Tensor,
        *,
        add_regularization: bool = False,
    ) -> torch.Tensor:
        n = x.size(1)  # observation count
        cov = (1 / (n - 1)) * torch.matmul(x, y.T)

        m = x.size(0)  # features count
        return (
            cov + self._reg_constant * torch.eye(m, device=x.device)
            if add_regularization
            else cov
        )

    def _return_computation(
        self, neg_correlation: torch.Tensor
    ) -> dict[str, torch.Tensor]:
        return {"loss": neg_correlation}

    def _compute_covariance_matrices(
        self, view1: torch.Tensor, view2: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # So that observations are columns, features are rows
        view1, view2 = view1.T, view2.T

        view1_bar = view1 - view1.mean(dim=1).unsqueeze(dim=1)
        view2_bar = view2 - view2.mean(dim=1).unsqueeze(dim=1)

        sigma = self._covariance(view1_bar, view2_bar)
        sigma_1 = self._covariance(view1_bar, view1_bar, add_regularization=True)
        sigma_2 = self._covariance(view2_bar, view2_bar, add_regularization=True)

        return sigma, sigma_1, sigma_2

    def forward(
        self, view1: torch.Tensor, view2: torch.Tensor
    ) -> dict[str, torch.Tensor]:
        device = view1.device

        sigma, sigma_1, sigma_2 = self._compute_covariance_matrices(view1, view2)

        D1, V1 = torch.linalg.eigh(sigma_1)
        D2, V2 = torch.linalg.eigh(sigma_2)

        # To increase stability, give up eigenvectors with small eigenvalues
        # Indices of rows with elements of `D1` larger than `eps`
        large_eigh_idxs = torch.gt(D1, self._eps).nonzero()[:, 0]
        D1 = D1[large_eigh_idxs]
        # Forget eigenvectors with small eigenvalues
        V1 = V1[:, large_eigh_idxs]

        large_eigh_idxs = torch.gt(D2, self._eps).nonzero()[:, 0]
        D2 = D2[large_eigh_idxs]
        V2 = V2[:, large_eigh_idxs]

        sigma_1_root_inv = V1 @ torch.diag(D1**-0.5) @ V1.T
        sigma_2_root_inv = V2 @ torch.diag(D2**-0.5) @ V2.T

        # The matrix whose singular values are canonical correlations
        A = sigma_1_root_inv @ sigma @ sigma_2_root_inv

        if self._output_dim is None:
            # We are using all singular values
            corr = torch.trace(torch.sqrt(A.T @ A))
            return self._return_computation(-corr)

        A_times_A = A.T @ A
        A_times_A = torch.add(
            A_times_A,
            (self._reg_constant * torch.eye(A_times_A.shape[0]).to(device)),
        )
        eigenvalues = torch.linalg.eigvalsh(A_times_A)
        eigenvalues = torch.where(
            eigenvalues > self._eps,
            eigenvalues,
            (torch.ones(eigenvalues.shape).double() * self._eps).to(device),
        )

        eigenvalues = eigenvalues.topk(self._output_dim)[0]
        corr = torch.sum(torch.sqrt(eigenvalues))

        return self._return_computation(-corr)


class RunningCCALoss(CCALoss):
    """CCA loss with running means for covariances and means"""

    def __init__(
        self,
        view1_dimension: int,
        view2_dimension: int,
        output_dimension: Optional[int] = None,
        regularization_constant: float = 0.001,
        epsilon: float = 1e-9,
        beta_mu: float = 0.9,
        beta_sigma: float = 0.9,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(
            output_dimension,
            regularization_constant,
            epsilon,
            *args,
            **kwargs,
        )
        factory_kwargs = {"device": device, "dtype": dtype}

        self.register_buffer("sigma1", torch.eye(view1_dimension, **factory_kwargs))
        self.sigma1: torch.Tensor
        self.register_buffer("sigma2", torch.eye(view2_dimension, **factory_kwargs))
        self.sigma2: torch.Tensor
        self.register_buffer(
            "sigma", torch.zeros(view1_dimension, view2_dimension, **factory_kwargs)
        )
        self.sigma: torch.Tensor

        self.register_buffer("mu1", torch.zeros(view1_dimension, **factory_kwargs))
        self.mu1: torch.Tensor
        self.register_buffer("mu2", torch.zeros(view2_dimension, **factory_kwargs))
        self.mu2: torch.Tensor

        self._beta_mu = beta_mu
        self._beta_sigma = beta_sigma
        self._beta_mu_power = 1
        self._beta_sigma_power = 1

    @staticmethod
    def _running_update(
        beta: float, old: torch.Tensor, new: torch.Tensor
    ) -> torch.Tensor:
        return old.detach() * beta + (1 - beta) * new

    def _compute_covariance_matrices(
        self, view1: torch.Tensor, view2: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        # So that observations are columns, features are rows
        view1, view2 = view1.T, view2.T

        view1_mean, view2_mean = self._compute_means(view1, view2)

        view1_bar = view1 - view1_mean.unsqueeze(dim=1)
        view2_bar = view2 - view2_mean.unsqueeze(dim=1)

        new_sigma = self._covariance(view1_bar, view2_bar)
        new_sigma1 = self._covariance(view1_bar, view1_bar)
        new_sigma2 = self._covariance(view2_bar, view2_bar)

        self.sigma = self._running_update(self._beta_sigma, self.sigma, new_sigma)
        self.sigma1 = self._running_update(self._beta_sigma, self.sigma1, new_sigma1)
        self.sigma2 = self._running_update(self._beta_sigma, self.sigma2, new_sigma2)

        self._beta_sigma_power *= self._beta_sigma
        sigma = self.sigma / (1 - self._beta_sigma_power)
        sigma1 = self.sigma1 / (1 - self._beta_sigma_power)
        sigma2 = self.sigma2 / (1 - self._beta_sigma_power)

        return sigma, sigma1, sigma2

    def _compute_means(
        self, view1: torch.Tensor, view2: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Parameters:
        -----------
            view1: torch.Tensor
                Embeddings of first view as columns.
            view2: torch.Tensor
                Embeddings of second view as columns.
        """
        self.mu1 = self._running_update(self._beta_mu, self.mu1, view1.mean(dim=1))

        self.mu2 = self._running_update(self._beta_mu, self.mu2, view2.mean(dim=1))

        self._beta_mu_power *= self._beta_mu
        mu1 = self.mu1 / (1 - self._beta_mu_power)
        mu2 = self.mu2 / (1 - self._beta_mu_power)
        return mu1, mu2

    def _return_computation(
        self, neg_correlation: torch.Tensor
    ) -> dict[str, torch.Tensor]:
        return {
            **super()._return_computation(neg_correlation),
            "covariance_mat": self.sigma.detach(),
            "sigma2": (self.sigma2 / (1 - self._beta_sigma_power)).detach(),
        }


class SoftCCALoss(torch.nn.Module):
    """According to:
    Chang, Xiaobin, Tao Xiang, and Timothy M. Hospedales. "Scalable and
    effective deep CCA via soft decorrelation." Proceedings of the IEEE
    Conference on Computer Vision and Pattern Recognition. 2018.
    """

    def __init__(
        self,
        sdl1: StochasticDecorrelationLoss,
        sdl2: StochasticDecorrelationLoss,
        lam: float,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.sdl1 = sdl1
        self.sdl2 = sdl2

        self.lam = lam

    def forward(
        self, view1: torch.Tensor, view2: torch.Tensor
    ) -> dict[str, torch.Tensor]:
        sdl1, sdl2 = self.sdl1(view1), self.sdl2(view2)

        # TODO: Is this correct?
        l2 = torch.linalg.norm(view1 - view2, dim=(0, 1))

        return {
            "sdl1": sdl1,
            "sdl2": sdl2,
            "l2": l2,
            "loss": l2 * self.lam + sdl1 + sdl2,
        }


class StochasticDecorrelationLoss(torch.nn.Module):
    def __init__(
        self,
        dimension: int,
        alpha: float = 0.8,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        factory_kwargs = {"device": device, "dtype": dtype}

        self.register_buffer(
            "sigma", torch.zeros(dimension, dimension, **factory_kwargs)
        )
        self.sigma: torch.Tensor
        self.batch_norm = torch.nn.BatchNorm1d(dimension, affine=False)

        self.alpha = alpha
        self.norm_factor = 0

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        inputs = self.batch_norm(inputs)

        # Batch size
        m = inputs.size(0)

        new_sigma = (1 / (m - 1)) * inputs.T @ inputs
        self.sigma = self.alpha * self.sigma.detach() + new_sigma

        self.norm_factor = self.alpha * self.norm_factor + 1
        apprx_sigma = self.sigma / self.norm_factor
        apprx_sigma = apprx_sigma.abs()

        loss = apprx_sigma.sum() - apprx_sigma.trace()

        return loss


class DeepNet(torch.nn.Module):
    """Feed-forward net for DeepCCA loss"""

    def __init__(
        self,
        layer_features: list[int],
        input_features: int,
        activation: str = "relu",
        norm: Optional[str] = "layer",
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        norm_class = None
        if norm is not None and norm == "layer":
            norm_class = torch.nn.LayerNorm
        elif norm is not None and norm == "batch":
            norm_class = torch.nn.BatchNorm1d

        layers = []
        features = [input_features] + layer_features
        for input_dim, output_dim in zip(features[:-1], features[1:], strict=True):
            if norm_class is not None:
                layers.append(norm_class(input_dim))

            layers.append(get_activation(activation)())
            layers.append(torch.nn.Linear(input_dim, output_dim))

        # Use ModuleList instead of Sequential to allow empty layers
        self.layers = torch.nn.ModuleList(modules=layers)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        outputs = inputs
        for layer in self.layers:
            outputs = layer(outputs)

        return outputs


class ProjectionLoss(torch.nn.Module):
    def __init__(
        self,
        net1: Optional[DeepNet],
        net2: Optional[DeepNet],
        loss_fn: torch.nn.Module,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.net1 = net1
        self.net2 = net2
        self.loss_fn = loss_fn

    def forward(
        self, view1: torch.Tensor, view2: torch.Tensor
    ) -> dict[str, torch.Tensor]:
        projected_view1 = self.net1(view1) if self.net1 is not None else view1
        projected_view2 = self.net2(view2) if self.net2 is not None else view2

        return {
            **self.loss_fn(projected_view1, projected_view2),
            "projected_view1": projected_view1,
            "projected_view2": projected_view2,
        }


# Here just as a reminder how to do it in case I need to
def get_cross_corr(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    # All variables as rows, observations as columns
    var_obs = torch.cat([x.T, y.T]).detach()
    corr = torch.corrcoef(var_obs)
    x_vars = x.size(1)
    cross_corr = corr[x_vars:, :x_vars]
    return torch.sum(cross_corr)


class CosineDistanceLoss(torch.nn.Module):
    def __init__(self, dim: int = 1, eps: float = 1e-8, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.dim = dim
        self.eps = eps

    def forward(self, inputs: torch.Tensor, outputs: torch.Tensor) -> torch.Tensor:
        return 1 - torch.nn.functional.cosine_similarity(
            inputs, outputs, self.dim, self.eps
        )
