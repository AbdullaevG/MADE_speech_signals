"""
Copyright 2018-2019 Tsinghua University, Author: Hongyu Xiang. Apache 2.0.

This script shows the implementation of CRF loss function.
Taken from https://github.com/thu-spmi/CAT/
"""

import torch

import ctc_crf_base


def _assert_no_grad(tensor):
    assert not tensor.requires_grad, "shouldn't require grads"


class _CTC_CRF(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx, logits, labels, input_lengths, label_lengths, lamb=0.1, size_average=True
    ):
        logits = logits.contiguous()

        batch_size = logits.size(0)
        costs_alpha_den = torch.zeros(logits.size(0)).type_as(logits)
        costs_beta_den = torch.zeros(logits.size(0)).type_as(logits)

        grad_den = torch.zeros(logits.size()).type_as(logits)

        costs_ctc = torch.zeros(logits.size(0))
        act = torch.transpose(logits, 0, 1).contiguous()
        grad_ctc = torch.zeros(act.size()).type_as(logits)

        ctc_crf_base.gpu_ctc(
            act,
            grad_ctc,
            labels,
            label_lengths,
            input_lengths,
            logits.size(0),
            costs_ctc,
            0,
        )
        ctc_crf_base.gpu_den(
            logits, grad_den, input_lengths.cuda(), costs_alpha_den, costs_beta_den
        )

        grad_ctc = torch.transpose(grad_ctc, 0, 1)
        costs_ctc = costs_ctc.to(logits.get_device())

        grad_all = grad_den - (1 + lamb) * grad_ctc
        costs_all = costs_alpha_den - (1 + lamb) * costs_ctc
        costs = torch.FloatTensor([costs_all.sum()]).to(logits.get_device())

        if size_average:
            grad_all = grad_all / batch_size
            costs = costs / batch_size

        ctx.grads = grad_all

        return costs

    @staticmethod
    def backward(ctx, grad_output):
        return (
            ctx.grads * grad_output.to(ctx.grads.device),
            None,
            None,
            None,
            None,
            None,
            None,
        )


class CTC_CRF_LOSS(torch.nn.Module):
    """CTC-CRF function module.

    :param float lamb: ctc smoothing coefficient (0.0 ~ 1.0)
    :param bool size_average: perform size average
    """

    def __init__(self, lamb=0.1, size_average=True):
        """Construct a CTC_CRF_LOSS object."""
        super(CTC_CRF_LOSS, self).__init__()
        self.ctc_crf = _CTC_CRF.apply
        self.lamb = lamb
        self.size_average = size_average

    def forward(self, logits, labels, input_lengths, label_lengths):
        """CTC-CRF forward.

        :param torch.Tensor logits: batch of padded log prob sequences (B, Tmax, odim)
        :param torch.Tensor labels:
            batch of padded character id sequence tensor (B, Lmax)
        :param torch.Tensor input_lengths: batch of lengths of log prob sequences (B)
        :param torch.Tensor label_lengths: batch of lengths character id sequences (B)
        :return: ctc-crf loss value
        :rtype: torch.Tensor
        """
        assert len(labels.size()) == 1
        _assert_no_grad(labels)
        _assert_no_grad(input_lengths)
        _assert_no_grad(label_lengths)
        return self.ctc_crf(
            logits, labels, input_lengths, label_lengths, self.lamb, self.size_average
        )
