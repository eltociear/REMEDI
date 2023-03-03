"""Generate a bunch of REMEDI directions for analysis."""
import argparse
import logging
from typing import cast

from remedi import data, editors, models, precompute
from remedi.utils import experiment_utils, logging_utils

import baukit
import torch
import torch.utils.data
from tqdm.auto import tqdm

logger = logging.getLogger(__name__)


def main(args: argparse.Namespace) -> None:
    """Generate the directions."""
    experiment = experiment_utils.setup_experiment(args)
    data.disable_caching()
    logging_utils.configure(args=args)

    device = args.device or "cuda" if torch.cuda.is_available() else "cpu"
    mt = models.load_model(args.model, device=device, fp16=args.fp16)

    split = f"train[5000:{5000 + args.size}]"
    dataset = data.load_dataset(args.dataset, split=split)

    layers = args.layers
    if layers is None:
        layers = models.determine_layers(mt)

    for layer in layers:
        results_file = (
            experiment.results_dir / args.editor_type / str(layer) / "dump.pth"
        )
        if results_file.exists():
            logger.info(f"found existing dump for layer {layer}; skipping")
            continue

        editor = editors.load_editor(
            mt, args.editor_type, layer, editors_dir=args.editors_dir, device=device
        )
        if editor is None:
            logger.warning(f"skipping dump for layer {layer}")
            continue

        logger.info(f"begin layer {layer}")
        subsequent_layer_paths = models.determine_layer_paths(
            mt, layers=models.determine_layers(mt)[layer:], return_dict=True
        )

        precomputed = precompute.editor_inputs_from_dataset(
            mt=mt,
            dataset=dataset,
            layers=[layer],
            device=device,
            batch_size=args.batch_size,
        )

        columns = data.column_names(
            precomputed, exclude=["target_mediated", "target_unmediated", "source"]
        )

        samples = []
        with precomputed.formatted_as("torch", columns=columns):
            loader = torch.utils.data.DataLoader(
                cast(torch.utils.data.Dataset, precomputed),
                batch_size=args.batch_size,
            )
            for batch in tqdm(loader, desc="generate directions"):
                with baukit.TraceDict(mt.model, subsequent_layer_paths.values()) as ret:
                    with editors.apply(editor, device=device) as edited_mt:
                        outputs = edited_mt.model.compute_model_outputs(batch)

                hs_attr = batch[f"context.attribute.hiddens.{layer}.average"]

                # Record entity reps for all layers after the edit.
                entity_idx = batch[f"prompt.entity.token_range.last"][:, 0]
                hs_entity = [
                    {
                        layer: ret[layer_path][bi, ei].detach().cpu()
                        for layer, layer_path in subsequent_layer_paths.items()
                    }
                    for bi, ei in enumerate(entity_idx)
                ]

                for index, (direction, h_entity, h_attr) in enumerate(
                    zip(outputs.direction, hs_entity, hs_attr)
                ):
                    sample = {
                        "direction": direction.cpu(),
                        "h_entity": h_entity,
                        "h_attr": h_attr.cpu(),
                    }
                    for key in ("id", "entity", "prompt", "context", "attribute"):
                        sample[key] = batch[key][index]
                    samples.append(sample)

        logger.info(f"dumping layer {layer} directions to {results_file}")
        results_file.parent.mkdir(exist_ok=True, parents=True)
        torch.save(samples, results_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="generate directions to analyze")
    parser.add_argument(
        "--size", type=int, default=1000, help="number of test data points to use"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=editors.DEFAULT_BATCH_SIZE,
        help="model batch size",
    )
    data.add_dataset_args(parser)
    models.add_model_args(parser)
    editors.add_editor_args(parser)
    experiment_utils.add_experiment_args(parser)
    logging_utils.add_logging_args(parser)
    args = parser.parse_args()
    main(args)
