import os
from typing import List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import torch
from tqdm import tqdm

from src.libs.utils import read_json, read_pickle
from src.preprocessing.utils import calculate_overlap, resample


class Dataset(torch.utils.data.Dataset):
    """Dataset class."""

    def __init__(
        self,
        tokenized_dir: str,
        max_length: int,
        stride: int,
        min_answer_length: int,
        save_to_memory: bool = True,
        selected_questions: Optional[List[str]] = None,
    ):
        """Init.

        Args:
            tokenized_dir (str): Directory to tokenized data (from Preparer).
            max_length (int): Maximum length of each sample (Check Hugging Face's Tokenizer class for more detail).
            stride (int): Stride length (Check Hugging Face's Tokenizer class for more detail).
            min_answer_length (int): Minimum answer length to consider including that answer.
            save_to_memory (bool, optional): Whether to save the whole data to memory or not. Defaults to True.
            selected_questions (Optional[List[str]], optional): Selected questions.
                If not provided, will be sampling from all questions.
                Defaults to None.
        """

        self.tokenized_dir = tokenized_dir
        self.max_length = max_length
        self.stride = stride
        self.min_answer_length = min_answer_length
        self.save_to_memory = save_to_memory

        self.content = None
        if self.save_to_memory:
            contexts = {
                os.path.splitext(f)[0]: read_pickle(f"{tokenized_dir}/context/{f}")
                for f in tqdm(
                    os.listdir(f"{tokenized_dir}/context"), desc="loading contexts"
                )
            }
            questions = {
                os.path.splitext(f)[0]: read_pickle(f"{tokenized_dir}/question/{f}")
                for f in tqdm(
                    os.listdir(f"{tokenized_dir}/question"), desc="loading questions"
                )
            }
            self.content = {"context": contexts, "question": questions}

        self.tokenizer_info = read_json(f"{tokenized_dir}/tokenizer_info.json")
        self.answers_span = pd.read_csv(f"{tokenized_dir}/answers_span.csv")

        if selected_questions:
            self.answers_span = self.answers_span[
                self.answers_span.question_id.isin(selected_questions)
            ]
            questions = {k: v for k, v in questions.items() if k in selected_questions}

        context_start = len(self.tokenizer_info["seperators"]) - (
            list(reversed(self.tokenizer_info["seperators"])).index(-1) + 1
        )
        self.n_seps_before_context = sum(
            (np.array(self.tokenizer_info["seperators"]) >= 0)[:context_start]
        )

        subsample_spans = self.generate_subsamples_span()
        self.subsample_spans = resample(subsample_spans, subsample_spans.answer_end > 0)

    def __getitem__(self, idx: int) -> dict:
        """Gets sample.

        Args:
            idx (int): Index of sample.

        Returns:
            dict: Sample.
        """

        instance = self.subsample_spans.iloc[idx, :]
        question = self._get_instance(instance["question_id"], instance_type="question")
        context = self._get_instance(instance["context_id"], instance_type="context")

        subcontext = {
            k: context[k][instance["subcontext_start"] : instance["subcontext_end"] + 1]
            for k in ["input_ids", "attention_mask"]
        }
        subsample = self.combine_qc(question, subcontext)
        subsample["input_ids"] += [self.tokenizer_info["padding_id"]] * (
            self.max_length - len(subsample["input_ids"])
        )
        subsample["attention_mask"] += [0] * (
            self.max_length - len(subsample["attention_mask"])
        )

        subsample["start_positions"] = instance["answer_start"]
        subsample["end_positions"] = instance["answer_end"]

        return {key: torch.tensor(val) for key, val in subsample.items()}

    def __len__(self) -> int:
        """Returns total number of samples.

        Returns:
            int: Total number of samples.
        """
        return len(self.subsample_spans)

    def generate_subsample_span(
        self, question: dict, context: dict, answer_span: Tuple[int, int]
    ) -> pd.DataFrame:
        """Generates sample span.

        Args:
            question (dict): Dictionary containing information of the question.
            context (dict): Dictionary containing information of the context.
            answer_span (Tuple[int, int]): (start_position, end_position).

        Returns:
            pd.DataFrame: Sample span
        """

        len_question = len(question["input_ids"])
        len_context = len(context["input_ids"])
        n_seps = sum(np.array(self.tokenizer_info["seperators"]) >= 0)

        subcontext_max_length = self.max_length - len_question - n_seps
        stride_ = subcontext_max_length - self.stride

        n_sub = (len_context - self.stride) // stride_ + 1

        subcontext_spans = (
            np.repeat([[0, subcontext_max_length - 1]], n_sub, axis=0)
            + np.ones((n_sub, 2), dtype=int)
            * stride_
            * np.array(range(0, n_sub), dtype=int)[:, None]
        )

        ans_start, ans_end = answer_span
        answers_shifted = [
            [
                ans_start - context_start + len_question + self.n_seps_before_context,
                ans_end - context_start + len_question + self.n_seps_before_context,
            ]
            if (
                calculate_overlap(context_start, context_end, ans_start, ans_end + 1)
                >= min(ans_end - ans_start + 1, self.min_answer_length)
            )
            else [0, 0]
            for context_start, context_end in subcontext_spans
        ]
        # answers_shifted = np.array(answers_shifted)

        return pd.DataFrame(
            np.concatenate([subcontext_spans, answers_shifted], axis=1),
            columns=[
                "subcontext_start",
                "subcontext_end",
                "answer_start",
                "answer_end",
            ],
        )

    def generate_subsamples_span(self) -> pd.DataFrame:
        """Combines generated samples into 1 dataframe.

        Returns:
            pd.DataFrame: Combined samples.
        """

        subsample_spans = []
        for _, (question_id, context_id, ans_start, ans_end) in tqdm(
            self.answers_span.iterrows(),
            total=len(self.answers_span),
            desc="generating sub-samples spans",
        ):
            context = self._get_instance(context_id, instance_type="context")
            question = self._get_instance(question_id, instance_type="question")

            subsample_span = self.generate_subsample_span(
                question, context, (ans_start, ans_end)
            )
            subsample_span.insert(0, "context_id", context_id)
            subsample_span.insert(0, "question_id", question_id)

            subsample_spans.append(subsample_span)

        return pd.concat(subsample_spans).reset_index(drop=True)

    def combine_qc(self, question: dict, context: dict) -> dict:
        """Combines a question and a context into 1 content with seperators.

        Args:
            question (dict): Dictionary containing information of the question.
            context (dict): Dictionary containing information of the context.

        Returns:
            dict: Content.
        """

        result: dict = {k: [] for k in context.keys()}
        content = [question, context]

        for sep in self.tokenizer_info["seperators"]:
            if sep >= 0:
                sep_dict = {
                    "input_ids": sep,
                    "attention_mask": 1,
                    "offset_mapping": (0, 0),
                    "sequence_ids": None,
                }
                result = {k: v + [sep_dict[k]] for k, v in result.items()}

            else:
                instance = content.pop(0)
                result = {k: v + instance[k] for k, v in result.items()}

        return result

    def _get_instance(self, idenifier: Union[int, str], instance_type: str) -> dict:
        """Gets instance.

        Args:
            id (Union[int, str]): Instance ID.
            instance_type (str): Instance type. Either "question" or "context".

        Returns:
            dict: Instance.
        """

        assert instance_type in (
            "question",
            "context",
        ), 'instance_type must be either "question" or "context".'

        return (
            self.content[instance_type][idenifier]
            if self.content
            else read_pickle(f"{self.tokenized_dir}/{instance_type}/{idenifier}.pickle")
        )
