from __future__ import annotations
from functools import reduce
from typing import Any, Iterable, Optional, TYPE_CHECKING, cast
from datasets.fingerprint import generate_random_fingerprint
import faiss
import numpy as np
from tqdm.auto import tqdm

from transformer_document_embedding.datasets import col
from transformer_document_embedding.pipelines.classification_eval import smart_unbatch
from transformer_document_embedding.pipelines.pipeline import EvalPipeline


if TYPE_CHECKING:
    import torch
    from datasets import Dataset
    from transformer_document_embedding.models.embedding_model import EmbeddingModel
    from transformer_document_embedding.datasets.document_dataset import DocumentDataset


class RetrievalEval(EvalPipeline):
    def _get_nearest_ids_from_faiss(
        self,
        true_dataset: Dataset,
        embeddings: Iterable[np.ndarray],
        *,
        k: Optional[int] = None,
    ) -> Iterable[tuple[list[int], list[int]]]:
        faiss_dataset = true_dataset.add_column(
            name=col.EMBEDDING,
            column=map(lambda vec: vec / np.linalg.norm(vec), embeddings),
            new_fingerprint=generate_random_fingerprint(),
        )
        faiss_dataset.add_faiss_index(
            col.EMBEDDING, metric_type=faiss.METRIC_INNER_PRODUCT
        )

        if k is None:
            k = len(faiss_dataset)

        for article in faiss_dataset:
            article = cast(dict[str, Any], article)

            if len(article[col.LABEL]) == 0:
                continue

            nearest_targets = faiss_dataset.get_nearest_examples(
                col.EMBEDDING,
                np.array(article[col.EMBEDDING]),
                k=k
                + 1,  # We're later removing the first hit, which is the query itself.
            )

            true_ids = [target_article[col.ID] for target_article in article[col.LABEL]]
            pred_ids = nearest_targets.examples[col.ID][1:]

            yield true_ids, pred_ids

    def _evaluate_ir_metrics(
        self,
        true_pred_ids_iterable: Iterable[tuple[list[int], list[int]]],
        *,
        hits_thresholds: list[int] | np.ndarray,
        iterable_length: Optional[int] = None,
        verbose: bool = False,
    ) -> dict[str, float]:
        hits_thresholds = np.array(hits_thresholds, dtype=np.int32)
        hit_percentages = [[] for _ in hits_thresholds]
        reciprocal_rank = 0
        percentile_ranks = []

        total_queries = 0

        for true_ids, pred_ids in tqdm(
            true_pred_ids_iterable,
            desc="Checking similarities",
            disable=not verbose,
            total=iterable_length,
        ):
            max_rank = len(pred_ids)
            first_hit_ind = max_rank
            query_hits = np.zeros(len(hits_thresholds))
            # For all predictions
            for i, pred_id in enumerate(pred_ids):
                # Skip those which are incorrect
                if pred_id not in true_ids:
                    continue

                # Save the best-ranking correct prediction index
                if first_hit_ind > i:
                    first_hit_ind = i

                percentile_ranks.append(i / max_rank)
                # For every correct prediction under a threshold we add 1
                query_hits += (i < hits_thresholds).astype("int32")

            # first_hit_ind could be zero if len(true_ids) == 0
            reciprocal_rank += 1 / (first_hit_ind if first_hit_ind > 0 else 1)
            total_queries += 1

            for perctanges, num_of_hits in zip(
                hit_percentages, query_hits, strict=True
            ):
                perctanges.append(num_of_hits / len(true_ids))

        results = {
            "mean_reciprocal_rank": reciprocal_rank / total_queries,
            "mean_percentile_rank": np.mean(percentile_ranks).item(),
        }

        for percentages, threshold in zip(
            hit_percentages, hits_thresholds, strict=True
        ):
            results[f"hit_rate_at_{threshold}"] = np.mean(percentages).item()

        return results

    def __call__(
        self,
        model: EmbeddingModel,
        _: Optional[torch.nn.Module],
        dataset: DocumentDataset,
    ) -> dict[str, float]:
        test_split = dataset.splits["test"]
        embeddings_iter = (
            embed.numpy(force=True)
            for embed in smart_unbatch(model.predict_embeddings(test_split), 1)
        )

        true_pred_ids_iter = self._get_nearest_ids_from_faiss(
            test_split,
            embeddings_iter,
            k=1000,
        )

        test_sims_total = reduce(
            lambda acc, doc: acc + int(len(doc[col.LABEL]) > 0),
            test_split,
            0,
        )

        return self._evaluate_ir_metrics(
            true_pred_ids_iter,
            hits_thresholds=[10, 100],
            iterable_length=test_sims_total,
            verbose=True,
        )