from __future__ import annotations

from typing import TYPE_CHECKING

from datasets import load_dataset
from torcheval.metrics import (
    BinaryAUPRC,
    BinaryAccuracy,
    BinaryF1Score,
    BinaryPrecision,
    BinaryRecall,
)
from transformer_document_embedding.tasks.hf_task import HFTask
from transformer_document_embedding.utils.evaluation import aggregate_batches
from datasets import DatasetDict

if TYPE_CHECKING:
    from typing import Iterable
    from datasets import Dataset
    import numpy as np


class IMDBClassification(HFTask):
    """Classification task done using the IMDB dataset.

    `datasets.Dataset` of documents; by default with 'train', 'test' and
    'unsupervised' splits.

    Each document is dictionary with keys:
        - 'text' (str) - text of the document,
        - 'label' (int) - 1/0 sentiment class index; not present for the
          unsupervised split
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(add_ids=True, **kwargs)
        self._path = "imdb"

    def _retrieve_dataset(self) -> DatasetDict:
        dataset_dict = load_dataset(self._path)
        assert isinstance(dataset_dict, DatasetDict)
        return dataset_dict

    def evaluate(
        self, split: Dataset, pred_batches: Iterable[np.ndarray]
    ) -> dict[str, float]:
        import torch

        metrics = {
            "accuracy": BinaryAccuracy(),
            "recall": BinaryRecall(),
            "precision": BinaryPrecision(),
            "f1": BinaryF1Score(),
            "auprc": BinaryAUPRC(),
        }

        for metric in metrics.values():
            metric.reset()

        for pred_batch, true_batch in aggregate_batches(
            pred_batches, iter(split), lambda x: x["label"]
        ):
            for met in metrics.values():
                met.update(torch.from_numpy(true_batch), torch.from_numpy(pred_batch))

        return {name: met.compute().numpy().item() for name, met in metrics.items()}
