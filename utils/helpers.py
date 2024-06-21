import re
from time import perf_counter
import numpy as np
import torch
from transformers.tokenization_utils_base import BatchEncoding
from bert_score import BERTScorer

from .pipelines import Encoder


def count_words(text: str):
	return len(text.split())


def combine_subsections(sections):
	text = ""
	for sec in sections:
		sec_text = "\n\n".join(sec["paragraphs"])
		if sec["section_title"]:
			sec_text = f"Section {sec["section_title"]}:\n\n{sec_text}"
		text = f"{text}\n\n{sec_text}" if text else sec_text
		if sec["subsections"]:
			sub_text = combine_subsections(sec["subsections"])
			text = f"{text}\n\n{sub_text}" if text else sub_text
	return text


def get_device() -> str:
	if torch.cuda.is_available():
		return "cuda"
	if torch.backends.mps.is_available():
		return "mps"
	return "cpu"


class TextProcessor:

	preprocessing_pats_subs = [
		# Non-ASCII quotes
		(r"‘|’", "'"),
		(r"“|”", '"'),
		# Non-ASCII characters
		(r"[^\x00-\x7f]+", ""),
		# Emails
		(r"[^\s]+@[^\s]+\.com", ""),
		# Hyperlinks
		(r"[^\s]*://[^\s]*", ""),
		# Hashtags
		(r"#[^\s]+", ""),
		# HTML tags
		(r"<[^\n>]+>", "")
	]

	# Numbers
	_number_pat_sub = (r"[+?\d+-?]+", "")

	_whitespace_pats_subs = [
		# Multiple spaces and tabs
		(r"([ \t]){2,}", r"\1"),
		# Spaces and tabs before newline
		(r"[ \t]\n", "\n"),
		# Multiple newlines
		(r"\n{3,}", "\n\n"),
	]

	def __init__(
			self, pats_subs: list[tuple[str]]=None, ignore_tokens: list[str]=None,
			remove_nums: bool=False
		) -> None:
		if pats_subs is None:
			pats_subs = []
		if remove_nums:
			pats_subs.append(TextProcessor._number_pat_sub)
		if ignore_tokens:
			pats_subs.append((re.compile(r"|".join(ignore_tokens)), ""))
		pats_subs.extend(TextProcessor._whitespace_pats_subs)
		self.pats_subs = [
			(re.compile(pat), sub) for pat, sub in pats_subs
		]
	
	def __call__(self, texts: str|list[str]) -> list[str]:
		if isinstance(texts, str):
			texts = [texts]
		texts = [self.process(text) for text in texts]
		return texts
		
	def process(self, text: str) -> str:
		for pat, sub in self.pats_subs:
			text = pat.sub(sub, text)
		text = text.strip()
		return text


class SummarizationDataset:

	def __init__(
			self, texts_summaries: list[tuple[str]], encoder: Encoder, batch_size: int,
			shuffle: bool=False, device: str|torch.device|None=None, seed: int|None=None
		) -> None:
		tokenizer = encoder.tokenizer
		texts_summaries = sorted(texts_summaries, key=lambda x: count_words(x[0]))
		self.batches = batches = []
		for i in range(0, len(texts_summaries), batch_size):
			batch = texts_summaries[i:i+batch_size]
			texts = [pair[0] for pair in batch]
			summaries = [pair[1] for pair in batch]
			encodings = encoder(texts)
			summ_encodings = tokenizer(
				summaries, return_tensors="pt", padding=True
			)["input_ids"]
			summ_encodings[summ_encodings == tokenizer.pad_token_id] = -100
			batches.append(
				BatchEncoding({**encodings, "labels": summ_encodings})
			)
		self.shuffle = shuffle
		self.device = device
		self.seed = seed
		np.random.seed(seed)
		self.num_batches = len(batches)
		self.index = None
	
	def __iter__(self):
		self.index = -1
		if self.shuffle:
			self.batches = np.random.permutation(self.batches)
		return self
	
	def __next__(self) -> BatchEncoding:
		index = self.index
		if index is None or index + 1 == self.num_batches:
			raise StopIteration()
		self.index += 1
		encodings = self.batches[self.index]
		return encodings.to(self.device)
	
	def __len__(self) -> int:
		return self.num_batches


class Evaluator:

	def __init__(
			self, pipelines, texts_summaries: tuple[str]|list[tuple[str]],
			device: str|torch.device|None=None
		) -> None:
		if not isinstance(texts_summaries, list):
			texts_summaries = [texts_summaries]
		self.pipelines = pipelines
		self.texts = [pair[0] for pair in texts_summaries]
		self.summaries = [pair[1] for pair in texts_summaries]
		self.bert_scorer = BERTScorer(lang="en", device=device)
		self.generated_summaries = []
	
	def generate_summaries(self) -> list[int]:
		summaries = self.generated_summaries
		time_taken = []
		for pipeline in self.pipelines:
			start = perf_counter()
			summary = pipeline(self.texts)
			time = (perf_counter() - start) * 1000
			summaries.extend(summary)
			time_taken.append(time)
		return time_taken

	def get_bertscore(self) -> list[torch.Tensor]:
		if not self.generated_summaries:
			print("Generating summaries")
			self.generate_summaries()
		summaries = self.summaries
		num_pipelines = len(self.pipelines)
		summaries *= num_pipelines
		metrics = self.bert_scorer.score(self.generated_summaries, summaries)
		metrics = [
			metric.reshape((num_pipelines, -1)).mean(dim=1)
			for metric in metrics
		]
		return metrics
