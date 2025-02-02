"""V2 backend for `asr_recog.py` using py:class:`espnet.nets.beam_search.BeamSearch`."""

import json
import logging
import numpy as np
import sys

import torch

from kaldiio import WriteHelper

from espnet.asr.asr_utils import add_results_to_json
from espnet.asr.asr_utils import get_model_conf
from espnet.asr.asr_utils import torch_load
from espnet.asr.pytorch_backend.asr import load_trained_model
from espnet.nets.asr_interface import ASRInterface
from espnet.nets.batch_beam_search import BatchBeamSearch
from espnet.nets.beam_search import BeamSearch
from espnet.nets.lm_interface import dynamic_import_lm
from espnet.nets.scorer_interface import BatchScorerInterface
from espnet.nets.scorers.length_bonus import LengthBonus
from espnet.utils.deterministic_utils import set_deterministic_pytorch
from espnet.utils.io_utils import LoadInputsAndTargets

if sys.version_info[0] == 2:
    from itertools import izip_longest as zip_longest
else:
    from itertools import zip_longest as zip_longest


def recog_v2(args):
    """Decode with custom models that implements ScorerInterface.

    Notes:
        The previous backend espnet.asr.pytorch_backend.asr.recog
        only supports E2E and RNNLM

    Args:
        args (namespace): The program arguments.
        See py:func:`espnet.bin.asr_recog.get_parser` for details

    """
    logging.warning("experimental API for custom LMs is selected by --api v2")
    if args.batchsize > 1:
        raise NotImplementedError("multi-utt batch decoding is not implemented")
    if args.streaming_mode is not None:
        raise NotImplementedError("streaming mode is not implemented")
    if args.word_rnnlm:
        raise NotImplementedError("word LM is not implemented")

    set_deterministic_pytorch(args)
    model, train_args = load_trained_model(args.model)
    assert isinstance(model, ASRInterface)
    model.eval()

    load_inputs_and_targets = LoadInputsAndTargets(
        mode="asr",
        load_output=False,
        sort_in_input_length=False,
        preprocess_conf=train_args.preprocess_conf
        if args.preprocess_conf is None
        else args.preprocess_conf,
        preprocess_args={"train": False},
    )

    if args.rnnlm:
        lm_args = get_model_conf(args.rnnlm, args.rnnlm_conf)
        # NOTE: for a compatibility with less than 0.5.0 version models
        lm_model_module = getattr(lm_args, "model_module", "default")
        lm_class = dynamic_import_lm(lm_model_module, lm_args.backend)
        lm = lm_class(len(train_args.char_list), lm_args)
        torch_load(args.rnnlm, lm)
        lm.eval()
    else:
        lm = None

    if args.ngram_model:
        from espnet.nets.scorers.ngram import NgramFullScorer
        from espnet.nets.scorers.ngram import NgramPartScorer

        if args.ngram_scorer == "full":
            ngram = NgramFullScorer(args.ngram_model, train_args.char_list)
        else:
            ngram = NgramPartScorer(args.ngram_model, train_args.char_list)
    else:
        ngram = None

    scorers = model.scorers()
    scorers["lm"] = lm
    scorers["ngram"] = ngram
    scorers["length_bonus"] = LengthBonus(len(train_args.char_list))
    weights = dict(
        decoder=1.0 - args.ctc_weight,
        ctc=args.ctc_weight,
        lm=args.lm_weight,
        ngram=args.ngram_weight,
        length_bonus=args.penalty,
    )
    beam_search = BeamSearch(
        beam_size=args.beam_size,
        vocab_size=len(train_args.char_list),
        weights=weights,
        scorers=scorers,
        sos=model.sos,
        eos=model.eos,
        token_list=train_args.char_list,
        pre_beam_score_key=None if args.ctc_weight == 1.0 else "full",
    )
    # TODO(karita): make all scorers batchfied
    if args.batchsize == 1:
        non_batch = [
            k
            for k, v in beam_search.full_scorers.items()
            if not isinstance(v, BatchScorerInterface)
        ]
        if len(non_batch) == 0:
            beam_search.__class__ = BatchBeamSearch
            logging.info("BatchBeamSearch implementation is selected.")
        else:
            logging.warning(
                f"As non-batch scorers {non_batch} are found, "
                f"fall back to non-batch implementation."
            )

    if args.ngpu > 1:
        raise NotImplementedError("only single GPU decoding is supported")
    if args.ngpu == 1:
        device = "cuda"
    else:
        device = "cpu"
    dtype = getattr(torch, args.dtype)
    logging.info(f"Decoding device={device}, dtype={dtype}")
    model.to(device=device, dtype=dtype).eval()
    beam_search.to(device=device, dtype=dtype).eval()

    # read json data
    with open(args.recog_json, "rb") as f:
        js = json.load(f)["utts"]
    new_js = {}
    with torch.no_grad():
        for idx, name in enumerate(js.keys(), 1):
            logging.info("(%d/%d) decoding " + name, idx, len(js.keys()))
            batch = [(name, js[name])]
            feat = load_inputs_and_targets(batch)[0][0]
            enc = model.encode(torch.as_tensor(feat).to(device=device, dtype=dtype))
            nbest_hyps = beam_search(
                x=enc, maxlenratio=args.maxlenratio, minlenratio=args.minlenratio
            )
            nbest_hyps = [
                h.asdict() for h in nbest_hyps[: min(len(nbest_hyps), args.nbest)]
            ]
            new_js[name] = add_results_to_json(
                js[name], nbest_hyps, train_args.char_list
            )

    with open(args.result_label, "wb") as f:
        f.write(
            json.dumps(
                {"utts": new_js}, indent=4, ensure_ascii=False, sort_keys=True
            ).encode("utf_8")
        )


