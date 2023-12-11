from __future__ import annotations
from functools import partial
from typing import TYPE_CHECKING, Any, Iterable
import logging

import torch
from torcheval.metrics import Max, Mean
from tqdm.auto import tqdm


from transformer_document_embedding.models.transformer.base import TransformerBase
from transformer_document_embedding.models.trainer import (
    MetricLogger,
    TorchTrainer,
    TrainingMetric,
)
from transformer_document_embedding.utils.metrics import (
    CosineDistanceWithSBERT,
    MSEWithSBERT,
    VMemMetric,
    WindowedNonResetableCCAMetricZoo,
    WindowedNonResetableCorrelationMetric,
)
from transformer_document_embedding.utils.similarity_losses import (
    ContrastiveLoss,
    create_sim_based_loss,
)
import transformer_document_embedding.utils.training as train_utils
from transformer_document_embedding.utils import cca_losses

if TYPE_CHECKING:
    from transformers import PreTrainedModel
    from torcheval.metrics import (
        Metric,
    )
    from torch.utils.data import DataLoader
    from datasets import Dataset
    from transformer_document_embedding.tasks.experimental_task import ExperimentalTask
    from typing import Optional
    import numpy as np


logger = logging.getLogger(__name__)


def log_max_abs_grad(
    metric: Metric,
    *_,
    param_name: str,
    model: torch.nn.Module,
) -> None:
    param = model.get_parameter(param_name)
    grad = param.grad
    if grad is None:
        metric.update(torch.tensor([torch.nan], device=param.device))
    else:
        metric.update(grad.abs().max())


