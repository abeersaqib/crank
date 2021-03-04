#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright (c) 2020 K. Kobayashi <root.4mac@gmail.com>
#
# Distributed under terms of the MIT license.
"""
Cyclic VQVAE w/ LSGAN trainer

"""

import random

import torch
from crank.net.trainer import LSGANTrainer


class CycleGANTrainer(LSGANTrainer):
    def __init__(
        self,
        model,
        optimizer,
        criterion,
        dataloader,
        writer,
        expdir,
        conf,
        feat_conf,
        scheduler=None,
        scaler=None,
        resume=0,
        device="cuda",
        n_jobs=-1,
    ):
        super().__init__(
            model,
            optimizer,
            criterion,
            dataloader,
            writer,
            expdir,
            conf,
            feat_conf,
            scheduler=scheduler,
            scaler=scaler,
            resume=resume,
            device=device,
            n_jobs=n_jobs,
        )

    def update_G(self, batch, loss, phase="train"):
        enc_h = self._get_enc_h(batch)
        enc_h_cv = self._get_enc_h(batch, use_cvfeats=True)
        dec_h, spkrvec = self._get_dec_h(batch)
        dec_h_cv, spkrvec_cv = self._get_dec_h(batch, use_cvfeats=True)
        feats = batch["in_feats"]

        # cycle loss
        cycle_outputs = self.model["G"].cycle_forward(
            feats, enc_h, dec_h, enc_h_cv, dec_h_cv, spkrvec, spkrvec_cv
        )
        loss = self.calculate_vqvae_loss(batch, cycle_outputs[0]["org"], loss)
        loss = self.calculate_cyclevqvae_loss(batch, cycle_outputs, loss)

        if self.conf["use_spkradv_training"]:
            loss = self.calculate_spkradv_loss(
                batch, cycle_outputs[0]["org"], loss, phase=phase
            )

        # adversarial loss for org and cv
        loss = self.calculate_cycleadv_loss(batch, cycle_outputs, loss)

        if phase == "train" and not self.stop_generator:
            self.step_model(loss, model="G")
        return loss

    def update_D(self, batch, loss, phase="train"):
        enc_h = self._get_enc_h(batch)
        enc_h_cv = self._get_enc_h(batch, use_cvfeats=True)
        dec_h, spkrvec = self._get_dec_h(batch)
        dec_h_cv, spkrvec_cv = self._get_dec_h(batch, use_cvfeats=True)
        feats = batch["in_feats"]

        # train discriminator
        outputs = self.model["G"].cycle_forward(
            feats, enc_h, dec_h, enc_h_cv, dec_h_cv, spkrvec, spkrvec_cv
        )
        loss = self.calculate_cycle_discriminator_loss(batch, outputs, loss)

        if phase == "train":
            self.step_model(loss, model="D")
        return loss

    def calculate_cycleadv_loss(self, batch, outputs, loss):
        def return_sample(x):
            return self.model["D"](x.transpose(1, 2)).transpose(1, 2)

        mask = batch["decoder_mask"]
        for c in range(self.conf["n_cycles"]):
            for io in ["org", "cv"]:
                lbl = f"{c}cyc_{io}"
                D_inputs = self.get_D_inputs(
                    batch, outputs[c][io]["decoded"], label="cv"
                )
                D_outputs = return_sample(D_inputs)
                if self.conf["acgan_flag"]:
                    D_outputs, spkr_cls = torch.split(
                        D_outputs, [1, self.n_spkrs], dim=2
                    )
                    D_outputs = D_outputs.masked_select(mask)
                    loss[f"D_acgan_adv_{lbl}"] = self.criterion["ce"](
                        spkr_cls.reshape(-1, spkr_cls.size(2)),
                        batch[f"{io}_h"].reshape(-1),
                    )
                    loss["G"] += (
                        self.conf["alpha"]["acgan"] * loss[f"D_acgan_adv_{lbl}"]
                    )
                loss[f"D_adv_{lbl}"] = self.criterion["mse"](
                    D_outputs, torch.ones_like(D_outputs)
                )
                loss["G"] += self.conf["alpha"]["adv"] * loss[f"D_adv_{lbl}"]
        return loss

    def calculate_cycle_discriminator_loss(self, batch, outputs, loss):
        def return_sample(x):
            return self.model["D"](x.transpose(1, 2)).transpose(1, 2)

        for c in range(self.conf["n_cycles"]):
            lbl = f"{c}cyc"
            real_inputs = self.get_D_inputs(batch, batch["in_feats"], label="org")
            org_fake_inputs = self.get_D_inputs(
                batch, outputs[0]["org"]["decoded"].detach(), label="org"
            )
            cv_fake_inputs = self.get_D_inputs(
                batch, outputs[0]["cv"]["decoded"].detach(), label="cv"
            )
            sample = {
                "real": return_sample(real_inputs),
                "org_fake": return_sample(org_fake_inputs),
                "cv_fake": return_sample(cv_fake_inputs),
            }

            if self.conf["acgan_flag"]:
                for k in sample.keys():
                    if k in ["real", "org_fake"]:
                        h = batch["org_h"]
                    else:
                        h = batch["cv_h"]
                    sample[k], spkr_cls = torch.split(
                        sample[k], [1, self.n_spkrs], dim=2
                    )
                    loss[f"D_ce_{k}_{lbl}"] = self.criterion["ce"](
                        spkr_cls.reshape(-1, spkr_cls.size(2)),
                        h.reshape(-1),
                    )
                    if not (self.conf["use_real_only_acgan"] and k == "org_fake"):
                        loss["D"] += (
                            self.conf["alpha"]["acgan"] * loss[f"D_ce_{k}_{lbl}"]
                        )

            real_sample = sample["real"].masked_select(batch["decoder_mask"])
            loss[f"D_real_{lbl}"] = self.criterion["mse"](
                real_sample, torch.ones_like(real_sample)
            )
            fake_key = random.choice(["org_fake", "cv_fake"])
            if fake_key == "org_fake":
                mask = batch["cycle_decoder_mask"]
            else:
                mask = batch["decoder_mask"]
            fake_sample = sample[fake_key].masked_select(mask)
            loss[f"D_fake_{lbl}"] = self.criterion["mse"](
                fake_sample, torch.zeros_like(fake_sample)
            )
            loss["D"] += (
                self.conf["alpha"]["fake"] * loss[f"D_fake_{lbl}"]
                + self.conf["alpha"]["real"] * loss[f"D_real_{lbl}"]
            )
        return loss
