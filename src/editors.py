"""Editing models."""
import contextlib
from dataclasses import dataclass
from typing import Any, Literal, Optional, Sequence, cast, overload

from src import precompute
from src.utils import dataset_utils, model_utils, tokenizer_utils, training_utils
from src.utils.typing import (
    Dataset,
    Device,
    Model,
    ModelGenerateOutput,
    ModelOutput,
    Tokenizer,
)

import torch
import torch.utils.data
import transformers.modeling_outputs
from baukit import nethook, runningstats
from dataclasses_json import DataClassJsonMixin
from torch import nn, optim
from tqdm.auto import tqdm

DEFAULT_ALPHA = 1.0
DEFAULT_LAM = 0.25
DEFAULT_N_TOP = 10
DEFAULT_N_GENERATE = 10
DEFAULT_BATCH_SIZE = 128
DEFAULT_MAX_EPOCHS = 10
DEFAULT_PATIENCE = 2
DEFAULT_LR = 1e-2
DEFAULT_HOLD_OUT = 0.1


class apply_direction(contextlib.AbstractContextManager):
    """Context manager that adds directions to specific hiddens during forward pass.

    Args:
        model: The model to apply the directions to.
        layer: The layer to apply the directions at.
        directions: Directions to apply. Needs shape (batch_size, hidden_size).
        token_ranges: Token ranges to apply direction at. Needs shape (batch_size, 2).
        alpha: Step size for applying the direction.

    """

    def __init__(
        self,
        *,
        model: Model,
        layer: int,
        directions: torch.Tensor,
        token_ranges: torch.Tensor,
        alpha: float = DEFAULT_ALPHA,
    ):
        """Initialize the context manager."""
        self.model = model
        self.layer = layer
        self.directions = directions
        self.token_ranges = token_ranges
        self.alpha = alpha
        self._trace = None

    def __enter__(self) -> Model:
        """Hook the model so the direction is applied."""
        [layer_path] = model_utils.determine_layer_paths(
            self.model, layers=[self.layer]
        )

        def edit_output(output: tuple[torch.Tensor, ...]) -> tuple[torch.Tensor, ...]:
            """Apply the directions."""
            if output[0].shape[1] == 1:
                # This condition is satisfied only when we're generating beyond the
                # prompt, in which case, we never want to edit the output.
                return output
            for bi, (i, j) in enumerate(self.token_ranges.tolist()):
                output[0][bi, i:j] = (
                    output[0][bi, i:j] + self.alpha * self.directions[bi]
                )
            return (output[0], *output[1:])

        self._trace = nethook.Trace(
            self.model, layer=layer_path, edit_output=edit_output
        )
        return self.model

    def __exit__(self, *_: Any) -> Optional[bool]:
        """Unhook the model."""
        assert self._trace is not None
        self._trace.close()
        self._trace = None
        return None


