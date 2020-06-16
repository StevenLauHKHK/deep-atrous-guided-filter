"""
Val Script for Phase/Amp mask
"""
# Libraries
from sacred import Experiment
from tqdm import tqdm
from collections import defaultdict
import numpy as np
from loss import GLoss
import logging
import cv2

# Torch Libs
import torch
from torch.utils.tensorboard import SummaryWriter

# Modules
from dataloader import get_dataloaders
from utils.dir_helper import dir_init
from utils.tupperware import tupperware
from models import get_model
from metrics import PSNR
from config import initialise
from skimage.metrics import structural_similarity as ssim


# Typing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.typing_alias import *

# Train helpers
from utils.train_helper import set_device, load_models, AvgLoss_with_dict

# Experiment, add any observers by command line
ex = Experiment("val")
ex = initialise(ex)

# To prevent "RuntimeError: received 0 items of ancdata"
torch.multiprocessing.set_sharing_strategy("file_system")


@ex.automain
def main(_run):
    args = tupperware(_run.config)
    args.lambda_perception = 0.0
    args.finetune = False
    args.batch_size = 1

    # Set device, init dirs
    device, source_device = set_device(args)
    dir_init(args)

    # Get data
    data = get_dataloaders(args)

    data.val_loader = data.train_loader

    # Model
    G, _ = get_model.model(args, source_device=source_device, target_device=device)
    G = G.to(device)

    # Load Models
    (G, _), _, global_step, start_epoch, loss = load_models(
        G,
        D=None,
        g_optimizer=None,
        d_optimizer=None,
        args=args,
        tag=args.inference_mode,
    )

    # Compatibility with checkpoints without global_step
    if not global_step:
        global_step = start_epoch * len(data.train_loader) * args.batch_size
    start_epoch = global_step // len(data.train_loader.dataset)

    _metrics_dict = {"PSNR": 0.0, "SSIM": 0.0}
    avg_metrics = AvgLoss_with_dict(loss_dict=_metrics_dict, args=args)

    logging.info(f"Loaded experiment {args.exp_name} trained for {start_epoch} epochs.")

    # Run val for an epoch
    avg_metrics.reset()
    pbar = tqdm(range(len(data.val_loader) * args.batch_size), dynamic_ncols=True)

    # Val and test paths
    val_path = args.output_dir / f"val_{args.inference_mode}"
    val_path.mkdir(exist_ok=True, parents=True)

    test_path = args.output_dir / f"test_{args.inference_mode}"
    test_path.mkdir(exist_ok=True, parents=True)

    with torch.no_grad():
        G.eval()

        if data.val_loader:
            for i, batch in enumerate(data.val_loader):
                metrics_dict = defaultdict(float)

                source, target, filename = batch
                source, target = (source.to(device), target.to(device))

                output = G(source)

                # PSNR
                metrics_dict["PSNR"] += PSNR(output, target)

                for e in range(args.batch_size):
                    # Compute SSIM
                    target_numpy = (
                        target[e]
                        .mul(0.5)
                        .add(0.5)
                        .permute(1, 2, 0)
                        .cpu()
                        .detach()
                        .numpy()
                    )

                    output_numpy = (
                        output[e]
                        .mul(0.5)
                        .add(0.5)
                        .permute(1, 2, 0)
                        .cpu()
                        .detach()
                        .numpy()
                    )
                    metrics_dict["SSIM"] += ssim(
                        target_numpy, output_numpy, multichannel=True, data_range=1.0
                    )

                    # Dump to output folder
                    # Phase and amplitude are nested
                    name = filename[e]
                    path_output = val_path / ("output_" + name)

                    cv2.imwrite(
                        str(path_output),
                        (output_numpy[:, :, ::-1] * 255.0).astype(np.int),
                    )

                metrics_dict["SSIM"] = metrics_dict["SSIM"] / args.batch_size
                avg_metrics += metrics_dict

                pbar.update(args.batch_size)
                pbar.set_description(
                    f"Val Epoch : {start_epoch} Step: {global_step}| PSNR: {avg_metrics.loss_dict['PSNR']:.3f}"
                )

            with open(val_path / "metrics.txt", "w") as f:
                L = [
                    f"exp_name:{args.exp_name} trained for {start_epoch} epochs\n",
                    "Metrics \n\n",
                ]
                L = L + [f"{k}:{v}\n" for k, v in avg_metrics.loss_dict.items()]
                f.writelines(L)

        if data.test_loader:
            pbar = tqdm(
                range(len(data.test_loader) * args.batch_size), dynamic_ncols=True
            )
            for i, batch in enumerate(data.test_loader):

                source, filename = batch
                source = source.to(device)

                output = G(source)

                for e in range(args.batch_size):
                    output_numpy = (
                        output[e]
                        .mul(0.5)
                        .add(0.5)
                        .permute(1, 2, 0)
                        .cpu()
                        .detach()
                        .numpy()
                    )

                    # Dump to output folder
                    name = filename[e]
                    path_output = filename / ("output_" + name)

                    cv2.imwrite(
                        str(path_output),
                        (output_numpy[:, :, ::-1] * 255.0).astype(np.int),
                    )

                pbar.update(args.batch_size)
                pbar.set_description(f"Test Epoch : {start_epoch} Step: {global_step}")
