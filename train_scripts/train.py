"""
Train Script for Phase Mask and Amplitude Mask
"""
# Libraries
from sacred import Experiment
from tqdm import tqdm
from collections import defaultdict
import logging
import numpy as np

# Torch Libs
import torch
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

# Modules
from dataloader import get_dataloaders
from utils.dir_helper import dir_init
from models import get_model
from loss import GLoss, DLoss
from config import initialise
from metrics import PSNR

# Typing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.typing_alias import *

# Train helpers
from utils.train_helper import (
    set_device,
    get_optimisers,
    load_models,
    save_weights,
    ExpLoss_with_dict,
    AvgLoss_with_dict,
    pprint_args,
)
from utils.tupperware import tupperware

# Experiment, add any observers by command line
ex = Experiment("Unet-Train")
ex = initialise(ex)

# To prevent "RuntimeError: received 0 items of ancdata"
torch.multiprocessing.set_sharing_strategy("file_system")
torch.autograd.set_detect_anomaly(True)


@ex.automain
def main(_run):
    args = tupperware(_run.config)

    # Set device, init dirs
    device, source_device = set_device(args)
    dir_init(args)

    # Get data
    data = get_dataloaders(args)

    # Model
    # source_device: where D, VGG are placed
    # target_device: where G, data are placed. Typically cuda:0
    G, D = get_model.model(args, source_device=source_device, target_device=device)

    # Init the Low Res network
    G.init_lr("ckpts/lr_net_latest.pth")
    G = G.to(device)

    # Don't load disc unless needed
    if args.lambda_adversarial:
        D = D.to(source_device)

    # Optimisers
    (g_optimizer, d_optimizer), (g_lr_scheduler, d_lr_scheduler) = get_optimisers(
        G, D, args
    )

    # Load Models
    (G, D), (g_optimizer, d_optimizer), global_step, start_epoch, loss = load_models(
        G, D, g_optimizer, d_optimizer, args
    )

    if args.dataparallel:
        G = torch.nn.DataParallel(G, device_ids=args.device_list)
        if args.lambda_adversarial:
            D = torch.nn.DataParallel(D, device_ids=args.device_list)

    writer = SummaryWriter(log_dir=str(args.run_dir / args.exp_name))
    writer.add_text("Args", pprint_args(args))

    # Initialise losses
    g_loss = GLoss(args, device, perception_device=source_device)
    d_loss = DLoss(args)

    # Compatibility with checkpoints without global_step
    if not global_step:
        global_step = start_epoch * len(data.train_loader) * args.batch_size

    start_epoch = global_step // len(data.train_loader.dataset)

    # Exponential averaging of loss
    loss_dict = {
        "g_loss": 0.0,
        "d_loss": 0.0,
        "adversarial_loss": 0.0,
        "perception_loss": 0.0,
        "image_loss": 0.0,
        "train_PSNR": 0.0,
    }

    metric_dict = {"PSNR": 0.0, "g_loss": 0.0}
    avg_metrics = AvgLoss_with_dict(loss_dict=metric_dict, args=args)
    exp_loss = ExpLoss_with_dict(loss_dict=loss_dict, args=args)

    # Pbars
    train_pbar = tqdm(
        range(len(data.train_loader) * args.batch_size), dynamic_ncols=True
    )

    val_pbar = (
        tqdm(range(len(data.val_loader) * args.batch_size), dynamic_ncols=True)
        if data.val_loader
        else None
    )

    test_pbar = (
        tqdm(range(len(data.test_loader) * args.batch_size), dynamic_ncols=True)
        if data.test_loader
        else None
    )

    try:
        for epoch in range(start_epoch, args.num_epochs):
            # Train mode
            G.train()
            D.train()
            train_pbar.reset()

            for i, batch in enumerate(data.train_loader):
                # allows for interrupted training
                if (
                    (global_step + 1) % (len(data.train_loader) * args.batch_size) == 0
                ) and (epoch == start_epoch):
                    break

                loss_dict = defaultdict(float)

                source, target, filename = batch
                source, target = (source.to(device), target.to(device))

                if args.lambda_adversarial:
                    # ------------------------------- #
                    # Update Disc
                    # ------------------------------- #
                    D.zero_grad()
                    G.zero_grad()

                    output = G(source).detach()

                    real_logit = D(target)
                    fake_logit = D(output)

                    d_loss(real_logit=real_logit, fake_logit=fake_logit)

                    d_loss.total_loss.backward()

                    loss_dict["d_loss"] += d_loss.total_loss.item()
                    d_optimizer.step()
                else:
                    loss_dict["d_loss"] += 0.0

                # ------------------------------- #
                # Update Gen
                # ------------------------------- #
                G.zero_grad()

                output = G(source)

                if args.lambda_adversarial:
                    real_logit = D(target)
                    fake_logit = D(output)

                    g_loss(
                        fake_logit=fake_logit,
                        real_logit=real_logit,
                        output=output,
                        target=target,
                    )
                else:
                    g_loss(output=output, target=target, pretrain=True)

                g_loss.total_loss.backward()

                # Train PSNR
                loss_dict["train_PSNR"] += PSNR(output, target)

                g_optimizer.step()
                # Update lr schedulers
                g_lr_scheduler.step(epoch + i / len(data.train_loader))

                if args.lambda_adversarial:
                    d_lr_scheduler.step(
                        epoch - args.pretrain_epochs + i / len(data.train_loader)
                    )

                # Accumulate all losses
                loss_dict["g_loss"] += g_loss.total_loss.item()
                loss_dict["adversarial_loss"] += g_loss.adversarial_loss.item()
                loss_dict["perception_loss"] += g_loss.perception_loss.item()
                loss_dict["image_loss"] += g_loss.image_loss.item()

                exp_loss += loss_dict
                train_pbar.update(args.batch_size)
                global_step += args.batch_size

                train_pbar.set_description(
                    f"Epoch: {epoch + 1} | Gen loss: {exp_loss.loss_dict['g_loss']:.3f} "
                )

                if i % 10 == 0:
                    gen_lr = g_optimizer.param_groups[0]["lr"]
                    writer.add_scalar("lr/gen", gen_lr, global_step)

                    if args.lambda_adversarial:
                        disc_lr = d_optimizer.param_groups[0]["lr"]
                        writer.add_scalar("lr/disc", disc_lr, global_step)

                    for metric in exp_loss.loss_dict:
                        writer.add_scalar(
                            f"Train_Metrics/{metric}",
                            exp_loss.loss_dict[metric],
                            global_step,
                        )

                # Display images
                if i % (args.log_interval) == 0:
                    n = np.min([3, args.batch_size])
                    for e in range(n):
                        source_vis = source[e].mul(0.5).add(0.5)
                        target_vis = target[e].mul(0.5).add(0.5)
                        output_vis = output[e].mul(0.5).add(0.5)

                        writer.add_image(
                            f"Source/Train_{e+1}",
                            source_vis.cpu().detach(),
                            global_step,
                        )

                        writer.add_image(
                            f"Target/Train_{e + 1}",
                            target_vis.cpu().detach(),
                            global_step,
                        )

                        writer.add_image(
                            f"Output/Train_{e + 1}",
                            output_vis.cpu().detach(),
                            global_step,
                        )

                        writer.add_text(
                            f"Filename/Train_{e+1}", filename[e], global_step
                        )

            # Save ckpt at end of epoch
            logging.info(
                f"Saving weights at epoch {epoch + 1} global step {global_step}"
            )

            # Save weights
            save_weights(
                epoch=epoch,
                global_step=global_step,
                G=G,
                D=D,
                g_optimizer=g_optimizer,
                d_optimizer=d_optimizer,
                loss=loss,
                tag="latest",
                args=args,
            )

            train_pbar.refresh()

            # Run val and test only occasionally
            if epoch % args.val_test_epoch_interval != 0:
                continue

            # Val and test
            with torch.no_grad():
                G.eval()
                D.eval()

                if data.val_loader:
                    val_pbar.reset()
                    filename_static = []
                    avg_metrics.reset()

                    for i, batch in enumerate(data.val_loader):
                        metrics_dict = defaultdict(float)

                        source, target, filename = batch
                        source, target = (source.to(device), target.to(device))

                        output = G(source)

                        if args.lambda_adversarial:
                            fake_logit = D(output)
                            real_logit = D(target)

                            g_loss(
                                fake_logit=fake_logit,
                                real_logit=real_logit,
                                output=output,
                                target=target,
                            )
                        else:
                            g_loss(output=output, target=target, pretrain=True)

                        metrics_dict["g_loss"] += g_loss.total_loss.item()

                        # Save image
                        if args.static_val_image in filename:
                            filename_static = filename
                            source_static = source
                            target_static = target
                            output_static = output

                        # PSNR
                        metrics_dict["PSNR"] += PSNR(output, target)

                        avg_metrics += metrics_dict
                        val_pbar.update(args.batch_size)
                        val_pbar.set_description(
                            f"Val Epoch : {epoch + 1} Step: {global_step}| PSNR: {avg_metrics.loss_dict['PSNR']:.3f}"
                        )

                    for metric in avg_metrics.loss_dict:
                        writer.add_scalar(
                            f"Val_Metrics/{metric}",
                            avg_metrics.loss_dict[metric],
                            global_step,
                        )

                    n = np.min([3, args.batch_size])
                    for e in range(n):
                        source_vis = source[e].mul(0.5).add(0.5)
                        target_vis = target[e].mul(0.5).add(0.5)
                        output_vis = output[e].mul(0.5).add(0.5)

                        writer.add_image(
                            f"Source/Val_{e+1}", source_vis.cpu().detach(), global_step
                        )
                        writer.add_image(
                            f"Target/Val_{e+1}", target_vis.cpu().detach(), global_step
                        )
                        writer.add_image(
                            f"Output/Val_{e+1}", output_vis.cpu().detach(), global_step
                        )

                        writer.add_text(
                            f"Filename/Val_{e + 1}", filename[e], global_step
                        )

                    for e, filename in enumerate(filename_static):
                        if filename == args.static_val_image:
                            source_vis = source_static[e].mul(0.5).add(0.5)
                            target_vis = target_static[e].mul(0.5).add(0.5)
                            output_vis = output_static[e].mul(0.5).add(0.5)

                            writer.add_image(
                                f"Source/Val_Static",
                                source_vis.cpu().detach(),
                                global_step,
                            )
                            writer.add_image(
                                f"Target/Val_Static",
                                target_vis.cpu().detach(),
                                global_step,
                            )
                            writer.add_image(
                                f"Output/Val_Static",
                                output_vis.cpu().detach(),
                                global_step,
                            )

                            writer.add_text(
                                f"Filename/Val_Static", filename[e], global_step
                            )

                            break

                    logging.info(
                        f"Saving weights at END OF epoch {epoch + 1} global step {global_step}"
                    )

                    # Save weights
                    if avg_metrics.loss_dict["g_loss"] < loss:
                        is_min = True
                        loss = avg_metrics.loss_dict["g_loss"]
                    else:
                        is_min = False

                    # Save weights
                    save_weights(
                        epoch=epoch,
                        global_step=global_step,
                        G=G,
                        D=D,
                        g_optimizer=g_optimizer,
                        d_optimizer=d_optimizer,
                        loss=loss,
                        is_min=is_min,
                        args=args,
                        tag="best",
                    )

                    val_pbar.refresh()

                # Test
                if data.test_loader:
                    filename_static = []
                    test_pbar.reset()

                    for i, batch in enumerate(data.test_loader):
                        source, filename = batch
                        source = source.to(device)

                        output = G(source)

                        # Save image
                        if args.static_test_image in filename:
                            filename_static = filename
                            source_static = source
                            output_static = output

                        test_pbar.update(args.batch_size)
                        test_pbar.set_description(
                            f"Test Epoch : {epoch + 1} Step: {global_step}"
                        )

                    n = np.min([3, args.batch_size])
                    for e in range(n):
                        source_vis = source[e].mul(0.5).add(0.5)
                        output_vis = output[e].mul(0.5).add(0.5)

                        writer.add_image(
                            f"Source/Test_{e+1}", source_vis.cpu().detach(), global_step
                        )

                        writer.add_image(
                            f"Output/Test_{e+1}", output_vis.cpu().detach(), global_step
                        )

                        writer.add_text(
                            f"Filename/Test_{e + 1}", filename[e], global_step
                        )

                    for e, filename in enumerate(filename_static):
                        if filename == args.static_test_image:
                            source_vis = source_static[e]
                            output_vis = output_static[e]

                            writer.add_image(
                                f"Source/Test_Static",
                                source_vis.cpu().detach(),
                                global_step,
                            )

                            writer.add_image(
                                f"Output/Test_Static",
                                output_vis.cpu().detach(),
                                global_step,
                            )

                            writer.add_text(
                                f"Filename/Test_Static", filename_static[e], global_step
                            )

                            break

                    test_pbar.refresh(

    except KeyboardInterrupt:
        logging.info("-" * 89)
        logging.info("Exiting from training early. Saving models")

        for pbar in [train_pbar, val_pbar, test_pbar]:
            if pbar:
                pbar.refresh()

        save_weights(
            epoch=epoch,
            global_step=global_step,
            G=G,
            D=D,
            g_optimizer=g_optimizer,
            d_optimizer=d_optimizer,
            loss=loss,
            is_min=True,
            args=args,
        )