class EditedModel(nn.Module):
    """Wrapper around an LM which knows how to automatically apply mediation.

    Importantly, this model assumes inputs look like `ContextMediationSample`s
    or batches of them. See `src/utils/dataset_utils.py` for more details.
    """

    def __init__(
        self,
        editor: "Editor",
        mt: Optional[model_utils.ModelAndTokenizer] = None,
        alpha: float = DEFAULT_ALPHA,
        device: Optional[Device] = None,
    ):
        """Wrap the model to be edited."""
        super().__init__()
        self.mt = mt if mt is not None else editor.mt
        self.editor = editor
        self.alpha = alpha
        self.device = device

    @overload
    def _run_edited_model(
        self,
        batch: dataset_utils.ContextMediationInput,
        generate: Literal[False] = ...,
        inputs: Optional[transformers.BatchEncoding] = ...,
        **kwargs: Any,
    ) -> ModelOutput:
        ...

    @overload
    def _run_edited_model(
        self,
        batch: dataset_utils.ContextMediationInput,
        generate: Literal[True],
        inputs: Optional[transformers.BatchEncoding] = ...,
        **kwargs: Any,
    ) -> ModelGenerateOutput:
        ...

    def _run_edited_model(
        self,
        batch: dataset_utils.ContextMediationInput,
        generate: bool = False,
        inputs: Optional[transformers.BatchEncoding] = ...,
        **kwargs: Any,
    ) -> ModelOutput | ModelGenerateOutput:
        """Run the model on the inputs, editing its hidden reps in the process."""
        layer = self.editor.layer

        precomputed = {**batch}
        if not precompute.has_editor_inputs(precomputed):
            precomputed = precompute.editor_inputs_from_batch(
                self.mt,
                batch,
                layers=[layer],
                device=self.device,
                return_target_token_ids=False,
            )
        entity_ij = precomputed["prompt.token_range.entity"]
        hiddens_attr = precomputed[f"context.hiddens.{layer}.attribute"]

        # Make type checker happy.
        entity_ij = cast(torch.Tensor, entity_ij)
        hiddens_attr = cast(torch.Tensor, hiddens_attr)

        directions = self.editor(hiddens_attr.to(self.device))

        if inputs is None:
            prompt = batch["prompt"]
            inputs, _ = precompute.inputs_from_batch(
                self.mt, prompt, device=self.device
            )

        with apply_direction(
            model=self.mt.model,
            layer=layer,
            directions=directions,
            token_ranges=entity_ij,
            alpha=self.alpha,
        ) as model:
            if generate:
                kwargs.setdefault("max_new_tokens", DEFAULT_N_GENERATE)
                outputs = model.generate(**inputs, **kwargs)
            else:
                outputs = model(**inputs, **kwargs)

        return outputs

    def forward(
        self,
        batch: dataset_utils.ContextMediationInput,
        inputs: Optional[transformers.BatchEncoding] = None,
        **kwargs: Any,
    ) -> ModelOutput:
        """Compute the edited outputs for the context mediation sample.

        Args:
            batch: The sample or batch of samples.
            inputs: Precomputed tokenized prompt. If not set, will read
                `batch["prompt"]` and pass it through the tokenizer.

        Returns:
            Standard huggingface outputs, but for the edited model.

        """
        return self._run_edited_model(batch, inputs=inputs, generate=False, **kwargs)

    def generate(
        self,
        batch: dataset_utils.ContextMediationInput,
        inputs: Optional[transformers.BatchEncoding] = None,
        **kwargs: Any,
    ) -> ModelGenerateOutput:
        """Forwards to `mt.model.generate`, but still applies editor."""
        return self._run_edited_model(batch, inputs=inputs, generate=True, **kwargs)


@dataclass(frozen=True)
class EditedModelAndTokenizer:
    """Counterpart to ModelAndTokenizer for edited models."""

    model: EditedModel
    tokenizer: Tokenizer


class apply(contextlib.AbstractContextManager):
    """Context manager that computes and applies edit directions in place.

    Args:
        editor: The editor to apply.
        mt: The model/tokenizer to apply the editor to. By default, uses the model
            that the editor was trained, but you could (in theory) specify any
            model which has the same number of layers!

    """

    def __init__(
        self,
        editor: "Editor",
        mt: Optional[model_utils.ModelAndTokenizer] = None,
        alpha: float = DEFAULT_ALPHA,
        device: Optional[Device] = None,
    ):
        """Initialize the context manager."""
        self.mt = mt
        self.editor = editor
        self.alpha = alpha
        self.device = device
        self._hooked: EditedModel | None = None

    def __enter__(
        self,
    ) -> EditedModelAndTokenizer:
        """Wrap the model."""
        self._hooked = EditedModel(
            self.editor, mt=self.mt, alpha=self.alpha, device=self.device
        )
        return EditedModelAndTokenizer(self._hooked, self._hooked.mt.tokenizer)

    def __exit__(self, *_: Any) -> Optional[bool]:
        """Unwrap the model."""
        self._hooked = None
        return None