def calc_logits(args):
    """Infer logits into kaldi matrix.

    Args:
        args (namespace): The program arguments.
        See py:func:`espnet.bin.asr_recog.get_parser` for details

    """
    set_deterministic_pytorch(args)
    model, train_args = load_trained_model(args.model)
    assert isinstance(model, ASRInterface)
    model.eval()

    if hasattr(model, "mtlalpha"):
        if model.mtlalpha == 0:
            raise NotImplementedError("Only pure attention mode is not supported")
        elif model.mtlalpha < 1:
            logging.warning("Attention decoder is ignored.")

    load_inputs_and_targets = LoadInputsAndTargets(
        mode="asr",
        load_output=False,
        sort_in_input_length=False,
        preprocess_conf=train_args.preprocess_conf
        if args.preprocess_conf is None
        else args.preprocess_conf,
        preprocess_args={"train": False},
    )

    if args.ngpu == 1:
        device = "cuda"
    else:
        device = "cpu"

    dtype = getattr(torch, args.dtype)
    logging.info(f"Decoding device={device}, dtype={dtype}")
    model.to(device=device, dtype=dtype).eval()

    # NOTE: for a compatibility with zero batchsize
    batchsize = args.batchsize if args.batchsize > 0 else 1

    # read json data
    with open(args.recog_json, "rb") as f:
        js = json.load(f)["utts"]

    def grouper(n, iterable, fillvalue=None):
        kargs = [iter(iterable)] * n
        return zip_longest(*kargs, fillvalue=fillvalue)

    def calc_logits_ctc(model, feats):
        hs_pad, hlens = model.encode_batch(feats)
        logits_pad = model.ctc.log_softmax(hs_pad)
        return logits_pad, hlens

    def calc_logits_transducer(model, feats):
        if model.etype == "transformer":
            hs_pad, hlens = model.encode_transformer_batch(feats)
        else:
            hs_pad, hlens = model.encode_rnn_batch(feats)
        if model.dtype == "transformer":
            logits_pad = model.decoder.calc_logits_batch(hs_pad, hlens)
        else:
            logits_pad = model.dec.calc_logits_batch(hs_pad, hlens)
        return logits_pad, hlens

    calc_logits_func = (
        calc_logits_transducer
        if "transducer" in train_args.model_module
        else calc_logits_ctc
    )

    with torch.no_grad(), WriteHelper(args.result_mat, compression_method=2) as writer:
        # sort data if batchsize > 1
        keys = list(js.keys())
        if batchsize > 1:
            feat_lens = [js[key]["input"][0]["shape"][0] for key in keys]
            sorted_index = sorted(range(len(feat_lens)), key=lambda i: -feat_lens[i])
            keys = [keys[i] for i in sorted_index]
        for names in grouper(batchsize, keys, None):
            names = [name for name in names if name]
            batch = [(name, js[name]) for name in names]
            feats = (
                load_inputs_and_targets(batch)[0]
                if args.num_encs == 1
                else load_inputs_and_targets(batch)
            )
            logits_pad, hlens = calc_logits_func(model, feats)
            for i in range(len(names)):
                logit = logits_pad[i, : hlens[i]]
                logit = logit.cpu().data.numpy()
                logit[logit == -np.inf] = -1e16
                writer(names[i], logit)
