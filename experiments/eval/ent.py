"""Evaluate REMEDI on how entailed attribute probs change."""
from experiments import utils
from experiments.aliases import *

from invoke import task


@task(name=MC)
def eval_ent_mc(c, model=DEFAULT_MODEL, device=None):
    """Evaluate REMEDI on entailment."""
    editors_dir = utils.require_editors_dir(model=model, dataset=CF)
    name = utils.experiment_name("eval_ent_mcrae", model=model)
    layer = REMEDI_EDITOR_LAYER[model][MC]
    cmd = f"python -m scripts.eval_entailment -n {name} -m {model} -l {layer} -e {editors_dir}"
    cmd = utils.maybe_set_device(cmd, device=device)
    c.run(cmd)