def editing_loss(
    *,
    editor: "Editor",
    batch: dict,
    lam: float = DEFAULT_LAM,
    kl: Optional[nn.KLDivLoss] = None,
    device: Optional[Device] = None,
) -> torch.Tensor:
    """Apply the edit to the representat

    See `src.precompute.editor_inputs_from_dataset` for expected format of `batch`.
    """
    prompt = batch["prompt"]
    mediated_token_ids = batch["target_mediated.token_id"]

    inputs = editor.mt.tokenizer(
        prompt, return_tensors="pt", padding="longest", truncation=True
    ).to(device)

    # If necessary, determine original next token distribution.
    logps_orig = None
    if kl is not None:
        with torch.inference_mode():
            outputs_orig = editor.mt.model(**inputs)
            logps_orig = torch.log_softmax(outputs_orig.logits, dim=-1)

    with apply(editor, device=device) as mt_edit:
        outputs_edit = mt_edit.model(batch, inputs=inputs)
        logps_edit = torch.log_softmax(outputs_edit.logits, dim=-1)

    # Compute simple loss: the probability of the target token post-edit.
    loss = torch.tensor(0.0, device=device)
    for bi, mti in enumerate(mediated_token_ids.tolist()):
        logp_mediated = logps_edit[bi, -1, mti]
        loss += -logp_mediated
    batch_size = mediated_token_ids.shape[0]
    loss /= batch_size

    # If specified, include a KL loss term with the original token distribution.
    if kl is not None:
        assert logps_orig is not None
        logps_edit = logps_edit[torch.arange(batch_size), -1]
        logps_orig = logps_orig[torch.arange(batch_size), -1]
        loss += lam * kl(logps_edit, logps_orig)

    return loss


@dataclass(frozen=True)
class EditorTrainingRun:
    """Results from running `Editor.fit`."""

    dataset: Dataset


@dataclass(frozen=True)
class EditorEvaluationResult(DataClassJsonMixin):
    """Result for single sample from `Editor.evaluate`."""

    sample: dict[str, str]

    # Note: Need explicit types here so DataClassJsonMixin works.
    before_top_tokens: list[str]
    before_top_scores: list[float]
    before_generations: list[str]

    after_top_tokens: list[str]
    after_top_scores: list[float]
    after_generations: list[str]

    before_target_mediated_score: Optional[float] = None
    before_target_unmediated_score: Optional[float] = None

    after_target_mediated_score: Optional[float] = None
    after_target_unmediated_score: Optional[float] = None


@dataclass(frozen=True)
class EditorEvaluateRun(DataClassJsonMixin):
    """Wrapper around a list of individual evaluation results."""

    results: list[EditorEvaluationResult]


