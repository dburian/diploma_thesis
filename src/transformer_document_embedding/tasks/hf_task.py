from __future__ import annotations
import math
from abc import abstractmethod
from typing import TYPE_CHECKING

import numpy as np

from transformer_document_embedding.tasks.experimental_task import ExperimentalTask

if TYPE_CHECKING:
    from typing import Any, Optional, Union
    from datasets.dataset_dict import DatasetDict


class HFTask(ExperimentalTask):
    def __init__(
        self,
        *,
        data_size_limit: Optional[Union[int, dict]] = None,
        add_ids: bool = False,
        validation_source_fraction: Optional[float] = None,
        validation_source: Optional[str] = None,
    ) -> None:
        self._data_size_limit = data_size_limit

        self._add_ids = add_ids
        self._validation_fraction = validation_source_fraction
        self._validation_source = validation_source
        self._splits = None

    @property
    def splits(self) -> DatasetDict:
        if self._splits is None:
            dataset = self._retrieve_dataset()
            self._splits = self._create_splits(dataset)

        return self._splits

    @abstractmethod
    def _retrieve_dataset(self) -> DatasetDict:
        """Obtains the dataset. By default using the load_dataset function."""
        raise NotImplementedError()

    def _shorten_splits(self, dataset: DatasetDict) -> DatasetDict:
        if self._data_size_limit is None:
            return dataset

        if not isinstance(self._data_size_limit, dict):
            self._data_size_limit = {
                split_name: self._data_size_limit for split_name in dataset.keys()
            }

        for name, split in dataset.items():
            limit = self._data_size_limit.get(name, None)
            if limit is not None and len(split) > limit:
                dataset[name] = split.select(range(limit))

        return dataset

    def _create_splits(self, dataset: DatasetDict) -> DatasetDict:
        """Creates splits."""
        if self._add_ids:
            begin_id = 0

            def map_fn(_, idx: int) -> dict[str, Any]:
                return {"id": idx + begin_id}

            for name, split in dataset.items():
                dataset[name] = split.map(map_fn, with_indices=True)
                begin_id += len(dataset[name])

        if (
            "validation" not in dataset
            and self._validation_source is not None
            and self._validation_fraction is not None
        ):
            validation_source = dataset[self._validation_source]

            source_len = len(validation_source)
            val_len = math.floor(source_len * self._validation_fraction)

            all_indices = np.arange(source_len)
            np.random.shuffle(all_indices)

            val_indices = all_indices[:val_len]
            new_source_indicies = all_indices[val_len:]

            dataset["validation"] = validation_source.select(val_indices)
            dataset[self._validation_source] = validation_source.select(
                new_source_indicies
            )

        dataset = self._shorten_splits(dataset)

        return dataset

    @abstractmethod
    def evaluate(self, split, pred_batches) -> dict[str, float]:
        raise NotImplementedError()
