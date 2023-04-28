import os
import sys
import torch
import logging
from typing import Dict, Optional, Sequence

from transformers import Trainer, DataCollatorWithPadding
from transformers.trainer import TRAINING_ARGS_NAME
from transformers.tokenization_utils import PreTrainedTokenizer

from .config import FinetuningArguments

from .other import (
    save_trainable_params,
    save_valuehead_params,
    FINETUNING_ARGS_NAME
)


logger = logging.getLogger(__name__) # setup logging
logger.setLevel(logging.INFO)
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)


# Note: The ChatGLM tokenizer assigns False on token to be attended in attention mask. In general settings, it should be True.
# Refer to: https://huggingface.co/THUDM/chatglm-6b/blob/6650ae3a53c28fc176d06762ca80b05d5ab3792b/tokenization_chatglm.py#L401
class PairwiseDataCollatorForChatGLM(DataCollatorWithPadding):
    r"""
    Data collator for ChatGLM. It is capable of dynamically padding for batched data.

    Inspired by: https://github.com/tatsu-lab/stanford_alpaca/blob/65512697dc67779a6e53c267488aba0ec4d7c02a/train.py#L156
    """
    def __init__(
            self,
            tokenizer: PreTrainedTokenizer,
            inference_mode: bool = False
    ):
        super().__init__(tokenizer, padding=True)
        self.inference_mode = inference_mode

    def __call__(self, features: Sequence[Dict[str, Sequence]]) -> Dict[str, torch.Tensor]:
        r"""
        Pads batched data to the longest sequence in the batch. We adopt left-padding for pairwise data.

        ChatGLM is able to generate attentions masks and position ids by itself.
        """
        if self.inference_mode:
            raise NotImplementedError
        accept_ids, reject_ids = [[torch.tensor(feature[key]).flip(0) for feature in features] for key in ("accept_ids", "reject_ids")]
        accept_ids = torch.nn.utils.rnn.pad_sequence(accept_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id)
        reject_ids = torch.nn.utils.rnn.pad_sequence(reject_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id)
        features = {"accept_ids": accept_ids.flip(-1), "reject_ids": reject_ids.flip(-1)}
        return features

class PairwiseTrainerForChatGLM(Trainer):
    r"""
    Inherits Seq2SeqTrainer to compute generative metrics such as BLEU and ROUGE.
    """

    def __init__(self, finetuning_args: FinetuningArguments, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.finetuning_args = finetuning_args

    def compute_loss(self, model, inputs, return_outputs=False):
        r"""
        Computes pairwise loss.

        There are two different implmentations:
        [1] https://github.com/lvwerra/trl/blob/52fecee8839ad826ad1e6c83a95c99a4116e98d2/examples/summarization/scripts/reward_summarization.py#L181
        [2] https://github.com/microsoft/DeepSpeedExamples/blob/f4ad1d5721630185a9088565f9201929a8b1ffdf/applications/DeepSpeed-Chat/training/utils/model/reward_model.py#L37
        Now we adopt the first implementation. We will consider adopting the second implementation later.
        """
        _, _, r_accept = model(input_ids=inputs["accept_ids"])
        _, _, r_reject = model(input_ids=inputs["reject_ids"]) # (seq_len x batch size)
        return -torch.log(torch.sigmoid(r_accept[-1] - r_reject[-1])).mean()

    def _save(self, output_dir: Optional[str] = None, state_dict: Optional[Dict[str, torch.Tensor]] = None) -> None:
        r"""
        Saves trainable parameters as model checkpoints.

        Override to inject custom behavior.
        """
        output_dir = output_dir if output_dir is not None else self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Saving model checkpoint to {output_dir}")
        if hasattr(self.model.pretrained_model, "peft_config"): # LoRA
            self.model.pretrained_model.save_pretrained(output_dir) # only save peft weights with the built-in method
        else: # Freeze and P-Tuning
            save_trainable_params(output_dir, self.model.pretrained_model)
        if hasattr(self.model, "v_head"):
            save_valuehead_params(output_dir, self.model.v_head) # save value head weights
        torch.save(self.args, os.path.join(output_dir, TRAINING_ARGS_NAME))
        torch.save(self.finetuning_args, os.path.join(output_dir, FINETUNING_ARGS_NAME))