class Editor(nn.Module):
    """A simple linear editing model."""

    def __init__(self, *, mt: model_utils.ModelAndTokenizer, layer: int):
        """Initialize the editor."""
        super().__init__()
        self.mt = mt
        self.layer = layer
        self.to(device=model_utils.determine_device(mt), dtype=torch.float16)

    def __call__(self, attribute: torch.Tensor) -> torch.Tensor:
        """Map the attribute hidden representation to an entity edit direction."""
        raise NotImplementedError

    def fit(
        self,
        *,
        dataset: Dataset,
        max_epochs: int = DEFAULT_MAX_EPOCHS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        hold_out: float = DEFAULT_HOLD_OUT,
        lr: float = DEFAULT_LR,
        lam: float = DEFAULT_LAM,
        patience: int = DEFAULT_PATIENCE,
        device: Optional[Device] = None,
    ) -> EditorTrainingRun:
        """Train this editor.

        Args:
            mt: The model and tokenizer.
            dataset: Any context mediation dataset. If a DatasetDict, must have train
                and val splits.
            max_epochs: Max epochs to train for.
            batch_size: Batch size for training editor. Note this is constrained mostly
                by how many sentences the model can process at once!
            hold_out: Hold out this fraction of data for validation.
            lr: Learning rate. Defaults to 1e-2.
            patience: Stop after val loss does not improve for this many epochs.
            device: Run editor and model on this device.

        """
        dataset = dataset_utils.maybe_train_test_split(dataset, test_size=hold_out)

        self.mt.model.to(device)
        self.to(device)

        self.mt.model.eval()
        for parameter in self.mt.model.parameters():
            parameter.requires_grad_(True)

        optimizer = optim.AdamW(self.parameters(), lr=lr)
        stopper = training_utils.EarlyStopping(patience=patience)

        with dataset.formatted_as("torch"):
            train = cast(torch.utils.data.Dataset, dataset["train"])
            val = cast(torch.utils.data.Dataset, dataset["test"])
            train_loader = torch.utils.data.DataLoader(train, batch_size=batch_size)
            val_loader = torch.utils.data.DataLoader(val, batch_size=batch_size)

            # TODO(evandez): More flexible loss function.
            kl = nn.KLDivLoss(reduction="batchmean", log_target=True).to(device)

            best = self.state_dict()
            for epoch in range(max_epochs + 1):
                desc = f"epoch {epoch}/{max_epochs}"

                self.train()
                train_loss = 0.0
                train_progress_bar = tqdm(train_loader)
                for batch in train_progress_bar:
                    optimizer.zero_grad()
                    loss = editing_loss(
                        editor=self, batch=batch, kl=kl, lam=lam, device=device
                    )
                    if epoch > 0:
                        loss.backward()
                        optimizer.step()
                    train_loss += loss.item()
                    train_progress_bar.set_description(
                        f"{desc} train={loss.item():.2f}"
                    )
                train_loss /= len(train_loader)

                self.eval()
                val_loss = 0.0
                val_progress_bar = tqdm(val_loader)
                for batch in val_progress_bar:
                    with torch.inference_mode():
                        loss = editing_loss(
                            editor=self,
                            batch=batch,
                            kl=kl,
                            lam=lam,
                            device=device,
                        )
                    val_loss += loss.item()
                    val_progress_bar.set_description(
                        f"{desc} val={loss.item():.3f} best={stopper.best:.2f}"
                    )
                val_loss /= len(val_loader)

                if stopper(val_loss):
                    break
                elif stopper.improved:
                    best = self.state_dict()

        self.load_state_dict(best)
        return EditorTrainingRun(dataset=dataset)

    @torch.inference_mode()
    def evaluate(
        self,
        dataset: Dataset,
        batch_size: int = DEFAULT_BATCH_SIZE,
        n_top: int = DEFAULT_N_TOP,
        n_generate: int = DEFAULT_N_GENERATE,
        alpha: float = DEFAULT_ALPHA,
        device: Optional[Device] = None,
    ) -> EditorEvaluateRun:
        """Evaluate the editor on a held out set.

        Args:
            dataset: Dataset to evaluate on.
            batch_size: Model batch size.
            n_top: Number of top words/probs to return.
            n_generate: Number of tokens to generate.
            alpha: Step size for applying edit directions.
            device: Send all data to this device. Defaults to None.

        Returns:
            The evaluation results, one per entry in the dataset.

        """
        self.mt.eval_()
        self.mt.to_(device)
        self.eval().to(device)
        include_target_probs = "target_mediated" in dataset.column_names

        results = []
        with dataset.formatted_as("torch"):
            loader = torch.utils.data.DataLoader(
                cast(torch.utils.data.Dataset, dataset), batch_size=batch_size
            )
            for batch in tqdm(loader, desc=f"evaluate editor (layer={self.layer})"):
                prompts = batch["prompt"]
                current_batch_size = len(prompts)
                inputs, _ = precompute.inputs_from_batch(
                    self.mt, prompts, device=device
                )

                generate_kwargs = dict(
                    do_sample=False,
                    max_new_tokens=n_generate,
                    return_dict_in_generate=True,
                    output_scores=True,
                    pad_token_id=self.mt.tokenizer.eos_token_id,
                )
                outputs_before = self.mt.model.generate(**inputs, **generate_kwargs)
                with apply(self, alpha=alpha, device=device) as edited_mt:
                    outputs_after = edited_mt.model.generate(
                        batch, inputs=inputs, **generate_kwargs
                    )

                batched_results: dict = {
                    "sample": [
                        {
                            key: batch[key][bi]
                            for key in dataset_utils.ContextMediationSample.__required_keys__
                        }
                        for bi in range(current_batch_size)
                    ]
                }
                for key, outputs in (
                    ("before", outputs_before),
                    ("after", outputs_after),
                ):
                    first_token_scores = outputs.scores[0]
                    top_scores, top_token_ids = first_token_scores.topk(k=n_top, dim=-1)
                    top_tokens = tokenizer_utils.batch_convert_ids_to_tokens(
                        top_token_ids, self.mt.tokenizer
                    )
                    generations = self.mt.tokenizer.batch_decode(
                        outputs.sequences, skip_special_tokens=True
                    )

                    batched_results[f"{key}_top_scores"] = top_scores.tolist()
                    batched_results[f"{key}_top_tokens"] = top_tokens
                    batched_results[f"{key}_generations"] = generations

                    if include_target_probs:
                        batch_indices = torch.arange(current_batch_size)
                        for target_key in ("mediated", "unmediated"):
                            target_id = batch[f"target_{target_key}.token_id"]
                            target_probs = first_token_scores[batch_indices, target_id]
                            target_prob_key = f"{key}_target_{target_key}_score"
                            batched_results[target_prob_key] = target_probs.tolist()

                # Flatten results.
                for bi in range(current_batch_size):
                    result_kwargs: dict
                    result_kwargs = {k: vs[bi] for k, vs in batched_results.items()}
                    result = EditorEvaluationResult(**result_kwargs)
                    results.append(result)

            return EditorEvaluateRun(results)


