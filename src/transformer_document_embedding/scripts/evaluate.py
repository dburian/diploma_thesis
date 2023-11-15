"""Runs experiment for given model and task.

# TODO: Document this module once the interface settles...
"""
from __future__ import annotations

import argparse
import logging
import os

from transformer_document_embedding.scripts.args import (
    add_common_args,
)


from transformer_document_embedding.experiments.config import ExperimentConfig
from transformer_document_embedding.experiments.result import save_csv_results
from transformer_document_embedding.scripts.pipelines import (
    InitializeModelAndTask,
    TrainingPipeline,
)

training_pipeline = TrainingPipeline(train=True)
initialization_pipeline = InitializeModelAndTask()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    add_common_args(parser)
    training_pipeline.add_args(parser)
    initialization_pipeline.add_args(parser)

    return parser.parse_args()


def log_results(log_path: str, results: dict[str, float]) -> None:
    import tensorflow as tf

    with tf.summary.create_file_writer(log_path).as_default():
        for name, res in results.items():
            tf.summary.scalar(name, res, step=1)

        tf.summary.flush()


def evaluate_best(
    config: ExperimentConfig,
    args: argparse.Namespace,
) -> dict[str, float]:
    model, task = initialization_pipeline.run(config)

    training_pipeline.run(args, model, task, config)

    logging.info("Evaluating on test data...")
    test_predictions = model.predict(task.test)
    results = task.evaluate(task.test, test_predictions)
    logging.info("Evaluation done. Results:\n%s", results)

    save_csv_results(results, config.experiment_path)

    test_log_path = os.path.join(config.experiment_path, "test")
    log_results(test_log_path, results)

    return results


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        format="%(asctime)s : %(levelname)s : %(message)s", level=logging.INFO
    )

    config = ExperimentConfig.from_yaml(
        args.config, args.output_base_path, name=args.name
    )

    evaluate_best(config, args)


if __name__ == "__main__":
    main()