class StaticContextualLoss(torch.nn.Module):
    def __init__(
        self,
        contextual_max_length: Optional[int],
        lam: Optional[float] = None,
        static_loss: Optional[torch.nn.Module] = None,
        contextual_loss: Optional[torch.nn.Module] = None,
        contextual_key: str = "sbert",
        static_key: str = "dbow",
        len_key: str = "length",
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
    def contextual_max_length(self) -> Optional[int]:
        return self._contextual_max_length

    def forward(
        self, inputs: torch.Tensor, targets: dict[str, torch.Tensor]
    ) -> dict[str, torch.Tensor]:
        outputs = {"loss": torch.tensor(0, device=inputs.device, dtype=inputs.dtype)}

        if self.contextual_loss is not None:
            mask = (
                targets[self._len_key] <= self._contextual_max_length
                if self.contextual_max_length is not None
                else torch.ones_like(targets[self._len_key])
            ).unsqueeze(-1)

            contextual_loss = self.contextual_loss(
                inputs, targets[self._contextual_key], mask=mask
            )
            if isinstance(contextual_loss, dict):
                just_contextual_loss = contextual_loss.pop("loss")
                outputs.update(
                    {
                        f"contextual_{key}": value
                        for key, value in contextual_loss.items()
                    }
                )
                contextual_loss = just_contextual_loss

            weight_sum = mask.sum()
            if weight_sum > 0:
                contextual_loss /= weight_sum

            if self._lam is not None:
                contextual_loss *= self._lam

            outputs["contextual_mask"] = mask
            outputs["contextual_loss"] = contextual_loss
            outputs["loss"] += contextual_loss

        if self.static_loss is not None:
            static_loss_outputs = self.static_loss(inputs, targets[self._static_key])
            static_loss = torch.mean(static_loss_outputs.pop("loss"))

            outputs.update(
                {f"static_{key}": value for key, value in static_loss_outputs.items()}
            )
            outputs["static_loss"] = static_loss
            outputs["loss"] += static_loss

        return outputs


class _SequenceEmbeddingModel(torch.nn.Module):
    def __init__(
        self,
        transformer: PreTrainedModel,
        pooler: torch.nn.Module,
        loss: StaticContextualLoss,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.transformer = transformer
        self.pooler = pooler
        self.loss = loss

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> dict[str, torch.Tensor]:
        input_kws = {}
        # Needed for Longformer
        if "global_attention_mask" in kwargs:
            input_kws["global_attention_mask"] = kwargs.pop("global_attention_mask")

        outputs = self.transformer(
            input_ids=input_ids,
            attention_mask=attention_mask,
            head_mask=head_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            output_attentions=False,
            output_hidden_states=False,
            return_dict=True,
            inputs_embeds=None,
            **input_kws,
        )
        pooled_output = self.pooler(
            **outputs,
            attention_mask=attention_mask,
            **input_kws,
        )
        loss_outputs = self.loss(pooled_output, kwargs) if len(kwargs) > 0 else {}

        return {
            **outputs,
            **loss_outputs,
            "embeddings": pooled_output,
        }


class TransformerStudent(TransformerBase):
    def __init__(
        self,
        transformer_model: str,
        batch_size: int,
        pooler_type: str,
        contextual_max_length: Optional[int],
        contextual_loss_kwargs: Optional[dict[str, Any]],
        static_loss_kwargs: Optional[dict[str, Any]],
        transformer_model_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            transformer_model=transformer_model,
            transformer_model_kwargs=transformer_model_kwargs,
            batch_size=batch_size,
            pooler_type=pooler_type,
        )
        loss = self._construct_loss(
            static_loss_kwargs,
            contextual_loss_kwargs,
            contextual_max_length,
            transformer_hidden_size=self._transformer.config.hidden_size,
        )

        self._model: _SequenceEmbeddingModel = _SequenceEmbeddingModel(
            self._transformer, self._pooler, loss
        )

    def _construct_loss(
        self,
        static_loss_kwargs: Optional[dict[str, Any]],
        contextual_loss_kwargs: Optional[dict[str, Any]],
        contextual_max_length: Optional[int],
        transformer_hidden_size: int,
    ) -> StaticContextualLoss:
        loss_kwargs: dict[str, Any] = {
            "static_loss": None,
            "contextual_loss": None,
            "contextual_max_length": contextual_max_length,
        }
        if static_loss_kwargs is not None:
            loss_kwargs["static_key"] = static_loss_kwargs.pop("static_key")

            loss_kwargs["static_loss"] = self._construct_static_loss(
                **static_loss_kwargs,
                transformer_hidden_size=transformer_hidden_size,
            )

        if contextual_loss_kwargs is not None:
            for param_name in ["contextual_key", "lam"]:
                loss_kwargs[param_name] = contextual_loss_kwargs.pop(param_name)

            loss_kwargs["contextual_loss"] = self._construct_contextual_loss(
                **contextual_loss_kwargs,
            )

        return StaticContextualLoss(**loss_kwargs)

    def _construct_static_loss(
        self,
        static_loss_type: str,
        transformer_projection_layers: list[int],
        static_projection_layers: list[int],
        projection_norm: Optional[str],
        static_embed_dim: int,
        cca_output_dim: Optional[int],
        transformer_hidden_size: int,
        soft_cca_lam: Optional[float] = None,
        contrastive_lam: Optional[float] = None,
    ) -> cca_losses.ProjectionLoss:
        view1_dim = (
            transformer_projection_layers[-1]
            if len(transformer_projection_layers) > 0
            else transformer_hidden_size
        )
        view2_dim = (
            static_projection_layers[-1]
            if len(static_projection_layers) > 0
            else static_embed_dim
        )

        static_loss_fn = None
        if static_loss_type == "cca":
            static_loss_fn = cca_losses.CCALoss(output_dimension=cca_output_dim)
        elif static_loss_type == "running_cca":
            static_loss_fn = cca_losses.RunningCCALoss(
                view1_dimension=view1_dim,
                view2_dimension=view2_dim,
                output_dimension=cca_output_dim,
            )
        elif static_loss_type == "soft_cca":
            assert (
                soft_cca_lam is not None
            ), "To use soft_cca, `soft_cca_lam` must be set"
            static_loss_fn = cca_losses.SoftCCALoss(
                sdl1=cca_losses.StochasticDecorrelationLoss(view1_dim),
                sdl2=cca_losses.StochasticDecorrelationLoss(view2_dim),
                lam=soft_cca_lam,
            )
        else:
            static_loss_fn = create_sim_based_loss(
                static_loss_type, contrastive_lam=contrastive_lam
            )

        def _construct_net(
            layers: list[int], input_features: int
        ) -> Optional[cca_losses.DeepNet]:
            if len(layers) == 0:
                return None

            return cca_losses.DeepNet(
                layer_features=layers,
                input_features=input_features,
                norm=projection_norm,
            )

        net1 = _construct_net(transformer_projection_layers, transformer_hidden_size)
        net2 = _construct_net(static_projection_layers, static_embed_dim)

        return cca_losses.ProjectionLoss(
            net1=net1,
            net2=net2,
            loss_fn=static_loss_fn,
        )

    def _construct_contextual_loss(
        self, contextual_loss_type: str, contrastive_lam: Optional[float]
    ) -> torch.nn.Module:
        return create_sim_based_loss(
            contextual_loss_type, contrastive_lam=contrastive_lam
        )

    def train(
        self,
        task: ExperimentalTask,
        epochs: int,
        warmup_steps: int,
        grad_accumulation_steps: int,
        patience: Optional[int],
        weight_decay: float,
        fp16: bool,
        max_grad_norm: float,
        log_every_step: int,
        validate_every_step: Optional[int],
        dataloader_sampling: str,
        lr: float,
        bucket_limits: list[int],
        save_best: bool,
        global_attention_type: str,
        device: Optional[str] = None,
        log_dir: Optional[str] = None,
        model_dir: Optional[str] = None,
        **_,
    ) -> None:
        to_dataloader = partial(
            self._to_dataloader,
            sampling=dataloader_sampling,
            return_length=False,
            global_attention_type=global_attention_type,
            sampler_kwargs={
                "effective_batch_size": self._batch_size * grad_accumulation_steps,
                "bucket_limits": bucket_limits,
            },
        )

        train_data = to_dataloader(task.train)

        val_data = None
        if task.validation is not None:
            val_data = to_dataloader(task.validation, training=False)

        optimizer = torch.optim.AdamW(
            train_utils.get_optimizer_params(self._model, weight_decay),
            lr=lr,
        )

        lr_scheduler = train_utils.get_linear_lr_scheduler_with_warmup(
            optimizer,
            warmup_steps // grad_accumulation_steps,
            epochs * len(train_data) // grad_accumulation_steps,
        )

        save_model_callback = self._get_save_model_callback(save_best, model_dir)

        if self._transformer.supports_gradient_checkpointing:
            self._transformer.gradient_checkpointing_enable()

        train_logger = None
        val_logger = None
        if log_dir is not None:
            metrics = self._get_train_metrics(val_data, log_every_step)
            train_logger = MetricLogger("train", metrics, log_dir)
            val_logger = MetricLogger(
                "val", [m.clone() for m in metrics], log_dir, log_lr=False
            )

        trainer = TorchTrainer(
            model=self._model,
            train_data=train_data,
            val_data=val_data,
            optimizer=optimizer,
            train_logger=train_logger,
            val_logger=val_logger,
            fp16=fp16,
            max_grad_norm=max_grad_norm,
            grad_accumulation_steps=grad_accumulation_steps,
            lr_scheduler=lr_scheduler,
            validate_every_step=validate_every_step,
            save_model_callback=save_model_callback,
            patience=patience,
            device=device,
        )
        trainer.train(epochs=epochs)

    def _get_train_metrics(
        self, val_data: Optional[DataLoader], default_log_freq: int
    ) -> list[TrainingMetric]:
        contextual_len_thres = self._model.loss.contextual_max_length
        train_metrics = [
            TrainingMetric("used_vmem", VMemMetric(), default_log_freq),
            TrainingMetric(
                "mean_length",
                Mean(),
                default_log_freq,
                lambda metric, _, batch: metric.update(batch["length"]),
            ),
            TrainingMetric(
                "sbert_mse", MSEWithSBERT(max_input_length=None), default_log_freq
            ),
            TrainingMetric(
                "sbert_mse_norm",
                MSEWithSBERT(max_input_length=None, normalize=True),
                default_log_freq,
            ),
            TrainingMetric(
                "sbert_cos_dist",
                CosineDistanceWithSBERT(max_input_length=None),
                default_log_freq,
            ),
            TrainingMetric(
                "max_abs_transformer_grad",
                Max(),
                default_log_freq,
                partial(
                    log_max_abs_grad,
                    param_name="transformer.encoder.layer.11.output.dense.weight",
                    model=self._model,
                ),
            ),
            *self._get_projection_metrics(self._model, val_data, default_log_freq),
        ]

        if isinstance(self._model.loss.contextual_loss, ContrastiveLoss):
            train_metrics.extend(
                [
                    TrainingMetric(
                        "contextual_contrastive_positive",
                        Mean(),
                        default_log_freq,
                        lambda metric, outputs, _: metric.update(
                            outputs["contextual_contrastive_positive"]
                        ),
                    ),
                    TrainingMetric(
                        "contextual_contrastive_negative",
                        Mean(),
                        default_log_freq,
                        lambda metric, outputs, _: metric.update(
                            outputs["contextual_contrastive_negative"]
                        ),
                    ),
                ]
            )

        if isinstance(
            self._model.loss.static_loss, cca_losses.ProjectionLoss
        ) and isinstance(self._model.loss.static_loss.loss_fn, ContrastiveLoss):
            train_metrics.extend(
                [
                    TrainingMetric(
                        "static_contrastive_positive",
                        Mean(),
                        default_log_freq,
                        lambda metric, outputs, _: metric.update(
                            outputs["static_contrastive_positive"]
                        ),
                    ),
                    TrainingMetric(
                        "static_contrastive_negative",
                        Mean(),
                        default_log_freq,
                        lambda metric, outputs, _: metric.update(
                            outputs["static_contrastive_negative"]
                        ),
                    ),
                ]
            )

        # sbert_mse_None would be equal to sbert_mse, same for cos_dist
        if contextual_len_thres is not None:
            train_metrics.extend(
                [
                    TrainingMetric(
                        f"sbert_mse_{contextual_len_thres}",
                        MSEWithSBERT(max_input_length=contextual_len_thres),
                        default_log_freq,
                    ),
                    TrainingMetric(
                        f"sbert_mse_norm_{contextual_len_thres}",
                        MSEWithSBERT(
                            max_input_length=contextual_len_thres,
                            normalize=True,
                        ),
                        default_log_freq,
                    ),
                    TrainingMetric(
                        f"sbert_cos_dist_{contextual_len_thres}",
                        CosineDistanceWithSBERT(max_input_length=contextual_len_thres),
                        default_log_freq,
                    ),
                ]
            )

        if self._model.loss.contextual_loss is not None:
            train_metrics.extend(
                [
                    TrainingMetric(
                        "mean_contextual_loss",
                        Mean(),
                        default_log_freq,
                        lambda metric, outputs, _: metric.update(
                            outputs["contextual_loss"]
                        ),
                    ),
                    TrainingMetric(
                        "mean_contextual_mask",
                        Mean(),
                        default_log_freq,
                        lambda metric, outputs, _: metric.update(
                            outputs["contextual_mask"]
                        ),
                    ),
                ]
            )

        if self._model.loss.static_loss is not None:
            train_metrics.append(
                TrainingMetric(
                    "mean_static_loss",
                    Mean(),
                    default_log_freq,
                    lambda metric, outputs, _: metric.update(outputs["static_loss"]),
                )
            )

            if isinstance(self._model.loss.static_loss.loss_fn, cca_losses.SoftCCALoss):
                train_metrics.extend(
                    [
                        TrainingMetric(
                            "mean_projection_l2_norm",
                            Mean(),
                            default_log_freq,
                            lambda metric, outputs, _: metric.update(
                                outputs["static_l2"]
                            ),
                        ),
                        TrainingMetric(
                            "mean_projection_sdl1",
                            Mean(),
                            default_log_freq,
                            lambda metric, outputs, _: metric.update(
                                outputs["static_sdl1"]
                            ),
                        ),
                        TrainingMetric(
                            "mean_projection_sdl2",
                            Mean(),
                            default_log_freq,
                            lambda metric, outputs, _: metric.update(
                                outputs["static_sdl2"]
                            ),
                        ),
                    ]
                )

        return train_metrics

    def _get_projection_metrics(
        self,
        model: _SequenceEmbeddingModel,
        val_data: Optional[DataLoader],
        default_log_freq: int,
    ) -> list[TrainingMetric]:
        if model.loss.static_loss is None or not isinstance(
            model.loss.static_loss, cca_losses.ProjectionLoss
        ):
            return []

        train_metrics = []
        sample_projection_weight_path = None

        for net_name in ["net1", "net2"]:
            net = getattr(model.loss.static_loss, net_name)
            if net is not None:
                layer_count = len(net.layers)
                sample_projection_weight_path = (
                    f"loss.static_loss.{net_name}.layers.{layer_count -1}.weight"
                )
                break

        if sample_projection_weight_path is not None:
            train_metrics.append(
                TrainingMetric(
                    "max_abs_dcca_grad",
                    Max(),
                    default_log_freq,
                    partial(
                        log_max_abs_grad,
                        param_name=sample_projection_weight_path,
                        model=model,
                    ),
                )
            )

        def _update_with_projected_views(metric, outputs, _):
            metric.update(
                outputs["static_projected_view1"], outputs["static_projected_view2"]
            )

        for n_components, window_size in [
            (256, 256 * 10),
            (512, 512 * 5),
            (768, 768 * 5),
        ]:
            cca_metric = WindowedNonResetableCCAMetricZoo(
                n_components=n_components,
                window_size=window_size,
            )
            metric_name = f"cca_{n_components}x{cca_metric.window_size}"
            if (
                val_data is not None
                and (val_size := len(val_data) * (val_data.batch_size or 1))
                < cca_metric.window_size
            ):
                logger.warn(
                    "Validation data smaller than CCA window. "
                    "Metric '%s' will be outdated by %d inputs.",
                    metric_name,
                    cca_metric.window_size - val_size,
                )

            log_freq = cca_metric.window_size // self._batch_size
            train_metrics.extend(
                [
                    TrainingMetric(
                        metric_name,
                        cca_metric,
                        log_freq,
                        _update_with_projected_views,
                    ),
                    TrainingMetric(
                        f"corr_x{cca_metric.window_size}",
                        WindowedNonResetableCorrelationMetric(cca_metric.window_size),
                        log_freq,
                        _update_with_projected_views,
                    ),
                ]
            )

        return train_metrics

    @torch.inference_mode()
    def predict(self, inputs: Dataset) -> Iterable[np.ndarray]:
        self._model.eval()
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(f"Predicting using {device}")
        self._model.to(device)

        data = self._to_dataloader(inputs, training=False)
        for batch in tqdm(data, desc="Predicting batches"):
            if "labels" in batch:
                del batch["labels"]
            train_utils.batch_to_device(batch, device)
            embeddings = self._model(**batch)["embeddings"]
            yield embeddings.numpy(force=True)