class RandomEditor(Editor):
    """An editor that just picks a random edit direction."""

    def __init__(
        self,
        *,
        mt: model_utils.ModelAndTokenizer,
        layer: int,
        mean: torch.Tensor | None = None,
        covariance: torch.Tensor | None = None,
    ) -> None:
        """Initialize the editor."""
        super().__init__(mt=mt, layer=layer)

        hidden_size = model_utils.determine_hidden_size(mt)
        device = model_utils.determine_device(mt)

        if mean is None:
            mean = torch.zeros(hidden_size)
        if covariance is None:
            covariance = torch.ones(hidden_size)

        self.mean: torch.Tensor
        self.register_buffer("mean", mean.to(device))

        self.covariance: torch.Tensor
        self.register_buffer("covariance", covariance.to(device))

    def forward(self, attribute: torch.Tensor) -> torch.Tensor:
        """Select a random direction."""
        distribution = torch.distributions.MultivariateNormal(
            self.mean, self.covariance
        )
        return distribution.sample((len(attribute),))

    def fit(
        self,
        *,
        dataset: Dataset,
        batch_size: int = DEFAULT_BATCH_SIZE,
        hold_out: float = DEFAULT_HOLD_OUT,
        device: Optional[Device] = None,
        **_: Any,
    ) -> EditorTrainingRun:
        """Estimate mean and variance of entity representations."""
        dataset = dataset_utils.maybe_train_test_split(dataset, test_size=hold_out)

        self.mt.model.to(device)
        self.to(device)

        rc = runningstats.Covariance()
        with dataset.formatted_as("torch"):
            loader = torch.utils.data.DataLoader(
                cast(torch.utils.data.Dataset, dataset["train"]), batch_size=batch_size
            )
            for batch in tqdm(loader, desc="estimate mean/cov"):
                if not precompute.has_entity_deltas(batch):
                    batch = precompute.entity_deltas_from_batch(
                        self.mt, batch, layers=[self.layer], device=device
                    )
                rc.add(batch[f"prompt_in_context.delta.{self.layer}"])

        self.mean[:] = rc.mean()
        self.covariance[:] = rc.covariance()
        return EditorTrainingRun(dataset)


class LinearEditor(Editor):
    """A simple linear model, optionally with a rank constraint."""

    def __init__(
        self,
        *,
        mt: model_utils.ModelAndTokenizer,
        layer: int,
        rank: Optional[int] = None,
    ):
        """Initialize the editor.

        Args:
            rank: Rank constraint on the linear transformation. Defaults to None.

        """
        super().__init__(mt=mt, layer=layer)

        hidden_size = model_utils.determine_hidden_size(mt)

        self.linear: nn.Linear | nn.Sequential
        if rank is None:
            self.linear = nn.Linear(hidden_size, hidden_size)
        else:
            self.linear = nn.Sequential(
                nn.Linear(hidden_size, rank),
                nn.Linear(rank, hidden_size),
            )

    def __call__(self, attribute: torch.Tensor) -> torch.Tensor:
        """Compute the edit direction."""
        return self.linear(attribute)


# TODO(evandez): Small fixes needed for this file:
# - Why does editor become fp32 after training?
# - Need a way to have evaluation results point back to original dataset.
# - This currently tokenizes the prompt twice, can we avoid?
# - Show running average loss in progress bar, not batch loss.
